"""接口冒烟测试：不需要真实名册，任何部署都能跑。

    RC_BASE=http://127.0.0.1:8100 RC_PW=dormroll python3 tests-api.py

只做只读检查与「应当被拒绝」的写操作，不会往表里写任何东西。
"""
import json
import os
import urllib.error
import urllib.request

B = os.environ.get("RC_BASE", "http://127.0.0.1:8100").rstrip("/")
PW = os.environ.get("RC_PW", "dormroll")
FLOORS = os.environ.get("RC_FLOORS", "b1,b2,b3,a2,a3,a4,a5").split(",")

fails = []


def ok(cond, msg):
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        fails.append(msg)


def call(path, body=None, tok=None, method=None):
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(B + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


def post(p, b, t=None):
    return call(p, b, t, "POST")


def get(p, t=None):
    return call(p, None, t)


print("【1】鉴权")
ok(get("/api/roster")[0] == 401, "无令牌读名册应 401")
ok(post("/upload", {"type": "marked", "n": [["张三", "101", "DS"]]})[0] == 401, "无令牌上传应 401")
ok(get("/api/roster", "abc.def.ghi")[0] == 401, "伪造令牌应 401")

print("【2】登录与名册结构")
tokens = {}
for aid in [f.strip() for f in FLOORS if f.strip()]:
    st, j = post("/auth/login", {"id": aid, "password": PW})
    if st != 200 or not j.get("ok"):
        print(f"  - {aid} 无此账号，跳过")
        continue
    tokens[aid] = j["access_token"]
    st, r = get("/api/roster", tokens[aid])
    rooms = r.get("dormData") or []
    ok(st == 200 and bool(rooms), f"{aid} 名册可读（{len(rooms)} 房）")
    ok(all(len(x.get("students") or []) == 8 for x in rooms), f"{aid} 每房 8 床")

if not tokens:
    print("  ✗ 没有任何账号可登录（先跑 server/ops/make_accounts.py --demo）")
    raise SystemExit(1)

print("【3】脏数据必须被拒绝")
tok = tokens[next(iter(tokens))]
ok(post("/upload", {"type": "marked", "n": {"a": 1}}, tok)[0] == 400, "n 非数组应 400")
ok(post("/upload", {"type": "marked", "n": [["张三", "101", "ZZ"]]}, tok)[0] == 400, "非法假别应 400")
ok(post("/upload", {"type": "marked", "d": "abc", "n": [["张三", "101", "DS"]]}, tok)[0] == 400, "非法日期应 400")
ok(post("/upload", {"type": "marked", "n": [["不存在的人", "999", "DS"]]}, tok)[0] == 400, "不在本层名册应 400")

print("【4】管理接口")
ok(post("/api/reset-day", {"date": "2026-01-01"}, tok)[0] == 401, "层令牌不能重置某天")
ok(get("/api/pending")[0] == 401, "未授权不能读待填信箱")

print("【5】OTA 清单（可选）")
st, j = get("/api/app/latest")
if st == 200:
    ok(bool(j.get("versionName")), "安卓清单可读：%s" % j.get("versionName"))
else:
    print("  - 尚未发布安卓包，跳过")

print()
print("失败 %d 项" % len(fails))
raise SystemExit(1 if fails else 0)
