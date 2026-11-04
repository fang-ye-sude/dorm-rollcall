"""生成本地演示名册（虚构数据）：七层各 2 间房。

    python3 examples/seed_demo.py            # 写入 $RC_DATA 或 ./data
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("RC_DATA", os.path.join(ROOT, "data"))

FLOORS = [("B", 1, "roster_b1f.json", "101"), ("B", 2, "roster_B2f.json", "201"),
          ("B", 3, "roster_B3f.json", "301"), ("A", 2, "roster_A2f.json", "202"),
          ("A", 3, "roster_A3f.json", "302"), ("A", 4, "roster_A4f.json", "402"),
          ("A", 5, "roster_A5f.json", "502")]

NAMES = ["张三", "李四", "王五", "赵六", "钱七", "孙八", "周九", "吴十"]


def main():
    os.makedirs(DATA, exist_ok=True)
    for i, (b, f, fname, room) in enumerate(FLOORS):
        beds = [{"name": NAMES[(i + k) % len(NAMES)], "class": "25示例班", "mixed": False}
                for k in range(4)] + [None] * 4
        data = {"building": b, "floor": f, "rooms": [
            {"room": room, "className": "25示例班", "department": "", "beds": beds},
            {"room": str(int(room) + 2), "className": "25示例二班", "department": "",
             "beds": [{"name": NAMES[(i + k + 2) % len(NAMES)], "class": "25示例二班",
                       "mixed": k % 3 == 0} if k < 3 else None for k in range(8)]}
        ]}
        path = os.path.join(DATA, fname)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        print("写出", path)


if __name__ == "__main__":
    main()
