"""住宿表导入：分布图（房号/床位）+ 个人信息表（班级）→ 七层名册 JSON。

口径（2026-11-04 与姐姐确认）：
- 宿舍号、床位：只认分布图；
- 班级：个人信息表为准；表里没有的，用图上简写 + 房主班年级兜底；
- 名字后面的班级简写一律剥离，只留姓名；
- 与房主班不同的人标 mixed。

用法：
  python3 ops/roster_import.py --b B座图.xlsx --a A座图.xlsx \
      --male 男生个人信息.xls --female 女生学生个人信息.xls [--dry]
"""
import argparse
import collections
import json
import os
import re
import sys

VENDOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor")
if os.path.isdir(VENDOR) and VENDOR not in sys.path:
    sys.path.insert(0, VENDOR)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

FLOOR_FILES = {
    ("B", 1): "roster_b1f.json", ("B", 2): "roster_B2f.json", ("B", 3): "roster_B3f.json",
    ("A", 2): "roster_A2f.json", ("A", 3): "roster_A3f.json",
    ("A", 4): "roster_A4f.json", ("A", 5): "roster_A5f.json",
}

CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}
RULES_FILE = os.path.join(DATA, "state", "roster_rules.json")
TYPE = {
    "网络": "网络", "电商": "电商", "数据": "大数据", "大数据": "大数据", "航空": "航空",
    "民1": "民1", "民2": "民2", "会展": "会展", "商英": "商英", "商英1": "商英1", "商英2": "商英2",
    "商1": "商英1", "商2": "商英2", "跨境": "跨境", "国商": "国商", "直播": "直播",
    "会计": "会计", "社区": "社区", "轨道": "轨道", "智能": "智能", "数媒": "数媒",
    "养老": "养老1", "养老1": "养老1", "养老2": "养老2",
    "幼保": "幼保1班", "幼1": "幼保1班", "幼2": "幼保2班",
}
KEYS = sorted(TYPE, key=len, reverse=True)


def load_rules(path=None):
    """可调参数面：别名/人物改名/班级覆盖/房主班覆盖/忽略名/安全阈值。
    文件不存在就用空规则，行为与以前一致。"""
    p = path or RULES_FILE
    try:
        with open(p, encoding="utf-8") as fh:
            r = json.load(fh)
    except Exception:
        r = {}
    return {
        "aliases": r.get("aliases") or {},          # 图上简写 → 班级片段
        "name_fix": r.get("name_fix") or {},        # 图上错名 → 正确姓名
        "overrides": r.get("overrides") or {},      # 姓名 → 班级（最高优先）
        "room_class": r.get("room_class") or {},    # 房号 → 房主班
        "drop_names": set(r.get("drop_names") or []),
    }


def rule_count(rules):
    return sum(len(rules.get(k) or []) for k in ("aliases", "name_fix", "overrides", "room_class", "drop_names"))


def norm_cls(c):
    return str(c or "").strip().replace("智慧养老", "养老").replace("幼儿保育", "幼保").replace("学前教育", "幼保")


def split_name(tok):
    """「李四（航空）」「王五 网络」「张三商1」→ (姓名, 简写)"""
    t = re.sub(r"\s+", " ", str(tok).strip())
    m = re.match(r"^(.*?)[（(]([^）)]*)[）)]$", t)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = re.match(r"^(.*?)\s*(2[456][\u4e00-\u9fa5]+\d?班?)$", t)
    if m:
        return m.group(1).strip(), m.group(2)
    for k in KEYS:
        if t.endswith(k) and len(t) > len(k):
            return t[:-len(k)].strip(), k
    return t, ""


def parse_chart(path, rules=None):
    rules = rules or {"aliases": {}, "name_fix": {}, "drop_names": set(), "room_class": {}}
    """→ {(楼, 层): [ {room, roomClass, beds:[{name,hint}|None ×8] } ]}"""
    import openpyxl
    wb = openpyxl.load_workbook(path)
    ws = wb.worksheets[0]
    out = collections.defaultdict(list)
    floor = None
    for r in range(1, ws.max_row + 1):
        a = ws.cell(r, 1).value
        if isinstance(a, str) and "住宿生" in a:
            m = re.search(r"([一二三四五])层", a)
            if m:
                floor = CN_NUM[m.group(1)]
        room = ws.cell(r, 3).value
        if floor and room is not None and str(room).strip().isdigit() and len(str(room).strip()) == 3:
            rc = norm_cls((rules.get("room_class") or {}).get(str(room).strip()) or ws.cell(r, 2).value)
            beds = []
            for c in range(4, 12):
                v = ws.cell(r, c).value
                if isinstance(v, str) and v.strip():
                    nm, hint = split_name(v)
                    nm = (rules.get("name_fix") or {}).get(nm, nm)
                    if nm not in (rules.get("drop_names") or set()):
                        beds.append({"name": nm, "hint": hint})
                else:
                    beds.append(None)
            out[floor].append({"room": str(room).strip(), "roomClass": rc, "beds": beds})
    return out


