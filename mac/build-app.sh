#!/usr/bin/env bash
# Build build/Keepmum.app: release binary, Info.plist, icon, the bundled engine (bundle-runtime.sh), code signature.
#   SKIP_RUNTIME=1 ./build-app.sh   the dashboard alone, without the engine (faster, for UI work)
#   SWIFT_CONFIG=debug ./build-app.sh   an unoptimized dashboard: ~1 s to rebuild after an edit, not ~8
#   RUNTIME_CACHE=0 ./build-app.sh  build the engine afresh even if its inputs did not change
#   SIGN_IDENTITY="Developer ID Application: ..." ./build-app.sh   (default: first Apple Development identity, else ad-hoc)
#   BUNDLE_ID=com.example.keepmum ./build-app.sh
#   SPARKLE_FEED_URL=http://127.0.0.1:8000/appcast.xml ./build-app.sh   another update feed, to test updates
#   APP_OUT=/tmp/test/Keepmum.app ./build-app.sh   build somewhere other than build/Keepmum.app
#
# Updates (Sparkle) are on only when the build has a public key: SPARKLE_PUBLIC_KEY, or the one in
# sparkle-public-key (mac/README.md, "Releasing"). Without one the app has no updater.
set -euo pipefail
cd "$(dirname "$0")"
BUNDLE_ID=${BUNDLE_ID:-app.keepmum.Keepmum}
VERSION=${VERSION:-0.0.1}
# Apple's version fields take numbers and dots only, and Sparkle compares CFBundleVersion. keepmum's
# PEP 440 version (0.1.0a2) becomes 0.1.0 for display and a build number that rises in PEP 440 order:
# major*1000000 + minor*10000 + patch*100, plus 1-29 for alpha 1-29, 31-59 for beta, 61-89 for rc,
# 99 for the release (0.1.0a1 = 10001, 0.1.0a2 = 10002, 0.1.0 = 10099).
if [[ $VERSION =~ ^([0-9]+)\.([0-9]+)(\.([0-9]+))?((a|b|rc)([0-9]+))?$ ]]; then
  MAJOR=${BASH_REMATCH[1]} MINOR=${BASH_REMATCH[2]} PATCH=${BASH_REMATCH[4]:-0} STAGE=${BASH_REMATCH[6]} N=${BASH_REMATCH[7]:-0}
  case $STAGE in a) PRE=$N ;; b) PRE=$((30 + N)) ;; rc) PRE=$((60 + N)) ;; *) PRE=99 ;; esac
  if [ "$MINOR" -gt 99 ] || [ "$PATCH" -gt 99 ] || { [ -n "$STAGE" ] && { [ "$N" -lt 1 ] || [ "$N" -gt 29 ]; }; }; then
    echo "version $VERSION is out of the build number's range" >&2; exit 1
  fi
else
  echo "version $VERSION is not MAJOR.MINOR[.PATCH][a|b|rcN]" >&2; exit 1
