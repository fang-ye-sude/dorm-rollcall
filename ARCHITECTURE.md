# 架构与实现要点

这份文档写给「第一次接手、要读懂或改这个项目」的人（或 AI Agent）。按数据流读一遍即可上手。

## 1. 角色与隔离

- **七层账号**：`b1/b2/b3/a2/a3/a4/a5`，每层一个账号，令牌里带 `scope {building, floor}`；
- 服务端所有名册、上传、写表都按 `scope` 取数，**层与层之间互不可见**；
- 管理员（`RC_ADMIN`）可看全部、重置某天、导入名册。

登录：`POST /auth/login {id, password}` → `access_token`（HMAC 签名，默认 12 小时）。
令牌校验失败 → 401，客户端自动回选层页。

## 2. 存储：SQLite（记录）+ 文件（名册/日志）

**记录类数据走数据库** `data/rollcall.db`（WAL 模式，标准库 `sqlite3`，零外部依赖）：

| 表 | 一行是什么 | 关键字段 |
| --- | --- | --- |
| `uploads` | 一次客户端上传 | `ts, date, building, floor, kind, n, ref, payload, gz, sha256` |
| `plans` | 一层一天的待填计划 | `date, building, floor, payload, status(pending/done)`，唯一键 `(date,building,floor)` |
| `accounts` | 一个层账号 | `id, label, salt, pass, building, floor, enabled` |
| `kv` | 配置/标记 | `k, v` |
| `events` | 操作日志 | `ts, level, msg` |

- 上传不再「一条一个 json.gz 文件」：原始 JSON 与 gzip 副本都存库，`/raw/db/<id>` 直接下载；
- 计划不再「一层一个 pending 文件」：`upsert` 到 `plans`，重复上传自动覆盖，`ack` 置 `done`；
- 删除是一条 `DELETE`，然后按同一天/层最新的一条上传重算计划（或删除计划）；
- 首次启动会把老的 `data/marked`、`data/roll`、`data/pending`、`accounts.json` 自动迁移进库，迁移标记写 `kv`，可重复执行。

文件只保留：名册 `roster_*.json`（派生物）、`logs/`（文本日志）、`state/`（配置）。


名册结构：

```json
{"building": "B", "floor": 1, "rooms": [
  {"room": "103", "className": "25示例班", "beds": [
    {"name": "张三", "class": "25示例班", "mixed": false},
    null, null, null, null, null, null, null
  ]}
]}
```

## 3. 一次点名的生命周期

```
客户端点人（本地状态 absent[room]=Set<bed>, marks[room#bed]=DS|DB|XS|XB）
      │ 点「上传」
      ▼
POST /upload  {type:"marked", d:"2026/11/4 21:08:00", n:[[姓名,房号,假别],…]}
      │ 服务端校验：形状、条数、日期格式、本层名册归属、同人查重
      ▼
marked/<时间戳>.json.gz   →  生成 pending/<日期>-<层>.json（计划）
      │
      ├─ 21:40 定时 pipeline_ops.py fill
      └─ 或客户端点「写表」→ POST /write-now（单飞锁，避免连点）
      ▼
写主表当日行 C:L（应到 / 中专 / 大专 / 四维 / 请假名单）
写周末 sheet（若当天在该周的周末块）
写体温表（当月 sheet，病/事 标到人）
      ▼
回执 /api/ack + 群日报
```

## 4. 写表流水线（`pipeline_ops.py`）

| 函数 | 作用 | 调用次数 |
| --- | --- | --- |
| `fill_main` | 写主表一行：备份旧值 → 写入 → 回读校验（不符再写一次） | 3 次 dws |
| `fill_weekend` | 写本周周末 sheet 的 B:D 区，并清理表尾残留 | 3–5 次 |
| `fill_temp` | 本地 xlsx 里标病/事，保留 RAND 体温公式 | 0 次（纯本地） |
| `cmd_fill` | 取计划 → **各层并行**（3 路）→ 失败单独通知 | — |

优化点（详见 docs/06）：

