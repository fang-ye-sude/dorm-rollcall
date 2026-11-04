#!/bin/bash
# OTA 一键发布：安卓 APK / iOS Ad Hoc IPA
# 用法：
#   ./publish-ota.sh android            # 自动读 AndroidManifest 的 versionCode/versionName
#   ./publish-ota.sh ios                # 自动读 project.yml 的版本，构建并发布
#   ./publish-ota.sh both
set -e
cd "$(dirname "$0")"
# 目标主机（可选）：设了 RC_SSH_HOST 才会上传，否则只构建并打印命令
HOST=${RC_SSH_HOST:-}
CT=${RC_LXC:-}
BASE_API=${RC_BASE_API:-https://example.com/dianming/api}
ADIR=${RC_APP_DIR:-/srv/dormroll}/app/android
IDIR=${RC_APP_DIR:-/srv/dormroll}/app/ios

push() {  # push <本地> <目标路径>；没配 RC_SSH_HOST 就只打印命令
  if [ -z "$HOST" ]; then
    echo "    [dry] 需手动上传：scp \"$1\" <host>:$2"
    return 0
  fi
  scp -q "$1" "$HOST:/tmp/_ota_up" || { echo "scp 失败"; exit 1; }
  if [ -n "$CT" ]; then
    ssh "$HOST" "pct push $CT /tmp/_ota_up $2" >/dev/null
  else
    ssh "$HOST" "cp /tmp/_ota_up $2"
  fi
}

publish_android() {
  local code name file sha size
  if [ -f AndroidAppPro/app/build.gradle ]; then
    code=$(grep -o 'versionCode [0-9]*' AndroidAppPro/app/build.gradle | head -1 | tr -dc '0-9')
    name=$(grep -o 'versionName "[^"]*"' AndroidAppPro/app/build.gradle | head -1 | sed 's/.*"//; s/"//')
  else
    code=$(grep -o 'android:versionCode="[0-9]*"' AndroidApp/AndroidManifest.xml | head -1 | tr -dc '0-9')
    name=$(grep -o 'android:versionName="[^"]*"' AndroidApp/AndroidManifest.xml | head -1 | sed 's/.*="//; s/"//')
  fi
  echo "== 安卓 v$code / $name"
  local apk
  if [ -f AndroidAppPro/settings.gradle ]; then
    # 新工程（Material 3）
    ( cd AndroidAppPro && JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" \
        "$(ls -d ~/.gradle/wrapper/dists/gradle-8.14.3-all/*/gradle-8.14.3/bin/gradle | head -1)" \
        --no-daemon assembleRelease >/dev/null 2>&1 )
    apk=AndroidAppPro/app/build/outputs/apk/release/app-release.apk
  else
    ( cd AndroidApp && bash build-release.sh >/dev/null 2>&1 )
    apk=AndroidApp/build-release/DormRoll-release.apk
  fi
  file="DormRoll-$name.apk"
  sha=$(shasum -a 256 "$apk" | cut -d' ' -f1)
  size=$(stat -f%z "$apk")
  push "$apk" "$ADIR/$file"
  ssh "$HOST" "pct exec $CT -- bash -c 'cat > $ADIR/latest.json <<EOF
{\"ok\":true,\"versionCode\":$code,\"versionName\":\"$name\",\"file\":\"$file\",\"url\":\"/dianming/api/api/app/apk/$file\",\"sha256\":\"$sha\",\"size\":$size,\"notes\":\"主页可检查更新\"}
EOF'"
  echo "   已发：$file（$size 字节）"
}

publish_ios() {
  local ver build file sha size
  ver=$(grep -o 'CFBundleShortVersionString: "[^"]*"' project.yml | head -1 | sed 's/.*: "//; s/"//')
  build=$(grep -o 'CFBundleVersion: "[^"]*"' project.yml | head -1 | sed 's/.*: "//; s/"//')
  echo "== iOS v$ver ($build)"
  ./build-ios-adhoc.sh >/dev/null 2>&1
  file="DormRoll-$ver.ipa"
  sha=$(shasum -a 256 /tmp/export/DormRoll.ipa | cut -d' ' -f1)
  size=$(stat -f%z /tmp/export/DormRoll.ipa)
  push /tmp/export/DormRoll.ipa "$IDIR/$file"
  cat > /tmp/_manifest_ota.plist <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>items</key>
  <array>
    <dict>
      <key>assets</key>
      <array>
        <dict>
          <key>kind</key>
          <string>software-package</string>
          <key>url</key>
          <string>$BASE_API/api/app/ios/$file</string>
        </dict>
      </array>
      <key>metadata</key>
      <dict>
        <key>bundle-identifier</key>
        <string>com.example.dormroll</string>
        <key>bundle-version</key>
        <string>$ver</string>
        <key>kind</key>
        <string>software</string>
        <key>title</key>
        <string>宿舍点名</string>
      </dict>
    </dict>
  </array>
</dict>
</plist>
PLIST
  push /tmp/_manifest_ota.plist "$IDIR/manifest.plist"
  ssh "$HOST" "pct exec $CT -- bash -c 'cat > $IDIR/latest.json <<EOF
{\"ok\":true,\"version\":\"$ver\",\"build\":\"$build\",\"channel\":\"adhoc\",\"notes\":\"主页可检查更新\",\"ipa_url\":\"$BASE_API/api/app/ios/$file\",\"ipa_sha256\":\"$sha\",\"ipa_size\":$size,\"manifest_url\":\"itms-services://?action=download-manifest&url=$BASE_API/api/app/ios/manifest.plist\"}
EOF'"
  echo "   已发：$file（$size 字节）"
}

case "${1:-both}" in
  android) publish_android ;;
  ios) publish_ios ;;
  both) publish_android; publish_ios ;;
  *) echo "用法: $0 [android|ios|both]"; exit 1 ;;
esac
echo "完成。清单：$BASE_API/api/app/latest · $BASE_API/api/app/latest-ios"