def parse_personal(path):
    """个人信息表 → {姓名: 班级}；.xls 用 xlrd，.xlsx 用 openpyxl"""
    out = {}
    if not path or not os.path.exists(path):
        return out
    if path.lower().endswith(".xls"):
        import xlrd
        wb = xlrd.open_workbook(path)
        for sh in wb.sheets():
            for r in range(1, sh.nrows):
                nm = str(sh.cell_value(r, 2)).strip()
                cl = norm_cls(sh.cell_value(r, 1))
                if nm and cl:
                    out[nm] = cl
    else:
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True)
        for ws in wb.worksheets:
            for row in ws.iter_rows(min_row=2, values_only=True):
                if len(row) >= 3 and row[2]:
                    nm, cl = str(row[2]).strip(), norm_cls(row[1])
                    if nm and cl:
                        out[nm] = cl
    return out


def existing_classes():
    m = {}
    for f in os.listdir(DATA):
        if not (f.startswith("roster_") and f.endswith(".json")):
            continue
        try:
            d = json.load(open(os.path.join(DATA, f), encoding="utf-8"))
        except Exception:
            continue
        for r in d.get("rooms") or []:
            for b in r.get("beds") or []:
                if isinstance(b, dict) and b.get("name") and b.get("class"):
                    m[str(b["name"]).strip()] = norm_cls(b["class"])
    return m


def resolve(name, hint, room_class, official, known, overrides, aliases=None):
    if name in overrides:
        cl = norm_cls(overrides[name])
        return cl, bool(room_class and cl != room_class)
    cl = official.get(name) or known.get(name)
    if not cl and hint:
        cl = norm_cls(hint) if re.match(r"^2[456]", hint) else None
        if not cl:
            yr = re.match(r"^(2[456])", room_class or "")
            t = TYPE.get(hint) or (aliases or {}).get(hint)
            if yr and t:
                cl = yr.group(1) + t
    cl = cl or room_class
    return cl, bool(room_class and cl != room_class)


def build(building, floors, official, known, overrides, aliases=None):
    """→ {floor: [room...]} + 统计"""
    made = {}
    for fl, items in floors.items():
        rooms = []
        for it in items:
            beds = []
            for b in it["beds"]:
                if not b:
                    beds.append(None)
                    continue
                cl, mixed = resolve(b["name"], b["hint"], it["roomClass"], official, known,
                                    overrides, aliases)
                beds.append({"name": b["name"], "class": cl, "mixed": mixed})
            if any(beds):
                rooms.append({"room": it["room"],
                              "className": it["roomClass"] or next((x["class"] for x in beds if x), ""),
                              "department": "", "beds": beds})
        rooms.sort(key=lambda r: r["room"])
        made[fl] = rooms
    return made


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--b", help="B 座分布图 xlsx")
    ap.add_argument("--a", help="A 座分布图 xlsx")
    ap.add_argument("--male", help="男生个人信息 .xls/.xlsx")
    ap.add_argument("--female", help="女生个人信息 .xls/.xlsx")
    ap.add_argument("--overrides", help="人工修正 JSON：{姓名: 班级}")
    ap.add_argument("--rules", help="规则文件（默认 data/state/roster_rules.json）")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    rules = load_rules(args.rules)
    official = {}
    official.update(parse_personal(args.male))
    official.update(parse_personal(args.female))
    known = existing_classes()
    overrides = dict(rules.get("overrides") or {})
    if args.overrides:
        overrides.update(json.loads(args.overrides))

    made = {}
    if args.b:
        for fl, rooms in build("B", parse_chart(args.b, rules), official, known, overrides,
                               rules.get("aliases")).items():
            made[("B", fl)] = rooms
    if args.a:
        for fl, rooms in build("A", parse_chart(args.a, rules), official, known, overrides,
                               rules.get("aliases")).items():
            made[("A", fl)] = rooms

    summary = {}
    for key, rooms in made.items():
        fname = FLOOR_FILES.get(key)
        if not fname:
            continue
        path = os.path.join(DATA, fname)
        old = {}
        if os.path.exists(path):
            try:
                d0 = json.load(open(path, encoding="utf-8"))
                old = {str(r["room"]): {b["name"] for b in r.get("beds") or [] if b}
                       for r in d0.get("rooms") or []}
            except Exception:
                pass
        new = {r["room"]: {b["name"] for b in r["beds"] if b} for r in rooms}
        people = sum(len(v) for v in new.values())
        added = {rm: sorted(v - old.get(rm, set())) for rm, v in new.items() if v - old.get(rm, set())}
        removed = {rm: sorted(v - new.get(rm, set())) for rm, v in old.items() if v - new.get(rm, set())}
        summary["%s%d" % key] = {"rooms": len(rooms), "people": people,
                                 "added": added, "removed": removed}
        if not args.dry:
            d = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
            d["rooms"] = rooms
            d["building"] = key[0]
            d["floor"] = key[1]
            json.dump(d, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(json.dumps({"ok": True, "dry": args.dry, "floors": summary,
                      "official": len(official), "known": len(known),
                      "params": {"rules_total": rule_count(rules),
                                 "aliases": len(rules.get("aliases") or {}),
                                 "name_fix": len(rules.get("name_fix") or {}),
                                 "overrides": len(overrides),
                                 "room_class": len(rules.get("room_class") or {}),
                                 "drop_names": len(rules.get("drop_names") or {})}},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
