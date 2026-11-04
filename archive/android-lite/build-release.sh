#!/bin/bash
# 正式签名包：用 keystore/dianming-release.keystore 签，装到任何安卓机都能过系统校验
set -e
SDK="$HOME/Library/Android/sdk"; BT="$SDK/build-tools/36.1.0"
PLATFORM="$SDK/platforms/android-34/android.jar"
KS="keystore/dianming-release.keystore"
PASS=$(cat keystore/keystore.pass)
OUT="build-release"
rm -rf "$OUT"; mkdir -p "$OUT/gen" "$OUT/classes" "$OUT/dex"

"$BT/aapt2" compile -o "$OUT/res.zip" --dir res
"$BT/aapt2" link -o "$OUT/base.apk" -I "$PLATFORM" --manifest AndroidManifest.xml \
  -R "$OUT/res.zip" --java "$OUT/gen" --min-sdk-version 24 --target-sdk-version 34 --auto-add-overlay
find src "$OUT/gen" -name '*.java' > "$OUT/sources.txt"
javac -encoding UTF-8 -source 11 -target 11 -nowarn -classpath "$PLATFORM" -d "$OUT/classes" @"$OUT/sources.txt"
find "$OUT/classes" -name '*.class' > "$OUT/classes.txt"
"$BT/d8" --lib "$PLATFORM" --min-api 24 --release --output "$OUT/dex" @"$OUT/classes.txt"
cp "$OUT/base.apk" "$OUT/unsigned.apk"
(cd "$OUT/dex" && zip -q -u "$OLDPWD/$OUT/unsigned.apk" classes.dex)
"$BT/zipalign" -f -p 4 "$OUT/unsigned.apk" "$OUT/aligned.apk"
"$BT/apksigner" sign --ks "$KS" --ks-pass "pass:$PASS" --key-pass "pass:$PASS" \
  --v1-signing-enabled true --v2-signing-enabled true --v3-signing-enabled true \
  --out "$OUT/DormRoll-release.apk" "$OUT/aligned.apk"
"$BT/apksigner" verify --print-certs "$OUT/DormRoll-release.apk" | head -4
ls -lh "$OUT/DormRoll-release.apk"
