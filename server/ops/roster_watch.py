"""住宿表自动更新看门人：按配置的源下载分布图/个人信息，变了才导入。

配置：data/state/roster_sources.json
  {
    "sources": [
      {"key": "b",    "url": "https://.../B座男生宿舍分布图.xlsx"},
      {"key": "a",    "url": "https://.../A座女生宿舍分布图.xlsx"},
      {"key": "male", "url": "https://.../男生个人信息.xls"},
      {"key": "female","url": "https://.../女生学生个人信息.xls"}
    ],
    "apply": false,          # true=变了直接写名册；false=只预演并留报告
    "overrides": {"张三": "26商英2"}
  }
状态：data/state/roster_sources_state.json（每个源的 sha256 与时间）

用法：
  python3 ops/roster_watch.py [--apply] [--force]
  退出码：0 无变化/成功；2 有变化但未应用（apply=false 或新手保护）；1 出错
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
STATE_DIR = os.path.join(DATA, "state")
CFG = os.path.join(STATE_DIR, "roster_sources.json")
STATE = os.path.join(STATE_DIR, "roster_sources_state.json")
LOG = os.path.join(DATA, "logs", "roster_watch.log")


def log(msg):
    line = time.strftime("[%m-%d %H:%M:%S] ") + str(msg)
    print(line)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def fetch(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "rollcall-roster-watch/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="有变化就写名册（否则只预演）")
    ap.add_argument("--force", action="store_true", help="忽略新手保护（人数骤降也照写）")
    args = ap.parse_args()

    cfg = load_json(CFG, {})
    sources = [s for s in (cfg.get("sources") or []) if s.get("url")]
    if not sources:
        log("没有配置任何源（roster_sources.json 为空）")
        return 0

    state = load_json(STATE, {})
    tmpdir = tempfile.mkdtemp(prefix="roster-src-")
    changed, files = [], {}
    for s in sources:
        key = str(s.get("key") or "").strip()
        if key not in ("b", "a", "male", "female"):
            log("跳过未知源 key=%s" % key)
            continue
        try:
            blob = fetch(s["url"])
        except Exception as e:
            log("下载失败 %s：%s" % (key, str(e)[:120]))
            continue
        h = hashlib.sha256(blob).hexdigest()
        ext = ".xls" if key in ("male", "female") else ".xlsx"
        fp = os.path.join(tmpdir, key + ext)
        with open(fp, "wb") as fh:
            fh.write(blob)
        files[key] = fp
        old = (state.get(key) or {}).get("sha256")
        if old != h:
            changed.append(key)
            state[key] = {"sha256": h, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "size": len(blob)}
            log("发现更新 %s（%d 字节，sha %s…）" % (key, len(blob), h[:10]))
        else:
            log("无变化 %s" % key)

    if not changed:
        return 0

    cmd = [sys.executable, os.path.join(ROOT, "ops", "roster_import.py")]
    for k, flag in (("b", "--b"), ("a", "--a"), ("male", "--male"), ("female", "--female")):
        if k in files:
            cmd += [flag, files[k]]
    if cfg.get("overrides"):
        cmd += ["--overrides", json.dumps(cfg["overrides"], ensure_ascii=False)]
    if not (args.apply or cfg.get("apply")):
        cmd.append("--dry")

    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "vendor"))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env, cwd=ROOT)
    if r.returncode != 0:
        log("导入失败：%s" % (r.stderr or r.stdout)[-300:])
        return 1
    try:
        out = json.loads(r.stdout)
    except Exception:
        log("导入输出不可解析：%s" % r.stdout[-200:])
        return 1

    floors = out.get("floors") or {}
    brief = "、".join("%s %d房%d人" % (k, v["rooms"], v["people"]) for k, v in floors.items())
    if out.get("dry"):
        log("预演完成（未写表）：%s" % brief)
        return 2
    log("已写入名册：%s" % brief)
    try:
        with open(STATE, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=1)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
