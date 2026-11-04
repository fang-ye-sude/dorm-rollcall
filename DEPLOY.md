# 部署指南

## 0. 角色

| 组件 | 需要 | 说明 |
| --- | --- | --- |
| 服务端 | Linux / macOS，Python 3.10+ | 零第三方依赖；仅名册导入需要 `openpyxl`（读 xlsx）与 `xlrd`（读 xls） |
| 表格写入 | 你的表格服务 CLI（本项目用 `dws`） | 负责读写在线表格；路径由 `RC_DWS` 指定 |
| iOS | macOS + Xcode 15+ + 付费开发者账号 | Ad Hoc 分发需在描述文件里登记设备 UDID |
| Android | JDK 17 + Android SDK 34/35 | `android/` 用 Gradle；`android-lite/` 用 SDK 自带 aapt2/d8 |

## 1. 服务端

```bash
git clone <repo> dormroll && cd dormroll
mkdir -p data/state data/logs data/marked data/pending

# 账号：一层一个，PBKDF2(200k) 哈希存
python3 server/ops/make_accounts.py --demo                    # 演示：全层口令 dormroll
python3 server/ops/make_accounts.py --id b1 --pw '强口令' --building B --floor 1
python3 server/ops/make_accounts.py --list                    # 查看已有账号

# 演示名册（换了真实名册就不用跑）
python3 examples/seed_demo.py

export RC_DATA=$PWD/data
export RC_WEB=$PWD/server/suite_web
export RC_ADMIN='换成你的管理口令'
export RC_TOKEN=''                 # 旧版全局令牌，建议留空
export RC_DWS=/usr/local/bin/dws   # 换成你的表格 CLI

python3 server/rollcall_suite.py   # 默认 :8100
```

记录默认落在 `data/rollcall.db`（SQLite，自动创建；老的文件记录首次启动会自动迁进去）。

对接钉钉/企业微信这类平台时，把群会话 ID、应用密钥等放进 `data/state/dingtalk.json`（权限 600），不要写进代码：

```json
{"group_cid": "...", "app_key": "...", "uid": "...", "corp_id": "..."}
```

systemd：

```ini
[Unit]
Description=dormroll server
After=network.target

[Service]
Environment=RC_DATA=/srv/dormroll/data
Environment=RC_WEB=/srv/dormroll/server/suite_web
Environment=RC_ADMIN=CHANGE_ME_ADMIN
Environment=RC_TOKEN=
Environment=RC_DWS=/usr/local/bin/dws
ExecStart=/usr/bin/python3 /srv/dormroll/server/rollcall_suite.py
Restart=always

[Install]
WantedBy=multi-user.target
```

定时写表（21:40）与补扫（23:50）：

```ini
# /etc/systemd/system/dormroll-fill.service
[Service]
Type=oneshot
Environment=RC_DATA=/srv/dormroll/data
WorkingDirectory=/srv/dormroll
ExecStart=/usr/bin/python3 ops/pipeline_ops.py fill
```

```ini
# /etc/systemd/system/dormroll-fill.timer
[Timer]
OnCalendar=*-*-* 21:40
Persistent=true

[Install]
WantedBy=timers.target
```

名册看门（可选，每天两次）：

```ini
# dormroll-roster.timer
[Timer]
OnCalendar=*-*-* 06:30,18:30
```

### 反向代理

```nginx
location /dormroll/ {
    proxy_pass http://127.0.0.1:8100/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    client_max_body_size 4m;
}
```

> 客户端里把 `Api.BASE` 改成你的地址（`android/.../Api.java`、`ios/Sources/API.swift`）。
> 若前面有 WAF/云盾拦截纯 TLS 客户端，Android 端可保留 `WebTransport`（走 WebView），它自带浏览器指纹。

## 2. 名册

```bash
# 预演（不写盘）
python3 ops/roster_import.py --b 分布图.xlsx --a 分布图.xlsx \
    --male 男生信息.xls --female 女生信息.xls --dry

# 确认写入
python3 ops/roster_import.py --b ... --a ... --male ... --female ...
```

也可走接口（管理员）：

```bash
curl -X POST https://example.com/rollcall/api/roster/import \
  -H "Authorization: Bearer $RC_ADMIN" -H 'Content-Type: application/json' \
  -d '{"a":"<xlsx 的 base64>","confirm":true}'
```

自动源：编辑 `data/state/roster_sources.json` 填 URL，或走 `POST /api/roster/sources`。

## 3. Android

### Material 3 版

```bash
cd android
echo "sdk.dir=$HOME/Library/Android/sdk" > local.properties
./gradlew assembleRelease        # 未配置签名时输出未签名包
```

签名（可选）：

```bash
cp keystore.properties.example keystore.properties
# 填 storeFile / storePassword / keyAlias / keyPassword
./gradlew assembleRelease
```

### 极简版（36 KB）

```bash
cd android-lite
bash build-release.sh            # 需要 aapt2 / d8 / apksigner 与一个 keystore
```

## 4. iOS

```bash
cd ios
xcodegen generate                # 由 project.yml 生成工程
xcodebuild -project DormRoll.xcodeproj -scheme DormRoll \
  -configuration Release -destination 'generic/platform=iOS' \
  -archivePath /tmp/DormRoll.xcarchive archive -allowProvisioningUpdates
xcodebuild -exportArchive -archivePath /tmp/DormRoll.xcarchive \
  -exportOptionsPlist ExportOptions.plist -exportPath /tmp/export -allowProvisioningUpdates
```

`ExportOptions.plist` 关键项：

```xml
<key>method</key><string>ad-hoc</string>
<key>teamID</key><string>YOURTEAMID</string>
<key>signingStyle</key><string>automatic</string>
```

> Ad Hoc OTA 只能装到描述文件里登记过的设备；新设备要先加 UDID 再重新导出。
> 版本号写在 `ios/project.yml` 的 `info.properties` 里（xcodegen 会覆盖 `Info.plist`）。

## 5. OTA 发布

```bash
# 改版本号后
./publish-ota.sh both      # 构建 + 上传 + 重写 latest.json / manifest.plist
```

服务端目录：

```
app/android/latest.json + DormRoll-<ver>.apk
app/ios/latest.json + DormRoll-<ver>.ipa + manifest.plist
```

## 6. 自检

```bash
./run-tests.sh                      # 逻辑 + API 回归
python3 tests-api.py                # 只跑接口
```

首次部署后建议逐项确认：登录、名册条数、上传、`/write-now`（空信箱会安全跳过）、OTA 清单可下载。
