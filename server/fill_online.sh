#!/bin/bash
# B座点名·信箱搬运工 —— Mac 端 cron/launchd 用，零 AI
# 职责：应用容器 一体机算好的"待填信箱"(/api/pending) → 本机已授权的 dws 写钉钉在线表 → 销账(/api/ack)
# 背景：应用容器 上的 dws 未扫码授权；写表动作由 Mac 代为执行，计算与数据全在内部服务器。
PY=${PYTHON:-python3}
LOG=/tmp/dianming-cron.log
BASE=https://example.com/dianming/api
ADMIN=CHANGE_ME_ADMIN

$PY - << 'EOF' >> "$LOG" 2>&1
import json, subprocess, urllib.request, datetime
BASE, ADMIN = "https://example.com/dianming/api", "CHANGE_ME_ADMIN"
today = datetime.date.today().strftime("%Y%m%d")
try:
    pend = json.load(urllib.request.urlopen(f"{BASE}/api/pending?admin={ADMIN}", timeout=20))["plans"]
except Exception as e:
    print(f"{datetime.datetime.now():%F %T} 信箱读取失败: {e}"); raise SystemExit(0)
if not pend:
    raise SystemExit(0)  # 空信箱，安静退出
for plan in pend:
    # 今日新件 或 之前没写成过的（pending 存在本身就说明没写成），统一都写
    vals = json.dumps([[{"type": "text", "text": str(x)} for x in plan["values"]]], ensure_ascii=False)
    r = subprocess.run(["dws", "sheet", "range", "update", "--node", "7dx2rn0JbooGgj5Bfb3zp2eA8MGjLRb3",
                        "--sheet-id", "s1", "--range", plan["range"], "--values", vals, "--format", "json"],
                       capture_output=True, text=True, timeout=60)
    if '"success": true' in (r.stdout + r.stderr):
        urllib.request.urlopen(urllib.request.Request(
            f"{BASE}/api/ack?admin={ADMIN}&date={plan['date']}", method="POST", data=b"{}"), timeout=20).read()
        print(f"{datetime.datetime.now():%F %T} 销账 {plan['date']} {plan['range']} ✓")
        subprocess.run(["dws", "chat", "message", "send", "--conversation-id", "cidri5LiTJWFqtiR4OmMm/+vw==",
                        "--content", f"[点名自动填表] {plan['date']} 已写入 {plan['range']}（{plan['students']}人·请假{plan['leaveN']}）",
                        "--yes"], capture_output=True, timeout=30)
    else:
        print(f"{datetime.datetime.now():%F %T} 写入失败 {plan['date']}: {(r.stdout+r.stderr)[-150:]}")
EOF
