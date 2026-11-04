#!/usr/bin/env python3
"""点名 → 请假汇总表 自动填写 + 发布/备份 demo
用法:
  python3 auto_fill.py --record latest [--owner dorm|dept] [--out 输出.xlsx]
  读取服务器最新(v2/v1)点名记录 → 名册归正 → 填桌面白表当日块 B座一层行 → 输出成品
  --owner: dorm=写I/J(宿管办，返校日)  dept=写K/L(部系，周一~四)  默认按当天星期自动选
数据口径与 web/app.js 一致：走读进事假列；请假数=事假+病假；病假名单空（iPad 暂无病情标注）。
"""
import argparse, datetime, gzip, io, json, os, re, sys, urllib.request

SERVER = "https://example.com/dianming/api"
TOKEN = "CHANGE_ME_TOKEN"
HERE = os.path.dirname(os.path.abspath(__file__))
ROSTER_JSON = os.path.join(HERE, "..", "data", "roster_b1f.json")
BLANK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "住宿生请假汇总表-空白模板.xlsx")
DESKTOP_BLANK = os.environ.get("RC_BLANK_XLSX", "")  # 原件路径（可选）；项目 data/ 为权威副本
WEEKDAY_CN = "一二三四五六日"
# 新白表：五块（周日~周四），块头行 1/25/49/73/97；日期格 = 块头+3；B座一层数据行 = 块头+14
BLOCK_HEAD_ROWS = [1, 25, 49, 73, 97]
SICK_DEFAULT = set()   # 暂无自动病情来源，需要时在此/参数补充


def fetch(path):
    return json.load(urllib.request.urlopen(SERVER + path, timeout=20))


def normalize(d):
    if d.get("v") == 2 and isinstance(d.get("n"), list):
        return {"date": d["d"], "total": d["t"], "present": d["p"], "absent": d["a"],
                "absentStudents": [{"name": x[0], "room": str(x[1])} for x in d["n"] if isinstance(x, list)]}
    return d


def get_marked(name="latest"):
    """取最新(或指定)标记件 v4 + 其关联回执，合成填表原料。
    返回 dict: date,total,present,absent, students:[{name,room,q}], weekend"""
    idx = fetch(f"/index?token={TOKEN}")["items"]
    marked_files = [i["f"] for i in idx if i.get("type") == "marked"]
    if not marked_files:
        return None
    f = marked_files[-1] if name == "latest" else name
    raw = fetch(f"/get?token={TOKEN}&name={f}")
    # /get 对标记件已归一成 v1 形态（absentStudents 带 q）；也兼容原 v4（n）
    if raw.get("absentStudents"):
        mk = {"d": raw.get("date"), "weekend": raw.get("weekend", False), "ref": raw.get("ref"),
              "n": [[s["name"], s["room"], s.get("q", "DS")] for s in raw["absentStudents"]]}
        roll_hint = {"total": raw.get("total"), "present": raw.get("present")}
    else:
        mk = raw; roll_hint = None
    roll = None
    if mk.get("ref"):
        try:
            roll = normalize(fetch(f"/get?token={TOKEN}&name={mk['ref']}"))
        except Exception:
            roll = None
    if roll is None and roll_hint and roll_hint.get("total") is not None:
        roll = {"date": mk["d"], "total": roll_hint["total"], "present": roll_hint["present"]}
    if roll is None:  # 仍无：退回用最新 rollcall 的计数
        rolls = [i for i in idx if i.get("type") != "marked"]
        if rolls:
            roll = normalize(fetch(f"/get?token={TOKEN}&name={rolls[-1]['f']}"))
    stus = [{"name": x[0], "room": str(x[1]), "q": x[2] if len(x) > 2 else "DS"} for x in mk.get("n", [])]
    return {"file": f, "date": mk.get("d") or (roll or {}).get("date"),
            "total": (roll or {}).get("total"), "present": (roll or {}).get("present"),
            "absent": (roll or {}).get("absent", len(stus)),
            "students": stus, "weekend": bool(mk.get("weekend"))}


def load_roster():
    r = json.load(open(ROSTER_JSON, encoding="utf-8"))
    idx = {}   # (name, room) -> class
    for room in r["rooms"]:
        for s in room["students"]:
            idx[(s["name"], room["room"])] = s["class"]
    return r, idx


