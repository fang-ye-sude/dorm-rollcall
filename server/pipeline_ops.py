#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""应用容器 全自跑：发布白表 / 取件填表 / 深夜补扫 / 周五备份。
分工不变：rollcall_suite 只算（计划+weekend_rows 入信箱），本脚本只写在线表+发链接。"""
import datetime, fcntl, gzip, json, os, re, shutil, subprocess, sys, time, urllib.request

BASE = "http://127.0.0.1:8100"


def _svc_auth():
    """服务间凭据：优先环境变量，其次 state/service_auth.json（600），不在代码里写死。"""
    a = os.environ.get("RC_ADMIN", "")
    t = os.environ.get("RC_TOKEN", "")
    if a and t:
        return a, t
    try:
        d = json.load(open(os.path.join(ROOT, "data", "state", "service_auth.json"), encoding="utf-8"))
        return a or d.get("admin", ""), t or d.get("token", "")
    except Exception:
        return a, t


ADMIN, TOKEN = "", ""   # 启动时由 main() 前的 _svc_auth() 填
DWS = "/usr/local/bin/dws"
HERE = os.path.dirname(os.path.abspath(__file__))            # /srv/dormroll/ops
ROOT = os.path.normpath(os.path.join(HERE, ".."))            # /srv/dormroll
APP = os.path.normpath(os.path.join(HERE, "..", "app"))      # /srv/dormroll/app
BACKUP = os.path.normpath(os.path.join(HERE, "..", "backup"))
# 钉钉相关标识不写死在代码里：放 data/state/dingtalk.json，或走环境变量
#   {"group_cid": "...", "app_key": "...", "uid": "...", "corp_id": "..."}
_dt = {}
try:
    _dt = json.load(open(os.path.join(ROOT, "data", "state", "dingtalk.json"), encoding="utf-8"))
except Exception:
    _dt = {}
GROUP_CID = os.environ.get("RC_DINGTALK_GROUP") or _dt.get("group_cid", "")
ZHANG_DAD = os.environ.get("RC_DINGTALK_APPKEY") or _dt.get("app_key", "")
ZHANG_DAD_UID = os.environ.get("RC_DINGTALK_UID") or _dt.get("uid", "")
ZHANG_DAD_CORP = os.environ.get("RC_DINGTALK_CORP") or _dt.get("corp_id", "")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))
import table_layout as TL
HEADS = TL.HEADS
OFFS = TL.OFFS


def LOG(m):
    print(time.strftime("[%m-%d %H:%M:%S]"), m, flush=True)


ADMIN, TOKEN = _svc_auth()


def jget(url):
    return json.load(urllib.request.urlopen(url, timeout=20))


def jpost(url, body=None):
    req = urllib.request.Request(url, data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=20))


def _parse(out):
    i = out.find("{")
    if i < 0:
        return {}
    try:
        return json.loads(out[i:])
    except Exception:
        return {}


def dws(*args, check=True, tries=3):
    last = ""
    for attempt in range(1, tries + 1):
        r = subprocess.run([DWS] + [str(a) for a in args] + ["--format", "json"],
                           capture_output=True, text=True, timeout=180)
        out = (r.stdout or "") + (r.stderr or "")
        last = out
        d = _parse(r.stdout or "")
        ok = '"success": true' in out or (isinstance(d, dict) and (d.get("success") or d.get("ok")))
        if ok:
            return d
        if attempt < tries:
            time.sleep(0.6 * attempt)        # 轻量退避：网络抖动/限流多半一次就好
            continue
        break
    if check:
        raise RuntimeError("dws %s → %s" % (" ".join(str(a) for a in args[:6]), last[:220]))
    return {}


def revoke(node):
    """撤下旧表：移除群协作权限（姐姐令：每次换表都把上次的撤下来）"""
    if not node or node.startswith("PLACEHOLDER"):
        return
    try:
        dws("drive", "permission", "remove", "--node", node,
            "--members", '[{"type":"CONVERSATION","id":"%s"}]' % GROUP_CID, "--yes")
        LOG("已撤下旧表 %s…" % node[:8])
    except Exception as e:
        LOG("撤旧表失败 %s… %s" % (node[:8], str(e)[:80]))


def grant_edit(node):
    """把表的权限给到"可编辑"：群协作 EDITOR + 张昊天爸爸 EDITOR。幂等，失败不阻断发布。"""
    if not node or str(node).startswith("PLACEHOLDER"):
        return
    ok = 0
    _m = []
    if GROUP_CID:
        _m.append('[{"type":"CONVERSATION","id":"%s","roleId":"EDITOR"}]' % GROUP_CID)
    if ZHANG_DAD_UID and ZHANG_DAD_CORP:
        _m.append('[{"type":"USER","id":"%s","corpId":"%s","roleId":"EDITOR"}]' % (ZHANG_DAD_UID, ZHANG_DAD_CORP))
    for members in _m:
        try:
            dws("drive", "permission", "add", "--node", node, "--members", members)
            ok += 1
        except Exception as e:
            LOG("授权失败（%s…）：%s" % (str(node)[:8], str(e)[:100]))
    LOG("已授编辑权限 %s…（%d/2）" % (str(node)[:8], ok))


def send_group(text):
    try:
        dws("chat", "message", "send", "--conversation-id", GROUP_CID, "--content", text, "--yes")
    except Exception as e:
        LOG("群消息失败 %s" % e)


def send_user(uid, text):
    dws("chat", "message", "send", "--open-dingtalk-id", uid, "--content", text, "--yes")


def notify_fail(tag, detail):
    LOG("!! %s %s" % (tag, detail))
    send_group("[点名] %s：%s" % (tag, detail[:90]))


def VALUES(rows):
    return json.dumps([[{"type": "text", "text": "" if v is None else str(v)} for v in row] for row in rows],
                      ensure_ascii=False)


def norm(x):
    """单元格值归一到"回读可比较"的字符串；None 必须归空串（VALUES 写出去的是 ""）。"""
    return "" if x is None else str(x).strip()


def lock(name):
    """进程互斥（cron 与手动/补扫可能撞车）。拿不到返回 None，进程退出自动释放。"""
    fh = open(os.path.join("/tmp", "rc-%s.lock" % name), "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def write_range(node, sid, rng, rows):
    dws("sheet", "range", "update", "--node", node, "--sheet-id", sid,
        "--range", rng, "--values", VALUES(rows))


def read_range(node, sid, rng):
    d = dws("sheet", "range", "read", "--node", node, "--sheet-id", sid, "--range", rng)
    return d.get("cells") or d.get("data", {}).get("cells") or []


# ---------- 自检 ----------

def selfcheck(plan, node):
    d = datetime.date.fromisoformat(plan["date"])
    iso = d.isoweekday()
    seq = None
    blk = plan.get("block")                       # 调休/补班：rollcall 用 config.blockmap 指定块，优先认它
    VALID = {h + o for h in HEADS for o in OFFS}
    if isinstance(blk, int) and 1 <= blk <= 5 and (plan.get("row", 0) - HEADS[blk - 1]) in OFFS:
        seq = blk - 1
    if seq is None:
        seq = {7: 0, 1: 1, 2: 2, 3: 3, 4: 4}.get(iso)
    if seq is None or (plan["row"] - HEADS[seq]) not in OFFS or plan["row"] not in VALID:
        raise RuntimeError("行位异常 row=%s 星期=%s" % (plan.get("row"), iso))
    title = plan.get("sheet_title") or "B1"
    b, f = title[0], int(title[1:])
    want = "%s座%s层" % (b, "一二三四五六"[f - 1])
    cells = read_range(node, "s1", "B%d:B%d" % (HEADS[seq], HEADS[seq] + 23))
    col = [str((row[0] if row else {}).get("value") or "") for row in cells]
    if want not in col or col.index(want) + HEADS[seq] != plan["row"] - 1:
        raise RuntimeError("结构异常：%s 不在 row-1（表结构与预期不符）" % want)
    v = plan["values"]
    if len(v) != 10 or any(v[i] != "/" for i in (1, 3, 5)):
        raise RuntimeError("values 形态异常")
    if not (sum(plan["counts"].values()) == plan["students"] == plan["leaveN"]):
        raise RuntimeError("数据异常：counts 不平")
    return seq


def check_class_order(flat):
    seq = [r[0] for r in flat if r and r[0]]
    seen = []
    for c in seq:
        if not seen:
            seen.append(c)
        elif c != seen[-1]:
            if c in seen:
                raise RuntimeError("班级序穿插：%s" % c)
            seen.append(c)


# ---------- 填表 ----------

def backup_row(plan, node, rng):
    """覆盖前先留底：原行有内容就存一份，事后可查可还原。"""
    try:
        got = read_range(node, "s1", rng)
        old = [(c or {}).get("value", "") for c in (got[0] if got else [])]
        if not any(str(x).strip() for x in old):
            return
        new = [str(x) for x in plan["values"]]
        if [norm(x) for x in old] == [norm(x) for x in new]:
            return                                  # 内容一样，不必留底
        d = os.path.join(ROOT, "data", "logs", "rowbackup")
        os.makedirs(d, exist_ok=True)
        fn = os.path.join(d, "%s-%s-%s.json" % (plan.get("date"), plan.get("sheet_title", "?"),
                                               time.strftime("%H%M%S")))
        json.dump({"range": rng, "old": old, "new": plan["values"],
                   "ts": time.strftime("%Y-%m-%d %H:%M:%S")},
                  open(fn, "w", encoding="utf-8"), ensure_ascii=False)
        LOG("覆盖前留底 %s → %s" % (rng, os.path.basename(fn)))
    except Exception as e:
        LOG("留底失败（不影响写入）：%s" % str(e)[:80])


def fill_main(plan, node, dry):
    rng = plan["range"]
    if dry:
        LOG("[dry] 主表写 %s %s" % (node[:8], rng))
        return
    want = [[norm(x) for x in plan["values"]]]
    backup_row(plan, node, rng)
    write_range(node, "s1", rng, [plan["values"]])
    for attempt in (1, 2):
        got = read_range(node, "s1", rng)
        got = [[norm((c or {}).get("value") or "") for c in (row or [])] for row in got]
        if got == want:
            return
        if attempt == 1:
            write_range(node, "s1", rng, [plan["values"]])
    raise RuntimeError("主表回读不符")


_SHEETS_CACHE = {}


def list_sheets(wnode):
    hit = _SHEETS_CACHE.get(wnode)
    if hit:
        return hit
    d = dws("sheet", "+list-sheets", "--node", wnode)
    sheets = d.get("data", {}).get("sheets") or d.get("sheets") or []
    _SHEETS_CACHE[wnode] = sheets
    return sheets


def fill_weekend(plan, wnode, dry):
    rows = plan.get("weekend_rows")
    if rows is None:                       # 非周末件/旧计划没有名单：别动表
        LOG("周末件缺 weekend_rows，跳过")
        return
    sheets = list_sheets(wnode)
    title = plan.get("sheet_title") or "B1"
    sid = next((s.get("sheetId") for s in sheets if s.get("title") == title), None)
    if not sid:
        raise RuntimeError("周末表找不到 %s sheet" % title)
    n = len(rows)
    TAIL = 51                              # B1 名单区表尾行
    if dry:
        LOG("[dry] 周末表写 %s B2:D%d N=%d" % (wnode[:8], n + 1, n))
        return
    want = [[norm(c) for c in r] for r in rows]
    flat = []
    if n:
        rng = "B2:D%d" % (n + 1)
        write_range(wnode, sid, rng, rows)
        got = read_range(wnode, sid, rng)
        flat = [[norm((c or {}).get("value") or "") for c in (row or [])] for row in got]
        if flat != want:
            write_range(wnode, sid, rng, rows)
            got = read_range(wnode, sid, rng)
            flat = [[norm((c or {}).get("value") or "") for c in (row or [])] for row in got]
            if flat != want:
                raise RuntimeError("周末表回读不符")
    # 残留清除：n 行之后到表尾（n=0 即整片清空，避免上周名单留在表上）
    if n + 2 <= TAIL:
        tail_rng = "B%d:D%d" % (n + 2, TAIL)
        tail = read_range(wnode, sid, tail_rng)
        if any(norm((c or {}).get("value") or "") for row in tail for c in (row or [])):
            write_range(wnode, sid, tail_rng, [["", "", ""] for _ in range(TAIL - n - 1)])
    check_class_order(flat)


def cmd_fill(sweep=False, dry=False, targets=("main", "weekend", "temp")):
    lk = lock("fill")                      # 定时 21:40 / 23:50 补扫 / 手动"立即填入"可能撞车
    if lk is None:
        LOG("另一个填表进程在跑，本次跳过")
        return 0
    p = jget("%s/api/pending?admin=%s" % (BASE, ADMIN))
    plans, node, wnode = p["plans"], p.get("node"), p.get("weekend_node")
    today = time.strftime("%Y%m%d")
    if not plans:
        if sweep:
            LOG("空箱·静默")
            return 0
        idx = jget("%s/index?token=%s" % (BASE, TOKEN))["items"]
        if not any(i.get("type") == "marked" and str(i.get("f") or "").startswith(today) for i in idx):
            early = time.localtime().tm_hour * 60 + time.localtime().tm_min < 21 * 60 + 30
            if dry:
                LOG("[dry] 空箱且今日无件%s" % ("（21:30前不预警）" if early else ""))
            elif not early:
                send_group("[点名] 今晚还没有标记件，表未更新（如稍后补传，23:50 会自动写入）")
            else:
                LOG("21:30前空箱·静默")
        else:
            LOG("当日已填过·静默")
        return 0
    fails = 0
    reports = []
    # 只写今天；非今日的计划一律归档（防上周/上月残留落到同星期块）
    today_iso = time.strftime("%Y-%m-%d")
    keep = []
    for plan in plans:
        if plan.get("date") == today_iso:
            keep.append(plan)
        else:
            try:
                # 库里销账（计划表 status=done）；老文件形态一并兼容迁移
                jpost("%s/api/ack?admin=%s" % (BASE, ADMIN), {"date": plan.get("date")})
                pd = os.path.join(ROOT, "data", "pending")
                src = os.path.join(pd, (plan.get("file") or plan.get("date")) + ".json")
                if os.path.exists(src):
                    os.replace(src, os.path.join(ROOT, "data", "logs", "superseded-" + os.path.basename(src)))
                LOG("非今日计划归档（不写表）: %s" % plan.get("date"))
            except Exception as e:
                LOG("旧计划归档失败 %s: %s" % (plan.get("date"), e))
    plans = keep
    if not plans:
        LOG("今日无待填计划·静默")
        return 0
    def run_one(plan):
        t0 = time.time()
        done = []
        try:
            if "main" in targets:
                if not node or str(node).startswith("PLACEHOLDER"):
                    raise RuntimeError("在线表 node 未配置")
                selfcheck(plan, node)
                fill_main(plan, node, dry)
                done.append("主表")
            if plan.get("weekend") and "weekend" in targets:
                if not wnode or wnode == "PLACEHOLDER_WK":
                    raise RuntimeError("weekend_node 未配置")
                fill_weekend(plan, wnode, dry)
                done.append("周末表")
            if "temp" in targets:
                fill_temp(plan, dry)
                done.append("体温表")
            if not dry and "main" in targets:
                jpost("%s/api/ack?admin=%s&date=%s" % (BASE, ADMIN, plan.get("file") or plan["date"]))
            LOG("%s %s %s %s妥%s 用时%.1fs" % (plan["date"], plan.get("sheet_title", "?"), plan["range"],
                                          "+".join(done), " [dry]" if dry else "", time.time() - t0))
            return plan.get("report") or "", None
        except Exception as e:
            notify_fail("[点名填表] %s 停" % plan["date"], str(e))
            return "", e

    # 各层互不相干 → 并行写；限 3 路，避免把接口打出限流
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(3, max(1, len(plans)))) as ex:
        outs = list(ex.map(run_one, plans))
    for rep, err in outs:
        if err is not None:
            fails += 1
        elif rep:
            reports.append(rep)
    if reports and not dry and "main" in targets:
        send_group("[点名日报]\n" + "\n".join(r for r in reports if r))
    return 1 if fails else 0


# ---------- 发布 ----------

def week_days(ref):
    off = TL.DAY_SEQ.get(ref.isoweekday())
    if off is None:                        # 周五/周六：不硬崩 KeyError
        raise ValueError("%s 是周%s，不在点名周内（周日~周四）" % (ref, "一二三四五六日"[ref.isoweekday() - 1]))
    sun = ref - datetime.timedelta(days=off)
    return [sun + datetime.timedelta(days=k) for k in range(5)]


def upload_convert(path, name):
    d = dws("doc", "upload", "--file", path, "--name", name, "--convert")
    nid = d.get("nodeId") or d.get("result", {}).get("nodeId")
    if not nid:
        raise RuntimeError("upload 未返回 nodeId: %s" % str(d)[:150])
    return nid


def rename_retry(node, base):
    """改名，撞名（本周已发过同名片）自动加 (2)(3)… 后缀"""
    for suffix in ("", "(2)", "(3)", "(4)"):
        try:
            dws("drive", "rename", "--node", node, "--name", base + suffix)
            return base + suffix
        except RuntimeError:
            if suffix == "(4)":
                raise
    return base


def cmd_publish(dry=False, ref=None):
    lk = lock("publish")
    if lk is None:
        LOG("另一个发布进程在跑，本次跳过")
        return 0
    ref = datetime.date.fromisoformat(ref) if ref else datetime.date.today()
    try:
        days = week_days(ref)
    except ValueError as e:
        notify_fail("[发布]", str(e))
        return 1
    main_out, wk_out = "/tmp/rc_main.xlsx", "/tmp/rc_wk.xls"
    for f in (main_out, wk_out):
        if os.path.exists(f):
            os.remove(f)
    af_args = ["python3", os.path.join(APP, "server", "auto_fill.py"), "--blank", "--out", main_out,
               "--date", ref.isoformat()]
    r = subprocess.run(af_args, capture_output=True, text=True)
    if not os.path.exists(main_out):
        notify_fail("[发布]", "--blank 失败 " + (r.stdout + r.stderr)[-160:])
        return 1
    import openpyxl
    wb = openpyxl.load_workbook(main_out)["Sheet1"]
    got = [wb.cell(h + 3, 1).value for h in HEADS]
    want = ["%s" % d.strftime("%Y.%m.%d") for d in days]
    if got != want:
        notify_fail("[发布]", "五日期不对: %s" % got)
        return 1
    old = jget("%s/api/pending?token=%s" % (BASE, TOKEN))
    old_node, old_wk = old.get("node"), old.get("weekend_node")
    shutil.copy(os.path.join(APP, "data", "周末未返校统计-原版格式模板.xls"), wk_out)
    LOG("自检过·开始上传（%s~%s）" % (want[0], want[-1]))
    if dry:
        LOG("[dry] 停在上传前")
        return 0
    try:
        nodeA = upload_convert(main_out, "住宿生请假汇总表%s至%s" % (days[0].strftime("%m%d"), days[-1].strftime("%m%d")))
        nodeB = upload_convert(wk_out, "周末未返校统计%s至%s" % (days[0].strftime("%m%d"), days[-1].strftime("%m%d")))
        # 旧表先改名"已撤-…"腾出正名，正名不带 (2)(3)（姐姐令：上次撤了就不会重名）
        stamp = time.strftime("%m%d%H%M")
        for old_n, tag in ((old_node, "旧请假表"), (old_wk, "旧返校表")):
            if old_n:
                try:
                    dws("drive", "rename", "--node", old_n, "--name", "已撤-%s-%s" % (tag, stamp))
                except Exception as e:
                    LOG("旧表改名失败 %s… %s" % (old_n[:8], str(e)[:60]))
        nameA = rename_retry(nodeA, "%s-%s住宿生请假汇总表" % (days[0].strftime("%m.%d"), days[-1].strftime("%m.%d")))
        nameB = rename_retry(nodeB, "周末未返校统计")
        grant_edit(nodeA)
        grant_edit(nodeB)
        jpost("%s/api/config?admin=%s" % (BASE, ADMIN),
              {"node": nodeA, "weekend_node": nodeB,
               "daterange": "%s-%s" % (want[0], want[-1])})
        v = jget("%s/api/pending?admin=%s" % (BASE, ADMIN))
        if v.get("node") != nodeA or v.get("weekend_node") != nodeB:
            notify_fail("[发布]", "config 回验不一致")
            return 1
        send_user(ZHANG_DAD, "https://alidocs.dingtalk.com/i/nodes/%s" % nodeA)
        send_user(ZHANG_DAD, "https://alidocs.dingtalk.com/i/nodes/%s" % nodeB)
        if old_node and old_node != nodeA:
            revoke(old_node)
        if old_wk and old_wk != nodeB:
            revoke(old_wk)
        LOG("发布完 %s~%s nodeA=%s… nodeB=%s…" % (want[0], want[-1], nodeA[:8], nodeB[:8]))
        return 0
    except Exception as e:
        notify_fail("[发布] 失败", str(e))
        return 1


# ---------- 备份 ----------

def cmd_backup():
    os.makedirs(BACKUP, exist_ok=True)
    p = jget("%s/api/pending?token=%s" % (BASE, TOKEN))
    node, wnode = p["node"], p.get("weekend_node")
    snap = {"node": node, "weekend_node": wnode, "ts": time.strftime("%F %T")}
    for r_ in (15, 39, 63, 87, 111):
        snap["C%d:L%d" % (r_, r_)] = read_range(node, "s1", "C%d:L%d" % (r_, r_))
    if wnode:
        try:
            wk_sheets = dws("sheet", "+list-sheets", "--node", wnode).get("data", {}).get("sheets", [])
            wk_sid = next((x["sheetId"] for x in wk_sheets if x.get("title") == "B1"),
                          wk_sheets[0]["sheetId"] if wk_sheets else "s5")
            snap["weekend_B1"] = read_range(wnode, wk_sid, "A1:D51")
        except Exception as e:
            snap["weekend_B1_error"] = str(e)[:120]
    stamp = time.strftime("%Y%m%d")
    with open(os.path.join(BACKUP, "周快照_%s.json" % stamp), "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False)
    try:
        subprocess.run(["tar", "czf", os.path.join(BACKUP, "ct102-data-%s.tgz" % stamp),
                        "-C", "/srv/dormroll", "data",
                        "-C", "/srv/dormroll/app", "data/宿舍点体温.xlsx",
                        "-C", "/srv/dormroll", "roster_b1f.json", "ops"], check=True)
    except Exception as e:
        notify_fail("[点名备份]", "打包失败：%s" % str(e)[:120])
        return 1
    send_group("[点名备份] 在线快照+应用容器全档（含体温主档/名册/脚本） 已存 backup/")
    LOG("备份完")


# ---------- 体温表 ----------

TEMP = os.path.join(APP, "data", "宿舍点体温.xlsx")
RAND_F = "=ROUND(RAND()*(37-36.3)+36.3,1)"


def temp_sections(ws):
    """解析体温表分段：标题行/日期行/日→列/学生行[(row,room,name)]（段内房间前向填充）"""
    secs = []
    titles = [r for r in range(1, ws.max_row + 1)
              if ws.cell(r, 1).value and "点名表" in str(ws.cell(r, 1).value)]
    for i, t in enumerate(titles):
        drow = t + 1
        end = titles[i + 1] - 1 if i + 1 < len(titles) else ws.max_row
        days = {}
        if str(ws.cell(drow, 2).value or "").strip() == "日期":
            days = {ws.cell(drow, c).value: c for c in range(3, ws.max_column + 1)
                    if isinstance(ws.cell(drow, c).value, (int, float))}
        cur = None; students = []
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
        secs.append({"drow": drow, "days": days, "students": students, "end": end})
    return secs


def _suite():
    for p in (HERE, ROOT):
        if p not in sys.path:
            sys.path.insert(0, p)
    import rollcall_suite as R
    return R


_ALIAS = None
def _canon(name):
    """名字别名归正；别名表只取一次（原来每格都 sys.path.insert + roster()）"""
    global _ALIAS
    if _ALIAS is None:
        _ALIAS = _suite().roster()["name_aliases"]
    return _ALIAS.get(name, name)


def _db_marked(name):
    """从库里取上传件（/raw/db/<id> 返回 gzip）。"""
    uid = str(name).split("/", 1)[1]
    req = urllib.request.Request("%s/raw/db/%s" % (BASE, uid),
                                 headers={"Authorization": "Bearer %s" % ADMIN})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(gzip.decompress(r.read()))


def fill_temp(plan, dry=False):
    """当月工作表填 病/事：照点名件，已有手填标记不动；在校生的RAND体温公式原样保留"""
    y, m = plan["date"][:7].split("-")
    key = "%s.%s" % (y, m)
    if not os.path.exists(TEMP):
        raise RuntimeError("体温主档不在 %s" % TEMP)
    import openpyxl
    from openpyxl.utils import get_column_letter
    wb = openpyxl.load_workbook(TEMP)
    if key not in wb.sheetnames:
        src_names = sorted([n for n in wb.sheetnames if re.match(r"^\d{4}\.\d{2}$", n)])
        if not src_names:
            LOG("体温表缺 %s 且无可克隆月表，跳过" % key)
            return
        src = wb[src_names[-1]]
        ws_new = wb.copy_worksheet(src)
        ws_new.title = key
        y_, m_ = key.split(".")
        y_, m_ = int(y_), int(m_)
        import calendar as _cal
        ndays = _cal.monthrange(y_, m_)[1]
        day_list = [d for d in range(1, ndays + 1)
                    if datetime.date(y_, m_, d).weekday() in (0, 1, 2, 3, 6)]
        last_col = 2 + len(day_list)
        for sec in temp_sections(ws_new):
            for c in range(3, max(ws_new.max_column, last_col) + 1):
                ws_new.cell(sec["drow"], c).value = day_list[c - 3] if 3 <= c <= last_col else None
            for rr, _room, _nm in sec["students"]:
                for c in range(3, max(ws_new.max_column, last_col) + 1):
                    ws_new.cell(rr, c).value = RAND_F if c <= last_col else None
        wb.save(TEMP)
        LOG("体温表缺 %s，已按 %s 克隆生成 %d 日列" % (key, src.title, len(day_list)))
        wb = openpyxl.load_workbook(TEMP)
    ws = wb[key]
    secs = temp_sections(ws)
    global_days = next((s["days"] for s in secs if s["days"]), {})
    rowmap = {}
    for s in secs:
        for r, room, nm in s["students"]:
            if room:
                rowmap.setdefault((room, _canon(nm)), r)
    mk_name = plan.get("marked_file")
    if not mk_name:
        raise RuntimeError("体温填：计划缺 marked_file")
    R = _suite()
    if str(mk_name).startswith("db/"):
        mk = _db_marked(mk_name)
    else:
        mk_path = os.path.join(R.DATA, mk_name)
        if not os.path.exists(mk_path):
            raise RuntimeError("体温填：找不到 %s" % mk_name)
        mk = R.read_json(mk_path)
    stus = R.build_students({"students": [{"name": x[0], "room": str(x[1]),
                                           "q": x[2] if len(x) > 2 else "DS"} for x in mk.get("n", [])]})
    day = int(plan["date"][8:10])
    col = global_days.get(day)
    if not col:
        LOG("体温表 %s 无 %d 日列，跳过" % (key, day))
        return
    want = []
    miss = []
    for s in stus:
        r = rowmap.get((str(s["room"]), s["name"]))
        if not r:
            cand = [rr for (rm, n), rr in rowmap.items() if n == s["name"]]
            r = cand[0] if len(cand) == 1 else None
        if not r:
            miss.append("%s@%s" % (s["name"], s["room"]))
            continue
        mark = "病" if s["q"].endswith("B") else "事"
        v = ws.cell(r, col).value
        manual = v not in (None, "") and not (isinstance(v, str) and v.startswith("=")) and v not in ("事", "病")
        if manual:
            continue  # 姐姐手填（走/停等）不动；系统旧标记(事/病)允许刷新
        want.append((r, col, mark))
    if miss:
        LOG("体温表 %s 缺 %d 人（跳过）：%s" % (key, len(miss), "、".join(miss)))
    # 重传改判：先把当日列里系统旧的 病/事 全清回随机公式（手填的走/停不动）
    cleared = []
    for sec in secs:
        c0 = (sec.get("days") or {}).get(day)
        if not c0:
            continue
        for rr, _room, _nm in sec["students"]:
            v0 = ws.cell(rr, c0).value
            if isinstance(v0, str) and v0.strip() in ("事", "病"):
                ws.cell(rr, c0).value = RAND_F
                cleared.append((rr, c0))
    if dry:
        LOG("[dry] 体温表 %s %d日 清 %d 格、写 %d 格" % (key, day, len(cleared), len(want)))
        return
    for r, c, mark in want:
        ws.cell(r, c).value = mark
    wb.save(TEMP)
    # 读回核验
    wb2 = openpyxl.load_workbook(TEMP)
    ws2 = wb2[key]
    bad = [(r, c) for r, c, mark in want if ws2.cell(r, c).value != mark]
    if bad:
        raise RuntimeError("体温表回读不符 %d 格" % len(bad))
    # 同步到在线共享表（若已配 temp_node）
    try:
        p2 = jget("%s/api/pending?token=%s" % (BASE, TOKEN))
        tn = p2.get("temp_node")
        if tn and want:
            sheets = dws("sheet", "+list-sheets", "--node", tn).get("data", {}).get("sheets", [])
            y_, m_ = key.split(".")
            want_title = "%s年%d月" % (y_, int(m_))
            sid = next((x["sheetId"] for x in sheets if x.get("title") in (key, want_title)), None)
            if sid:
                # 按列合并成一次读写：原来每格起一个 dws 进程，20 人就是 40+ 次
                cols = sorted({c for _, c in cleared} | {c for _, c, _ in want})
                cells = {}
                for r_, c_ in cleared:
                    cells[(r_, c_)] = ""
                for r_, c_, mk_ in want:
                    cells[(r_, c_)] = mk_
                for c_ in cols:
                    rows_c = sorted(r for (r, c) in cells if c == c_)
                    lo, hi = rows_c[0], rows_c[-1]
                    L = get_column_letter(c_)
                    cur = read_range(tn, sid, "%s%d:%s%d" % (L, lo, L, hi))
                    vals = [[(x or {}).get("value", "")] for x in (cur or [])]
                    while len(vals) < hi - lo + 1:
                        vals.append([""])
                    for r_ in rows_c:
                        vals[r_ - lo] = [cells[(r_, c_)]]
                    write_range(tn, sid, "%s%d:%s%d" % (L, lo, L, hi), vals)
                LOG("体温共享表 %s 同步 %d 格（清 %d，按列 %d 次写）" % (key, len(want), len(cleared), len(cols)))
    except Exception as e:
        LOG("体温共享表同步失败（不影响本地）：%s" % str(e)[:80])
    LOG("体温表 %s %d日 写 %d 格 ✓" % (key, day, len(want)))


def purge_old(days=None, dry=False):
    """原始件到期清理：marked/roll/日志归档，超过 N 天（默认 180）删除。
    只删原始上传件与过期归档，不碰 pending/done/名册。"""
    days = int(days or os.environ.get("RC_KEEP_DAYS") or 180)
    cutoff = time.time() - days * 86400
    targets = [os.path.join(ROOT, "data", "marked"),
               os.path.join(ROOT, "data", "roll"),
               os.path.join(ROOT, "data", "logs")]
    killed = 0
    kept = 0
    for d in targets:
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            fp = os.path.join(d, name)
            if not os.path.isfile(fp):
                continue
            if d.endswith("logs") and not (name.startswith("superseded-") or name.endswith(".log.old")):
                continue                      # 只清归档，不动当前日志
            try:
                if os.path.getmtime(fp) < cutoff:
                    if not dry:
                        os.remove(fp)
                    killed += 1
                    LOG("[%s] 删除过期 %s（%d 天前）" % ("dry" if dry else "purge", name,
                                                      int((time.time() - os.path.getmtime(fp)) / 86400)))
                else:
                    kept += 1
            except Exception as e:
                LOG("清理失败 %s: %s" % (name, str(e)[:60]))
    LOG("原始件清理完成：删 %d 件，保留 %d 件（保留期 %d 天%s）"
        % (killed, kept, days, "，dry" if dry else ""))
    return killed


def build_temp_sheet(prev_key, dry=False):
    """把上月体温 sheet 单拎成独立 xlsx（临时件）"""
    import openpyxl
    if not (os.path.exists(TEMP) and prev_key in openpyxl.load_workbook(TEMP).sheetnames):
        return None
    wb = openpyxl.load_workbook(TEMP)
    for n in list(wb.sheetnames):
        if n != prev_key:
            del wb[n]
    # 在线转换器不认带点的 sheet 名（"2026.11"→参数非法），副本改名
    y, m = prev_key.split(".")
    wb[prev_key].title = "%s年%d月" % (y, int(m))
    p1 = "/tmp/宿舍点体温_%s.xlsx" % prev_key
    wb.save(p1)
    return p1


def send_month_pack(cur_key, dry=False, old_temp=None):
    """每月1号：生成本月体温表→转在线共享表→发链接→撤上一张"""
    tmp = build_temp_sheet(cur_key, dry)
    if not tmp:
        LOG("体温主档无 %s sheet，跳过" % cur_key)
        return
    if dry:
        LOG("[dry] 体温共享表 %s → 转在线+撤旧 %s" % (cur_key, (old_temp or "-")[:8]))
        return
    y_, m_ = cur_key.split(".")
    nice = "宿舍点体温%s年%d月" % (y_, int(m_))
    node = upload_convert(tmp, nice)
    rename_retry(node, nice)
    grant_edit(node)
    send_user(ZHANG_DAD, "https://alidocs.dingtalk.com/i/nodes/%s" % node)
    jpost("%s/api/config?admin=%s" % (BASE, ADMIN), {"temp_node": node})
    revoke(old_temp)
    os.remove(tmp)
    LOG("体温 %s 共享表已发（%s…）" % (cur_key, node[:8]))


def cmd_tempmonth(dry=False, month=None):
    lk = lock("tempmonth")
    if lk is None:
        LOG("另一个体温表任务在跑，本次跳过")
        return 0
    """每月1号：发上月成品 → 生成本月空表（Sun~Thu 全列，体温RAND公式铺满）"""
    import openpyxl
    from openpyxl.utils import get_column_letter
    if not os.path.exists(TEMP):
        notify_fail("[体温表]", "主档不在 %s" % TEMP)
        return
    today = datetime.date.today()
    if month:
        y_, m_ = month.split("-")
        today = datetime.date(int(y_), int(m_), 1)
    cur_key = "%d.%02d" % (today.year, today.month)
    first = today.replace(day=1)
    prev = first - datetime.timedelta(days=1)
    prev_key = "%d.%02d" % (prev.year, prev.month)
    wb = openpyxl.load_workbook(TEMP)
    # ② 生成本月（先有表才能发）
    if cur_key in wb.sheetnames:
        LOG("%s 已存在，跳过生成" % cur_key)
        pend = jget("%s/api/pending?token=%s" % (BASE, TOKEN))
        if not pend.get("temp_node"):
            send_month_pack(cur_key, dry, None)   # 本月表尚未发共享表 → 补发
        return
    if prev_key not in wb.sheetnames:
        notify_fail("[体温表]", "无 %s 可克隆" % prev_key)
        return
    src = wb[prev_key]
    ws = wb.copy_worksheet(src)
    ws.title = cur_key
    # 收集 RAND 公式样板
    rand_f = RAND_F
    for srow in temp_sections(src)[0]["students"][:1]:
        v = src.cell(srow[0], 3).value
        if isinstance(v, str) and v.startswith("=ROUND(RAND"):
            rand_f = v
    # 本月日列：周日~周四
    next_first = (first + datetime.timedelta(days=31)).replace(day=1)
    ndays = (next_first - first).days
    days = [d for d in range(1, ndays + 1)
            if datetime.date(today.year, today.month, d).weekday() in (0, 1, 2, 3, 6)]
    last_col = 2 + len(days)
    secs = temp_sections(ws)
    for s in secs:
        # 日期行重写
        for c in range(3, max(ws.max_column, last_col) + 1):
            ws.cell(s["drow"], c).value = days[c - 3] if 3 <= c <= last_col else None
        # 学生行：日区全部重置为 RAND 公式
        for r, room, nm in s["students"]:
            for c in range(3, max(ws.max_column, last_col) + 1):
                ws.cell(r, c).value = rand_f if c <= last_col else None
    # 新列宽照抄末列
    for c in range(20, last_col + 1):
        letter = get_column_letter(c)
        ref = get_column_letter(19)
        if ref in ws.column_dimensions:
            ws.column_dimensions[letter].width = ws.column_dimensions[ref].width
    if dry:
        LOG("[dry] 生成 %s 日列 %d 个（%s~%s）" % (cur_key, len(days), days[0], days[-1]))
        return
    wb.save(TEMP)
    LOG("生成 %s：%d 日列（%s ~ %s），体温公式已铺满" % (cur_key, len(days), days[0], days[-1]))
    # ③ 本月表发成共享表格 + 撤上一张
    pend = jget("%s/api/pending?token=%s" % (BASE, TOKEN))
    send_month_pack(cur_key, dry, pend.get("temp_node"))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    dry = "--dry" in sys.argv
    ref = None
    for a in sys.argv[2:]:
        if len(a) == 10 and a[4] == "-":
            ref = a
    if cmd == "publish":
        sys.exit(cmd_publish(dry, ref))
    elif cmd == "fill":
        tg = next((a.split("=", 1)[1] for a in sys.argv[2:] if a.startswith("--targets=")), "main,weekend,temp")
        sys.exit(cmd_fill(sweep=False, dry=dry, targets=tuple(x.strip() for x in tg.split(",") if x.strip())))
    elif cmd == "sweep":
        tg = next((a.split("=", 1)[1] for a in sys.argv[2:] if a.startswith("--targets=")), "main,weekend,temp")
        sys.exit(cmd_fill(sweep=True, dry=dry, targets=tuple(x.strip() for x in tg.split(",") if x.strip())))
    elif cmd == "purge-old":
        days = None
        for a in sys.argv[2:]:
            if a.startswith("--days="):
                days = int(a.split("=", 1)[1])
        sys.exit(purge_old(days=days, dry=("--dry" in sys.argv)))
    elif cmd == "backup":
        cmd_backup()
    elif cmd == "tempmonth":
        mon = next((a for a in sys.argv[2:] if len(a) == 7 and a[4] == "-"), None)
        cmd_tempmonth(dry, mon)
    elif cmd == "monsend":
        # 手动补发某月三包：monsend 2026.10 [--dry]
        send_month_pack(sys.argv[2], dry)
    else:
        print("usage: pipeline_ops.py publish|fill|sweep|backup|tempmonth|monsend|purge-old [--dry] [--days=N]")
        sys.exit(2)
