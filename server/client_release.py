#!/usr/bin/env python3
"""client_release.py — 发布 iPad 页面包（OTA 用）

用法：
  python3 client_release.py --version 1.3.0 --dir /path/to/web-dist

做四件事：复制到 releases/<version> → 生成 manifest（含哈希）→ 切换 app/web 软链 → 只留最近 5 版。
"""
import argparse
import hashlib
import json
import os
import shutil
import time

ROOT = os.environ.get("RC_CLIENT_ROOT", "/srv/dormroll/app")
WEB = os.path.join(ROOT, "web")
REL = os.path.join(ROOT, "releases")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--dir", required=True, help="包含 index.html 的目录")
    args = ap.parse_args()

    src = os.path.abspath(args.dir)
    if not os.path.isfile(os.path.join(src, "index.html")):
        raise SystemExit("目录里没有 index.html：" + src)

    os.makedirs(REL, exist_ok=True)
    dst = os.path.join(REL, args.version)
    if os.path.exists(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst)

    files = []
    for name in sorted(os.listdir(dst)):
        if name.startswith(".") or name == "manifest.json":
            continue
        fp = os.path.join(dst, name)
        if not os.path.isfile(fp):
            continue
        with open(fp, "rb") as f:
            b = f.read()
        files.append({"path": name, "size": len(b),
                      "sha256": hashlib.sha256(b).hexdigest(), "required": True})
    with open(os.path.join(dst, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"app_version": args.version, "files": files, "ts": time.time()}, f,
                  ensure_ascii=False, indent=1)

    # 切换：优先原子替换软链；web 若是真目录则先挪开
    if os.path.isdir(WEB) and not os.path.islink(WEB):
        shutil.move(WEB, WEB + "-old-" + time.strftime("%Y%m%d%H%M%S"))
    tmp = WEB + ".new"
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.symlink(dst, tmp)
    os.replace(tmp, WEB)

    # 只留最近 5 个版本
    versions = sorted(os.listdir(REL), reverse=True)
    for old in versions[5:]:
        shutil.rmtree(os.path.join(REL, old), ignore_errors=True)

    print("published", args.version, "->", WEB, "（", len(files), "个文件）")


if __name__ == "__main__":
    main()
