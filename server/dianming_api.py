#!/usr/bin/env python3
"""B座点名接收端 v7 — 紧凑存储 + gzip + 分类目录
设计：只存"不可推导的事实"，班级/系部由名册推导，不落盘。
分类：点名回执 → roll/；填表标记件 → marked/；旧平档留在根，读取时三处都找。
v2 回执: {"v":2,"type":"rollcall","d","t","p","a","n":[["姓名 标注","房间"],...]}
v4 标记: {"v":4,"type":"marked","ref":回执名,"d","weekend","n":[["姓名","房间","DS|DB|XS|XB"],...]}
接口: POST /upload | GET /list /latest /index /get?name= | DELETE /delete?name= | GET / → GUI
"""
import json, os, re, sys, gzip, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

DATA_DIR = os.environ.get("DIANMING_DIR", "/var/lib/dianming")
WEB_DIR = os.environ.get("DIANMING_WEB", "/opt/dianming-web")   # 静态前端目录
TOKEN = os.environ.get("DIANMING_TOKEN", "")
ROLL, MARKED = "roll", "marked"
SUBDIRS = (ROLL, MARKED)
# 接受：裸文件名 / "roll/xxx" / "marked/xxx"，均可带 .gz
NAME_RE = re.compile(r"^(?:(?:roll|marked)/)?\d{8}-\d{6}[a-z0-9\-]*\.json(?:\.gz)?$")
MIME = {".html":"text/html; charset=utf-8", ".css":"text/css; charset=utf-8",
        ".js":"application/javascript; charset=utf-8", ".json":"application/json; charset=utf-8",
        ".svg":"image/svg+xml", ".png":"image/png", ".ico":"image/x-icon"}


def resolve(name):
    """把请求里的 name 解析成磁盘绝对路径；找不到返回 None。防目录穿越。"""
    if not name or ".." in name or not NAME_RE.match(name):
        return None
    base = os.path.basename(name)          # 只取末段，忽略传入的目录前缀后再自行定位
    for cand in (os.path.join(DATA_DIR, name),                 # 带前缀原样
                 os.path.join(DATA_DIR, ROLL, base),
                 os.path.join(DATA_DIR, MARKED, base),
                 os.path.join(DATA_DIR, base)):                # 旧平档
        for p in (cand, cand + ".gz"):
            if os.path.isfile(p):
                return p
    return None


def iter_records():
    """遍历三类目录，产出 (相对标签路径, 绝对路径)。"""
    out = []
    for sub in SUBDIRS:
        d = os.path.join(DATA_DIR, sub)
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if NAME_RE.match(f) or NAME_RE.match(sub + "/" + f):
                    out.append((f"{sub}/{f}", os.path.join(d, f)))
    if os.path.isdir(DATA_DIR):            # 兼容旧平档（根目录）
        for f in sorted(os.listdir(DATA_DIR)):
            if NAME_RE.match(f) and os.path.isfile(os.path.join(DATA_DIR, f)):
                out.append((f, os.path.join(DATA_DIR, f)))
    return sorted(out, key=lambda x: (os.path.getmtime(x[1]), x[0]))  # 按落盘时间排序，末项最新


def summary_type(d):
    return d.get("type") or ("rollcall" if d.get("v") == 2 or "absentStudents" in d else "?")


def to_v2(p):
    if p.get("type") == "marked":
        return {"v": 4, "type": "marked", "ref": p.get("ref"), "d": p.get("d"),
                "weekend": bool(p.get("weekend")), "n": p.get("n", [])}
    if p.get("type") != "rollcall":
        return {"v": 2, "type": p.get("type"),
                "d": p.get("d") or p.get("date") or time.strftime("%Y-%m-%d %H:%M:%S"),
                "msg": p.get("msg")}
    if p.get("v") == 2:
        return {"v": 2, "type": "rollcall", "d": p.get("d"), "t": p.get("t"),
                "p": p.get("p"), "a": p.get("a"), "n": p.get("n", [])}
    return {"v": 2, "type": "rollcall", "d": p.get("date"), "t": p.get("total"),
            "p": p.get("present"), "a": p.get("absent"),
            "n": [[s.get("name", ""), str(s.get("room", ""))] for s in p.get("absentStudents", [])]}


