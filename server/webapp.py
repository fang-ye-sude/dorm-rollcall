#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宿舍点名控制台 · ssdm.example.com
功能：发布指定周的两大表 / 生成指定月体温表 / 下载原始点名件 / 下载成品表
运行：python3 webapp.py（监听 0.0.0.0:8120），Basic 认证，凭据 /srv/dormroll/ops/web_auth.txt
"""
import base64, gzip, hmac, io, json, os, re, subprocess, sys, threading, time, zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, "/srv/dormroll/ops")
sys.path.insert(0, "/srv/dormroll")
import pipeline_ops as P
import rollcall_suite as R

OPS = "/srv/dormroll/ops"
AUTH = os.path.join(OPS, "web_auth.txt")
PORT = 8120
JOB = {"running": False, "log": [], "title": ""}
LOCK = threading.Lock()



LOGS_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>设备日志 · 宿舍点名</title>
<style>
body{margin:0;background:#f5f7fa;color:#16202b;font:14px/1.6 -apple-system,"PingFang SC",sans-serif}
header{display:flex;gap:12px;align-items:center;padding:14px 18px;background:#fff;border-bottom:1px solid #e3e8ef}
h1{font-size:17px;margin:0} a{color:#1d4e89;text-decoration:none}
form{margin-left:auto;display:flex;gap:6px} input,select,button{padding:5px 8px;border:1px solid #cfd8e3;border-radius:6px;font:inherit;background:#fff}
table{width:100%%;border-collapse:collapse;background:#fff} th,td{padding:7px 10px;border-bottom:1px solid #eef2f7;vertical-align:top;text-align:left}
th{background:#f8fafc;font-weight:600;position:sticky;top:0} td.ts{white-space:nowrap;color:#64748b}
td.msg{white-space:pre-wrap;word-break:break-all;max-width:640px} .empty{text-align:center;color:#94a3b8;padding:40px}
.bar{padding:8px 18px;color:#64748b;font-size:12px}
</style></head><body>
<header><h1>设备日志</h1><a href="/">← 回控制台</a>
<form method="get"><input name="device" placeholder="设备/层" value="%s">
<select name="level"><option value="">全部级别</option>
<option value="crash">crash</option><option value="error">error</option><option value="warn">warn</option><option value="info">info</option></select>
<button>筛选</button></form></header>
<div class="bar">最近 %d 条 · 每 30 秒自动刷新</div>
<table><thead><tr><th>时间</th><th>设备</th><th>版本</th><th>级别</th><th>标签</th><th>内容</th></tr></thead>
<tbody>%s</tbody></table>
<script>
const lv=new URLSearchParams(location.search).get('level')||'';
document.querySelector('select[name=level]').value=lv;
setTimeout(()=>location.reload(),30000);
</script></body></html>"""

def creds():
    try:
        u, p = open(AUTH, encoding="utf-8").read().strip().split(":", 1)
        return u, p
    except Exception:
        return "ssdm", "ssdm"


def run_job(title, argv):
    with LOCK:
        if JOB["running"]:
            return False
        JOB.update(running=True, log=[], title=title)

    def _run():
        try:
            if argv and argv[0] == "__shell__":
                r = subprocess.run(["systemctl", "start", argv[1] + ".service"],
                                   capture_output=True, text=True, timeout=900)
                j = subprocess.run(["journalctl", "-u", argv[1] + ".service", "-n", "12", "--no-pager", "-o", "cat"],
                                   capture_output=True, text=True, timeout=30)
                r = type("R", (), {"stdout": (r.stdout or "") + (j.stdout or ""), "stderr": r.stderr})()
            else:
                r = subprocess.run([sys.executable, os.path.join(OPS, "pipeline_ops.py")] + argv,
                                   capture_output=True, text=True, timeout=900)
            JOB["log"] = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-40:]
        except Exception as e:
            JOB["log"] = ["异常：%s" % e]
        finally:
            with LOCK:
                JOB["running"] = False

    try:
        threading.Thread(target=_run, daemon=True).start()
    except Exception as e:                       # 线程起不来就别把 running 卡在 True
        with LOCK:
            JOB.update(running=False, log=["异常：%s" % e])
        return False
    return True


def dws_sheets(node):
    r = subprocess.run(["/usr/local/bin/dws", "sheet", "+list-sheets", "--node", node, "--format", "json"],
                       capture_output=True, text=True, timeout=60)
    try:
        return json.loads(r.stdout)["data"]["sheets"]
    except Exception:
        return []


def read_cells(node, sid, rng):
    r = subprocess.run(["/usr/local/bin/dws", "sheet", "range", "read", "--node", node,
                        "--sheet-id", sid, "--range", rng, "--format", "json"],
                       capture_output=True, text=True, timeout=60)
    try:
        cells = json.loads(r.stdout)["cells"]
    except Exception:
        return []
    return [[("" if (c or {}).get("value") is None else str(c.get("value"))) for c in row] for row in cells]


KIND_KEYS = {"main": "住宿生请假汇总表", "weekend": "周末未返校统计", "temp": "宿舍点体温"}
_FCACHE = {}


def find_files(kind, limit):
    """按类列云盘文件（60s 缓存：前端每 12s 轮询一次，别每次都起 dws）"""
    now = time.time()
    c = _FCACHE.get(kind)
    if c and now - c[0] < 60:
        return c[1][:limit]
    kw = KIND_KEYS.get(kind) or KIND_KEYS["main"]
    try:
        r = subprocess.run(["/usr/local/bin/dws", "drive", "+find-file", "--query", kw, "--format", "json"],
                           capture_output=True, text=True, timeout=60)
        raw = json.loads(r.stdout).get("files", [])[:40]
        if not isinstance(raw, list):
            raw = []
    except Exception:
        raw = []
    if raw:
        _FCACHE[kind] = (now, raw)
    return raw[:limit]


def table_items(kind, limit, sort_key):
    from concurrent.futures import ThreadPoolExecutor
    raw = find_files(kind, limit)
    with ThreadPoolExecutor(max_workers=8) as ex:
        metas = list(ex.map(lambda f: node_meta(f.get("dentryId")), raw))
    items = []
    for f, m in zip(raw, metas):
        items.append({"node": f.get("dentryId"),
                      "name": m.get("name") or f.get("name"),
                      "create": m.get("create", ""), "modify": m.get("modify", "")})
    items.sort(key=lambda x: x.get(sort_key) or "", reverse=True)
    return items


MARKED = "/srv/dormroll/data/marked"


def marked_path(f):
    """点名件文件名白名单校验，返回绝对路径；不合法返回 None"""
    if not f or not re.match(r"^[0-9A-Za-z][0-9A-Za-z._-]*$", f) or ".." in f:
        return None
    p = os.path.realpath(os.path.join(MARKED, f))
    if not p.startswith(MARKED + os.sep):
        return None
    return p


_XCACHE = {}


def export_table(kind, node):
    """按节点导出 xlsx：整表一次读取，结果按 (node, 最后修改时间) 缓存 180s"""
    import openpyxl, io as _io
    mt = node_meta(node).get("modify", "")
    ck = (kind, node)
    hit = _XCACHE.get(ck)
    if hit and hit[0] == mt and time.time() - hit[1] < 180:
        return hit[2], "table.xlsx"
    sheets = dws_sheets(node)
    if kind == "temp":
        wb2 = openpyxl.Workbook(); ws2 = wb2.active
        ws2.title = (sheets[0]["title"] if sheets else "Sheet1")
        rows = read_cells(node, sheets[0]["sheetId"], "A1:Z160") if sheets else []
        for i, row in enumerate(rows, 1):
            for j, v in enumerate(row, 1):
                if v != "":
                    ws2.cell(i, j).value = v
        bio = _io.BytesIO(); wb2.save(bio)
        data = bio.getvalue()
        _XCACHE[ck] = (mt, time.time(), data)
        return data, "table.xlsx"
    if kind == "main":
        wb = openpyxl.load_workbook(os.path.join(P.APP, "data", "住宿生请假汇总表-空白模板.xlsx"))
        ws = wb["Sheet1"]
        rows = read_cells(node, "s1", "A1:L123")
        for h in P.HEADS:
            di, ri = h + 3 - 1, h + 14 - 1
            if len(rows) > di and rows[di]:
                ws.cell(h + 3, 1).value = rows[di][0] or None
            if len(rows) > ri:
                for j, v in enumerate(rows[ri][2:12]):
                    ws.cell(h + 14, 3 + j).value = v or None
    else:
        wb = openpyxl.load_workbook(os.path.join(P.APP, "data", "周末未返校统计-原版格式模板.xlsx"))
        ws = wb["B1"]
        sid = next((x["sheetId"] for x in sheets if x.get("title") == "B1"), None)
        rows = read_cells(node, sid, "A1:D51") if sid else []
        for i, row in enumerate(rows[1:50]):
            for j, v in enumerate((row or [])[1:4]):
                ws.cell(2 + i, 2 + j).value = v or None
    bio = _io.BytesIO(); wb.save(bio)
    data = bio.getvalue()
    _XCACHE[ck] = (mt, time.time(), data)
    return data, "table.xlsx"