def build_names(rec, roster, idx):
    alias, tagmap = roster["name_aliases"], roster["alias_map"]
    out = []
    for st in rec["absentStudents"]:
        parts = str(st["name"]).strip().split()
        name = alias.get(parts[0], parts[0])
        cls = idx.get((name, str(st["room"])), "?")
        if len(parts) > 1 and tagmap.get(parts[1], "").strip():
            t = parts[1]
            cls = tagmap.get(t) or tagmap.get(t.replace("25", "")) or cls
        out.append({"name": name, "cls": cls,
                    "reason": "病假" if name in SICK_DEFAULT else "事假",
                    "room": str(st["room"])})
    # 排序按名册房间顺序
    order = {}
    for room in roster["rooms"]:
        for i, s in enumerate(room["students"]):
            order[(s["name"], room["room"])] = (int(room["room"]), i)
    out.sort(key=lambda x: order.get((x["name"], x["room"]), (999, 999)))
    return out


def fmt_group(items):
    m = {}
    for it in items:
        m.setdefault(it["cls"], []).append(it["name"])
    return " ".join(c + " ".join(ns) for c, ns in m.items())


def week_dates(d):
    """任一天 d → 本周日~周四 5 个日期（白表五块依次对应）；周五/周六不在点名周内"""
    offset = {7: 0, 1: 1, 2: 2, 3: 3, 4: 4}.get(d.isoweekday())
    if offset is None:       # date/datetime 都吃
        raise ValueError("%s 是周%s，不在点名周内（周日~周四）"
                         % (d.strftime("%Y-%m-%d"), WEEKDAY_CN[d.isoweekday() - 1]))
    sun = d - datetime.timedelta(days=offset)
    return [sun + datetime.timedelta(days=k) for k in range(5)]