def read_json(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        return json.load(f)


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/upload":
            return self._send(404, {"ok": False, "error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n <= 0 or n > 200_000:
                return self._send(400, {"ok": False, "error": "bad size"})
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception as e:
            return self._send(400, {"ok": False, "error": f"bad json: {e}"})
        if not TOKEN or payload.get("token") != TOKEN:
            return self._send(401, {"ok": False, "error": "token"})
        if payload.get("type") not in ("rollcall", "marked", "test"):
            return self._send(400, {"ok": False, "error": "type"})
        rec = to_v2(payload)
        sub = MARKED if rec.get("type") == "marked" else ROLL
        subdir = os.path.join(DATA_DIR, sub)
        os.makedirs(subdir, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        name = ts + ".json.gz"
        path = os.path.join(subdir, name)
        i = 0
        while os.path.exists(path):
            i += 1
            name = f"{ts}-{i}.json.gz"
            path = os.path.join(subdir, name)
        tmp = path + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
        return self._send(200, {"ok": True, "saved": f"{sub}/{name}", "type": rec.get("type")})

    def do_DELETE(self):
        # 隐藏式删除：挪进 .trash（列表/读取即刻消失，可人工找回，不物理销毁）
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path != "/delete":
            return self._send(404, {"ok": False, "error": "not found"})
        if not TOKEN or q.get("token", [""])[0] != TOKEN:
            return self._send(401, {"ok": False, "error": "token"})
        name = q.get("name", [""])[0]
        p = resolve(name) if NAME_RE.match(name) else None
        if not p:
            return self._send(404, {"ok": False, "error": "missing"})
        trash = os.path.join(DATA_DIR, ".trash")
        os.makedirs(trash, exist_ok=True)
        rel = os.path.relpath(p, DATA_DIR)
        dst = os.path.join(trash, time.strftime("%Y%m%d%H%M%S") + "-" + rel.replace(os.sep, "_"))
        os.replace(p, dst)
        return self._send(200, {"ok": True, "hidden": rel, "to": os.path.basename(dst)})

    def serve_static(self, urlpath):
        rel = urlpath.lstrip("/") or "index.html"
        if rel.endswith("/"):
            rel += "index.html"
        base = os.path.realpath(WEB_DIR)
        full = os.path.realpath(os.path.join(base, rel))
        if not full.startswith(base + os.sep) and full != base:   # 目录穿越防护
            return self._send(403, {"ok": False, "error": "forbidden"})
        if not os.path.isfile(full):
            return self._send(404, {"ok": False, "error": "not found"})
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        # HTML 每次取最新；js/css 允许浏览器缓存 60s（改版最多等一分钟或强刷）
        self.send_header("Cache-Control", "no-store" if ext == ".html" else "public, max-age=60")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        # 静态前端：/ 与 /xxx.html|css|js（无 token，公开；数据接口才要 token）
        if u.path in ("/", "/index.html") or (
            "." in u.path.rsplit("/", 1)[-1] and u.path.rsplit(".", 1)[-1].lower() in
            ("html", "css", "js", "ico", "svg", "png") and not u.path.startswith("/dianming")):
            return self.serve_static(u.path)
        if not TOKEN or q.get("token", [""])[0] != TOKEN:
            return self._send(401, {"ok": False, "error": "token"})
        records = iter_records()   # [(相对路径, 绝对路径)]，roll/ marked/ 与旧平档
        if u.path == "/list":
            return self._send(200, {"ok": True, "files": [rel for rel, _ in records]})
        if u.path == "/index":  # 一次返回全部摘要，供 GUI 下拉框
            items = []
            for rel, p in records:
                try:
                    d = read_json(p)
                    typ = summary_type(d)
                    if typ == "marked":
                        items.append({"f": rel, "type": "marked", "d": d.get("d"),
                                      "ref": d.get("ref"), "weekend": d.get("weekend", False),
                                      "t": None, "p": None, "a": len(d.get("n", []))})
                        continue
                    d = to_v2(d) if d.get("v") != 2 else d
                    items.append({"f": rel, "type": "rollcall", "d": d.get("d"), "t": d.get("t"),
                                  "p": d.get("p"), "a": d.get("a")})
                except Exception:
                    items.append({"f": rel, "d": "?", "t": None, "p": None, "a": None})
            return self._send(200, {"ok": True, "items": items})
        if u.path == "/latest":
            marked = [(rel, p) for rel, p in records if rel.startswith(MARKED + "/")]
            pool = marked or records
            if not pool:
                return self._send(404, {"ok": False, "error": "empty"})
            return self._send(200, read_json(pool[-1][1]))
        if u.path == "/get":
            name = q.get("name", [""])[0]
            p = resolve(name)
            if not p:
                return self._send(404, {"ok": False, "error": "missing"})
            return self._send(200, read_json(p))
        return self._send(404, {"ok": False, "error": "not found"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if not TOKEN:
        sys.exit("DIANMING_TOKEN env required")
    ThreadingHTTPServer((os.environ.get("DIANMING_BIND", "127.0.0.1"), 8099), H).serve_forever()