_TCACHE = {}
def node_meta(node):
    """返回 {name, create, modify}（doc info，缓存 120s）"""
    import time as _t
    now = _t.time()
    c = _TCACHE.get(node)
    if c and now - c[0] < (120 if (c[1]["name"] or c[1]["modify"]) else 20):
        return c[1]
    info = {"name": "", "create": "", "modify": ""}
    try:
        r = subprocess.run(["/usr/local/bin/dws", "doc", "info", "--node", node, "--format", "json"],
                           capture_output=True, text=True, timeout=45)
        d = json.loads(r.stdout)
        import datetime as _d
        for k, src in (("create", "createTime"), ("modify", "updateTime")):
            ms = d.get(src)
            if ms:
                info[k] = _d.datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M")
        info["name"] = d.get("name", "")
    except Exception:
        pass
    _TCACHE[node] = (now, info)     # 取失败只缓存 20s，避免瞬时故障锁定 2 分钟空白
    return info


_MCACHE = {"t": 0.0, "data": []}


def audit(kind, detail):
    """下载审计：只记时间、来源、类型、文件名，不记内容。"""
    try:
        rec = "%s %s %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                 getattr(audit, "peer", "-"), kind, detail)
        os.makedirs("/srv/dormroll/data/logs", exist_ok=True)
        with open("/srv/dormroll/data/logs/access.log", "a", encoding="utf-8") as fh:
            fh.write(rec)
        try:
            os.chmod("/srv/dormroll/data/logs/access.log", 0o600)
        except Exception:
            pass
    except Exception:
        pass


def marked_list():
    now = time.time()
    if _MCACHE["data"] and now - _MCACHE["t"] < 30:   # 前端 12s 轮询，别每次都把全目录解压一遍
        return _MCACHE["data"]
    out = []
    d = os.path.join("/srv/dormroll/data/marked")
    try:
        names = sorted(os.listdir(d), reverse=True)
    except OSError:
        return _MCACHE["data"] or []
    for f in names:
        if not f.endswith(".json.gz"):
            continue
        try:
            mk = R.read_json(os.path.join(d, f))
            dv = str(mk.get("d") or "")
            # d 只有日期（无时刻）时，用文件名里的上传时间补齐，列表才分得清是哪一次
            if dv and ":" not in dv and len(f) >= 15:
                try:
                    hm = f.split("-")[1][:6]
                    dv = "%s %s:%s:%s" % (dv.split()[0], hm[:2], hm[2:4], hm[4:6])
                except Exception:
                    pass
            out.append({"file": f, "date": dv, "n": len(mk.get("n", [])),
                        "weekend": bool(mk.get("weekend")), "size": os.path.getsize(os.path.join(d, f))})
        except Exception:
            pass
    out = out[:200]
    _MCACHE.update(t=now, data=out)
    return out


def current_nodes():
    try:
        p = P.jget("%s/api/pending?token=%s" % (P.BASE, P.TOKEN))
        res = {}
        for tag, key in (("主表", "node"), ("周末表", "weekend_node"), ("体温表", "temp_node")):
            n = p.get(key)
            if not n:
                res[tag] = None
                continue
            m = node_meta(n)
            res[tag] = {"node": n, "name": m.get("name", ""), "url": "https://alidocs.dingtalk.com/i/nodes/%s" % n,
                        "create": m.get("create", ""), "modify": m.get("modify", "")}
        return res
    except Exception as e:
        return {"err": str(e)[:120]}


def build_week_xlsx(kind, node, sheet_id, rng, template, out_name):
    """从在线表读回数据 → 套标版模板 → 返回 xlsx bytes"""
    import openpyxl
    rows = P.read_range(node, sheet_id, rng) if rng else []
    wb = openpyxl.load_workbook(template)
    ws = wb["Sheet1"] if kind == "main" else wb["B1"]
    if kind == "main":
        for h in P.HEADS:
            row = h + 14
            vals = P.read_range(node, "s1", "C%d:L%d" % (row, row))
            for j, c in enumerate(vals[0] if vals else []):
                ws.cell(row, 3 + j).value = c.get("value")
            d = P.read_range(node, "s1", "A%d:A%d" % (h + 3, h + 3))
            if d and d[0]:
                ws.cell(h + 3, 1).value = d[0][0].get("value")
    else:
        for i, row in enumerate(rows[:49]):
            for j, c in enumerate(row or []):
                v = c.get("value")
                ws.cell(2 + i, 2 + j).value = v if v not in (None, "") else None
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue(), out_name