def parse_date(s):
    """标记件日期兼容 'YYYY/MM/DD HH:MM:SS' 与 ISO 形态；认不出返回 None"""
    s = str(s or "").strip()
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M", "%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--marked", default="latest", help="标记件文件名，默认最新一份")
    ap.add_argument("--out", default=None)
    ap.add_argument("--blank", action="store_true",
                    help="纯白表模式：只填五块日期(A列)，不灌任何当晚数据——周日发布用")
    ap.add_argument("--date", default=None, help="基准日 YYYY-MM-DD（发布周表用，默认今天）")
    args = ap.parse_args()

    # —— 纯白表：load 模板 → 按"今天所在周"填五个日期 → 存。数据行保持骨架(斜杠/公式)，等每晚取件任务逐日填 ——
    if args.blank:
        import openpyxl
        ref = datetime.date.fromisoformat(args.date) if args.date else datetime.date.today()
        try:
            days = week_dates(ref)
        except ValueError as e:
            print(json.dumps({"error": str(e)}, ensure_ascii=False))
            sys.exit(1)
        wb = openpyxl.load_workbook(BLANK)
        ws = wb["Sheet1"]
        for h, day in zip(BLOCK_HEAD_ROWS, days):
            ws.cell(h + 3, 1).value = f"{day:%Y.%m.%d}"
        OUTDIR = os.path.join(HERE, "..", "out")
        os.makedirs(OUTDIR, exist_ok=True)
        out = args.out or os.path.join(OUTDIR, f"{days[0]:%Y.%m.%d}-{days[-1]:%Y.%m.%d}住宿生请假汇总表.xlsx")
        wb.save(out)
        print(json.dumps({"blank": True, "out": out, "dates": [f"{x:%Y-%m-%d}" for x in days]}, ensure_ascii=False))
        return

    M = get_marked(args.marked)
    if not M:
        print(json.dumps({"error": "服务器还没有标记件——iPad 点名屏保存后，切填表屏标四维并上传"}, ensure_ascii=False))
        sys.exit(1)
    if not M["date"]:
        print(json.dumps({"error": "标记件缺日期字段"}, ensure_ascii=False))
        sys.exit(1)

    d = parse_date(M["date"])
    if not d:
        print(json.dumps({"error": "标记件日期认不出：%s" % M["date"]}, ensure_ascii=False))
        sys.exit(1)
    wd = d.isoweekday()
    seq = {7: 0, 1: 1, 2: 2, 3: 3, 4: 4}.get(wd)
    if seq is None:                      # 先判星期，再算周四列，免得周五/周六死在 week_dates 里
        print(json.dumps({"error": "点名日期 %s（周%s）不在周日~周四范围" % (d.date(), WEEKDAY_CN[wd - 1])}, ensure_ascii=False))
        sys.exit(1)
    roster, idx = load_roster()
    alias = roster["name_aliases"]
    for s in M["students"]:
        parts = str(s["name"]).strip().split()
        s["name"] = alias.get(parts[0], parts[0])
        if len(parts) > 1 and roster["alias_map"].get(parts[1]):
            s["cls"] = roster["alias_map"][parts[1]]
        else:
            s["cls"] = idx.get((s["name"], s["room"]), "?")
    order = {}
    for room in roster["rooms"]:
        for i, st in enumerate(room["students"]):
            order[(st["name"], room["room"])] = (int(room["room"]), i)
    M["students"].sort(key=lambda x: order.get((x["name"], x["room"]), (999, 999)))

    Q_OF = {9: "DS", 10: "DB", 11: "XS", 12: "XB"}

    import openpyxl
    wb = openpyxl.load_workbook(BLANK)
    ws = wb["Sheet1"]
    days = week_dates(d)
    for h, day in zip(BLOCK_HEAD_ROWS, days):
        ws.cell(h + 3, 1).value = f"{day:%Y.%m.%d}"
    h = BLOCK_HEAD_ROWS[seq]
    r = None
    for rr in range(h, h + 24):
        if str(ws.cell(rr, 2).value).strip() == "B座一层":
            r = rr + 1
    if r is None:
        print(json.dumps({"error": "白表块%d 找不到 B座一层 行" % (seq + 1)}, ensure_ascii=False))
        sys.exit(1)

    ws.cell(r, 3).value = M["total"] if M["total"] is not None else "?"
    ws.cell(r, 4).value = "/"
    ws.cell(r, 5).value = M["present"] if M["present"] is not None else "?"
    ws.cell(r, 6).value = "/"
    leave = M["students"]   # v10 口径：上标记屏的全算请假（DS=宿管办批事假，不扣）
    ws.cell(r, 7).value = len(leave)
    ws.cell(r, 8).value = "/"
    for c in (9, 10, 11, 12):
        members = [s for s in M["students"] if s["q"] == Q_OF[c]]
        if members:
            ws.cell(r, c).value = fmt_group(members)
        else:
            ws.cell(r, c).value = "/"

    OUTDIR = os.path.join(HERE, "..", "out")
    os.makedirs(OUTDIR, exist_ok=True)
    out = args.out or os.path.join(OUTDIR, f"{days[0]:%Y.%m.%d}-{days[-1]:%Y.%m.%d}住宿生请假汇总表.xlsx")
    wb.save(out)

    weekend_tsv = None
    if M["weekend"]:
        def full(c): return {"25民1": "25民航1", "25民2": "25民航2"}.get(c, c)
        lines = ["序号\t班级\t姓名\t请假原因"]
        for i, s in enumerate(M["students"], 1):
            reason = "病假" if s["q"].endswith("B") else "走读"
            lines.append(f"{i}\t{full(s['cls'])}\t{s['name']}\t{reason}")
        weekend_tsv = os.path.join(OUTDIR, f"周末返校_{days[0]:%Y%m%d}.tsv")
        with open(weekend_tsv, "w", encoding="utf-8") as f:      # 必须关文件，否则下游可能读到半截
            f.write("\n".join(lines))

    # 落机读计划：供 fill_online.sh（cron）直接写在线表，无需再算
    plan = {"node": None, "sheet": "s1", "range": f"C{r}:L{r}",
            "row": r, "date": M["date"],
            "values": [M["total"], "/", M["present"], "/", len(leave), "/",
                       fmt_group([s for s in M["students"] if s["q"] == "DS"]) or "/",
                       fmt_group([s for s in M["students"] if s["q"] == "DB"]) or "/",
                       fmt_group([s for s in M["students"] if s["q"] == "XS"]) or "/",
                       fmt_group([s for s in M["students"] if s["q"] == "XB"]) or "/"],
            "weekend_tsv": weekend_tsv}
    wn = os.path.join(OUTDIR, "week-node.txt")
    if os.path.exists(wn):
        with open(wn) as f:
            parts = f.read().split()
        plan["node"] = parts[0] if parts else None
    with open(os.path.join(OUTDIR, "fill-plan.json"), "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=1)     # 同上：不关文件 fill_online.sh 可能读到空计划

    print(json.dumps({"out": out, "marked": M["file"], "block": seq + 1, "row": r,
                      "date": M["date"], "weekday": "周" + WEEKDAY_CN[wd - 1],
                      "total": M["total"], "present": M["present"],
                      "students": len(M["students"]),
                      "cols": {k: sum(1 for s in M["students"] if s["q"] == k) for k in ("DS", "DB", "XS", "XB")},
                      "weekend_tsv": weekend_tsv}, ensure_ascii=False))


if __name__ == "__main__":
    main()
