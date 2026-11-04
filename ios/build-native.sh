#!/bin/bash
# B座点名 · 原生客户端（SwiftUI）打包签名
# 用法: ./build-native.sh <profile.mobileprovision>
set -euo pipefail
cd "$(dirname "$0")"

CERT="Apple Development: ye fang (RV4S45292R)"
PROFILE="${1:?用法: ./build-native.sh <profile.mobileprovision>}"
SDK="$(xcrun --sdk iphoneos --show-sdk-path)"
APP=build/DormRollNative.app

echo "== 1. 编译 SwiftUI 源码 =="
mkdir -p build
xcrun --sdk iphoneos swiftc \
  -module-name DormRoll \
  -target arm64-apple-ios17.0 \
  -sdk "$SDK" \
  -O \
  -o build/DormRollNative \
  DormRollNative/Sources/*.swift \
  -framework UIKit -framework SwiftUI
echo "  ✓ $(ls -lh build/DormRollNative | awk '{print $5}')"

echo "== 2. 拼装 .app =="
rm -rf "$APP"
mkdir -p "$APP"
cp build/DormRollNative "$APP/DormRoll"
cp DormRoll/Info.plist "$APP/Info.plist"
cp icons/*.png "$APP/" 2>/dev/null || true
# 内置七层名册：首次安装/断网也有表可看
mkdir -p "$APP/Roster"
cp DormRoll/bundle/*.json "$APP/Roster/" 2>/dev/null || true
chmod 755 "$APP/DormRoll"
plutil -lint "$APP/Info.plist"

echo "== 3. 签名 =="
cp "$PROFILE" "$APP/embedded.mobileprovision"
codesign --force --sign "$CERT" --timestamp=none \
  --entitlements ents_dorm.plist --generate-entitlement-der "$APP"
codesign -vv "$APP"
echo "✅ DormRoll.app（原生）已签讫: $APP"
echo ""
echo "装机：xcrun devicectl device install app --device <UDID> $APP"