class H(BaseHTTPRequestHandler):
    server_version = "ssdm/1.0"

    def log_message(self, *a):
        pass

    def _eq(self, a, b):
        try:
            return hmac.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))
        except Exception:
            return False

    def _cookies(self):
        out = {}
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def authed(self):
        u, p = creds()
        # ① 一次性口令 ?k=xxx → 种 cookie（方便手机/浏览器直接打开）
        u0 = urlparse(self.path)
        q0 = parse_qs(u0.query)
        if p and self._eq((q0.get("k") or [""])[0], p):
            self._cookie = "ssdm=%s; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax" % p
            self._k_seen = True
            return True
        # ② cookie 按名取值比较（不能整串子串匹配，否则 ssdm=pw 出现在别的 cookie 里也算通过）
        if p and self._eq(self._cookies().get("ssdm", ""), p):
            return True
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            cu, cp = base64.b64decode(h[6:]).decode("utf-8").split(":", 1)
        except Exception:
            return False
        return self._eq(cu, u) and bool(p) and self._eq(cp, p)

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        body = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")   # 外链（alidocs）不带 Referer，避免把 ?k= 口令带出去
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        ck = getattr(self, "_cookie", None)
        if ck:
            self.send_header("Set-Cookie", ck)
            self._cookie = None
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        audit.peer = self.client_address[0]
        try:
            self._get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:                  # 兜底：任何异常都要给前端一个 JSON，别让页面卡在“读取中”
            try:
                self._send(500, {"err": "服务内部错误：%s" % str(e)[:200]})
            except Exception:
                pass

    def _get(self):
        if not self.authed():
            return self._send(401, "需要登录", "text/plain; charset=utf-8",
                              {"WWW-Authenticate": 'Basic realm="ssdm"'})
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if getattr(self, "_k_seen", False) and "k" in q and u.path in ("/", "/index.html"):
            self._k_seen = False                # 口令已换成 cookie，重定向掉 URL 里的密码（防日志/Referer 泄露）
            return self._send(302, "", "text/plain; charset=utf-8", {"Location": "/"})
        if u.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if u.path == "/logs":
            try:
                import store as STORE
                dev = (q.get("device", [""])[0] or "").strip()
                lvl = (q.get("level", [""])[0] or "").strip()
                items = STORE.list_device_logs(limit=300, device=dev or None, level=lvl or None)
            except Exception as e:
                items, dev, lvl = [], "", ""
                LOG("设备日志读取失败：%r" % (e,))
            def esc(x):
                return (str(x or "").replace("&", "&amp;").replace("<", "&lt;")
                        .replace(">", "&gt;").replace('"', "&quot;"))
            rows = []
            for it in items:
                import datetime as _dt
                ts = _dt.datetime.fromtimestamp(it["ts"]).strftime("%m-%d %H:%M:%S")
                col = {"crash": "#c0392b", "error": "#d35400", "warn": "#b7950b"}.get(it["level"], "#2c3e50")
                rows.append("<tr><td class=ts>%s</td><td>%s</td><td>%s</td><td style=\"color:%s;font-weight:600\">%s</td>"
                            "<td>%s</td><td class=msg>%s</td></tr>" % (
                                ts, esc(it["device"]), esc(it["version"]), col, esc(it["level"]),
                                esc(it["tag"]), esc(it["msg"])))
            html = LOGS_PAGE % (esc(dev), len(items),
                                "".join(rows) or "<tr><td colspan=6 class=empty>暂无日志</td></tr>")
            return self._send(200, html, "text/html; charset=utf-8")
        if u.path == "/api/logs":
            try:
                import store as STORE
                return self._send(200, {"ok": True, "items": STORE.list_device_logs(
                    limit=max(1, min(500, int((q.get("limit", ["200"])[0] or 200)))),
                    device=(q.get("device", [""])[0] or None),
                    level=(q.get("level", [""])[0] or None))})
            except Exception as e:
                return self._send(500, {"ok": False, "err": str(e)[:120]})
        if u.path == "/api/status":
            return self._send(200, {"nodes": current_nodes(),
                                    "job": {"running": JOB["running"], "title": JOB["title"],
                                            "log": JOB["log"]},
                                    "marks": marked_list()[:30]})
        if u.path == "/api/overview":
            import datetime as _dt
            today = _dt.date.today()
            wk_start = today - _dt.timedelta(days=(today.weekday() + 1) % 7)   # 周日
            wk_end = wk_start + _dt.timedelta(days=4)
            mk = marked_list()
            def _same_day(txt):
                mm = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})", str(txt))
                return bool(mm) and (int(mm.group(1)), int(mm.group(2)), int(mm.group(3))) == (today.year, today.month, today.day)
            tmark = next((m for m in mk if _same_day(m.get("date"))), None)
            done = "/srv/dormroll/data/logs/done"
            done_list = sorted(os.listdir(done), reverse=True) if os.path.isdir(done) else []
            filled = today.isoformat() + ".json" in done_list
            logp = "/srv/dormroll/data/logs/fill.log"
            last = {"ts": "", "line": ""}
            if os.path.exists(logp):
                for ln in os.popen("tail -n 40 %s" % logp).read().splitlines()[::-1]:
                    if "销账" in ln:
                        last = {"ts": ln[:19], "line": ln[20:]}
                        break
            return self._send(200, {
                "today": today.isoformat(),
                "week": {"start": wk_start.isoformat(), "end": wk_end.isoformat(),
                         "label": "%s~%s" % (wk_start.strftime("%m.%d"), wk_end.strftime("%m.%d"))},
                "marked": tmark, "filledToday": filled,
                "doneCount": len(done_list), "last": last,
                "plans": len(P.jget("%s/api/pending?admin=%s" % (P.BASE, P.ADMIN)).get("plans", [])),
            })
        if u.path == "/api/history":
            out = []
            logp = "/srv/dormroll/data/logs/fill.log"
            if os.path.exists(logp):
                lines = os.popen("tail -n 200 %s" % logp).read().splitlines()
                for ln in lines[::-1]:
                    if "销账" in ln or "计划入信箱" in ln:
                        out.append(ln)
                    if len(out) >= 8:
                        break
            return self._send(200, {"lines": out})
        if u.path == "/api/pending":
            p2 = P.jget("%s/api/pending?admin=%s" % (P.BASE, P.ADMIN))
            plans = []
            for pl in p2.get("plans", []):
                plans.append({"date": pl.get("date"), "range": pl.get("range"), "row": pl.get("row"),
                              "values": pl.get("values"), "weekend": bool(pl.get("weekend")),
                              "weekendN": len(pl.get("weekend_rows") or []),
                              "counts": pl.get("counts")})
            return self._send(200, {"plans": plans})
        if u.path == "/api/tables":
            kind = (q.get("kind") or ["main"])[0]
            return self._send(200, {"items": table_items(kind, 24, "modify")})
        if u.path == "/api/library":
            kind = (q.get("kind") or ["main"])[0]
            return self._send(200, {"items": table_items(kind, 40, "create")})
        if u.path.startswith("/static/"):
            fn = u.path[len("/static/"):]
            if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", fn) or ".." in fn:
                return self._send(400, {"err": "bad"})
            base = os.path.realpath(os.path.join(OPS, "static"))
            fp = os.path.realpath(os.path.join(base, fn))
            if not fp.startswith(base + os.sep) or not os.path.isfile(fp):
                return self._send(404, {"err": "not found"})
            ctype = "text/css" if fn.endswith(".css") else ("application/javascript" if fn.endswith(".js") else "application/octet-stream")
            with open(fp, "rb") as fh:
                return self._send(200, fh.read(), ctype, {"Cache-Control": "public, max-age=600"})
        if u.path == "/api/raw":
            f = (q.get("f") or [""])[0]
            fp = marked_path(f)
            if not fp or not os.path.isfile(fp):
                return self._send(404, {"err": "not found"})
            try:
                try:
                    raw0 = open(fp, "rb").read(7)
                except Exception:
                    raw0 = b""
                if raw0.startswith(b"RCENC1"):
                    txt = json.dumps(R.read_json(fp), ensure_ascii=False, indent=2)
                else:
                    txt = gzip.open(fp, "rt", encoding="utf-8").read()
                try:
                    txt = json.dumps(json.loads(txt), ensure_ascii=False, indent=2)
                except Exception:
                    pass
            except Exception:
                txt = open(fp, "rb").read().decode("utf-8", "replace")
            return self._send(200, txt, "text/plain; charset=utf-8")
        if u.path == "/api/mark":
            f = (q.get("f") or [""])[0]
            fp = marked_path(f)
            if not fp or not os.path.isfile(fp):
                return self._send(404, {"err": "not found"})
            mk = R.read_json(fp)
            return self._send(200, {"date": mk.get("d", ""), "weekend": bool(mk.get("weekend")),
                                    "n": mk.get("n", []), "file": f})
        if u.path == "/api/preview":
            node = (q.get("node") or [""])[0]
            sid = (q.get("sid") or [""])[0]
            if not node:
                return self._send(400, {"err": "node"})
            d = dws_sheets(node)
            sheets = d or []
            if not sheets:
                return self._send(200, {"sheets": [], "rows": [], "sid": ""})
            if not sid or sid not in [x["sheetId"] for x in sheets]:
                sid = sheets[0]["sheetId"]
            rows = read_cells(node, sid, "A1:L40")
            return self._send(200, {"sheets": sheets, "sid": sid, "rows": rows})
        if u.path == "/download/table":
            audit("download", "table:%s" % (q.get("kind") or [""])[0])
            kind = (q.get("kind") or ["main"])[0]
            node = (q.get("node") or [""])[0]
            if kind not in ("main", "weekend", "temp"):
                return self._send(400, {"err": "kind"})
            if not node:
                return self._send(400, {"err": "node"})
            data, name = export_table(kind, node)
            return self._send(200, data,
                              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                              {"Content-Disposition": 'attachment; filename="table.xlsx"'})
        if u.path == "/api/timers":
            out = {}
            for t in ("rc-publish", "rc-fill", "rc-sweep", "rc-tempmonth", "rc-backup", "rc-web"):
                en = subprocess.run(["systemctl", "is-enabled", t + ".timer"], capture_output=True, text=True).stdout.strip()
                ac = subprocess.run(["systemctl", "is-active", t + ".timer"], capture_output=True, text=True).stdout.strip()
                out[t] = {"enabled": en, "active": ac}
            return self._send(200, out)
        if u.path == "/api/job":
            return self._send(200, {"running": JOB["running"], "title": JOB["title"], "log": JOB["log"]})
        if u.path == "/download/mark":
            audit("download", "mark:%s" % (q.get("f") or [""])[0])
            f = (q.get("f") or [""])[0]
            p = marked_path(f)
            if not p or not os.path.isfile(p):
                return self._send(404, {"err": "not found"})
            with open(p, "rb") as fh:
                return self._send(200, fh.read(), "application/gzip",
                                  {"Content-Disposition": 'attachment; filename="%s"' % f})
        if u.path == "/download/week":
            kind = (q.get("kind") or ["main"])[0]
            if kind not in ("main", "weekend"):
                return self._send(400, {"err": "kind"})
            nodes = current_nodes()
            n = nodes.get("主表" if kind == "main" else "周末表")
            if not isinstance(n, dict) or not n.get("node"):
                return self._send(409, {"err": "该表未配置或在役指向读取失败：%s" % str(nodes.get("err") or "")[:80]})
            if kind == "main":
                data, name = build_week_xlsx("main", n["node"], "s1", "", os.path.join(
                    P.APP, "data", "住宿生请假汇总表-空白模板.xlsx"), "住宿生请假汇总表.xlsx")
            else:
                data, name = build_week_xlsx("weekend", n["node"], "s5", "A1:D51", os.path.join(
                    P.APP, "data", "周末未返校统计-原版格式模板.xlsx"), "周末未返校统计.xlsx")
            return self._send(200, data,
                              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                              {"Content-Disposition": 'attachment; filename="%s"' % name.encode("utf-8").decode("latin-1", "ignore")})
        if u.path == "/download/temp":
            mon = (q.get("m") or [""])[0]
            import openpyxl
            wb = openpyxl.load_workbook(P.TEMP)
            key = mon or ("%d.%02d" % (__import__("datetime").date.today().year,
                                       __import__("datetime").date.today().month))
            if key not in wb.sheetnames:
                return self._send(404, {"err": "无该月"})
            for n2 in list(wb.sheetnames):
                if n2 != key:
                    del wb[n2]
            bio = io.BytesIO()
            wb.save(bio)
            nm = "宿舍点体温_%s.xlsx" % key
            return self._send(200, bio.getvalue(),
                              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                              {"Content-Disposition": 'attachment; filename="%s"' % nm.encode("utf-8").decode("latin-1", "ignore")})
        return self._send(404, {"err": "not found"})

    def do_POST(self):
        try:
            self._post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._send(500, {"err": "服务内部错误：%s" % str(e)[:200]})
            except Exception:
                pass

    def _post(self):
        if not self.authed():
            return self._send(401, "需要登录", "text/plain; charset=utf-8",
                              {"WWW-Authenticate": 'Basic realm="ssdm"'})
        u = urlparse(self.path)
        try:
            ln = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            ln = 0
        if ln < 0 or ln > (1 << 20):
            return self._send(413, {"err": "请求体过大"})
        # 只收 JSON：跨站表单发不出 application/json，顺带挡掉 Basic 认证下的 CSRF
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ln and ctype != "application/json":
            return self._send(415, {"err": "Content-Type 需为 application/json"})
        try:
            body = json.loads(self.rfile.read(ln)) if ln else {}
        except Exception:
            return self._send(400, {"err": "请求体不是合法 JSON"})   # 别静默当成空对象，否则会触发默认全量填表
        if not isinstance(body, dict):
            body = {}
        if u.path == "/api/publish":
            d = body.get("date") or ""
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                return self._send(400, {"err": "日期格式 YYYY-MM-DD"})
            ok = run_job("发布 %s 周" % d, ["publish", d])
            return self._send(200, {"ok": ok, "msg": "已开始" if ok else "有任务在跑"})
        if u.path == "/api/fill":
            tg = body.get("targets") or ["main", "weekend", "temp"]
            tg = [x for x in tg if x in ("main", "weekend", "temp")] or ["main"]
            dry = bool(body.get("dry"))
            argv = ["fill", "--targets=" + ",".join(tg)]
            if dry:
                argv.append("--dry")
            ok = run_job("填表（%s%s）" % ("/".join(tg), "·试运行" if dry else ""), argv)
            return self._send(200, {"ok": ok, "msg": "已开始" if ok else "有任务在跑"})
        if u.path == "/api/setnode":
            kind = body.get("kind"); node = (body.get("node") or "").strip()
            key = {"main": "node", "weekend": "weekend_node", "temp": "temp_node"}.get(kind)
            if not key or not node:
                return self._send(400, {"err": "参数不全"})
            try:
                P.jpost("%s/api/config?admin=%s" % (P.BASE, P.ADMIN), {key: node})
                return self._send(200, {"ok": True})
            except Exception as e:
                return self._send(500, {"err": str(e)[:120]})
        if u.path == "/api/timer":
            name = body.get("name"); act = body.get("action")
            if name not in ("rc-publish", "rc-fill", "rc-sweep", "rc-tempmonth", "rc-backup"):
                return self._send(400, {"err": "未知任务"})
            if act == "run":
                ok = run_job("立即执行 " + name, ["__shell__", name])
                return self._send(200, {"ok": ok, "msg": "已开始" if ok else "有任务在跑"})
            if act not in ("on", "off"):
                return self._send(400, {"err": "未知动作"})
            unit = name + ".timer"
            cmd = ["systemctl", "enable", "--now", unit] if act == "on" else ["systemctl", "disable", "--now", unit]
            r = subprocess.run(cmd, capture_output=True, text=True)
            return self._send(200, {"ok": r.returncode == 0, "msg": (r.stdout + r.stderr)[:120]})
        if u.path == "/api/temp":
            m = body.get("month") or ""
            if not re.match(r"^\d{4}-\d{2}$", m):
                return self._send(400, {"err": "月份格式 YYYY-MM"})
            ok = run_job("生成 %s 体温表" % m, ["tempmonth", m])
            return self._send(200, {"ok": ok, "msg": "已开始" if ok else "有任务在跑"})
        return self._send(404, {"err": "not found"})


