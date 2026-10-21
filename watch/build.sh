#!/usr/bin/env bash
# Builds the Wristle watch APK without Gradle.
#
# Needs: JDK (javac, keytool), aapt, dx, zipalign, apksigner and an android.jar.
#   Ubuntu/Debian/WSL:
#     sudo apt install default-jdk aapt apksigner zipalign dalvik-exchange android-sdk-platform-23
#
# Signing key (never commit it):
#   KEYSTORE       path to the keystore   (default: ~/.wristle/release.keystore)
#   KEYSTORE_PASS  its password           (default: read from ~/.wristle/keystore-password.txt)
# If neither exists, a new key is created there. Keep a backup: every future
# update must be signed with the same key or it won't install over the old app.
set -euo pipefail
cd "$(dirname "$0")"

for d in /usr/lib/android-sdk/build-tools/*/; do PATH="$d:$PATH"; done
ANDROID_JAR="${ANDROID_JAR:-/usr/lib/android-sdk/platforms/android-23/android.jar}"
DX="$(command -v dx || command -v dalvik-exchange)"
KEY_DIR="$HOME/.wristle"
KEYSTORE="${KEYSTORE:-$KEY_DIR/release.keystore}"
PASS_FILE="$KEY_DIR/keystore-password.txt"
VERSION="$(sed -n 's/.*android:versionName="\([^"]*\)".*/\1/p' AndroidManifest.xml)"
OUT=build
APK="$OUT/wristle-$VERSION.apk"

rm -rf "$OUT" && mkdir -p "$OUT/gen" "$OUT/classes"

echo "1/5 resources"
aapt package -f -m -J "$OUT/gen" -M AndroidManifest.xml -S res -I "$ANDROID_JAR" -F "$OUT/app.unsigned.apk"

echo "2/5 compile"
javac -encoding UTF-8 -source 8 -target 8 -Xlint:-options -nowarn \
  -bootclasspath "$ANDROID_JAR" -d "$OUT/classes" \
  $(find src "$OUT/gen" -name '*.java')

echo "3/5 dex"
"$DX" --dex --min-sdk-version=26 --output="$OUT/classes.dex" "$OUT/classes"

echo "4/5 package"
(cd "$OUT" && aapt add -f app.unsigned.apk classes.dex >/dev/null)
zipalign -f -p 4 "$OUT/app.unsigned.apk" "$OUT/app.aligned.apk"

echo "5/5 sign"
if [ -z "${KEYSTORE_PASS:-}" ]; then
  if [ -f "$PASS_FILE" ]; then
    KEYSTORE_PASS="$(cat "$PASS_FILE")"
  elif [ ! -f "$KEYSTORE" ]; then
    mkdir -p "$KEY_DIR" && chmod 700 "$KEY_DIR"
    KEYSTORE_PASS="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)"
    printf '%s' "$KEYSTORE_PASS" > "$PASS_FILE" && chmod 600 "$PASS_FILE"
  else
    echo "Set KEYSTORE_PASS for $KEYSTORE" >&2; exit 1
  fi
fi
export KEYSTORE_PASS  # keytool/apksigner read it from the environment, so it never shows up in `ps`
if [ ! -f "$KEYSTORE" ]; then
  echo "    creating a new signing key at $KEYSTORE (back it up!)"
  mkdir -p "$(dirname "$KEYSTORE")"
  keytool -genkeypair -keystore "$KEYSTORE" -storetype PKCS12 \
    -storepass:env KEYSTORE_PASS -keypass:env KEYSTORE_PASS \
    -alias wristle -keyalg RSA -keysize 3072 -validity 10000 \
    -dname "CN=Wristle" >/dev/null 2>&1
fi
apksigner sign --ks "$KEYSTORE" --ks-pass env:KEYSTORE_PASS --ks-key-alias wristle \
  --min-sdk-version 30 --v2-signing-enabled true --v3-signing-enabled true \
  --out "$APK" "$OUT/app.aligned.apk"
rm -f "$APK.idsig"
apksigner verify "$APK"
echo "Built $(pwd)/$APK"
