#!/bin/bash
# iOS Ad Hoc 真 OTA 打包：xcodegen 生成工程 → 归档 → 导出 ad-hoc IPA → 打印发布指引
# 前置：Xcode 已登录付费团队（YOURTEAMID），且开发者后台已同意最新协议（PLA）
set -e
cd "$(dirname "$0")"
xcodegen generate >/dev/null
rm -rf /tmp/DormRoll.xcarchive /tmp/export
xcodebuild -project DormRoll.xcodeproj -scheme DormRoll -configuration Release \
  -destination 'generic/platform=iOS' -archivePath /tmp/DormRoll.xcarchive archive -allowProvisioningUpdates | tail -2
cat > /tmp/ExportOptions.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>method</key><string>ad-hoc</string>
  <key>teamID</key><string>YOURTEAMID</string>
  <key>signingStyle</key><string>automatic</string>
  <key>compileBitcode</key><false/>
  <key>stripSwiftSymbols</key><true/>
  <key>destination</key><string>export</string>
</dict>
</plist>
PLIST
xcodebuild -exportArchive -archivePath /tmp/DormRoll.xcarchive \
  -exportOptionsPlist /tmp/ExportOptions.plist -exportPath /tmp/export -allowProvisioningUpdates | tail -3
ls -lh /tmp/export/*.ipa
echo "下一步：scp /tmp/export/*.ipa 到 应用容器 /srv/dormroll/app/ios/，并更新 latest.json 的 version/ipa_url/manifest_url"
