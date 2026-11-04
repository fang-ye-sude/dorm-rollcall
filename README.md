# DormRoll · 宿舍点名与填表自动化

一套面向中职/高中**住宿生晚点名 + 共享表格填报**的完整方案：手机端点人 → 服务端记账 → 定时（或一键）写入在线表格，并附 iOS / Android 双端原生客户端与 OTA 更新通道。

> 本仓库是**脱敏开源版**：不含任何真实学生姓名、房号名册、账号口令、密钥、服务器地址。所有示例数据均为虚构。

---

## 它能做什么

| 环节 | 说明 |
| --- | --- |
| 点名 | 老师在自己那一层的手机上点人标缺勤，分四类假别（宿事 / 宿病 / 部事 / 部病） |
| 上传 | 一键把本次缺勤传到服务端，按「层」隔离，互不干扰 |
| 写表 | 到点（默认 21:40）自动写入在线表格：主表当日行 + 周末 sheet + 体温表；也可一键「写表」 |
| 名册 | 分布图（xlsx）+ 个人信息表（xls）自动解析成七层名册，支持在线更新 |
| 客户端 | iOS（SwiftUI，SwiftUI 原生）、Android（Material 3）、Android Lite（36 KB 极简版） |
| 更新 | 双端应用内 OTA：查版本 → 下载 → 校验 → 安装（iOS 走 Ad Hoc + itms-services） |

---

## 架构

```
┌─────────────┐   HTTPS    ┌──────────────────────────┐   CLI    ┌──────────────┐
│ iOS / Android│ ─────────▶ │ 服务端（Python 标准库）    │ ───────▶ │ 在线表格服务   │
│  点名 + 上传  │            │  rollcall_suite.py :8100  │  dws     │ (WPS/文档)   │
└─────────────┘            │  ├ /upload  接收标记件      │          └──────────────┘
                           │  ├ /api/*   名册/清单/配置   │
                           │  ├ /write-now 立即写表       │
                           │  └ /api/app/*  OTA 分发     │
                           └──────────┬───────────────┘
                                      │ 读
                           ┌──────────▼───────────────┐
                           │ data/  名册 · 上传件 · 日志 │
                           └──────────────────────────┘

ops/pipeline_ops.py   定时任务：取计划 → 写主表/周末表/体温表 → 回执 + 群日报
ops/roster_import.py  名册导入：分布图 + 个人信息表 → 七层 roster_*.json
ops/roster_watch.py   名册看门：按源下载 → sha256 比对 → 有变化才导入
```

## 目录

```
server/            服务端（零第三方依赖，http.server + json）
  rollcall_suite.py   主服务：登录/名册/上传/写表/OTA/管理台
  pipeline_ops.py     填表流水线：主表、周末表、体温表、日报
  table_layout.py     表格坐标与四维口径
  webapp.py           独立网页面板（可选）
  suite_web/          管理台前端（原生 JS，无框架）
  ops/                名册导入与看门脚本
android/           Android 客户端（Gradle + Material 3）
android-lite/      Android 极简客户端（自定义 aapt2 构建，36 KB）
ios/               iOS 客户端（SwiftUI + XcodeGen）
docs/              需求、流程、原理、接口、运维、踩坑、红蓝对抗记录
examples/          示例名册与账号（虚构数据）
```

## 快速开始（服务端）

```bash
python3 -V                                   # 3.10+
python3 server/ops/make_accounts.py --demo   # 七层演示账号，口令 dormroll（公网请改）
python3 examples/seed_demo.py                # 虚构名册，七层各 2 间房

export RC_DATA=$PWD/data RC_WEB=$PWD/server/suite_web \
       RC_ADMIN=CHANGE_ME_ADMIN RC_TOKEN=
python3 server/rollcall_suite.py             # 监听 :8100
```

打开 `http://127.0.0.1:8100/api/manifest` 或 `http://127.0.0.1:8100/`（管理台，需 `?admin=` 口令）。

真实账号：`python3 server/ops/make_accounts.py --id b1 --pw 你的口令 --building B --floor 1`。

浏览器打开 `http://127.0.0.1:8100/` 就是管理台。

客户端构建见 [DEPLOY.md](DEPLOY.md)。

## 关键配置（环境变量）

| 变量 | 作用 |
| --- | --- |
| `RC_DATA` | 数据目录（名册、上传件、日志、状态） |
| `RC_WEB` | 管理台静态文件目录 |
| `RC_ADMIN` | 管理员口令（管理台 / 重置 / 名册导入） |
| `RC_TOKEN` | 旧版全局令牌（可选，建议置空） |
| `RC_DWS` | 表格 CLI 路径（默认 `/srv/dormroll/bin/dws`） |

## 名册口径（重要）

1. **房号、床位**：只认宿舍分布图（xlsx）；
2. **班级**：以个人信息表为准；表里没有的人，用「图上简写 + 房主班年级」兜底；
3. 名字后面的班级简写（`商1`、`网络`、`民航`…）一律剥离，只留姓名；
4. 与房主班不同的人标 `mixed`，混寝在界面里单独提示。

规则可调参数在 `data/state/roster_rules.json`（别名、错名纠正、班级覆盖、房主班覆盖、忽略名）。

## 四维假别

| 代码 | 含义 | 写入 |
| --- | --- | --- |
| `DS` | 宿管 · 事假 | 主表当日行 |
| `DB` | 宿管 · 病假 | 同上 |
| `XS` | 部系 · 事假 | 同上 |
| `XB` | 部系 · 病假 | 同上 |

统计与写表都按四维分开计数，客户端统计条实时显示 `宿事 x · 宿病 x · 部事 x · 部病 x`。

## 文档

- [ARCHITECTURE.md](ARCHITECTURE.md) —— 数据流、状态、写表逻辑、OTA 原理
- [DEPLOY.md](DEPLOY.md) —— 服务端 / iOS / Android 部署与签名
- [SECURITY.md](SECURITY.md) —— 脱敏范围与安全边界
- [docs/](docs/) —— 需求、流程、接口、运维、踩坑、红蓝对抗测试

## 许可

MIT，见 [LICENSE](LICENSE)。示例数据均为虚构，可自由替换。
