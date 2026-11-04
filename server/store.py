"""记录存储层：SQLite（WAL）。

以前是「一条上传一个 json.gz 文件、一层一个 pending 文件」，文件数会随天数线性膨胀，
删除/回溯还要靠文件名猜。改成数据库后：

  uploads  一条上传 = 一行（原始 JSON + gzip blob 都在库里）
  plans    一层一天一封「待填计划」，唯一键 (date, building, floor)，重复上传自动覆盖
  accounts 账号（可选，仍兼容老 accounts.json）
  kv       配置/状态
  events   日志

对外 API 形状保持不变，只是把落盘换成 SQL。
"""
import gzip
import hashlib
import json
import os
import sqlite3
import threading
import time

DB_NAME = "rollcall.db"
_lock = threading.RLock()
_conn = None
_data_dir = None


def _path(data_dir):
    return os.path.join(data_dir, DB_NAME)


def init(data_dir):
    """打开（必要时创建）数据库；返回连接。"""
    global _conn, _data_dir
    if _conn is not None and _data_dir == data_dir:
        return _conn
    os.makedirs(data_dir, exist_ok=True)
    conn = sqlite3.connect(_path(data_dir), check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS uploads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,            -- 20261104-213000
        date TEXT NOT NULL,          -- 2026-11-04
        building TEXT NOT NULL,
        floor INTEGER NOT NULL,
        kind TEXT NOT NULL,          -- marked / roll
        n INTEGER DEFAULT 0,         -- 人数
        ref TEXT,                    -- 回执名
        payload TEXT NOT NULL,       -- 原始 JSON
        gz BLOB,                     -- gzip 后的原始件
        sha256 TEXT,
        mtime REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_uploads_date ON uploads(date, building, floor);
    CREATE INDEX IF NOT EXISTS idx_uploads_ts ON uploads(ts);

    CREATE TABLE IF NOT EXISTS plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        building TEXT NOT NULL,
        floor INTEGER NOT NULL,
        payload TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',   -- pending / done
        created REAL NOT NULL,
        acked REAL,
        UNIQUE(date, building, floor)
    );

    CREATE TABLE IF NOT EXISTS accounts (
        id TEXT PRIMARY KEY,
        label TEXT,
        salt TEXT,
        pass TEXT,
        building TEXT,
        floor INTEGER,
        enabled INTEGER DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);

    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        level TEXT,
        msg TEXT
    );
    """)
    conn.commit()
    _conn, _data_dir = conn, data_dir
    return conn


def conn():
    if _conn is None:
        raise RuntimeError("store 未初始化")
    return _conn


def _exec(sql, args=()):
    with _lock:
        cur = conn().execute(sql, args)
        conn().commit()
        return cur


# ---------------- 上传件 ----------------

def add_upload(ts, date, building, floor, kind, n, ref, payload, mtime=None):
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    gz = gzip.compress(raw, 6)
    sha = hashlib.sha256(raw).hexdigest()
    cur = _exec(
        "INSERT INTO uploads(ts,date,building,floor,kind,n,ref,payload,gz,sha256,mtime)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (ts, date, building, int(floor), kind, int(n or 0), ref, json.dumps(payload, ensure_ascii=False),
         gz, sha, mtime or time.time()))
    return cur.lastrowid


def latest_upload(date, building, floor, kind="marked"):
    with _lock:
        row = conn().execute(
            "SELECT * FROM uploads WHERE date=? AND building=? AND floor=? AND kind=?"
            " ORDER BY ts DESC, id DESC LIMIT 1",
            (date, building, int(floor), kind)).fetchone()
    return dict(row) if row else None


def list_uploads(limit=500, offset=0):
    with _lock:
        rows = conn().execute(
            "SELECT id,ts,date,building,floor,kind,n,ref,sha256,mtime FROM uploads"
            " ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    return [dict(r) for r in rows]


def uploads_on(date):
    with _lock:
        rows = conn().execute(
            "SELECT id,ts,date,building,floor,kind,n,ref FROM uploads WHERE date=?"
            " ORDER BY id DESC", (date,)).fetchall()
    return [dict(r) for r in rows]


def get_upload(uid):
    with _lock:
        row = conn().execute("SELECT * FROM uploads WHERE id=?", (int(uid),)).fetchone()
    return dict(row) if row else None


def delete_upload(uid):
    _exec("DELETE FROM uploads WHERE id=?", (int(uid),))


def export_gz(uid):
    """取 gzip 原始件字节。"""
    with _lock:
        row = conn().execute("SELECT gz FROM uploads WHERE id=?", (int(uid),)).fetchone()
    return bytes(row["gz"]) if row else None


def payload_of(uid):
    with _lock:
        row = conn().execute("SELECT payload FROM uploads WHERE id=?", (int(uid),)).fetchone()
    return json.loads(row["payload"]) if row else None


# ---------------- 待填计划 ----------------

def upsert_plan(date, building, floor, payload, status="pending"):
    now = time.time()
    _exec("""INSERT INTO plans(date,building,floor,payload,status,created)
             VALUES(?,?,?,?,?,?)
             ON CONFLICT(date,building,floor) DO UPDATE SET
               payload=excluded.payload, status=excluded.status, created=excluded.created, acked=NULL""",
          (date, building, int(floor), json.dumps(payload, ensure_ascii=False), status, now))


def list_plans():
    with _lock:
        rows = conn().execute(
            "SELECT date,building,floor,payload,status,created FROM plans"
            " WHERE status='pending' ORDER BY date, building, floor").fetchall()
    out = []
    for r in rows:
        try:
            p = json.loads(r["payload"])
        except Exception:
            continue
        p.setdefault("file", r["date"])
        out.append(p)
    return out


def ack_plan(date, building=None, floor=None):
    if building is not None:
        cur = _exec("UPDATE plans SET status='done', acked=? WHERE date=? AND building=? AND floor=? AND status='pending'",
                    (time.time(), date, building, int(floor)))
    else:                                    # 只给日期时，按 date 前缀（兼容旧调用）
        cur = _exec("UPDATE plans SET status='done', acked=? WHERE date=? AND status='pending'",
                    (time.time(), date))
    return cur.rowcount


def drop_plan(date, building, floor):
    _exec("DELETE FROM plans WHERE date=? AND building=? AND floor=?", (date, building, int(floor)))


# ---------------- 账号 ----------------

def get_account(aid):
    with _lock:
        row = conn().execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
    return dict(row) if row else None


def list_accounts():
    with _lock:
        rows = conn().execute("SELECT * FROM accounts ORDER BY id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["scope"] = {"building": d.pop("building"), "floor": d.pop("floor")}
        d["enabled"] = bool(d.get("enabled", 1))
        out.append(d)
    return out


def put_account(aid, label, salt, pw_hash, building, floor, enabled=True):
    _exec("""INSERT INTO accounts(id,label,salt,pass,building,floor,enabled)
             VALUES(?,?,?,?,?,?,?)
             ON CONFLICT(id) DO UPDATE SET label=excluded.label, salt=excluded.salt,
               pass=excluded.pass, building=excluded.building, floor=excluded.floor,
               enabled=excluded.enabled""",
          (aid, label, salt, pw_hash, building, int(floor), 1 if enabled else 0))


# ---------------- 配置 / 日志 ----------------

def kv_get(k, default=None):
    with _lock:
        row = conn().execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def kv_set(k, v):
    _exec("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def log_event(level, msg):
    try:
        _exec("INSERT INTO events(ts,level,msg) VALUES(?,?,?)", (time.time(), level, msg))
    except Exception:
        pass


# ---------------- 从旧文件迁移 ----------------

def migrate_from_files(data_dir, log=print):
    """把 data/marked、data/roll、data/pending、accounts.json 搬进库；可重复执行。"""
    init(data_dir)
    n_up = n_plan = n_acc = 0

    def _date_of(d):
        s = str(d.get("d") or "")
        m = s[:10].replace("/", "-")
        if len(m) == 10 and m[4] == "-" and m[7] == "-":
            parts = m.split("-")
            return "%s-%02d-%02d" % (parts[0], int(parts[1]), int(parts[2]))
        ts = str(d.get("_ts") or "")
        return ts[:4] + "-" + ts[4:6] + "-" + ts[6:8] if len(ts) >= 8 else ""

    for sub, kind in (("marked", "marked"), ("roll", "roll")):
        d = os.path.join(data_dir, sub)
        if not os.path.isdir(d):
            continue
        exists = {(r["ts"], r["kind"]) for r in list_uploads(100000)}
        for f in sorted(os.listdir(d)):
            if not (f.endswith(".json") or f.endswith(".json.gz")):
                continue
            if not re_ts(f):
                continue
            ts = f[:15]
            path = os.path.join(d, f)
            try:
                if f.endswith(".gz"):
                    raw = gzip.decompress(open(path, "rb").read())
                else:
                    raw = open(path, "rb").read()
                payload = json.loads(raw)
            except Exception:
                continue
            if (ts, kind) in exists:
                continue
            date = _date_of(payload)
            sc = payload.get("scope") or {}
            b = payload.get("building") or sc.get("building") or "B"
            fl = payload.get("floor") or sc.get("floor") or 1
            n = len(payload.get("n") or []) if kind == "marked" else 0
            add_upload(ts, date, b, fl, kind, n, payload.get("ref"), payload,
                       os.path.getmtime(path))
            exists.add((ts, kind))
            n_up += 1

    pd = os.path.join(data_dir, "pending")
    if os.path.isdir(pd):
        for f in sorted(os.listdir(pd)):
            if not f.endswith(".json"):
                continue
            try:
                p = json.load(open(os.path.join(pd, f), encoding="utf-8"))
            except Exception:
                continue
            date = p.get("date") or f[:10]
            b = p.get("building") or (p.get("scope") or {}).get("building") or "B"
            fl = p.get("floor") or (p.get("scope") or {}).get("floor") or 1
            upsert_plan(date, b, fl, p)
            n_plan += 1

    af = os.path.join(data_dir, "state", "accounts.json")
    if os.path.exists(af):
        try:
            data = json.load(open(af, encoding="utf-8"))
            arr = data if isinstance(data, list) else (data.get("accounts") or [])
            for a in arr:
                sc = a.get("scope") or {}
                put_account(a.get("id"), a.get("label"), a.get("salt"), a.get("pass"),
                            sc.get("building") or a.get("building") or "B",
                            sc.get("floor") or a.get("floor") or 1,
                            a.get("enabled", True))
                n_acc += 1
        except Exception:
            pass

    if n_up or n_plan or n_acc:
        log("迁移入 SQLite：上传 %d、计划 %d、账号 %d" % (n_up, n_plan, n_acc))
    return {"uploads": n_up, "plans": n_plan, "accounts": n_acc}


def re_ts(name):
    return len(name) >= 15 and name[:8].isdigit() and name[8] == "-" and name[9:15].isdigit()
