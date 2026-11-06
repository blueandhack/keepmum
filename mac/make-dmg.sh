#!/bin/bash
# make-dmg.sh APP DMG VOLUME: the DMG keepmum ships in. The app and a link to Applications, on a
# background that says to drag one onto the other, with the app's icon as the volume's icon.
# dmgbuild (the release dependency group, pinned in uv.lock) lays out the window without Finder,
# so this runs unattended. Used by release.sh; on its own it makes an unsigned DMG to look at.
set -euo pipefail
cd "$(dirname "$0")"
APP=$1 DMG=$2 VOLUME=$3
ROOT=$(cd .. && pwd)

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
# The background at 1x and 2x, in one TIFF so Finder picks the right one for the screen.
"$APP/Contents/MacOS/Keepmum" --dmg-background "$work"
tiffutil -cathidpicheck "$work/background.png" "$work/background@2x.png" -out "$work/background.tiff" 2>/dev/null
rm -f "$DMG"
uv run --project "$ROOT" --frozen --group release dmgbuild -s dmg-settings.py \
  -D app="$APP" -D background="$work/background.tiff" -D icon="$APP/Contents/Resources/Keepmum.icns" \
  "$VOLUME" "$DMG"

# The app in the DMG must still pass the strict signature check: Sparkle checks an update's
# signature, and Finder metadata on the bundle (a DMG setting) is enough to break it.
mnt=$(mktemp -d)
hdiutil attach -quiet -nobrowse -readonly -mountpoint "$mnt" "$DMG"
ok=0
codesign --verify --deep --strict "$mnt/$(basename "$APP")" || ok=1
hdiutil detach -quiet "$mnt"
if [ $ok -ne 0 ]; then
  echo "make-dmg.sh: the app in $DMG fails codesign --verify --strict" >&2
  exit 1
fi
