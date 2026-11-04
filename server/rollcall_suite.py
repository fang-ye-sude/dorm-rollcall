#!/usr/bin/env python3
"""rollcall_suite — B座点名·填表 一体机（跑在 应用容器 pve-rollcall，:8100）

职责（全部本地计算，零 AI）：
  接收 iPad 上传（回执/标记件，gzip 落盘、分类 roll/ marked/）
  → 标记件一到：算出落列 → dws 写钉钉在线表 → 删同日期旧标记件 → 通知/留日志
  → 周末件(weekend=true)：生成周末返校 TSV，标记 pending
  管理台（Web UI + API）：历史查看/下载/删除、立即填入、改档重算、口令校验

存储 /srv/dormroll/data/
  roll/<ts>.json.gz  点名回执 {v:2,type,d,t,p,a,n:[[name,room]]}
  marked/<ts>.json.gz 标记件 {v:4,type,ref,weekend,d,n:[[name,room,q]]}
  state/config.json  node(在线表ID)/week/daterange/notified
  logs/fill.log      填报流水（append）

同日期幂等：新 marked 落库后，把旧的同日期 marked 挪 .trash —— 表上永远只剩最后一次标记。
"""
import json, os, re, sys, gzip, time, hmac, hashlib, base64, datetime, subprocess, tempfile, threading, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import store as STORE
from urllib.parse import urlparse, parse_qs

DATA = os.environ.get("RC_DATA", "/srv/dormroll/data")
WEB = os.environ.get("RC_WEB", "/srv/dormroll/web")
TOKEN = os.environ.get("RC_TOKEN", "")
ADMIN = os.environ.get("RC_ADMIN", "")
DWS = os.environ.get("RC_DWS", "/srv/dormroll/bin/dws")
def _pick_roster_root():
    """名册可能放 data/（新）或项目根（老部署），两处都认。"""
    cands = [os.path.join(DATA, "roster_b1f.json"),
             os.path.join(os.path.dirname(DATA), "roster_b1f.json")]
    for c in cands:
        if os.path.exists(c):
            return c
    return cands[0]


RosterJSON = os.environ.get("RC_ROSTER") or _pick_roster_root()
CLIENT_WEB = os.environ.get("RC_CLIENT_WEB", "/srv/dormroll/app/web")   # iPad 页面包（OTA 当前版）
APP_DIR = os.path.dirname(CLIENT_WEB)          # /srv/dormroll/app
RC_ROOT = os.path.dirname(APP_DIR)             # /srv/dormroll
ACCOUNTS_FILE = os.path.join(DATA, "state", "accounts.json")
STORE.init(DATA)
def _load_auth_secret():
    """签名密钥：环境变量 > state/auth_secret 文件 > 现场生成并 600 落盘。
    绝不回落到那个公开的上传 token（否则谁都能伪造任意楼层令牌）。"""
    env = os.environ.get("RC_AUTH_SECRET")
    if env and len(env) >= 32:
        return env
    path = os.path.join(DATA, "state", "auth_secret")
    try:
        v = open(path, encoding="utf-8").read().strip()
        if len(v) >= 32:
            return v
    except Exception:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    import secrets as _secrets
    v = _secrets.token_hex(32)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(v)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    print("已生成新的签名密钥 state/auth_secret（旧令牌全部失效）", flush=True)
    return v


AUTH_SECRET = _load_auth_secret()


def accounts():
    """账号以 SQLite 为准；库里没有时回落到老 accounts.json。"""
    try:
        rows = STORE.list_accounts()
        if rows:
            return rows
    except Exception:
        pass
    try:
        return json.load(open(ACCOUNTS_FILE, encoding="utf-8")).get("accounts", [])
    except Exception:
        return []


def scope_label(sc):
    cn = "一二三四五六"
    return "%s座%s层" % (sc.get("building", "B"), cn[int(sc.get("floor", 1)) - 1])


def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200000).hex()


