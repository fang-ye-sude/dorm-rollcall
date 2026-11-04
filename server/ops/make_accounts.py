"""生成 / 更新账号文件（data/state/accounts.json）。

用法：
  python3 server/ops/make_accounts.py --demo                  # 七层演示账号，口令 dormroll
  python3 server/ops/make_accounts.py --id b1 --pw 你的口令 --building B --floor 1
  python3 server/ops/make_accounts.py --list                  # 看现有账号
"""
import argparse
import hashlib
import json
import os
import secrets
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA = os.environ.get("RC_DATA", os.path.join(ROOT, "data"))
FILE = os.path.join(DATA, "state", "accounts.json")

FLOORS = [("b1", "B", 1), ("b2", "B", 2), ("b3", "B", 3),
          ("a2", "A", 2), ("a3", "A", 3), ("a4", "A", 4), ("a5", "A", 5)]
CN = "一二三四五"


def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200000).hex()


def load():
    try:
        with open(FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):            # 兼容裸数组写法
            return {"accounts": data}
        return data
    except Exception:
        return {"accounts": []}


def save(data):
    os.makedirs(os.path.dirname(FILE), exist_ok=True)
    with open(FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.chmod(FILE, 0o600)
    # 同时写进 SQLite（服务端以库为准；写失败也不影响文件形态）
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import store as STORE
        STORE.init(DATA)
        for a in data.get("accounts", []):
            sc = a.get("scope") or {}
            STORE.put_account(a.get("id"), a.get("label"), a.get("salt"), a.get("pass"),
                              sc.get("building", "B"), sc.get("floor", 1), a.get("enabled", True))
    except Exception as e:
        print("（写入 SQLite 失败，仅文件生效：%s）" % e)


def add(data, aid, pw, building, floor, label=None):
    salt = secrets.token_hex(8)
    rec = {"id": aid,
           "label": label or "%s座%s层" % (building, CN[floor - 1]),
           "enabled": True,
           "salt": salt,
           "pass": hash_pw(pw, salt),
           "scope": {"building": building, "floor": floor}}
    data["accounts"] = [a for a in data["accounts"] if a.get("id") != aid] + [rec]
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="生成七层演示账号，口令 dormroll")
    ap.add_argument("--id")
    ap.add_argument("--pw")
    ap.add_argument("--building", default="B")
    ap.add_argument("--floor", type=int, default=1)
    ap.add_argument("--label")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    data = load()
    if args.list:
        for a in data["accounts"]:
            sc = a.get("scope") or {}
            print("%-4s %-8s %s座%s层  enabled=%s" % (
                a.get("id"), a.get("label", ""), sc.get("building", "?"),
                sc.get("floor", "?"), a.get("enabled", True)))
        return 0

    if args.demo:
        for aid, b, f in FLOORS:
            add(data, aid, "dormroll", b, f)
        save(data)
        print("已生成 7 个演示账号 → %s" % FILE)
        print("口令统一为 dormroll（仅本地演示，公网请务必改）")
        return 0

    if not (args.id and args.pw):
        ap.print_help()
        return 1
    rec = add(data, args.id, args.pw, args.building, args.floor, args.label)
    save(data)
    print("已写入 %s（%s）" % (rec["id"], rec["label"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
