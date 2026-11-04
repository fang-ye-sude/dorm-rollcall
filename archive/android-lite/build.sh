#!/bin/bash
# 纯命令行构建（不依赖 Gradle）：aapt2 → javac → d8 → zipalign → apksigner
set -e
SDK="$HOME/Library/Android/sdk"
BT="$SDK/build-tools/36.1.0"
PLATFORM="$SDK/platforms/android-34/android.jar"
OUT="build"
rm -rf "$OUT"
mkdir -p "$OUT/gen" "$OUT/classes" "$OUT/dex"

echo "[1/5] 资源"
"$BT/aapt2" compile -o "$OUT/res.zip" --dir res
"$BT/aapt2" link -o "$OUT/base.apk" -I "$PLATFORM" \
  --manifest AndroidManifest.xml -R "$OUT/res.zip" \
  --java "$OUT/gen" --min-sdk-version 24 --target-sdk-version 34 --auto-add-overlay

echo "[2/5] 编译"
find src "$OUT/gen" -name '*.java' > "$OUT/sources.txt"
javac -encoding UTF-8 -source 11 -target 11 -nowarn \
  -classpath "$PLATFORM" -d "$OUT/classes" @"$OUT/sources.txt"

echo "[3/5] dex"
find "$OUT/classes" -name '*.class' > "$OUT/classes.txt"
"$BT/d8" --lib "$PLATFORM" --min-api 24 --output "$OUT/dex" @"$OUT/classes.txt"

echo "[4/5] 打包"
cp "$OUT/base.apk" "$OUT/unsigned.apk"
(cd "$OUT/dex" && zip -q -u "$OLDPWD/$OUT/unsigned.apk" classes.dex)
"$BT/zipalign" -f 4 "$OUT/unsigned.apk" "$OUT/aligned.apk"

echo "[5/5] 签名"
KS="$HOME/.android/debug.keystore"
if [ ! -f "$KS" ]; then
  keytool -genkeypair -v -keystore "$KS" -storepass android -alias androiddebugkey \
    -keypass android -keyalg RSA -keysize 2048 -validity 10000 \
    -dname "CN=Android Debug,O=Android,C=US" >/dev/null 2>&1
fi
"$BT/apksigner" sign --ks "$KS" --ks-pass pass:android --key-pass pass:android \
  --out "$OUT/DormRoll-android.apk" "$OUT/aligned.apk"
"$BT/apksigner" verify --print-certs "$OUT/DormRoll-android.apk" | head -3
ls -lh "$OUT/DormRoll-android.apk"
