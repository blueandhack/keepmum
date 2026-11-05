#!/usr/bin/env bash
# Build build/Keepmum.app: release binary, Info.plist, icon, the bundled engine (bundle-runtime.sh), code signature.
#   SKIP_RUNTIME=1 ./build-app.sh   the dashboard alone, without the engine (faster, for UI work)
#   SWIFT_CONFIG=debug ./build-app.sh   an unoptimized dashboard: ~1 s to rebuild after an edit, not ~8
#   RUNTIME_CACHE=0 ./build-app.sh  build the engine afresh even if its inputs did not change
#   SIGN_IDENTITY="Developer ID Application: ..." ./build-app.sh   (default: first Apple Development identity, else ad-hoc)
#   BUNDLE_ID=com.example.keepmum ./build-app.sh
set -euo pipefail
cd "$(dirname "$0")"
BUNDLE_ID=${BUNDLE_ID:-app.keepmum.Keepmum}
VERSION=${VERSION:-0.0.1}
# Apple's version fields take numbers and dots only: 0.1.0a1 (keepmum's PEP 440 version) is 0.1.0 there.
BUNDLE_VERSION=$(sed -E 's/^([0-9]+(\.[0-9]+)*).*/\1/' <<<"$VERSION")
APP=build/Keepmum.app

CONFIG=${SWIFT_CONFIG:-release}
swift build -c "$CONFIG"
rm -rf "$APP" build/icon.iconset
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" build/icon.iconset
cp ".build/$CONFIG/Keepmum" "$APP/Contents/MacOS/Keepmum"
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
  <key>CFBundleShortVersionString</key><string>${BUNDLE_VERSION}</string>
  <key>CFBundleVersion</key><string>${BUNDLE_VERSION}</string>
  <key>KeepmumVersion</key><string>${VERSION}</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

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
codesign "${SIGN[@]}" "$APP"
codesign --verify --strict --deep "$APP"
# Signed, the engine's native libraries must still load (library validation checks their Team ID).
if [ -x "$APP/Contents/Resources/python/bin/python3" ]; then
  "$APP/Contents/Resources/python/bin/python3" -I -B -c 'import numpy, onnxruntime, tokenizers, zstandard' \
    || { echo "the signed engine cannot load its native libraries" >&2; exit 1; }
fi
echo "built $APP, signed by: $(codesign -dv --verbose=2 "$APP" 2>&1 | awk -F= '/^Authority/ {print $2; exit}')"