PAGE = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>宿舍点名控制台</title>
<link rel="stylesheet" href="/static/xspreadsheet.css">
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#262b36;--txt:#e8eaf0;--sub:#8b93a7;--acc:#3b82f6;--ok:#22c55e;--warn:#f59e0b;--bad:#ef4444}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.65 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;color-scheme:dark}
.wrap{max-width:1060px;margin:0 auto;padding:32px 20px 72px}
h1{font-size:22px;margin:0 0 4px;font-weight:600}
.sub{color:var(--sub);font-size:13px;margin-bottom:24px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:760px){.grid{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px}
.card h2{font-size:15px;margin:0 0 12px;font-weight:600;color:#cfd6e4}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:6px 0}
input,button,select{font:inherit}
input[type=date],input[type=month],input[type=text],select{background:#0e1015;border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:7px 10px;max-width:100%}
button{background:var(--acc);border:0;color:#fff;border-radius:8px;padding:7px 13px;cursor:pointer}
button.ghost{background:transparent;border:1px solid var(--line);color:var(--txt)}
button.sm{padding:4px 10px;font-size:12px}
button.danger{background:#7f1d1d}
button:disabled{opacity:.45;cursor:not-allowed}
.hint{color:var(--sub);font-size:12px;margin-top:6px}
#log{background:#0b0d11;border:1px solid var(--line);border-radius:8px;padding:10px;font-size:12px;color:#b7c0d0;max-height:240px;overflow:auto;white-space:pre-wrap;margin:0}
table{width:100%;border-collapse:collapse;font-size:13px}
#marks td,#marks th{white-space:nowrap;padding:5px 6px}
#marks td:last-child{text-align:right}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}
th{color:var(--sub);font-weight:500}
a{color:#7fb2ff;text-decoration:none}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px}
.plan{border:1px dashed var(--line);border-radius:10px;padding:12px;margin-bottom:10px}
.chk{display:inline-flex;align-items:center;gap:6px;background:#0e1015;border:1px solid var(--line);border-radius:8px;padding:5px 10px;font-size:12px;color:var(--txt)}
.chk input{accent-color:var(--acc)}
.badge{font-size:11px;color:var(--sub);border:1px solid var(--line);border-radius:6px;padding:1px 6px;margin-left:6px}
.sw{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:7px 0;border-bottom:1px solid var(--line)}
.sw>div:first-child{min-width:0;flex:1 1 auto;overflow:hidden}
.sw>div:last-child{display:flex;align-items:center;gap:6px;flex:0 0 auto;flex-wrap:nowrap}
.sw select{max-width:320px}
.sw:last-child{border:0}
.mini{font-size:12px;color:var(--sub)}
.flow{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.step{flex:1 1 180px;border:1px solid var(--line);border-radius:10px;padding:10px 12px;background:#0e1015}
.step b{font-weight:600;font-size:13px;margin-left:2px}
.step .mini{margin-top:4px}
#toast{position:fixed;right:18px;bottom:18px;z-index:80;display:flex;flex-direction:column;gap:8px}
.tst{padding:10px 14px;border-radius:10px;border:1px solid var(--line);background:#171a21;color:var(--txt);font-size:13px;box-shadow:0 8px 24px rgba(0,0,0,.35);opacity:0;transform:translateY(8px);transition:.18s}
.tst.on{opacity:1;transform:none}
.tst.ok{border-color:#16553a}.tst.bad{border-color:#7f1d1d}
details.plist summary{cursor:pointer;color:var(--sub);font-size:12px}
.libtab.on{background:var(--acc);border-color:var(--acc);color:#fff}
.librow{display:flex;align-items:center;gap:10px;padding:7px 4px;border-bottom:1px solid var(--line)}
.librow .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.librow .tm{font-size:12px;color:var(--sub);white-space:nowrap}
.pv{display:none;position:fixed;top:0;right:0;bottom:0;left:0;background:rgba(0,0,0,.65);z-index:50;padding:10px}
.pv.on{display:block}
.pvbox{background:var(--card);border:1px solid var(--line);border-radius:12px;width:100%;height:100%;margin:0;padding:12px;display:flex;flex-direction:column}
.pvfill{flex:1 1 auto;min-height:0;position:relative;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:#0b0d11}
.pvfill>#pvRaw{position:absolute;top:0;right:0;bottom:0;left:0;width:100%;height:100%;margin:0;padding:12px;overflow:auto;font-size:12px;line-height:1.55;white-space:pre-wrap;word-break:break-all;color:#c7d0dd;background:#0b0d11}
.pvfill>#xsp{position:absolute;top:0;right:0;bottom:0;left:0;width:100%;height:100%}
.pvhead{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.pvbody{overflow:auto;border:1px solid var(--line);border-radius:8px}
.pvt{border-collapse:collapse;font-size:12px;width:100%}
.pvt td{border:1px solid var(--line);padding:3px 6px;white-space:nowrap}
.pvt tr:first-child td{background:#0b0d11;color:#cfd6e4}
#pvSwitch button.on{background:var(--acc);border-color:var(--acc);color:#fff}
@media(max-width:760px){
  .wrap{padding:20px 14px 56px}
  .sw{flex-wrap:wrap}
  .sw>div:last-child{flex:1 1 100%;flex-wrap:wrap;margin-top:6px}
  .sw select{max-width:100%;flex:1 1 160px;min-width:0}
}
@media(max-width:560px){
  .librow{flex-wrap:wrap}
  .librow .nm{flex:1 1 100%;white-space:normal;overflow:visible}
  .librow .tm{flex:1 1 100%}
}
</style></head><body><div class="wrap">
<h1>宿舍点名控制台</h1><a href="/logs" style="margin-left:12px;font-size:13px;color:#1d4e89">设备日志 →</a>
<div class="sub">B 座一层 · 请假表 / 周末未返校统计 / 宿舍点体温 —— 发布、填表、表指向、定时任务，皆在此处</div>

<div class="card" style="margin-bottom:16px">
  <h2>今日一线 <span class="badge" id="flowWeek">—</span></h2>
  <div class="flow">
    <div class="step"><span class="dot" id="dMark"></span><b>① 点名件</b><div class="mini" id="tMark">—</div></div>
    <div class="step"><span class="dot" id="dPlan"></span><b>② 待填计划</b><div class="mini" id="tPlan">—</div></div>
    <div class="step"><span class="dot" id="dFill"></span><b>③ 写入三表</b><div class="mini" id="tFill">—</div></div>
  </div>
  <div class="row" id="flowBtns"></div>
  <div class="mini" id="flowHint"></div>
</div>

<div class="card" style="margin-bottom:16px">
  <h2>待填计划 <span class="badge" id="planHint">读取中</span></h2>
  <div id="plans"><div class="mini">读取中…</div></div>
  <div style="margin-top:10px;border-top:1px solid var(--line);padding-top:8px"><span class="mini">最近记录</span><div id="hist"><div class="mini">…</div></div></div>
</div>

<div class="grid">
  <div class="card">
    <h2>发布周表</h2>
    <div class="row"><input type="date" id="d1"><span class="badge" id="d1range">—</span></div>
    <div class="row">
      <button class="ghost sm" data-wk="this">本周</button>
      <button class="ghost sm" data-wk="next">下周</button>
      <button class="ghost sm" data-wk="prev">上周</button>
      <button id="b1">生成并发布</button>
    </div>
    <div class="hint" id="pubHint">按所选日所在周（周日~周四）出两张共享表，链接发联系人；旧表撤权改名。</div>
  </div>
  <div class="card">
    <h2>生成体温表</h2>
    <div class="row"><input type="month" id="m1"><button id="b2">生成该月</button></div>
    <div class="hint">生成指定月份体温工作表，转共享表、发链接、撤上一张。</div>
  </div>
</div>

<div class="card" style="margin-top:16px">
  <h2>表指向（填表写往哪张表）</h2>
  <div id="nodes"></div>
  <div class="hint">上面三行是当前在役表；可从下拉里挑一张替换（慎用，换后填表即写往该表）。</div>
</div>

<div class="card" style="margin-top:16px">
  <h2>定时任务开关</h2>
  <div id="timers"><div class="mini">读取中…</div></div>
</div>

<div class="grid" style="margin-top:16px">
  <div class="card">
    <h2>表格库 · 预览与选择</h2>
    <div class="row" id="libTabs">
      <button class="ghost sm libtab" data-kind="main">请假汇总表</button>
      <button class="ghost sm libtab" data-kind="weekend">周末未返校统计</button>
      <button class="ghost sm libtab" data-kind="temp">当月体温表</button>
    </div>
    <div id="libList" style="margin-top:8px;max-height:300px;overflow:auto"><div class="mini">读取中…</div></div>
  </div>
  <div class="card">
    <h2>原始点名件</h2>
    <div style="max-height:260px;overflow:auto"><table id="marks"><thead><tr><th>时间</th><th>人数</th><th>类型</th><th>下载</th></tr></thead><tbody></tbody></table></div>
  </div>
</div>

<div id="toast"></div>
<div id="pv" class="pv"><div class="pvbox">
  <div class="pvhead"><b id="pvTitle">预览</b><span style="flex:1"></span><button class="ghost sm" onclick="closePreview()">关闭</button></div>
  <div id="pvMeta" class="mini" style="margin-bottom:8px"></div>
  <div class="row" id="pvSwitch" style="display:none;margin-bottom:8px">
    <button class="ghost sm" onclick="markView('raw')">原始文件</button>
    <button class="ghost sm" onclick="markView('table')">名单视图</button>
  </div>
  <div class="pvfill">
    <pre id="pvRaw"></pre>
    <div id="xsp"></div>
  </div>
</div></div>

<div class="card" style="margin-top:16px">
  <h2>任务输出</h2>
  <pre id="log">（无）</pre>
</div>

<script src="/static/xlsx.full.min.js"></script>
<script src="/static/xspreadsheet.js"></script>
<script>
const $=s=>document.querySelector(s);
const api=async(p,m,b)=>{
  const r=await fetch(p,{method:m||'GET',headers:{'Content-Type':'application/json'},body:b?JSON.stringify(b):undefined});
  const txt=await r.text();
  let d=null;
  try{ d=txt?JSON.parse(txt):{}; }catch(e){ d=null; }
  if(d===null||typeof d!=='object'){
    throw new Error(r.status===401?'未登录或登录已过期（HTTP '+r.status+'）':'返回异常（HTTP '+r.status+'）');
  }
  if(!r.ok){
    if(!('ok' in d)) d.ok=false;
    if(!('err' in d)) d.err='HTTP '+r.status;
  }
  return d;
};
const esc=x=>String(x==null?'':x).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const KINDS=[['主表','main'],['周末表','weekend'],['体温表','temp']];
const NAMES={'rc-publish':'周日发布','rc-fill':'每晚填表 21:40','rc-sweep':'深夜补扫 23:50','rc-tempmonth':'每月体温 1号','rc-backup':'周五备份'};
let sigNodes='', sigOpts={}, sigPlans='', sigTimers='';
let nodesBuilt=false, marksList=[], statusBusy=false, NODES={};

function buildNodes(){
  const box=$('#nodes'); box.innerHTML='';
  for(const [k,kind] of KINDS){
    const row=document.createElement('div'); row.className='sw';
    row.innerHTML=`<div><span class="dot" id="dot_${kind}" style="background:var(--warn)"></span><b>${k}</b>：<a id="lnk_${kind}" target="_blank">未配置</a><span class="mini" id="tm_${kind}"></span></div>
      <div><select id="sel_${kind}"><option value="">选择要切换的表…</option></select>
      <button class="ghost sm" data-kind="${kind}">切换</button></div>`;
    row.querySelector('button[data-kind]').onclick=()=>switchNode(kind);
    box.appendChild(row);
  }
  nodesBuilt=true;
}

async function refreshStatus(){
  if(statusBusy) return;                          // 慢请求别叠着跑（12s 一次，卡顿会越积越多）
  statusBusy=true;
  try{
    const d=await api('/api/status');
    if(!nodesBuilt) buildNodes();
    const ns=d.nodes||{}; NODES=ns; updatePubPreview();
    KINDS.forEach(([k,kind])=>{
      const v=ns[k];
      const dot=$('#dot_'+kind), lnk=$('#lnk_'+kind), tm=$('#tm_'+kind);
      if(v){ dot.style.background='var(--ok)';
             const prev=lnk.textContent;
             lnk.textContent=v.name || ((prev&&prev!=='未配置')?prev:'（无名）'); lnk.href=v.url;
             const tt='　建 '+(v.create||'-')+' · 改 '+(v.modify||'-');
             if(v.create||v.modify) tm.textContent=tt; }
      else { dot.style.background='var(--warn)'; lnk.textContent='未配置'; lnk.removeAttribute('href'); tm.textContent=''; }
      loadOptions(kind);
    });
    // 原始件
    const tb=$('#marks tbody'); tb.innerHTML='';
    marksList=(d.marks||[]);
    marksList.forEach((x,i)=>{
      const tr=document.createElement('tr');
      tr.innerHTML=`<td>${esc(x.date)}</td><td>${esc(x.n)}</td><td>${x.weekend?'返校':'普通'}</td>
        <td><a href="#" class="mkpv" data-i="${i}">预览</a>　<a href="/download/mark?f=${encodeURIComponent(x.file||'')}">下载</a></td>`;
      tb.appendChild(tr);
    });
    tb.querySelectorAll('a.mkpv').forEach(a=>{
      a.onclick=ev=>{ev.preventDefault();openMarkPreview(+a.dataset.i)};
    });
    const j=d.job||{};
    $('#log').textContent=(j.log&&j.log.length)?j.log.join('\n'):(j.running?'运行中…':'（无）');
  }catch(e){
    $('#planHint').textContent='连接失败';
    if(!$('#plans').querySelector('.plan')) $('#plans').innerHTML='<div class="mini">状态读取失败，12 秒后自动重试。</div>';
  }finally{
    statusBusy=false;
  }
  await refreshPlans(); await refreshTimers(); await refreshOverview(); await refreshHistory(); updatePubPreview();
}

async function loadOptions(kind){
  try{
    const d=await api('/api/tables?kind='+kind);
    const key=JSON.stringify((d.items||[]).map(x=>[x.node,x.name,x.modify]));
    if(sigOpts[kind]===key) return;              // 没变化就不重建，避免抽搐
    sigOpts[kind]=key;
    const sel=$('#sel_'+kind); if(!sel)return;
    const cur=sel.value;
    sel.innerHTML='<option value="">选择要切换的表…</option>';
    (d.items||[]).forEach(it=>{
      const o=document.createElement('option'); o.value=it.node;
      o.textContent=it.name+' ｜ 改 '+(it.modify||'-')+(it.create?' ｜ 建 '+it.create:'');
      sel.appendChild(o);
    });
    if(cur) sel.value=cur;
  }catch(e){}
}

async function switchNode(kind){
  const sel=$('#sel_'+kind); const node=sel.value;
  if(!node) return alert('先选一张表');
  const nm=sel.options[sel.selectedIndex].text;
  if(!confirm('把填表目标切到「'+nm+'」？切换后当晚数据将写往该表。'))return;
  try{
    const r=await api('/api/setnode','POST',{kind,node});
    if(r.ok){alert('已切换');sigOpts[kind]='';refreshStatus()}else{alert('失败：'+(r.err||''))}
  }catch(e){ alert('失败：'+e.message); }
}

async function refreshPlans(){
  try{
    const d=await api('/api/pending'); const ps=d.plans||[];
    $('#planHint').textContent=ps.length?`${ps.length} 份待填`:'空';
    const sig=JSON.stringify(ps.map(p=>[p.date,p.values,p.weekendN]));
    if(sig===sigPlans) return;                    // 计划没变不重建，免得勾选被刷掉
    sigPlans=sig;
    if(!ps.length){$('#plans').innerHTML='<div class="mini">信箱为空。点名上传后这里会出现待填计划，可挑目标表手动填。</div>';return}
    $('#plans').innerHTML=ps.map((p,i)=>{
      const v=p.values||[];
      const names=(v[6]||'')+(v[7]&&v[7]!=='/'?' '+v[7]:'');
      return `<div class="plan">
        <div><b>${esc(p.date)}</b> <span class="badge">${esc(p.range)}</span> ${p.weekend?'<span class="badge">含返校</span>':''}</div>
        <div class="mini">应到 ${esc(v[0])} · 实到 ${esc(v[2])} · 请假 ${esc(v[4])} · 周末名单 ${esc(p.weekendN)} 行</div>
        <div class="mini">将写：主表第 ${esc(p.row)} 行 · 体温 ${esc(String(p.date).slice(5,7))} 月 ${esc(String(p.date).slice(8,10))} 日 ${esc(v[4])} 格</div>
        <details class="plist"><summary>看名单（${esc(v[4])} 人）</summary><div class="mini">${esc(names)}</div></details>
        <div class="row">
          <label class="chk"><input type="checkbox" id="t_${i}_main" checked>主表</label>
          <label class="chk"><input type="checkbox" id="t_${i}_weekend" ${p.weekend?'checked disabled':''}>周末表</label>
          <label class="chk"><input type="checkbox" id="t_${i}_temp" checked>体温表</label>
          <button class="ghost sm" data-i="${i}" data-dry="1">试运行</button>
          <button class="sm" data-i="${i}" data-dry="0">立即填写</button>
        </div></div>`;
    }).join('');
    $('#plans').querySelectorAll('button[data-dry]').forEach(b=>{
      b.onclick=()=>doFill(+b.dataset.i, b.dataset.dry==='1');
    });
  }catch(e){
    $('#planHint').textContent='读取失败';
    if(!$('#plans').querySelector('.plan')) $('#plans').innerHTML='<div class="mini">读取失败，稍后自动重试。</div>';
  }
}

async function doFill(i,dry){
  const tg=[];
  const bm=$('#t_'+i+'_main'), bw=$('#t_'+i+'_weekend'), bt=$('#t_'+i+'_temp');
  if(!bm) return;
  if(bm.checked) tg.push('main');
  if(bw&&bw.checked) tg.push('weekend');
  if(bt&&bt.checked) tg.push('temp');
  if(!tg.length) return alert('至少选一张表');
  if(!dry && !confirm('按所选表写入？'+(tg.includes('main')?'（含销账，写后计划消失）':'（不含主表，不销账）')))return;
  try{
    const r=await api('/api/fill','POST',{targets:tg,dry});
    if(!r.ok) alert('未启动：'+(r.msg||r.err||''));
  }catch(e){ alert('失败：'+e.message); }
  setTimeout(refreshStatus,600);
}

async function refreshTimers(){
  try{
    const d=await api('/api/timers');
    const sig=JSON.stringify(d);
    if(sig===sigTimers) return;                  // 状态没变不重建
    sigTimers=sig;
    $('#timers').innerHTML=Object.keys(NAMES).map(k=>{
      const t=d[k]||{}; const on=(t.enabled==='enabled');
      return `<div class="sw"><div><span class="dot" style="background:${on?'var(--ok)':'var(--bad)'}"></span>${NAMES[k]} <span class="mini">${k}.timer · ${esc(t.enabled||'?')}</span></div>
      <div class="row" style="margin:0">
        <button class="ghost sm" data-run="${k}">立即执行</button>
        <button class="${on?'ghost':'sm'}" data-tg="${k}" data-act="${on?'off':'on'}">${on?'停用':'启用'}</button>
      </div></div>`;
    }).join('');
    $('#timers').querySelectorAll('button[data-run]').forEach(b=>{ b.onclick=()=>runNow(b.dataset.run); });
    $('#timers').querySelectorAll('button[data-tg]').forEach(b=>{ b.onclick=()=>toggleTimer(b.dataset.tg,b.dataset.act); });
  }catch(e){
    if(!$('#timers').querySelector('.sw')) $('#timers').innerHTML='<div class="mini">读取失败，稍后自动重试。</div>';
  }
}

async function toggleTimer(name,action){
  if(!confirm((action==='on'?'启用':'停用')+' '+name+'？'))return;
  try{
    const r=await api('/api/timer','POST',{name,action});
    if(!r.ok) alert('失败：'+(r.msg||r.err||''));
  }catch(e){ alert('失败：'+e.message); }
  sigTimers=''; refreshTimers();
}

async function runNow(name){
  if(!confirm('立即执行一次「'+(NAMES[name]||name)+'」？'))return;
  try{
    const r=await api('/api/timer','POST',{name,action:'run'});
    if(!r.ok) alert('未启动：'+(r.msg||r.err||''));
  }catch(e){ alert('失败：'+e.message); }
  setTimeout(()=>{sigPlans='';refreshStatus()},800);
}

$('#b1').onclick=async()=>{const d=$('#d1').value;if(!d)return alert('请选日期');const w=weekOf(d);if(!confirm('将发布 '+w.label+' 两张共享表，撤下旧表并发链接。继续？'))return;const btn=$('#b1');btn.disabled=true;btn.textContent='发布中…';try{const r=await api('/api/publish','POST',{date:d});if(!r.ok)alert('未启动：'+(r.msg||r.err||''))}catch(e){alert('失败：'+e.message)}finally{$('#b1').disabled=false;btn.textContent='生成并发布'}toast('发布任务已启动，完成后看日志',true);setTimeout(()=>{sigOpts={};refreshStatus()},800)};
$('#b2').onclick=async()=>{const m=$('#m1').value;if(!m)return alert('请选月份');if(!confirm('将生成 '+m+' 体温表并发布共享链接，撤下上一张。继续？'))return;$('#b2').disabled=true;try{const r=await api('/api/temp','POST',{month:m});if(!r.ok)alert('未启动：'+(r.msg||r.err||''))}catch(e){alert('失败：'+e.message)}finally{$('#b2').disabled=false}setTimeout(()=>{sigOpts={};refreshStatus()},800)};
let libKind='main', pvNode='', pvKind='', libItems=[];
async function loadLibrary(kind){
  libKind=kind;
  document.querySelectorAll('.libtab').forEach(b=>b.classList.toggle('on', b.dataset.kind===kind));
  const box=$('#libList'); box.innerHTML='<div class="mini">读取中…</div>';
  try{
    const d=await api('/api/library?kind='+kind);
    const items=d.items||[];
    libItems=items;
    if(!items.length){box.innerHTML='<div class="mini">这一类还没有表。</div>';return}
    box.innerHTML=items.map((it,i)=>`<div class="librow">
        <span class="nm" title="${esc(it.name)}">${esc(it.name)}</span>
        <span class="tm">建 ${esc(it.create||'-')} · 改 ${esc(it.modify||'-')}</span>
        <button class="ghost sm" data-i="${i}" data-act="pv">预览</button>
        <a class="ghost sm" style="padding:4px 10px;border:1px solid var(--line);border-radius:8px" href="/download/table?kind=${encodeURIComponent(kind)}&node=${encodeURIComponent(it.node||'')}">下载</a>
        <button class="ghost sm" data-i="${i}" data-act="cur">设为在役</button>
      </div>`).join('');
    box.querySelectorAll('button[data-act]').forEach(b=>{
      b.onclick=()=>{
        const it=libItems[+b.dataset.i]; if(!it) return;
        if(b.dataset.act==='pv') openPreview(it.node, it.name, libKind);
        else makeCurrent(libKind, it.node, it.name);
      };
    });
  }catch(e){box.innerHTML='<div class="mini">读取失败：'+esc(e.message||e)+'</div>'}
}
function renderSheet(sheets){
  const host=$('#xsp');
  host.innerHTML='';
  const xs=x_spreadsheet('#xsp',{mode:'read',showToolbar:false,showContextmenu:false,showBottomBar:true,
    view:{height:()=>Math.max(320,host.clientHeight-1),width:()=>Math.max(360,host.clientWidth-1)}});
  xs.loadData(sheets);
  return xs;
}
function aoaToSheet(name,aoa){
  const rows={};
  aoa.forEach((row,ri)=>{
    const cells={};
    (row||[]).forEach((v,ci)=>{ if(v!==''&&v!==null&&v!==undefined) cells[ci]={text:String(v)}; });
    if(Object.keys(cells).length) rows[ri]={cells};
  });
  return {name:name,rows:rows};
}
const bufCache=new Map();                        // key: kind+node → {t, buf}，2 分钟内复用，避免重复下载
const BUF_TTL=120000;
async function sheetBuf(kind,node){
  const key=kind+':'+node;
  const hit=bufCache.get(key);
  if(hit && Date.now()-hit.t<BUF_TTL) return hit.buf;
  const resp=await fetch('/download/table?kind='+encodeURIComponent(kind)+'&node='+encodeURIComponent(node));
  if(!resp.ok) throw new Error('HTTP '+resp.status);
  const buf=await resp.arrayBuffer();
  bufCache.set(key,{t:Date.now(),buf});
  if(bufCache.size>12) bufCache.delete(bufCache.keys().next().value);
  return buf;
}
async function openPreview(node,name,kind){
  pvNode=node; pvKind=kind;
  $('#pvTitle').textContent=name||'预览';
  $('#pvMeta').textContent='读取数据…';
  $('#pvSwitch').style.display='none';
  $('#pvRaw').style.display='none';
  $('#xsp').style.display='block';
  $('#pv').classList.add('on');
  try{
    const buf=await sheetBuf(kind,node);
    const wb=XLSX.read(buf,{type:'array'});
    const sheets=wb.SheetNames.map(n=>{
      const ws=wb.Sheets[n];
      const aoa=XLSX.utils.sheet_to_json(ws,{header:1,raw:false,defval:''});
      const sh=aoaToSheet(n,aoa);
      if(ws['!merges']&&ws['!merges'].length){ sh.merges=ws['!merges'].map(m=>XLSX.utils.encode_range(m)); }
      return sh;
    });
    $('#pvMeta').textContent=wb.SheetNames.length+' 个工作表 · 可上下左右拖动查看';
    renderSheet(sheets);
  }catch(e){ $('#pvMeta').textContent='读取失败：'+(e.message||e); }
}
let markFile=null;
async function openMarkPreview(i){
  const x=marksList[i]||{};
  const f=x.file||'', date=x.date||'';
  markFile=f;
  $('#pvTitle').textContent='原始点名件 · '+date;
  $('#pvMeta').textContent='读取中…';
  $('#pvSwitch').style.display='flex';
  $('#pv').classList.add('on');
  $('#xsp').style.display='none';
  $('#xsp').removeAttribute('data-filled');
  const pre=$('#pvRaw'); pre.style.display='block'; pre.textContent='读取中…';
  try{
    const r=await fetch('/api/raw?f='+encodeURIComponent(f));
    if(!r.ok) throw new Error('HTTP '+r.status);
    const txt=await r.text();
    pre.textContent=txt;
    $('#pvMeta').textContent='原始件 '+f+' · '+txt.split('\n').length+' 行（上传时原样保存，未经任何加工）';
    document.querySelectorAll('#pvSwitch button').forEach(b=>b.classList.toggle('on', b.textContent==='原始文件'));
  }catch(e){ pre.textContent='读取失败：'+(e.message||e); }
}
async function markView(mode){
  const pre=$('#pvRaw'), sp=$('#xsp');
  document.querySelectorAll('#pvSwitch button').forEach(b=>b.classList.toggle('on', b.textContent===(mode==='raw'?'原始文件':'名单视图')));
  if(mode==='raw'){ pre.style.display='block'; sp.style.display='none'; return; }
  pre.style.display='none'; sp.style.display='block';
  if(sp.dataset.filled==='1') return;
  try{
    const d=await api('/api/mark?f='+encodeURIComponent(markFile));
    const map={DS:'宿管办事假',DB:'宿管办病假',XS:'部系事假',XB:'部系病假'};
    const aoa=[['姓名','宿舍','类型']].concat((d.n||[]).map(x=>[x[0],String(x[1]),map[x[2]]||x[2]]));
    $('#pvMeta').textContent='时间 '+(d.date||'-')+' · 人数 '+(d.n||[]).length+(d.weekend?' · 已勾返校统计':'');
    renderSheet([aoaToSheet('点名件',aoa)]);
    sp.dataset.filled='1';
  }catch(e){ $('#pvMeta').textContent='读取失败：'+e; }
}
function closePreview(){ $('#pv').classList.remove('on'); $('#xsp').innerHTML=''; }
async function makeCurrent(kind,node,name){
  if(!confirm('把「'+name+'」设为在役表？'))return;
  try{
    const r=await api('/api/setnode','POST',{kind,node});
    if(r.ok){alert('已切换');sigOpts[kind]='';refreshStatus()}else alert('失败：'+(r.err||''));
  }catch(e){ alert('失败：'+e.message); }
}
document.querySelectorAll('.libtab').forEach(b=>b.onclick=()=>loadLibrary(b.dataset.kind));
const pad2=n=>String(n).padStart(2,'0');
const today=new Date();
function toast(msg, ok){
  const box=$('#toast'); const el=document.createElement('div');
  el.className='tst'+(ok?' ok':' bad'); el.textContent=msg; box.appendChild(el);
  requestAnimationFrame(()=>el.classList.add('on'));
  setTimeout(()=>{el.classList.remove('on'); setTimeout(()=>el.remove(),250)}, 3200);
}
function weekOf(dstr){
  const d=new Date(dstr+'T12:00:00');
  const off=d.getDay();                 // 周日=0，回退到本周日
  const start=new Date(d); start.setDate(d.getDate()-off);
  const end=new Date(start); end.setDate(start.getDate()+4);
  const f=x=>String(x.getMonth()+1).padStart(2,'0')+'.'+String(x.getDate()).padStart(2,'0');
  return {start:start,end:end,label:f(start)+'~'+f(end),iso:x=>x.toISOString().slice(0,10)};
}
function updatePubPreview(){
  const v=$('#d1').value; if(!v) return;
  const w=weekOf(v);
  $('#d1range').textContent=w.label+'（周日–周四）';
  const old=NODES['主表']&&NODES['主表'].name?NODES['主表'].name:'';
  const same=old && old.indexOf(w.label.replace('~','-'))>=0;
  let hint='将生成：'+w.label+'住宿生请假汇总表 + 周末未返校统计';
  if(same) hint='⚠️ '+w.label+' 已在役（'+old+'），再发布将撤权旧表并重建，请确认不是误点。';
  else if(old) hint+='；将撤下：'+old;

  $('#pubHint').textContent=hint;
}
async function refreshOverview(){
  try{
    const d=await api('/api/overview');
    $('#flowWeek').textContent='本周 '+d.week.label;
    const mk=d.marked, pl=d.plans, filled=d.filledToday;
    $('#dMark').style.background = mk?'var(--ok)':'var(--warn)';
    $('#tMark').textContent = mk? (mk.date+' · '+mk.n+' 人'+(mk.weekend?' · 含返校':'')) : '今日尚未上传点名件';
    $('#dPlan').style.background = pl?'var(--ok)':(filled?'var(--line)':'var(--warn)');
    $('#tPlan').textContent = pl? pl+' 份待填（可挑目标表）' : (filled?'信箱为空（今日已处理）':'信箱为空，等上传');
    $('#dFill').style.background = filled?'var(--ok)':(pl?'var(--warn)':'var(--line)');
    $('#tFill').textContent = filled? ('今日已写入 · 最近 '+(d.last.ts||'')) : (d.last.ts? '最近写入 '+d.last.ts : '尚未写入');
    const btns=[];
    if(pl){
      btns.push(`<button class="ghost sm" onclick="doFill(0,true)">试运行</button>`);
      btns.push(`<button class="sm" onclick="doFill(0,false)">写入三表并销账</button>`);
    }else if(!mk){
      btns.push(`<span class="mini">等方晔在 iPad 上传点名件</span>`);
    }else if(!filled){
      btns.push(`<span class="mini">今日件已上传但未写表，可到下方计划卡手动填</span>`);
    }
    $('#flowBtns').innerHTML=btns.join(' ');
    $('#flowHint').textContent = pl? '写入＝主表当日行 + 周末表（若有）+ 体温表当日列，成功后计划销账。' : '';
  }catch(e){ $('#flowHint').textContent='状态读取失败：'+e.message }
}
async function refreshHistory(){
  try{
    const d=await api('/api/history'); const box=$('#hist');
    if(!box) return;
    box.innerHTML=(d.lines||[]).slice(0,5).map(x=>`<div class="mini">· ${esc(x)}</div>`).join('')||'<div class="mini">暂无记录</div>';
  }catch(e){}
}
$('#d1').addEventListener('change',updatePubPreview);
document.querySelectorAll('[data-wk]').forEach(b=>b.onclick=()=>{
  const base=new Date(); base.setHours(12,0,0,0);
  const off=base.getDay(); const sun=new Date(base); sun.setDate(base.getDate()-off);
  if(b.dataset.wk==='next') sun.setDate(sun.getDate()+7);
  if(b.dataset.wk==='prev') sun.setDate(sun.getDate()-7);
  const pad=n=>String(n).padStart(2,'0');
  $('#d1').value=sun.getFullYear()+'-'+pad(sun.getMonth()+1)+'-'+pad(sun.getDate());
  updatePubPreview();
  document.querySelectorAll('[data-wk]').forEach(x=>x.classList.toggle('on',x===b));
});
$('#d1').value=today.getFullYear()+'-'+pad2(today.getMonth()+1)+'-'+pad2(today.getDate());  // 本地日期，别用 toISOString（UTC 会差一天）
$('#m1').value=today.getFullYear()+'-'+pad2(today.getMonth()+1);
if($('#m1').type!=='month'){ $('#m1').type='text'; $('#m1').placeholder='YYYY-MM'; $('#m1').style.width='120px'; }
updatePubPreview();
loadLibrary('main');refreshStatus();setInterval(()=>{if(!document.hidden)refreshStatus()},12000);
</script></body></html>"""


if __name__ == "__main__":
    if not os.path.exists(AUTH):
        open(AUTH, "w").write("ssdm:ssdm2026!")
        os.chmod(AUTH, 0o600)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