fi
SHORT_VERSION=$((10#$MAJOR)).$((10#$MINOR)).$((10#$PATCH))
BUILD_NUMBER=$((MAJOR * 1000000 + MINOR * 10000 + PATCH * 100 + PRE))
SPARKLE_PUBLIC_KEY=${SPARKLE_PUBLIC_KEY:-$(cat sparkle-public-key 2>/dev/null || true)}
SPARKLE_FEED_URL=${SPARKLE_FEED_URL:-https://raw.githubusercontent.com/blueandhack/keepmum/main/mac/appcast.xml}
APP=${APP_OUT:-build/Keepmum.app}

CONFIG=${SWIFT_CONFIG:-release}
swift build -c "$CONFIG"
rm -rf "$APP" build/icon.iconset
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" build/icon.iconset
cp ".build/$CONFIG/Keepmum" "$APP/Contents/MacOS/Keepmum"
# Sparkle, without its XPC services: those are for sandboxed apps, and keepmum is not one.
mkdir -p "$APP/Contents/Frameworks"
ditto ".build/$CONFIG/Sparkle.framework" "$APP/Contents/Frameworks/Sparkle.framework"
rm -rf "$APP/Contents/Frameworks/Sparkle.framework/Versions/B/XPCServices" "$APP/Contents/Frameworks/Sparkle.framework/XPCServices"
"$APP/Contents/MacOS/Keepmum" --icon build/icon.iconset
iconutil -c icns build/icon.iconset -o "$APP/Contents/Resources/Keepmum.icns"
# Claude and Anthropic logos (see Brand/NOTICE.md).
if compgen -G "Brand/*.png" >/dev/null; then cp Brand/*.png "$APP/Contents/Resources/"; fi

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Keepmum</string>
  <key>CFBundleDisplayName</key><string>keepmum</string>
  <key>CFBundleIdentifier</key><string>${BUNDLE_ID}</string>
  <key>CFBundleExecutable</key><string>Keepmum</string>
  <key>CFBundleIconFile</key><string>Keepmum</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>${SHORT_VERSION}</string>
  <key>CFBundleVersion</key><string>${BUILD_NUMBER}</string>
  <key>KeepmumVersion</key><string>${VERSION}</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
$(if [ -n "$SPARKLE_PUBLIC_KEY" ]; then cat <<SPARKLE
  <key>SUFeedURL</key><string>${SPARKLE_FEED_URL}</string>
  <key>SUPublicEDKey</key><string>${SPARKLE_PUBLIC_KEY}</string>
  <key>SURequireSignedFeed</key><true/>
  <key>SUVerifyUpdateBeforeExtraction</key><true/>
  <key>SUEnableSystemProfiling</key><false/>
  <key>SUScheduledCheckInterval</key><integer>86400</integer>
SPARKLE
fi)
</dict>
</plist>
PLIST
[ -n "$SPARKLE_PUBLIC_KEY" ] || echo "no Sparkle public key: this build has no updater" >&2

if [ -z "${SKIP_RUNTIME:-}" ]; then
  BUNDLE_ID=$BUNDLE_ID ./bundle-runtime.sh "$APP"
fi

IDENTITY=${SIGN_IDENTITY:-$(security find-identity -v -p codesigning | awk -F'"' '/Apple Development|Developer ID Application/ {print $2; exit}')}
SIGN=(--force --sign "${IDENTITY:--}")
if [ -n "$IDENTITY" ]; then
  SIGN+=(--options runtime)
  case $IDENTITY in "Developer ID"*) SIGN+=(--timestamp) ;; esac
else
  echo "no signing identity found; signing ad-hoc" >&2
fi
# Nested code first (every Mach-O in the engine), then the app, which seals the rest. One `file`
# per batch of paths, not per file: the engine is ~1,800 files, of which ~6 are Mach-O.
if [ -d "$APP/Contents/Resources/python" ]; then
  nested=()
  while IFS= read -r f; do nested+=("$f"); done < <(
    find "$APP/Contents/Resources" -type f -exec file -N --mime-type -F $'\t' {} + | awk -F'\t' '$2 ~ /mach-binary/ {print $1}')
  [ ${#nested[@]} -gt 0 ] || { echo "no Mach-O files found in the engine" >&2; exit 1; }
  codesign "${SIGN[@]}" "${nested[@]}"
fi
# Sparkle inside out: its helper tools, then the framework.
SPARKLE_B="$APP/Contents/Frameworks/Sparkle.framework/Versions/B"
codesign "${SIGN[@]}" "$SPARKLE_B/Autoupdate" "$SPARKLE_B/Updater.app"
codesign "${SIGN[@]}" "$APP/Contents/Frameworks/Sparkle.framework"
codesign "${SIGN[@]}" "$APP"
codesign --verify --strict --deep "$APP"
# Signed, the engine's native libraries must still load (library validation checks their Team ID).
if [ -x "$APP/Contents/Resources/python/bin/python3" ]; then
  "$APP/Contents/Resources/python/bin/python3" -I -B -c 'import zstandard, keepmum.cli' \
    || { echo "the signed engine cannot load its native libraries" >&2; exit 1; }
fi
echo "built $APP, signed by: $(codesign -dv --verbose=2 "$APP" 2>&1 | awk -F= '/^Authority/ {print $2; exit}')"