- 每层计划用 `ThreadPoolExecutor(max_workers=3)` 并行，七层约 13 秒；
- 周末表 sheet 列表在进程内缓存，每层少一次调用；
- `dws` 调用失败重试 3 次、退避 0.6s/1.2s（读写都可重入）；
- 每层日志带 `用时X.Xs`，便于观察退化。

## 5. 服务端网络层

- `ThreadingHTTPServer` + `protocol_version = "HTTP/1.1"`（长连接，客户端复用 TCP）；
- 响应 ≥800 B 自动 gzip（名册 4.8 KB → 1.1 KB），带 `ETag`，客户端带 `If-None-Match` 时返回 304；
- 上传体上限 `MAX_BODY`，超限先抽掉残留再回 413。

## 6. 名册导入（`ops/roster_import.py`）

```
分布图 xlsx ──parse_chart──▶ rooms[{room, roomClass, beds[8]}]
个人信息表 ──parse_personal──▶ {姓名: 班级}

班级决策：个人信息表 > 规则 overrides > 图上简写+房主班年级 > 房主班
姓名清洗：括号类（王五（航空））、附尾简写（张三商1）、错名纠正（name_fix）
混寝判定：class != roomClass → mixed=true
```

无 `--dry` 时写 `roster_*.json`；`confirm` 语义由调用方控制（接口默认预演）。

规则文件示例：

```json
{
  "aliases": {"商1": "商英1", "大数据": "大数据"},
  "name_fix": {"张 三": "张三"},
  "overrides": {"张三": "25示例班"},
  "room_class": {"205": "25示例班"},
  "drop_names": ["空", "—"]
}
```

## 7. 名册看门（`ops/roster_watch.py`）

配置 `data/state/roster_sources.json`：

```json
{"sources": [{"key": "b", "url": "https://example.com/b.xlsx"},
             {"key": "a", "url": "https://example.com/a.xlsx"}],
 "apply": false}
```

每轮：下载 → 算 sha256 → 与 `roster_sources_state.json` 比对 → **变了才导入**；
`apply=false` 只预演（返回码 2），`true` 才落盘。定时器示例：`OnCalendar=*-*-* 06:30,18:30`。

## 8. 客户端

### iOS（`ios/`）

- SwiftUI，`RootView` 按 `session` 切换选层 / 点名；
- 点名页：房卡网格（宽屏表格，窄屏卡片），点人标红，长按或「标记」页逐人设假别；
- 左缘侧滑返回：`dragX` 跟手位移 + 松手回弹/滑出动画；
- 更新：启动与回前台查 `/api/app/latest-ios`，三选项（立即 / 稍后 / 跳过此版本）。

### Android（`android/`）

- 单 Activity + 纯代码 Material 3 视图，无 XML 布局；
- 网络走 `WebTransport`：借系统 WebView 发 fetch（Chromium TLS 指纹，能过一些云盾策略），省掉打包 OkHttp；
- 房卡两列，每卡 8 床（窄屏 2×4，宽屏 4×2），名字按字数定字号；
- OTA：查 `/api/app/latest` → base64 下载 → SHA-256 校验 → 自建 `ApkProvider` 拉起安装器。

### Android Lite（`android-lite/`）

自定义构建（aapt2 + javac + d8），36 KB，零第三方依赖，保留同一套接口与 OTA。

## 9. OTA 分发

| 端 | 清单 | 包 | 安装 |
| --- | --- | --- | --- |
| Android | `/api/app/latest` | `/api/app/apk/<file>`（`?b64=1` 走文本通道） | 系统安装器 |
| iOS | `/api/app/latest-ios` | `/api/app/ios/<file>` + `manifest.plist` | `itms-services://` |

发版脚本：仓库根 `publish-ota.sh`（读版本号 → 构建 → 上传 → 重写清单）。

## 10. 已知边界

- 表格 CLI（`dws`）是外部依赖，本仓库不含；接入自己的表格服务时替换 `pipeline_ops.dws()` 即可；
- 服务端是单进程 + 文件锁，适合「几个班级 ~ 几百人」的量级；更大规模请换数据库与任务队列；
- 客户端 OTA 的信任基于 HTTPS + SHA-256，没有做证书固定（pinning）。