def sign_token(aid, scope, hours=12):
    payload = {"aid": aid, "scope": scope, "exp": int(time.time()) + hours * 3600}
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(AUTH_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()[:32]
    return raw + "." + sig


def verify_token(tok):
    try:
        raw, sig = tok.rsplit(".", 1)
        if not hmac.compare_digest(sig, hmac.new(AUTH_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()[:32]):
            return None
        payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def scope_of(q, headers):
    """账号 scope 为准：签名令牌 > 旧 token/admin（视作 B 座一层）"""
    auth = headers.get("Authorization", "") if hasattr(headers, "get") else ""
    tok = (str(auth).replace("Bearer ", "").strip()
           or q.get("access_token", [""])[0] or q.get("token", [""])[0])
    p = verify_token(tok) if tok else None
    if p:
        return p["scope"]
    return {"building": "B", "floor": 1}


def dept_of(cls):
    c = cls or ""
    if any(k in c for k in ("大数据", "网络", "电商", "直播", "智能", "数媒")):
        return "信息技术系"
    if any(k in c for k in ("民", "航空", "民航", "轨道")):
        return "交通服务系"
    if any(k in c for k in ("国商", "会计", "商英", "跨境", "会展")):
        return "国际商务系"
    if any(k in c for k in ("社区", "幼保", "养老")):
        return "学前教育系"
    return ""


def layout_rooms(rooms):
    """按分布图样式出 8 个床位，人名落在它原本的床位上（beds 里 None=空床）。
    兼容三种写法：beds 列表(可含 None/dict/str)、students(list[dict])、students(list[str])。"""
    out = []
    for rm in rooms:
        beds_src = rm.get("beds")
        if beds_src is None:
            beds_src = rm.get("students", []) or []
        seats = []
        for i in range(8):
            x = beds_src[i] if i < len(beds_src) else None
            if x is None:
                seats.append({"name": "", "class": ""})
            elif isinstance(x, dict):
                _stu = {"name": str(x.get("name", "")).strip(),
                        "class": str(x.get("class", "") or "").strip()}
                if x.get("mixed"):
                    _stu["mixed"] = True          # 混寝标记：与房主班不同的住客
                seats.append(_stu)
            else:
                parts = str(x).split()
                seats.append({"name": parts[0] if parts else "",
                              "class": parts[1] if len(parts) > 1 else ""})
        main = next((b["class"] for b in seats if b["name"] and b["class"]), "")
        out.append({"department": dept_of(main), "className": main,
                    "room": str(rm.get("room", "")), "students": seats})
    return out


def pick_roster_file(sc):
    """选定该层唯一名册文件：有 beds 的优先，其次 mtime 新。返回 (path, rooms)。"""
    b, f = sc.get("building", "B"), int(sc.get("floor", 1))
    bases = []
    for d in (os.path.dirname(RosterJSON), DATA, os.path.dirname(DATA)):
        if d and d not in bases:
            bases.append(d)
    cands = []
    for base in bases:
        cands += [os.path.join(base, "roster_%s%df.json" % (b, f)),
                  os.path.join(base, "roster_%s%df.json" % (b.lower(), f))]
    best_score, best_path, best_rooms = -1.0, None, []
    for p in cands:
        if not os.path.exists(p):
            continue
        try:
            data = json.load(open(p, encoding="utf-8"))
            rs = data.get("rooms", []) or []
        except Exception:
            continue
        hasbeds = 1 if any("beds" in r for r in rs) else 0
        score = (hasbeds, os.path.getmtime(p))       # 先比"有没有 beds"，同档比修改时间
        if best_path is None or score > best_score:
            best_score, best_path, best_rooms = score, p, rs
    if best_path is None and b == "B":
        try:
            best_path, best_rooms = RosterJSON, roster().get("rooms", []) or []
        except Exception:
            best_path, best_rooms = None, []
    return best_path, best_rooms


from table_layout import HEADS as TL_HEADS, ROW_OFF, OFFS as TL_OFFS, DAY_SEQ as TL_DAY_SEQ, \
    floor_label as TL_floor_label, row_of as tl_row_of


def track_of(cls):
    """学制：大专 -> 大专，其余（24/25/26 级）-> 中专"""
    c = cls or ""
    return "大专" if "大专" in c else "中专"


def roster_stats(sc):
    """该层应到总人数与中专/大专分布（唯一真源：名册）"""
    rooms = raw_rooms_for_scope(sc)
    total = mid = col = 0
    for rm in rooms:
        beds = rm.get("beds") or rm.get("students") or []
        for b in beds:
            if not b:
                continue
            if isinstance(b, dict):
                nm, cl = str(b.get("name", "")).strip(), str(b.get("class", "") or "")
            else:
                parts = str(b).split()
                nm, cl = (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")
            if not nm:
                continue
            total += 1
            if track_of(cl) == "大专":
                col += 1
            else:
                mid += 1
    return {"total": total, "mid": mid, "col": col}


def raw_rooms_for_scope(sc):
    """该层原始房间列表，按房号合并去重"""
    b = sc.get("building", "B")
    _, rooms = pick_roster_file(sc)
    if not rooms and b == "B":
        rooms = roster().get("rooms", []) or []
    merged = {}
    for rm in rooms:
        rn = str(rm.get("room", "")).strip()
        if not rn:
            continue
        if rn not in merged:
            merged[rn] = {"room": rn, "beds": rm.get("beds") or rm.get("students") or []}
        elif not merged[rn].get("beds"):
            merged[rn]["beds"] = rm.get("beds") or rm.get("students") or []
    # 房号排序：同层按数字升序，视觉与分布图一致
    return [merged[k] for k in sorted(merged, key=lambda x: (len(x), x))]


def roster_version(sc):
    p, _ = pick_roster_file(sc)
    try:
        return int(os.path.getmtime(p))
    except Exception:
        return 0


def roster_for_scope(sc):
    """按楼栋楼层给名单：优先 roster_<B><f>f.json，B 座一层回退到总名册"""
    b, f = sc.get("building", "B"), int(sc.get("floor", 1))
    af = os.path.join(os.path.dirname(RosterJSON), "roster_%s%df.json" % (b, f))
    rooms = []
    if os.path.exists(af):
        try:
            rooms = json.load(open(af, encoding="utf-8")).get("rooms", [])
        except Exception:
            rooms = []
    elif b == "B":
        rooms = [rm for rm in roster().get("rooms", []) if str(rm.get("room", "")).startswith(str(f))]
    merged = {}
    for rm in rooms:
        studs = rm.get("students", [])
        if not studs:
            # 空房也要留在表上，保持布局
            key = ("", str(rm["room"]))
            merged.setdefault(key, {"department": "", "className": "", "room": key[1], "students": []})
            continue
        for st in studs:
            key = (st.get("class", ""), str(rm["room"]))
            if key not in merged:
                merged[key] = {"department": "", "className": key[0], "room": key[1], "students": []}
            if st["name"] not in merged[key]["students"]:
                merged[key]["students"].append(st["name"])
    return list(merged.values())
PORT = 8100
ROLL, MARKED = "roll", "marked"
NAME_RE = re.compile(r"^(?:(?:roll|marked)/)?\d{8}-\d{6}[a-z0-9\-]*\.json(?:\.gz)?$")
Q_OF = {9: "DS", 10: "DB", 11: "XS", 12: "XB"}
WEEKDAY_CN = "一二三四五六日"
LOCK = threading.RLock()      # 保护写文件（config.json / pending/<date>.json 的读-改-写）
FILL_LOCK = threading.Lock()  # /write-now 单飞，避免连点重复写表
MAX_BODY = 500_000

# ---------------- 客户端 OTA ----------------
def client_manifest():
    """列出当前页面包的版本与文件哈希，客户端据此决定要不要更新"""
    files = []
    if os.path.isdir(CLIENT_WEB):
        for name in sorted(os.listdir(CLIENT_WEB)):
            if name.startswith(".") or name == "manifest.json":
                continue
            fp = os.path.join(CLIENT_WEB, name)
            if not os.path.isfile(fp):
                continue
            with open(fp, "rb") as f:
                b = f.read()
            files.append({"path": name, "size": len(b),
                          "sha256": hashlib.sha256(b).hexdigest(), "required": True})
    ver = "1.1.0"
    mf = os.path.join(CLIENT_WEB, "manifest.json")
    if os.path.exists(mf):
        try:
            ver = json.load(open(mf, encoding="utf-8")).get("app_version", ver)
        except Exception:
            pass
    try:
        rv = int(os.path.getmtime(RosterJSON))
    except Exception:
        rv = 0
    return {"app_version": ver, "roster_version": rv, "min_supported": "1.1.0", "files": files}


def serve_client_file(handler, rel):
    base = os.path.realpath(CLIENT_WEB)
    fp = os.path.realpath(os.path.join(base, rel))
    if not fp.startswith(base + os.sep) or not os.path.isfile(fp):
        return handler._send(404, {"ok": False, "error": "missing"})
    ext = os.path.splitext(fp)[1].lower()
    mime = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8", ".css": "text/css; charset=utf-8"}.get(ext, "application/octet-stream")
    with open(fp, "rb") as f:
        body = f.read()
    handler.send_response(200)
    handler.send_header("Content-Type", mime)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        pass


# ---------------- 名册 ----------------
_roster_cache = None
def _alias_tables():
    """别名表兜底：新名册可能没有 alias_map/name_aliases，缺了就空表，绝不 KeyError"""
    r = roster() or {}
    return (r.get("name_aliases") or {}), (r.get("alias_map") or {})


def roster():
    global _roster_cache
    if _roster_cache is None:
        _roster_cache = json.load(open(RosterJSON, encoding="utf-8"))
    return _roster_cache

def norm_name(name):
    """名字归正 + 班级推导：带括号/空格后缀（如「郭煜彬 民1」）按别名表补班级。"""
    alias, amap = _alias_tables()
    parts = str(name).strip().split()
    nm = alias.get(parts[0], parts[0])
    cls = None
    if len(parts) > 1:
        tag = parts[1]
        cls = amap.get(tag) or amap.get(tag.replace("25", ""))
    return nm, cls


def class_for(nm, room):
    """按（姓名, 房号）查班级；兼容 beds/students 两种名册结构。"""
    parts = str(nm).split()
    base = parts[0]
    idx = {}
    for room_ in (roster().get("rooms") or []):
        for s in (room_.get("beds") or room_.get("students") or []):
            if not s:
                continue
            if isinstance(s, dict):
                idx[(s.get("name"), str(room_.get("room")))] = s.get("class")
            else:
                p = str(s).split()
                idx[(p[0] if p else "", str(room_.get("room")))] = p[1] if len(p) > 1 else ""
    return idx.get((base, room))

def build_students(M, sc=None):
    """标记件 → [{name,cls,q,order}]，别名归正、班级推导、名册序排序。
    sc 给定时按该层名册查班级/顺序（多层关键），否则回退总名册。"""
    alias, amap = _alias_tables()
    idx, order = {}, {}
    oi = 0
    rooms = raw_rooms_for_scope(sc) if sc else (roster().get("rooms") or [])
    for room in rooms:
        beds = room.get("beds") or room.get("students") or []
        for s in beds:
            if not s:
                continue
            if isinstance(s, dict):
                nm, cl = str(s.get("name", "")).strip(), str(s.get("class", "") or "")
            else:
                parts = str(s).split()
                nm, cl = (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")
            if not nm:
                continue
            idx[(nm, str(room["room"]))] = cl
            order[(nm, str(room["room"]))] = oi
            oi += 1
    out = []
    seen = set()
    for x in M["students"]:
        parts = str(x["name"]).strip().split()
        nm = alias.get(parts[0], parts[0])
        cls = idx.get((nm, x["room"]))
        if len(parts) > 1 and (amap.get(parts[1]) or amap.get(parts[1].replace("25", ""))):
            cls2 = amap.get(parts[1]) or amap.get(parts[1].replace("25", ""))
            cls = cls2
        key = (nm, str(x["room"]))
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": nm, "room": x["room"], "cls": cls or "?", "q": x["q"],
                    "_ord": order.get((nm, x["room"]), 9999)})
    out.sort(key=lambda s: s["_ord"])
    return out

def fmt_group(items):
    m = {}
    for it in items:
        m.setdefault(it["cls"], []).append(it["name"])
    return " ".join(c + " ".join(ns) for c, ns in m.items())

# ---------------- 存储 ----------------
def to_stored(p):
    t = p.get("type")
    if t == "marked":
        return {"v": 4, "type": "marked", "ref": p.get("ref"), "d": p.get("d"),
                "weekend": bool(p.get("weekend")), "n": p.get("n", [])}
    if t == "rollcall":
        if p.get("v") == 2:
            return {"v": 2, "type": "rollcall", "d": p.get("d"), "t": p.get("t"),
                    "p": p.get("p"), "a": p.get("a"), "n": p.get("n", [])}
        return {"v": 2, "type": "rollcall", "d": p.get("date"), "t": p.get("total"),
                "p": p.get("present"), "a": p.get("absent"),
                "n": [[s.get("name", ""), str(s.get("room", ""))] for s in p.get("absentStudents", [])]}
    return {"v": 2, "type": t, "d": p.get("d") or time.strftime("%Y-%m-%d %H:%M:%S"), "msg": p.get("msg")}

# ---------- 原始件落盘加密（AES-256-GCM，透明读写）----------
_ENC_MAGIC = b"RCENC1"
_ENC_KEY = None


def _enc_key():
    """落盘密钥：state/at_rest.key（600），缺则生成。取不到就返回 None（回退明文）。"""
    global _ENC_KEY
    if _ENC_KEY is not None:
        return _ENC_KEY or None
    path = os.path.join(DATA, "state", "at_rest.key")
    try:
        if os.path.exists(path):
            k = open(path, "rb").read().strip()
        else:
            import secrets as _s
            k = _s.token_bytes(32)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(k)
            os.chmod(path, 0o600)
        _ENC_KEY = k if len(k) == 32 else b""
    except Exception:
        _ENC_KEY = b""
    return _ENC_KEY or None


def _encrypt_bytes(raw):
    key = _enc_key()
    if not key:
        return None
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        import secrets as _s
        nonce = _s.token_bytes(12)
        return _ENC_MAGIC + nonce + AESGCM(key).encrypt(nonce, raw, None)
    except Exception:
        return None


def _decrypt_bytes(blob):
    key = _enc_key()
    if not key or not blob.startswith(_ENC_MAGIC):
        return None
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce, ct = blob[len(_ENC_MAGIC):len(_ENC_MAGIC) + 12], blob[len(_ENC_MAGIC) + 12:]
        return AESGCM(key).decrypt(nonce, ct, None)
    except Exception:
        return None


def read_json(path):
    """读 JSON：自动识别加密件（RCENC1 头）与旧明文（gzip/纯文本）。"""
    with open(path, "rb") as f:
        blob = f.read()
    if blob.startswith(_ENC_MAGIC):
        plain = _decrypt_bytes(blob)
        if plain is None:
            raise RuntimeError("加密件无法解密（密钥不对？）: %s" % os.path.basename(path))
        return json.loads(gzip.decompress(plain).decode("utf-8"))
    if path.endswith(".gz") or blob[:2] == b"\x1f\x8b":
        return json.loads(gzip.decompress(blob).decode("utf-8"))
    return json.loads(blob.decode("utf-8"))

def write_json(path, obj):
    # 同目录唯一临时名 + 全局锁：并发写同一 path（config.json / pending/<date>.json）不再互相截断
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    with LOCK:
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
        os.close(fd)
        try:
            if path.endswith(".gz"):
                raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                raw = gzip.compress(raw)
                encrypt = ("/marked/" in path.replace("\\", "/")) or ("/roll/" in path.replace("\\", "/"))
                blob = _encrypt_bytes(raw) if encrypt else None
                with open(tmp, "wb") as f:
                    f.write(blob if blob else raw)
            else:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(obj, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

def resolve(name):
    if not name or ".." in name or not NAME_RE.match(name):
        return None
    base = os.path.basename(name)
    for cand in (os.path.join(DATA, name), os.path.join(DATA, ROLL, base), os.path.join(DATA, MARKED, base)):
        for p in (cand, cand + ".gz"):
            if os.path.isfile(p):
                return p
    return None

def iter_records():
    out = []
    for sub in (ROLL, MARKED):
        d = os.path.join(DATA, sub)
        if os.path.isdir(d):
            for f in os.listdir(d):
                # 只认完整件：排除 .tmp / .tmp-xxx 半成品，否则 json.load 会在写盘中途炸
                if re.match(r"^\d{8}-\d{6}", f) and (f.endswith(".json") or f.endswith(".json.gz")):
                    out.append((f"{sub}/{f}", os.path.join(d, f)))
    return sorted(out, key=lambda x: os.path.getmtime(x[1]))

# ---------------- 配置/日志 ----------------
def cfg():
    p = os.path.join(DATA, "state", "config.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            c = json.load(f)
        for k, v in {"node": "", "sheet": "s1", "week": "", "daterange": "", "notified": []}.items():
            c.setdefault(k, v)
        return c
    return {"node": "", "sheet": "s1", "week": "", "daterange": "", "notified": []}

def save_cfg(c):
    os.makedirs(os.path.join(DATA, "state"), exist_ok=True)
    write_json(os.path.join(DATA, "state", "config.json"), c)

def _boot_migrate():
    """首次启动把老的文件记录搬进 SQLite（可重复执行）。"""
    try:
        if STORE.kv_get("migrated_from_files") != "1":
            r = STORE.migrate_from_files(DATA, log=logline)
            STORE.kv_set("migrated_from_files", "1")
            logline("SQLite 初始化完成：%s" % r)
    except Exception as e:
        logline("文件迁移失败：%r" % (e,))


def logline(s):
    os.makedirs(os.path.join(DATA, "logs"), exist_ok=True)
    with open(os.path.join(DATA, "logs", "fill.log"), "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + s + "\n")

def notify(text):
    c = cfg()
    for cid in c.get("notified", []):
        try:
            subprocess.run([DWS, "chat", "message", "send", "--conversation-id", cid,
                            "--content", text, "--yes", "--format", "json"],
                           capture_output=True, timeout=30)
        except Exception as e:
            logline(f"通知失败 {cid}: {e}")

# ---------------- 填表核心 ----------------
def mark_date(mk):
    """标记件 d → YYYY-MM-DD；兼容 2026-10-19 与 2026/10/19 两种写法"""
    raw = (mk.get("d") or "").strip().split()[0].replace("-", "/") if mk.get("d") else ""
    try:
        return time.strftime("%Y-%m-%d", time.strptime(raw, "%Y/%m/%d"))
    except Exception:
        return ""

def block_for(dstr, iso):
    """日期归哪块（0..4）：config.blockmap 显式覆盖（调休/补班用）> 按星期默认"""
    bm = (cfg().get("blockmap") or {})
    if dstr in bm:
        v = int(bm[dstr])
        return v if 0 <= v <= 4 else None
    return TL_DAY_SEQ.get(iso)

def plan_for(mk):
    """标记件 → {date, block, row, values[10], leaveN}；不在周日~周四返回 None"""
    roll = None
    if mk.get("ref"):
        p = resolve(mk["ref"])
        if p:
            roll = read_json(p)
    if roll is None:
        rolls = [(rel, ab) for rel, ab in iter_records() if rel.startswith(ROLL + "/")
                 and (read_json(ab).get("d") or "")[:10] == (mk.get("d") or "")[:10]]
        if rolls:
            roll = read_json(rolls[-1][1])
    dstr = mark_date(mk)
    sc0 = {"building": mk.get("building", "B"), "floor": int(mk.get("floor", 1) or 1)}
    stus = build_students({"students": [{"name": x[0], "room": str(x[1]), "q": x[2] if len(x) > 2 else "DS"}
                                        for x in mk.get("n", [])]}, sc=sc0)
    if not dstr:
        return None
    wd = time.strptime(dstr, "%Y-%m-%d").tm_wday  # 0=Mon..6=Sun
    iso = 7 if wd == 6 else wd + 1
    seq = block_for(dstr, iso)                     # 调休可经 config.blockmap 显式指定块
    if seq is None:
        return None
    sc = sc0
    row = tl_row_of(seq, sc["building"], sc["floor"])
    if row is None:
        logline(f"未知楼层 {sc['building']}{sc['floor']} 的标记件，不生成计划")
        return None
    # 应到/实到/请假数一律以名册为准（回执里的 t/p 可能为 0 或缺失，不能当数）
    st = roster_stats(sc)
    def cnt(qs, track):
        return len([x for x in qs if x["q"] in qs and track_of(x["cls"]) == track])
    cols = [fmt_group([x for x in stus if x["q"] == Q_OF[c]]) or "/" for c in (9, 10, 11, 12)]
    # 模板原义：C=应到 D=/ E=实到 F=/ G=请假 H=/ I..L=四名单；中专/大专细分进日报
    values = [st["total"], "/", max(st["total"] - len(stus), 0), "/", len(stus), "/"] + cols
    unknown = [x["name"] for x in stus if x["cls"] == "?"]
    report = smart_report(dstr, sc, st, stus, unknown)
    return {"date": dstr, "block": seq + 1, "row": row, "range": f"C{row}:L{row}",
            "values": values,
            "leaveN": len(stus), "weekend": bool(mk.get("weekend")),
            "counts": {k: sum(1 for x in stus if x["q"] == k) for k in ("DS", "DB", "XS", "XB")},
            "students": len(stus),
            "mid": st["mid"], "col": st["col"], "report": report,
            "file": f"{dstr}-{sc['building']}{sc['floor']}",
            "sheet_title": f"{sc['building']}{sc['floor']}"}

BLANK_ROWS = os.path.join(DATA, "state", "blank_rows.json")


def reset_day(date):
    """一键重置某天请假表：主表七层行还原空白模板 + 体温当日标记清除 + 信箱/销账归档。
    只碰指定日期，绝不扫其他块。"""
    import subprocess as _sp
    d = datetime.datetime.strptime(date, "%Y-%m-%d").date()
    iso = d.isoweekday()
    seq = block_for(date, iso)
    if seq is None:
        return {"ok": False, "error": f"{date} 不在点名周（周日~周四）"}
    head = [1, 25, 49, 73, 97][seq]
    try:
        blank = json.load(open(BLANK_ROWS, encoding="utf-8"))
    except Exception:
        return {"ok": False, "error": "空白模板缺失 blank_rows.json"}
    c = cfg()
    node, sid = c.get("node"), c.get("sheet", "s1")
    if not node:
        return {"ok": False, "error": "未配置在线表 node"}
    rows_done = 0
    for o in (4, 6, 8, 10, 14, 16, 18):
        vals = blank.get(str(o))
        if vals is None:
            continue
        # 重置前留底：原行有内容就存一份，误点可还原
        try:
            g = _sp.run([DWS, "sheet", "range", "get", "--node", node, "--sheet-id", sid,
                         "--range", f"C{head + o}:L{head + o}", "--format", "json"],
                        capture_output=True, text=True, timeout=60)
            cells = (json.loads(g.stdout or "{}").get("cells") or [[]])[0]
            old_vals = [(c or {}).get("value", "") for c in cells]
            if any(str(x).strip() for x in old_vals):
                bd = os.path.join(DATA, "logs", "rowbackup")
                os.makedirs(bd, exist_ok=True)
                fn = os.path.join(bd, "%s-reset-%02d-%s.json" % (date, o, time.strftime("%H%M%S")))
                json.dump({"range": f"C{head + o}:L{head + o}", "old": old_vals},
                          open(fn, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception as e:
            logline(f"重置留底失败 C{head + o}: {str(e)[:80]}")
        payload = json.dumps([[{"type": "text", "text": str(x)} for x in vals]], ensure_ascii=False)
        r = _sp.run([DWS, "sheet", "range", "update", "--node", node, "--sheet-id", sid,
                     "--range", f"C{head + o}:L{head + o}", "--values", payload, "--format", "json"],
                    capture_output=True, text=True, timeout=60)
        if '"success": true' in (r.stdout + r.stderr):
            rows_done += 1
        else:
            logline(f"重置主表行失败 C{head + o}: {(r.stdout + r.stderr)[-160:]}")
    # 体温：本地主档 + 在线共享表，当日列 病/事 → 清
    tl = to = 0
    try:
        import openpyxl
        TEMP = os.path.join(os.path.dirname(os.path.dirname(DATA)), "app", "data", "宿舍点体温.xlsx")
        if not os.path.exists(TEMP):
            TEMP = "/srv/dormroll/app/data/宿舍点体温.xlsx"
        key = f"{d.year}.{d.month:02d}"
        if os.path.exists(TEMP):
            wb = openpyxl.load_workbook(TEMP)
            if key in wb.sheetnames:
                ws = wb[key]
                for sec in _temp_sections_local(ws):
                    col = sec["days"].get(d.day)
                    if not col:
                        continue
                    for rr, _room, _nm in sec["students"]:
                        v = ws.cell(rr, col).value
                        if isinstance(v, str) and v.strip() in ("病", "事"):
                            ws.cell(rr, col).value = "=ROUND(RAND()*(37-36.3)+36.3,1)"
                            tl += 1
                if tl:
                    wb.save(TEMP)
    except Exception as e:
        logline(f"重置本地体温失败: {e}")
    try:
        tnode = c.get("temp_node")
        if tnode:
            rr0 = _sp.run([DWS, "sheet", "+list-sheets", "--node", tnode, "--format", "json"],
                          capture_output=True, text=True, timeout=60)
            sheets = (json.loads(rr0.stdout or "{}").get("data", {}) or {}).get("sheets") or []
            tsid = next((x.get("sheetId") for x in sheets if str(d.month) in str(x.get("title", ""))), None)
            if tsid:
                from openpyxl.utils import get_column_letter as _gcl
                gg = _sp.run([DWS, "sheet", "range", "get", "--node", tnode, "--sheet-id", tsid,
                              "--range", "A1:AH120", "--format", "json"], capture_output=True, text=True, timeout=60)
                grid = (json.loads(gg.stdout or "{}").get("cells")) or []
                daycols = {}
                for i, row in enumerate(grid):
                    vs = [str((cc or {}).get("value", "") or "").strip() for cc in row]
                    if len(vs) > 1 and vs[1] == "日期":
                        for j, v in enumerate(vs):
                            if v == str(d.day):
                                daycols[i] = j
                titles = sorted(daycols)
                for ti, trow in enumerate(titles):
                    col = daycols[trow]
                    end = titles[ti + 1] - 2 if ti + 1 < len(titles) else len(grid)
                    for r2 in range(trow + 2, min(end, len(grid))):
                        vs = [str((cc or {}).get("value", "") or "").strip() for cc in grid[r2]]
                        nm = vs[1] if len(vs) > 1 else ""
                        v = vs[col] if len(vs) > col else ""
                        if nm and v in ("病", "事"):
                            L = _gcl(col + 1)
                            _sp.run([DWS, "sheet", "range", "update", "--node", tnode, "--sheet-id", tsid,
                                     "--range", f"{L}{r2 + 1}:{L}{r2 + 1}",
                                     "--values", json.dumps([[{"type": "text", "text": ""}]]), "--format", "json"],
                                    capture_output=True, text=True, timeout=60)
                            to += 1
    except Exception as e:
        logline(f"重置在线体温失败: {e}")
    # 信箱与销账归档
    pd = plan_dir()
    pend = 0
    for f in os.listdir(pd):
        if f.startswith(date) and f.endswith(".json"):
            os.replace(os.path.join(pd, f), os.path.join(DATA, "logs", "superseded-" + f))
            pend += 1
    dn = os.path.join(DATA, "logs", "done", date + ".json")
    if os.path.exists(dn):
        os.replace(dn, os.path.join(DATA, "logs", "superseded-" + date + ".json"))
    logline(f"一键重置 {date}：主表{rows_done}行 体温本地{tl} 在线{to} 信箱{pend}件")
    if rows_done < 7:
        return {"ok": False, "error": f"只写成功 {rows_done}/7 行，表可能没清干净，请重试或查日志",
                "rows": rows_done, "temp_local": tl, "temp_online": to, "pending": pend}
    return {"ok": True, "rows": rows_done, "temp_local": tl, "temp_online": to, "pending": pend}


def _temp_sections_local(ws):
    secs = []
    titles = [r for r in range(1, ws.max_row + 1)
              if ws.cell(r, 1).value and "点名表" in str(ws.cell(r, 1).value)]
    for i, t in enumerate(titles):
        drow = t + 1
        end = titles[i + 1] - 1 if i + 1 < len(titles) else ws.max_row
        days = {}
        if str(ws.cell(drow, 2).value or "").strip() == "日期":
            days = {ws.cell(drow, cc).value: cc for cc in range(3, ws.max_column + 1)
                    if isinstance(ws.cell(drow, cc).value, (int, float))}
        cur = None
        students = []
        for r in range(drow + 2, end + 1):
            v1, v2 = ws.cell(r, 1).value, ws.cell(r, 2).value
            if v1 not in (None, ""):
                cur = str(v1).strip()
            if v2 in (None, ""):
                continue
            nm = str(v2).strip()
            if nm in ("姓名", "日期"):
                continue
            students.append((r, cur, nm))
        secs.append({"drow": drow, "days": days, "students": students})
    return secs


def smart_report(dstr, sc, st, stus, unknown):
    """专用小智能：把当日数据写成人话，并标出异常（纯规则，不碰模型）"""
    c = {k: sum(1 for x in stus if x["q"] == k) for k in ("DS", "DB", "XS", "XB")}
    lines = [f"{dstr} {scope_label(sc)}：应到 {st['total']}（中专 {st['mid']}/大专 {st['col']}），"
             f"实到 {max(st['total'] - len(stus), 0)}，请假 {len(stus)}"
             f"（宿管事 {c['DS']} 宿管病 {c['DB']} 部系事 {c['XS']} 部系病 {c['XB']}）"]
    warns = []
    if unknown:
        warns.append("名册外姓名：" + "、".join(unknown[:6]))
    if len(stus) > st["total"]:
        warns.append(f"请假{len(stus)}人超过应到{st['total']}，名册可能过期")
    if len(stus) >= 10:
        warns.append(f"请假达 {len(stus)} 人，偏多")
    db3 = {}
    for x in stus:
        if x["q"] == "DB":
            db3[x["name"]] = db3.get(x["name"], 0) + 1
    same_room = {}
    for x in stus:
        same_room[x["room"]] = same_room.get(x["room"], 0) + 1
    hot = [r for r, n in same_room.items() if n >= 3]
    if hot:
        warns.append("同房集中请假：" + "、".join(hot))
    lines.append("异常：" + ("；".join(warns) if warns else "无"))
    return "\n".join(lines)


def weekend_rows(mk, sc=None):
    """周末未返校行：名册皆B座一层→只填 sheet B1；[班级全称,姓名,原因] **按班级序**（同班内名册房间序）"""
    if sc is None:
        sc = {"building": mk.get("building", "B"), "floor": int(mk.get("floor", 1) or 1)}
    stus = build_students({"students": [{"name": x[0], "room": str(x[1]), "q": x[2] if len(x) > 2 else "DS"}
                                        for x in mk.get("n", [])]}, sc=sc)
    def full(c): return {"25民1": "25民航1", "25民2": "25民航2"}.get(c, c)
    classes = []   # 班级首现序=名册班序，保证同班聚拢、班间有稳定次序
    for s in stus:
        if s["cls"] not in classes: classes.append(s["cls"])
    stus.sort(key=lambda s: (classes.index(s["cls"]), s["_ord"]))
    return [[full(s["cls"]), s["name"], "病假" if s["q"].endswith("B") else "走读"] for s in stus]

def purge_same_date(new_path, newdate, newmk=None):
    newmk = newmk or {}
    """同日期旧 marked → .trash（一日内多次改标记，表只认最新，库也不堆陈件）"""
    trash = os.path.join(DATA, ".trash")
    os.makedirs(trash, exist_ok=True)
    for rel, ab in iter_records():
        if rel.startswith(MARKED + "/") and ab != new_path:
            try:
                d0 = read_json(ab)
                if mark_date(d0) == newdate and \
                   d0.get("building", "B") == newmk.get("building", "B") and \
                   str(d0.get("floor", 1)) == str(newmk.get("floor", 1)):
                    dst = os.path.join(trash, time.strftime("%Y%m%d%H%M%S") + "-" + os.path.basename(ab))
                    os.replace(ab, dst)
                    logline(f"旧同日期件隐藏 {rel}")
            except Exception:
                pass

def plan_dir():
    d = os.path.join(DATA, "pending"); os.makedirs(d, exist_ok=True); return d

def process_marked(path):
    """算计划→落待填信箱→尝试写在线表→成功则销账。写不成信箱留着，AI/手工都能接。"""
    mk = read_json(path)
    plan = plan_for(mk)
    if not plan:
        logline(f"标记件不在周日~周四，仅存档 {path}")
        return
    plan["marked_file"] = os.path.relpath(path, DATA)
    plan["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    # 信箱只留当日一格：清掉非今日的陈计划（只清计划，upload 原件一律保留、不删不动）
    pd = plan_dir()
    lg = os.path.join(DATA, "logs"); os.makedirs(lg, exist_ok=True)
    for old in os.listdir(pd):
        stem = old[:-5]
        same_floor = stem == plan.get("file", plan["date"]) or stem.startswith(plan["date"] + "-" + mk.get("building", "B") + str(mk.get("floor", 1)))
        if old.endswith(".json") and not same_floor and not stem.startswith(plan["date"]):
            try:
                os.replace(os.path.join(pd, old), os.path.join(lg, "superseded-" + old))
                logline(f"陈计划过期归档 {old}")
            except OSError as e:      # 并发/已归档，不致命
                logline(f"陈计划归档跳过 {old}: {e}")
    pp = os.path.join(pd, plan.get("file", plan["date"]) + ".json")   # 每层一格，同日同层再投=覆盖
    if plan["weekend"]:
        try:
            plan["weekend_rows"] = weekend_rows(mk, {"building": mk.get("building", "B"),
                                                     "floor": int(mk.get("floor", 1) or 1)})
        except Exception as e:
            logline(f"周末行生成失败: {e}")
    # 并发/乱序保护：新件若比信箱里那份更旧（后台线程完成顺序不保证），不许覆盖较新的计划
    with LOCK:
        if os.path.exists(pp):
            try:
                with open(pp, encoding="utf-8") as f:
                    old_plan = json.load(f)
                om = os.path.join(DATA, old_plan.get("marked_file") or "")
                if os.path.isfile(om) and os.path.getmtime(om) > os.path.getmtime(path):
                    logline(f"计划 {plan['date']} 已由更新的标记件生成，跳过本次覆盖")
                    return
            except Exception:
                pass
        write_json(pp, plan)
    logline(f"计划入信箱 {plan['date']} {plan['range']} 请假{plan['leaveN']}" +
            (f" 周末{len(plan.get('weekend_rows') or [])}行" if plan.get("weekend_rows") is not None else "") +
            "（待 AI 任务取件填写）")


def _bg_process(path):
    """上传后的后台处理：任何异常只记日志，不让线程静默炸掉计划"""
    try:
        process_marked(path)
    except Exception as e:
        logline(f"标记件处理失败 {path}: {e}")


# ---------------- HTTP ----------------
def _eq(a, b):
    try:
        return hmac.compare_digest(str(a), str(b))
    except Exception:
        return False

def _pipeline_script():
    """pipeline_ops.py 位置：RC_PIPELINE > 本文件同目录 > 一体机固定路径"""
    cands = []
    if os.environ.get("RC_PIPELINE"):
        cands.append(os.environ["RC_PIPELINE"])
    cands += [os.path.join(os.path.dirname(os.path.abspath(__file__)), "pipeline_ops.py"),
              "/srv/dormroll/ops/pipeline_ops.py"]
    for c in cands:
        if os.path.isfile(c):
            return c
    return cands[-1]

class H(BaseHTTPRequestHandler):
    def _send(self, code, obj, ctype="application/json; charset=utf-8"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
        try:
            etag = '"%s"' % hashlib.md5(body).hexdigest()[:16]
            if code == 200 and len(body) >= 800:
                inm = (self.headers.get("If-None-Match") or "").strip()
                if inm and inm == etag:
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    return
            gz = False
            if len(body) >= 800 and "gzip" in (self.headers.get("Accept-Encoding") or "").lower():
                body = gzip.compress(body, 6)
                gz = True
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            if len(body) >= 300:
                self.send_header("ETag", etag)
            if gz:
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass                     # 客户端提前断开，别把 traceback 打到 stderr

    def _auth(self, q):
        return bool(TOKEN) and _eq(q.get("token", [""])[0], TOKEN)

    def _token_ok(self, q, payload=None):
        """签名令牌 / 旧 token 任一命中即可；楼栋以账号 scope 为准"""
        tok = (q.get("token", [""])[0] or (payload or {}).get("token")
               or self.headers.get("Authorization", "").replace("Bearer ", "").strip())
        if tok and verify_token(tok):
            return True
        if not TOKEN:
            return False
        if _eq(q.get("token", [""])[0], TOKEN):
            return True
        return isinstance(payload, dict) and _eq(payload.get("token"), TOKEN)

    def _read_body(self):
        """→ (payload, err_code)。err_code 0 表示成功。超长不读 body，直接 413 并关连接。"""
        n = self.headers.get("Content-Length")
        try:
            n = int(n) if n is not None else 0
        except (TypeError, ValueError):
            self.close_connection = True
            return None, 400
        if n < 0 or n > MAX_BODY:
            # 先抽掉一小段残留 body 再回 413，否则客户端还在写、收到的是 broken pipe 而不是状态码
            try:
                self.rfile.read(min(n, 2_000_000))
            except Exception:
                pass
            self.close_connection = True      # 超大件不回 keep-alive，避免半截 body 串到下一个请求
            return None, 413
        if n == 0:
            return {}, 0
        try:
            raw = self.rfile.read(n)
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return None, 400
        if not isinstance(payload, dict):
            return None, 400
        return payload, 0

    def do_OPTIONS(self):
        self.send_response(204)
        for k, v in (("Access-Control-Allow-Origin", "*"), ("Access-Control-Allow-Methods", "POST, GET, DELETE, OPTIONS"),
                     ("Access-Control-Allow-Headers", "Content-Type"), ("Access-Control-Max-Age", "86400")):
            self.send_header(k, v)
        self.end_headers()

    def do_POST(self):
        try:
            return self._post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            logline("POST 异常 %s: %r" % (self.path, e))
            try:
                self._send(500, {"ok": False, "error": "server: %s" % str(e)[:120]})
            except Exception:
                pass

    def _post(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        payload, err = self._read_body()
        if err:
            return self._send(err, {"ok": False, "error": "bad json" if err == 400 else "too large"})
        # ---- iPad 上传口（与原 example.com 契约一致）----
        if u.path == "/upload":
            if not self._token_ok(q, payload):
                return self._send(401, {"ok": False, "error": "token"})
            t = payload.get("type")
            if t not in ("rollcall", "marked", "test"):
                return self._send(400, {"ok": False, "error": "type"})
            if t == "marked":
                nn = payload.get("n")
                if not isinstance(nn, list) or any(
                        not isinstance(x, (list, tuple)) or len(x) < 2
                        or not isinstance(x[0], str) or not isinstance(x[1], (str, int))
                        or (len(x) > 2 and str(x[2]).upper() not in ("DS", "DB", "XS", "XB"))
                        for x in nn):
                    return self._send(400, {"ok": False, "error": "n 形状不对：应为 [[姓名,房号,DS|DB|XS|XB],…]"})
                if len(nn) > 400:
                    return self._send(400, {"ok": False, "error": "n 超过 400 条"})
                # —— 红队补丁：日期格式 + 本层归属 + 查重，防跨层污染/假名/坏日期 ——
                _d = str(payload.get("d", "") or "").strip()
                if _d and not re.fullmatch(r"\d{4}/\d{1,2}/\d{1,2}( \d{1,2}:\d{2}(:\d{2})?)?", _d):
                    return self._send(400, {"ok": False, "error": "日期格式应为 yyyy/M/d HH:mm:ss"})
                _sc0 = scope_of(q, self.headers)
                try:
                    _allow = {(str(r.get("room", "")).strip(), str(b.get("name", "")).strip())
                              for r in raw_rooms_for_scope(_sc0) for b in (r.get("beds") or []) if isinstance(b, dict)}
                except Exception:
                    _allow = set()
                if _allow:
                    _bad = [f"{x[1]}·{x[0]}" for x in nn
                            if (str(x[1]).strip(), str(x[0]).strip()) not in _allow]
                    if _bad:
                        return self._send(400, {"ok": False,
                                                "error": "%s 不在本层名册：%s" % (scope_label(_sc0), "、".join(_bad[:5]))})
                _seen = set()
                for x in nn:
                    k = (str(x[1]).strip(), str(x[0]).strip())
                    if k in _seen:
                        return self._send(400, {"ok": False, "error": "同一个人重复：%s %s" % k})
                    _seen.add(k)
            rec = to_stored(payload)
            sc = scope_of(q, self.headers)
            rec["building"], rec["floor"] = sc.get("building", "B"), sc.get("floor", 1)
            kind = "marked" if rec.get("type") == "marked" else "roll"
            ts = time.strftime("%Y%m%d-%H%M%S")
            try:
                date = mark_date(rec)
            except Exception:
                date = time.strftime("%Y-%m-%d")
            uid = STORE.add_upload(ts, date, rec["building"], rec["floor"], kind,
                                   len(rec.get("n") or []) if kind == "marked" else 0,
                                   rec.get("ref"), rec)
            if kind == "marked":
                try:
                    plan = plan_for(rec)
                    if not plan:
                        logline("非点名日，未生成计划（上传 #%d）" % uid)
                    else:
                        plan["marked_file"] = "db/%d" % uid
                        STORE.upsert_plan(plan["date"], rec["building"], rec["floor"], plan)
                        logline("计划入库 %s %s（上传 #%d）" % (plan["date"], scope_label(sc), uid))
                except Exception as e:
                    logline("计划生成失败（上传 #%d）：%s" % (uid, e))
            self._send(200, {"ok": True, "saved": "db/%d" % uid, "type": rec.get("type")})
            return
        if u.path == "/auth/login":
            aid = str(payload.get("id", "")).strip()
            pw = str(payload.get("password", ""))
            acc = next((a for a in accounts() if a.get("id") == aid), None)
            if not acc or not acc.get("enabled", True):
                return self._send(403, {"ok": False, "error": "账号不存在或已停用"})
            if hash_pw(pw, acc.get("salt", "")) != acc.get("pass", ""):
                logline(f"登录失败 {aid} from {self.client_address[0]}")
                return self._send(401, {"ok": False, "error": "账号或密码不对"})
            sc = acc["scope"]
            logline(f"登录成功 {aid} {scope_label(sc)}")
            return self._send(200, {"ok": True, "access_token": sign_token(aid, sc),
                                    "account": aid, "scope": sc, "label": scope_label(sc),
                                    "roster_version": int(os.path.getmtime(RosterJSON))})
        # ---- 管理 API ----
        # 三种身份之一即可：旧 admin 口令 / 任意有效签名令牌（层管也能点"写入表"）
        _authh = self.headers.get("Authorization", "").replace("Bearer ", "").strip()
        admin_ok = bool(ADMIN) and (q.get("admin", [""])[0] == ADMIN
                                    or payload.get("admin") == ADMIN
                                    or _authh == ADMIN)
        tok_ok = self._token_ok(q, payload)
        if not (admin_ok or tok_ok):
            return self._send(401, {"ok": False, "error": "admin"})
        if u.path in ("/api/roster/sources", "/api/roster/check"):
            _hdr = self.headers.get("Authorization", "").replace("Bearer ", "").strip()
            if not (bool(ADMIN) and (q.get("admin", [""])[0] == ADMIN
                                     or payload.get("admin") == ADMIN or _hdr == ADMIN)):
                return self._send(401, {"ok": False, "error": "admin"})
            _cfg = os.path.join(DATA, "state", "roster_sources.json")
            _st = os.path.join(DATA, "state", "roster_sources_state.json")
            if u.path == "/api/roster/sources":
                if self.command == "POST":
                    os.makedirs(os.path.dirname(_cfg), exist_ok=True)
                    slim = {"sources": payload.get("sources") or [],
                            "apply": bool(payload.get("apply")),
                            "overrides": payload.get("overrides") or {}}
                    with open(_cfg, "w", encoding="utf-8") as fh:
                        json.dump(slim, fh, ensure_ascii=False, indent=1)
                    logline("住宿表源已保存：%d 个源，apply=%s" % (len(slim["sources"]), slim["apply"]))
                    return self._send(200, {"ok": True, "saved": slim})
                cur, last = {}, {}
                try:
                    cur = json.load(open(_cfg, encoding="utf-8"))
                except Exception:
                    pass
                try:
                    last = json.load(open(_st, encoding="utf-8"))
                except Exception:
                    pass
                tail = []
                try:
                    with open(os.path.join(DATA, "logs", "roster_watch.log"), encoding="utf-8") as fh:
                        tail = fh.readlines()[-20:]
                except Exception:
                    pass
                return self._send(200, {"ok": True, "cfg": cur, "state": last, "log": tail})
            # /api/roster/check —— 立刻跑一次
            env = dict(os.environ, PYTHONPATH=os.path.join(RC_ROOT, "vendor"))
            r = subprocess.run([sys.executable, os.path.join(RC_ROOT, "ops", "roster_watch.py")],
                               capture_output=True, text=True, timeout=300, env=env, cwd=RC_ROOT)
            tail = []
            try:
                with open(os.path.join(DATA, "logs", "roster_watch.log"), encoding="utf-8") as fh:
                    tail = fh.readlines()[-30:]
            except Exception:
                pass
            return self._send(200, {"ok": r.returncode in (0, 2), "code": r.returncode,
                                    "out": (r.stdout or "")[-2000:], "err": (r.stderr or "")[-500:],
                                    "log": tail})
        if u.path == "/api/roster/import":
            _hdr_admin = self.headers.get("Authorization", "").replace("Bearer ", "").strip()
            if not (bool(ADMIN) and (q.get("admin", [""])[0] == ADMIN
                                     or payload.get("admin") == ADMIN
                                     or _hdr_admin == ADMIN)):
                return self._send(401, {"ok": False, "error": "admin"})
            import base64 as _b64, tempfile as _tf, subprocess as _sp
            tmpdir = _tf.mkdtemp(prefix="roster-")
            paths = {}
            for k, ext in (("b", ".xlsx"), ("a", ".xlsx"), ("male", ".xls"), ("female", ".xls")):
                raw = payload.get(k)
                if not raw:
                    continue
                try:
                    blob = base64.b64decode(str(raw))
                except Exception:
                    return self._send(400, {"ok": False, "error": "%s 不是合法 base64" % k})
                fp = os.path.join(tmpdir, k + ext)
                with open(fp, "wb") as fh:
                    fh.write(blob)
                paths[k] = fp
            if not paths:
                return self._send(400, {"ok": False, "error": "至少给一张分布图（b 或 a）"})
            cmd = [sys.executable, os.path.join(RC_ROOT, "ops", "roster_import.py")]
            for k, flag in (("b", "--b"), ("a", "--a"), ("male", "--male"), ("female", "--female")):
                if k in paths:
                    cmd += [flag, paths[k]]
            if payload.get("overrides"):
                cmd += ["--overrides", json.dumps(payload["overrides"], ensure_ascii=False)]
            if not payload.get("confirm"):
                cmd.append("--dry")
            env = dict(os.environ, PYTHONPATH=os.path.join(RC_ROOT, "vendor"))
            r = _sp.run(cmd, capture_output=True, text=True, timeout=180, env=env, cwd=RC_ROOT)
            if r.returncode != 0:
                return self._send(500, {"ok": False, "error": (r.stderr or r.stdout)[-400:]})
            try:
                out = json.loads(r.stdout)
            except Exception:
                return self._send(500, {"ok": False, "error": "导入输出不可解析", "raw": r.stdout[-300:]})
            if payload.get("confirm"):
                logline("住宿表导入（%s）%s" % ("正式" if not out.get("dry") else "预演",
                                              json.dumps({k: (v["rooms"], v["people"]) for k, v in out.get("floors", {}).items()},
                                                         ensure_ascii=False)))
            return self._send(200, out)
        if u.path in ("/api/reset-day", "/reset-day"):
            if not (bool(ADMIN) and (q.get("admin", [""])[0] == ADMIN or payload.get("admin") == ADMIN)):
                return self._send(401, {"ok": False, "error": "admin"})
            date = str(payload.get("date") or q.get("date", [""])[0] or time.strftime("%Y-%m-%d"))
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                return self._send(400, {"ok": False, "error": "bad date"})
            return self._send(200, reset_day(date))
        if u.path in ("/write-now", "/api/write-now"):
            # 立刻填表：走统一流水线，主表+周末表+体温表一并落数
            # 先看信箱：空的就直接说清楚，别让前端以为"写好了"
            today = time.strftime("%Y-%m-%d")
            try:
                n_today = sum(1 for pl in STORE.list_plans() if str(pl.get("date")) == today)
            except Exception:
                n_today = 0
            if n_today == 0:                       # 兼容还没迁移的老文件形态
                try:
                    pd = plan_dir()
                    n_today = len([f for f in os.listdir(pd)
                                   if f.startswith(today) and f.endswith(".json")])
                except Exception:
                    n_today = 0
            if n_today == 0:
                return self._send(200, {"ok": False, "error": "信箱为空：请先点「上传」，等提示成功后再写表",
                                        "log": ["没有本层今日计划，未执行写入"]})
            # ThreadingHTTPServer 下不阻塞其它请求，但连点会并发起多个 dws 写同一张表 → 单飞
            if not FILL_LOCK.acquire(blocking=False):
                logline("write-now 跳过：已有填表任务在执行")
                return self._send(200, {"ok": True, "skipped": True, "log": ["已有填表任务在执行，本次跳过"]})
            try:
                r = subprocess.run([sys.executable, _pipeline_script(), "fill",
                                    "--targets=main,weekend,temp"],
                                   capture_output=True, text=True, timeout=600)
                out = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
                wrote = any("妥" in ln for ln in out)                       # 有"…妥"才算真写成功
                skipped = (not wrote) and any(("无待填计划" in ln) or ("空箱" in ln)
                                              or ("本次跳过" in ln) or ("另一个填表进程" in ln)
                                              for ln in out)
                skip = skipped
                ok2 = (r.returncode == 0) and wrote and not any("!!" in ln or "停 " in ln for ln in out)
                if skip:
                    return self._send(200, {"ok": False, "skipped": True,
                                            "error": "没有可写的内容（信箱空或另一任务在写）",
                                            "log": out[-4:]})
                err2 = "" if ok2 else (out[-1] if out else "填表失败")
                return self._send(200, {"ok": ok2, "error": err2, "log": out[-4:]})
            except subprocess.TimeoutExpired:
                logline("write-now 超时（600s）")
                return self._send(500, {"ok": False, "error": "pipeline timeout"})
            except Exception as e:
                return self._send(500, {"ok": False, "error": str(e)[:160]})
            finally:
                FILL_LOCK.release()
        if u.path == "/api/fill-now":
            p = resolve(payload.get("f", ""))
            if not p:
                return self._send(404, {"ok": False, "error": "missing"})
            try:
                process_marked(p)
            except Exception as e:
                logline(f"fill-now 失败 {p}: {e}")
                return self._send(500, {"ok": False, "error": "process failed"})
            return self._send(200, {"ok": True})
        if u.path == "/api/ack":   # 兜底任务写成功后销账：把 pending 挪 logs/done
            date = str(payload.get("date") or payload.get("file") or q.get("date", [""])[0])
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(-[ABab]\d)?", date):   # 计划文件名，防 ../ 穿越
                return self._send(400, {"ok": False, "error": "bad date"})
            n = 0
            try:
                n = STORE.ack_plan(date)
            except Exception:
                n = 0
            if n:
                logline(f"销账 {date}（库）")
                return self._send(200, {"ok": True})
            pp = os.path.join(plan_dir(), date + ".json")
            if os.path.exists(pp):
                done = os.path.join(DATA, "logs", "done"); os.makedirs(done, exist_ok=True)
                os.replace(pp, os.path.join(done, os.path.basename(pp)))
                logline(f"销账 {date}")
                return self._send(200, {"ok": True})
            return self._send(200, {"ok": True, "skipped": "no pending"})   # 幂等：已销过账也算成功
        if u.path == "/api/config":
            if not (bool(ADMIN) and (q.get("admin", [""])[0] == ADMIN or payload.get("admin") == ADMIN)):
                return self._send(401, {"ok": False, "error": "admin"})
            with LOCK:                    # 读-改-写整体加锁，防并发 POST 丢更新
                c = cfg()
                c.update({k: payload[k] for k in ("node", "sheet", "week", "daterange", "notified", "blockmap", "weekend_node", "temp_node") if k in payload})
                save_cfg(c)
            return self._send(200, {"ok": True})
        return self._send(404, {"ok": False, "error": "not found"})

    def do_GET(self):
        try:
            return self._get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            logline("GET 异常 %s: %r" % (self.path, e))
            try:
                self._send(500, {"ok": False, "error": "server: %s" % str(e)[:120]})
            except Exception:
                pass

    def _get(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/manifest":
            if not self._token_ok(q) and (not ADMIN or q.get("admin", [""])[0] != ADMIN):
                return self._send(401, {"ok": False, "error": "auth"})
            m = client_manifest()
            sc = scope_of(q, self.headers)
            m["scope"], m["label"] = sc, scope_label(sc)
            m["roster_version"] = roster_version(sc)
            return self._send(200, m)
        if u.path == "/api/app/latest":
            f = os.path.join(APP_DIR, "android", "latest.json")
            try:
                with open(f, encoding="utf-8") as fh:
                    return self._send(200, json.load(fh))
            except Exception:
                return self._send(404, {"ok": False, "error": "尚未发布安卓包"})
        if u.path == "/api/app/latest-ios":
            f = os.path.join(APP_DIR, "ios", "latest.json")
            try:
                with open(f, encoding="utf-8") as fh:
                    return self._send(200, json.load(fh))
            except Exception:
                return self._send(404, {"ok": False, "error": "尚未发布 iOS 包"})
        if u.path.startswith("/api/app/ios/"):
            name = os.path.basename(u.path)
            f = os.path.join(APP_DIR, "ios", name)
            if not os.path.isfile(f):
                return self._send(404, {"ok": False, "error": "no such file"})
            with open(f, "rb") as fh:
                body = fh.read()
            ctype = ("application/x-plist" if name.endswith(".plist")
                     else "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if u.path.startswith("/api/app/apk/"):
            name = os.path.basename(u.path)
            f = os.path.join(APP_DIR, "android", name)
            if not os.path.isfile(f):
                return self._send(404, {"ok": False, "error": "no such file"})
            with open(f, "rb") as fh:
                body = fh.read()
            if q.get("b64", [""])[0] in ("1", "true"):
                return self._send(200, {"ok": True, "file": name, "size": len(body),
                                        "sha256": hashlib.sha256(body).hexdigest(),
                                        "b64": base64.b64encode(body).decode()})
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.android.package-archive")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if u.path == "/api/roster":
            if not self._token_ok(q) and not verify_token(q.get("token", [""])[0])                and not verify_token(self.headers.get("Authorization", "").replace("Bearer ", ""))                and (not ADMIN or q.get("admin", [""])[0] != ADMIN):
                return self._send(401, {"ok": False, "error": "auth"})
            sc = scope_of(q, self.headers)
            return self._send(200, {"label": scope_label(sc), "scope": sc,
                                    "roster_version": roster_version(sc),
                                    "dormData": layout_rooms(raw_rooms_for_scope(sc))})
        if u.path == "/api/roster/version":
            if not self._auth(q):
                return self._send(401, {"ok": False, "error": "token"})
            return self._send(200, {"roster_version": int(os.path.getmtime(RosterJSON))})
        if u.path.startswith("/static/client/"):
            # 客户端包内含凭据：必须凭令牌或管理口令取，不能匿名下载
            tok = (q.get("token", [""])[0] or self.headers.get("Authorization", "").replace("Bearer ", "").strip())
            if not (bool(ADMIN) and tok == ADMIN) and not self._token_ok(q):
                return self._send(401, {"ok": False, "error": "auth"})
            return serve_client_file(self, u.path[len("/static/client/"):])
        # 静态：/ 管理中心；/assistant/ 网页版填表助手（同 WEB 目录下）
        seg = u.path.lstrip("/") or "index.html"
        if seg.endswith("/"):
            seg += "index.html"
        head = seg.split("/", 1)[0]
        if head not in ("roll", "marked", "api", "list", "latest", "get", "index", "raw"):
            base = os.path.realpath(WEB)
            fp = os.path.realpath(os.path.join(base, seg))
            if (fp == base or fp.startswith(base + os.sep)) and os.path.isfile(fp):
                ext = os.path.splitext(fp)[1].lower()
                if ext in (".html", ".css", ".js"):
                    mime = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                            ".js": "application/javascript; charset=utf-8"}[ext]
                    body = open(fp, "rb").read()
                    self.send_response(200)
                    self.send_header("Content-Type", mime)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store" if ext == ".html" else "public, max-age=60")
                    self.end_headers()
                    try:
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
            return self._send(404, {"ok": False, "error": "gui missing"})
        if u.path.startswith("/raw/"):
            _h = self.headers.get("Authorization", "").replace("Bearer ", "").strip()
            _admin_ok = bool(ADMIN) and (_h == ADMIN or q.get("admin", [""])[0] == ADMIN)
            if not _admin_ok and not self._auth(q):
                return self._send(401, {"ok": False, "error": "token"})
            _name = u.path[5:]
            if _name.startswith("db/"):
                try:
                    uid = int(_name.split("/", 1)[1])
                except Exception:
                    return self._send(400, {"ok": False, "error": "bad id"})
                blob = STORE.export_gz(uid)
                if blob is None:
                    return self._send(404, {"ok": False, "error": "missing"})
                return self._send(200, blob, "application/gzip")
            p = resolve(u.path[5:])
            if not p:
                return self._send(404, {"ok": False, "error": "missing"})
            with open(p, "rb") as f:          # 别把 fd 漏在长跑进程里
                body = f.read()
            if body.startswith(_ENC_MAGIC):   # 加密件：解密后给人看
                try:
                    body = json.dumps(read_json(p), ensure_ascii=False, indent=2).encode("utf-8")
                except Exception:
                    body = "(encrypted file, decrypt failed)".encode("utf-8")
            return self._send(200, body, "application/gzip" if p.endswith(".gz") else "application/octet-stream")
        # iPad 兼容口
        if u.path in ("/list", "/latest", "/get", "/index"):
            if not self._auth(q):
                return self._send(401, {"ok": False, "error": "token"})
            recs = iter_records()
            if u.path == "/list":
                return self._send(200, {"ok": True, "files": [r for r, _ in recs]})
            if u.path == "/index":
                items = []
                for rel, ab in recs:
                    try:
                        d = read_json(ab)
                        if d.get("type") == "marked":
                            items.append({"f": rel, "type": "marked", "d": d.get("d"), "ref": d.get("ref"),
                                          "weekend": d.get("weekend", False), "a": len(d.get("n", []))})
                        else:
                            d = to_stored(d) if d.get("v") != 2 else d
                            items.append({"f": rel, "type": "rollcall", "d": d.get("d"), "t": d.get("t"),
                                          "p": d.get("p"), "a": d.get("a")})
                    except Exception:
                        items.append({"f": rel, "d": "?"})
                return self._send(200, {"ok": True, "items": items})
            if u.path == "/latest":
                marked = [(r, a) for r, a in recs if r.startswith(MARKED)]
                pool = marked or recs
                if not pool:
                    return self._send(404, {"ok": False, "error": "empty"})
                return self._send(200, read_json(pool[-1][1]))
            if u.path == "/get":
                p = resolve(q.get("name", [""])[0])
                if not p:
                    return self._send(404, {"ok": False, "error": "missing"})
                d = read_json(p)
                if not isinstance(d, dict):
                    return self._send(500, {"ok": False, "error": "bad record"})
                if d.get("type") == "marked":      # 标记件转 v1 形态（含 q），供网页助手直读
                    roll = None
                    if d.get("ref"):
                        rp = resolve(d["ref"])
                        roll = read_json(rp) if rp else None
                    items = [{"name": x[0], "room": str(x[1]),
                              "q": x[2] if len(x) > 2 else "DS"} for x in d.get("n", [])]
                    return self._send(200, {"date": d.get("d"), "total": (roll or {}).get("t"),
                                            "present": (roll or {}).get("p"), "absent": len(items),
                                            "absentStudents": items})
                return self._send(200, d)
        # 管理 API（读）
        if u.path == "/api/history":
            _h = self.headers.get("Authorization", "").replace("Bearer ", "").strip()
            if not ADMIN or (q.get("admin", [""])[0] != ADMIN and _h != ADMIN):
                return self._send(401, {"ok": False, "error": "admin"})
            out = []
            try:
                rows = STORE.list_uploads(500)
            except Exception:
                rows = []
            for row in rows:
                try:
                    d = STORE.payload_of(row["id"]) or {}
                    e = {"f": "db/%d" % row["id"], "type": d.get("type"), "d": d.get("d"),
                         "size": row.get("n") or 0, "sha256": row.get("sha256")}
                    if d.get("type") == "marked":
                        e.update({"ref": d.get("ref"), "weekend": d.get("weekend", False),
                                  "n": len(d.get("n", []))})
                        plan = None
                        try:
                            plan = plan_for(d)
                        except Exception:
                            pass
                        if plan is not None:
                            e["plan"] = plan
                    else:
                        e.update({"t": d.get("t"), "p": d.get("p"), "a": d.get("a")})
                    out.append(e)
                except Exception as ex:
                    out.append({"f": "db/%d" % row.get("id", 0), "error": str(ex)})
            out = list(reversed(out))
            _legacy = False
            for rel, ab in ([] if out else []):
                try:
                    d = read_json(ab)
                    e = {"f": rel, "type": d.get("type"), "d": d.get("d"), "size": os.path.getsize(ab)}
                    if d.get("type") == "marked":
                        e.update({"ref": d.get("ref"), "weekend": d.get("weekend", False), "n": len(d.get("n", []))})
                        try:
                            e["plan"] = plan_for(d)
                        except Exception:
                            pass
                    else:
                        e.update({"t": d.get("t"), "p": d.get("p"), "a": d.get("a")})
                    out.append(e)
                except Exception as ex:
                    out.append({"f": rel, "error": str(ex)})
            c = cfg()
            return self._send(200, {"ok": True, "items": list(reversed(out)), "cfg": c,
                                    "log": self._tail(os.path.join(DATA, "logs", "fill.log"))})
        if u.path == "/api/pending":   # 待填信箱（兜底任务/管理台读，token 或 admin 均可）
            if not self._auth(q) and (not ADMIN or q.get("admin", [""])[0] != ADMIN):
                return self._send(401, {"ok": False, "error": "auth"})
            try:
                items = STORE.list_plans()
            except Exception as e:
                logline(f"计划读取失败: {e}")
                items = []
            return self._send(200, {"ok": True, "plans": items, "node": cfg().get("node"),
                                    "sheet": cfg().get("sheet", "s1"), "weekend_node": cfg().get("weekend_node"), "temp_node": cfg().get("temp_node"),
                                    "last_done": self._latest_mtime(os.path.join(DATA, "logs", "done"))})
        if u.path == "/api/students":
            if not self._auth(q) and (not ADMIN or q.get("admin", [""])[0] != ADMIN):
                return self._send(401, {"ok": False, "error": "auth"})
            # 名册视图（每人的缺勤历史），iPad 点名页可 fetch
            hist = {}
            for row in (STORE.list_uploads(1000) if True else []):
                try:
                    d = STORE.payload_of(row["id"]) or {}
                    rel = "db/%d" % row["id"]
                    if d.get("type") != "marked":
                        continue
                    dt = mark_date(d)
                    for x in d.get("n", []):
                        nm = _alias_tables()[0].get(str(x[0]).split()[0], str(x[0]).split()[0])
                        hist.setdefault(nm, []).append({"date": dt, "q": x[2] if len(x) > 2 else "?", "f": rel})
                except Exception:
                    pass
            r = roster()
            stu = []
            for room in (r.get("rooms") or []):
                for s in (room.get("beds") or room.get("students") or []):
                    if not s:
                        continue
                    if isinstance(s, dict):
                        nm, cl = s.get("name") or "", s.get("class") or ""
                    else:
                        pp = str(s).split()
                        nm, cl = (pp[0] if pp else ""), (pp[1] if len(pp) > 1 else "")
                    if not nm:
                        continue
                    stu.append({"name": nm, "room": str(room.get("room")), "cls": cl,
                                "absent": sorted(hist.get(nm, []), key=lambda h: h["date"], reverse=True)[:60]})
            return self._send(200, {"ok": True, "students": stu})
        return self._send(404, {"ok": False, "error": "not found"})

    @staticmethod
    def _latest_mtime(d):
        try:
            fs = os.listdir(d)
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(
                max(os.path.getmtime(os.path.join(d, f)) for f in fs))) if fs else None
        except Exception:
            return None

    @staticmethod
    def _tail(p, n=80):
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.readlines()[-n:]

    def do_DELETE(self):
        try:
            return self._delete()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            logline("DELETE 异常 %s: %r" % (self.path, e))
            try:
                self._send(500, {"ok": False, "error": "server: %s" % str(e)[:120]})
            except Exception:
                pass

    def _delete(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/api/delete", "/delete"):
            if u.path == "/api/delete":
                ok = ADMIN and q.get("admin", [""])[0] == ADMIN
            else:
                ok = self._auth(q)                       # iPad 联动：token 即可
            if not ok:
                return self._send(401, {"ok": False, "error": "auth"})
            name = q.get("name", [""])[0]
            if name.startswith("db/"):
                try:
                    uid = int(name.split("/", 1)[1])
                except Exception:
                    return self._send(400, {"ok": False, "error": "bad id"})
                row = STORE.get_upload(uid)
                if not row:
                    return self._send(404, {"ok": False, "error": "missing"})
                STORE.delete_upload(uid)
                logline("删除(库) #%d %s %s" % (uid, row.get("date"), row.get("kind")))
                try:                      # 回退重算：该日该层还有更早的 marked 就重排计划
                    latest = STORE.latest_upload(row["date"], row["building"], row["floor"], "marked")
                    if latest:
                        pl = plan_for(STORE.payload_of(latest["id"]))
                        STORE.upsert_plan(pl["date"], row["building"], row["floor"], pl)
                    else:
                        STORE.drop_plan(row["date"], row["building"], row["floor"])
                except Exception as e:
                    logline("删除后重算失败 #%d: %s" % (uid, e))
                return self._send(200, {"ok": True, "deleted": name})
            p = resolve(name)
            if not p:
                return self._send(404, {"ok": False, "error": "missing"})
            trash = os.path.join(DATA, ".trash"); os.makedirs(trash, exist_ok=True)
            rel = os.path.relpath(p, DATA)
            dst = os.path.join(trash, time.strftime("%Y%m%d%H%M%S") + "-" + rel.replace(os.sep, "_"))
            os.replace(p, dst)
            logline(f"删除(隐藏) {rel}")
            extra = []
            d0 = None
            try:
                d0 = read_json(os.path.join(trash, os.path.basename(dst)))
            except Exception:
                pass
            if rel.startswith(ROLL + "/") and d0 is not None:
                # 级联：引用这份回执的 marked 件一并隐藏
                rn = os.path.basename(name).replace(".gz", "")
                for r2, ab in iter_records():
                    if r2.startswith(MARKED + "/"):
                        try:
                            m = read_json(ab)
                        except Exception:
                            continue
                        if m.get("ref") and os.path.basename(m["ref"]).replace(".gz", "") == rn:
                            d2 = os.path.join(trash, time.strftime("%Y%m%d%H%M%S") + "c-" + os.path.basename(ab))
                            os.replace(ab, d2)
                            extra.append(r2)
            if rel.startswith(MARKED + "/") and d0 is not None:
                # 回退重算：该日期还有更早 marked 则入信箱，否则清空该行
                date = mark_date(d0)
                leftovers = []
                for r2, ab in iter_records():
                    if r2.startswith(MARKED + "/"):
                        try:
                            m2 = read_json(ab)
                        except Exception:
                            continue
                        if mark_date(m2) == date:
                            leftovers.append((r2, ab, m2))
                pd = plan_dir()
                pp = os.path.join(pd, date + ".json")
                if leftovers:
                    leftovers.sort(key=lambda x: os.path.getmtime(x[1]))
                    plan = plan_for(leftovers[-1][2])
                    if plan:
                        plan["marked_file"] = leftovers[-1][0]
                        plan["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
                        write_json(pp, plan)
                        logline(f"删后回退 {date} → {leftovers[-1][0]}")
                elif os.path.exists(pp):
                    os.remove(pp)
                    logline(f"删后清信箱 {date}")
            return self._send(200, {"ok": True, "hidden": rel, "cascade": extra})
        return self._send(404, {"ok": False, "error": "not found"})

    def log_message(self, *a):
        pass

if __name__ == "__main__":
    for sub in (ROLL, MARKED):
        os.makedirs(os.path.join(DATA, sub), exist_ok=True)
    _boot_migrate()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
