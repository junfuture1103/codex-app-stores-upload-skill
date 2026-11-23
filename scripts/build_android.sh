#!/usr/bin/env bash
# Build a signed release AAB for Google Play.
#
# Usage:   APP_BUILD_NUMBER=23 [APP_PUSH=1] scripts/build_android.sh
# Output:  release-artifacts/app-android-build<APP_BUILD_NUMBER>.aab
#
# Signing + keystore come from the release config (RELEASE_CONFIG, default
# .private/release-config.json, falling back to the committed
# handoff/credentials/release-config.json). No secrets are hard-coded here.
# See release/STORE_AUTOMATION_HANDOFF.md for the full flow.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

: "${APP_BUILD_NUMBER:?set APP_BUILD_NUMBER (Android versionCode, must exceed the last uploaded code)}"
export APP_BUILD_NUMBER
export APP_PUSH="${APP_PUSH:-1}"           # wires FCM when google-services.json is present
export APP_REQUIRE_RELEASE_SIGNING=1

# JDK 17 (gradle/AGP requirement). Prefer java_home, fall back to Homebrew.
export JAVA_HOME="${JAVA_HOME:-$(/usr/libexec/java_home -v 17 2>/dev/null || echo /opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home)}"
export ANDROID_HOME="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
export ANDROID_SDK_ROOT="$ANDROID_HOME"
export PATH="$JAVA_HOME/bin:$PATH"

CONFIG="${RELEASE_CONFIG:-$ROOT/.private/release-config.json}"
[ -f "$CONFIG" ] || CONFIG="$ROOT/handoff/credentials/release-config.json"
[ -f "$CONFIG" ] || { echo "release config not found (set RELEASE_CONFIG)"; exit 2; }

eval "$(python3 - "$CONFIG" <<'PY'
import json, shlex, sys
a = json.load(open(sys.argv[1]))["android"]
for k, v in [("APP_ANDROID_KEYSTORE_PATH", a["keystorePath"]),
             ("APP_ANDROID_STORE_PASSWORD", a["storePassword"]),
             ("APP_ANDROID_KEY_ALIAS", a["keyAlias"]),
             ("APP_ANDROID_KEY_PASSWORD", a["keyPassword"])]:
    print("export %s=%s" % (k, shlex.quote(v)))
PY
)"

echo "[build_android] prebuild (versionCode=$APP_BUILD_NUMBER, push=$APP_PUSH) ..."
pnpm exec expo prebuild --platform android --clean --no-install 2>&1 | tail -3
grep -q "com.google.gms.google-services" android/app/build.gradle && echo "  google-services plugin OK"
test -f android/app/google-services.json && echo "  google-services.json OK"

cd android
echo "[build_android] bundleRelease ..."
./gradlew :app:bundleRelease --no-daemon --max-workers="${APP_BUILD_JOBS:-2}" \
  -PAPP_ANDROID_KEYSTORE_PATH="$APP_ANDROID_KEYSTORE_PATH" \
  -PAPP_ANDROID_STORE_PASSWORD="$APP_ANDROID_STORE_PASSWORD" \
  -PAPP_ANDROID_KEY_ALIAS="$APP_ANDROID_KEY_ALIAS" \
  -PAPP_ANDROID_KEY_PASSWORD="$APP_ANDROID_KEY_PASSWORD" \
  -PAPP_REQUIRE_RELEASE_SIGNING=1 2>&1 | tail -12

AAB="app/build/outputs/bundle/release/app-release.aab"
OUT="$ROOT/release-artifacts/app-android-build${APP_BUILD_NUMBER}.aab"
mkdir -p "$ROOT/release-artifacts"
cp "$AAB" "$OUT"
echo "[build_android] signer SHA1:"; keytool -printcert -jarfile "$AAB" 2>/dev/null | grep SHA1 | head -1
echo "[build_android] DONE -> $OUT"
