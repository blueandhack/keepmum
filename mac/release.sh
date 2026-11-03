#!/usr/bin/env bash
# release.sh: a Keepmum.app that opens on any Mac (macOS 14+, Apple silicon), as a notarized DMG.
#
#   ./release.sh                  build, sign with Developer ID, notarize, staple -> dist/
#   SKIP_NOTARIZE=1 ./release.sh  the same with any identity and no notarization, to test the steps
#
# Needs, once (see mac/README.md, "Releasing"):
#   - a "Developer ID Application" certificate in the login Keychain
#   - notary credentials stored as a Keychain profile: xcrun notarytool store-credentials keepmum-notary ...
#
# The version comes from pyproject.toml. The tree must be committed, so the DMG matches a tag.
# Nothing is uploaded except to Apple's notary service; publishing on GitHub is a separate step.
set -euo pipefail
cd "$(dirname "$0")"
ROOT=$(cd .. && pwd)
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml" | head -1)
NOTARY_PROFILE=${NOTARY_PROFILE:-keepmum-notary}
APP=build/Keepmum.app
DIST=$ROOT/dist
DMG=$DIST/Keepmum-$VERSION.dmg

if [ -n "$(git -C "$ROOT" status --porcelain)" ] && [ -z "${ALLOW_DIRTY:-}" ]; then
  echo "commit your changes first (or ALLOW_DIRTY=1 for a test build)" >&2
  exit 1
fi
if [ -z "${SKIP_NOTARIZE:-}" ]; then
  SIGN_IDENTITY=${SIGN_IDENTITY:-$(security find-identity -v -p codesigning | awk -F'"' '/Developer ID Application/ {print $2; exit}')}
  if [ -z "$SIGN_IDENTITY" ]; then
    echo "no Developer ID Application certificate in the Keychain; see mac/README.md, Releasing" >&2
    exit 1
  fi
  xcrun notarytool history --keychain-profile "$NOTARY_PROFILE" >/dev/null 2>&1 \
    || { echo "no notary credentials in Keychain profile '$NOTARY_PROFILE'; see mac/README.md, Releasing" >&2; exit 1; }
fi

# notarize <file>: submit, wait, and on rejection print Apple's log.
notarize() {
  local out id status
  out=$(xcrun notarytool submit "$1" --keychain-profile "$NOTARY_PROFILE" --wait --timeout 30m --output-format plist)
  id=$(plutil -extract id raw - <<<"$out")
  status=$(plutil -extract status raw - <<<"$out")
  echo "notarization of $(basename "$1"): $status ($id)"
  if [ "$status" != "Accepted" ]; then
    xcrun notarytool log "$id" --keychain-profile "$NOTARY_PROFILE" /dev/stderr
    exit 1
  fi
}

echo "== keepmum $VERSION"
(cd "$ROOT" && uv run --frozen pytest -q -p no:cacheprovider)
rm -rf "$APP" && SIGN_IDENTITY=${SIGN_IDENTITY:-} VERSION=$VERSION SWIFT_CONFIG=release RUNTIME_CACHE=0 SKIP_RUNTIME= ./build-app.sh

# The app first, so its own ticket is stapled and it opens offline once copied out of the DMG.
if [ -z "${SKIP_NOTARIZE:-}" ]; then
  ditto -c -k --keepParent "$APP" build/Keepmum-notarize.zip
  notarize build/Keepmum-notarize.zip
  rm build/Keepmum-notarize.zip
  xcrun stapler staple -q "$APP"
fi

# The DMG: the app and a link to /Applications to drag it onto.
mkdir -p "$DIST"
stage=build/dmg
rm -rf "$stage" "$DMG" && mkdir -p "$stage"
ditto "$APP" "$stage/Keepmum.app"
ln -s /Applications "$stage/Applications"
hdiutil create -quiet -volname "keepmum $VERSION" -srcfolder "$stage" -fs APFS -format UDZO -ov "$DMG"
rm -rf "$stage"
if [ -z "${SKIP_NOTARIZE:-}" ]; then
  codesign --force --timestamp --sign "$SIGN_IDENTITY" "$DMG"
  notarize "$DMG"
  xcrun stapler staple -q "$DMG"
  # What Gatekeeper will decide on another Mac.
  spctl --assess --type execute --verbose=2 "$APP"
  spctl --assess --type open --context context:primary-signature --verbose=2 "$DMG"
  xcrun stapler validate -q "$DMG"
fi

(cd "$DIST" && shasum -a 256 "Keepmum-$VERSION.dmg" > "Keepmum-$VERSION.dmg.sha256")
ls -l "$DMG"
cat "$DIST/Keepmum-$VERSION.dmg.sha256"
echo "next: git tag v$VERSION && git push origin v$VERSION, then gh release create (see mac/README.md)"
