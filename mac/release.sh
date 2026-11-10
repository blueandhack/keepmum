#!/usr/bin/env bash
# release.sh: a Keepmum.app that opens on any Mac (macOS 14+, Apple silicon), as a notarized DMG.
#
#   ./release.sh                  build, sign with Developer ID, notarize, staple -> dist/
#   SKIP_NOTARIZE=1 ./release.sh  the same with any identity and no notarization, to test the steps
#   SKIP_LIVE=1 ./release.sh      without the live e2e run (e2e/release-check.sh: real models on API
#                                 keys from 1Password, ~$0.15), when the keys are not at hand
#   APP_OUT=build/release/Keepmum.app ./release.sh   build there, leaving build/Keepmum.app (an app in use) alone
#   SKIP_WORKER=1 ./release.sh    without checking that keepmum.app's counting Worker is deployed
#
# Needs, once (see mac/README.md, "Releasing"):
#   - a "Developer ID Application" certificate in the login Keychain
#   - notary credentials stored as a Keychain profile: xcrun notarytool store-credentials keepmum-notary ...
#   - the update signing key in the Keychain, its public half in mac/sparkle-public-key
#     ($(./sparkle-tools.sh)/generate_keys)
#
# Also adds the release to mac/appcast.xml, the signed update feed (RELEASE_NOTES=notes.md puts the
# notes in it); commit and push that after the GitHub release is published.
#
# The version comes from pyproject.toml. The tree must be committed, so the DMG matches a tag.
# Nothing is uploaded except to Apple's notary service; publishing on GitHub is a separate step.
set -euo pipefail
cd "$(dirname "$0")"
ROOT=$(cd .. && pwd)
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml" | head -1)
NOTARY_PROFILE=${NOTARY_PROFILE:-keepmum-notary}
APP=${APP_OUT:-build/Keepmum.app}
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
SPARKLE=$(./sparkle-tools.sh)
UPDATES=
if [ -s sparkle-public-key ] && [ "$("$SPARKLE/generate_keys" -p 2>/dev/null)" = "$(cat sparkle-public-key)" ]; then
  UPDATES=1
elif [ -z "${SKIP_NOTARIZE:-}" ]; then
  echo "the update signing key is not in the Keychain, or does not match mac/sparkle-public-key; see mac/README.md, Releasing" >&2
  exit 1
else
  echo "no update signing key: the DMG is not added to appcast.xml" >&2
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
# keepmum.app's counting Worker (worker/) must be live before the app that talks to it ships: a
# release can send it something new (0.1.0a7: the macOS version and chip). .github/workflows/worker.yml
# deploys it from main; this checks that a deploy succeeded at or after the last change to worker/.
if [ -z "${SKIP_WORKER:-}" ]; then
  changed=$(git -C "$ROOT" log -1 --format=%H -- worker)
  deployed=""
  for sha in $(gh run list --repo blueandhack/keepmum --workflow worker.yml --branch main --status success \
                 --limit 30 --json headSha --jq '.[].headSha' 2>/dev/null); do
    if git -C "$ROOT" merge-base --is-ancestor "$changed" "$sha" 2>/dev/null; then deployed=$sha; break; fi
  done
  if [ -z "$deployed" ]; then
    echo "keepmum.app's Worker is not deployed with the last change to worker/ (${changed:0:9}): push main and" >&2
    echo "let .github/workflows/worker.yml deploy it, or run it by hand (worker/README.md); SKIP_WORKER=1 skips this" >&2
    exit 1
  fi
  echo "keepmum.app's Worker: deployed at ${deployed:0:9}"
fi
(cd "$ROOT" && uv run --frozen pytest -q -p no:cacheprovider)
./lint.sh  # SwiftLint on the app (.swiftlint.yml): a finding stops the release, as a failing test does
# The live suite: real agents and models through keepmum (e2e/README.md). A failure stops the
# release, as a failing test does; SKIP_LIVE=1 skips it, and says so.
if [ -z "${SKIP_LIVE:-}" ]; then
  "$ROOT/e2e/release-check.sh"
else
  echo "SKIP_LIVE: the live e2e run was skipped; this release was not checked against real models" >&2
fi
rm -rf "$APP" && SIGN_IDENTITY=${SIGN_IDENTITY:-} VERSION=$VERSION SWIFT_CONFIG=release RUNTIME_CACHE=0 SKIP_RUNTIME= \
  APP_OUT=$APP ./build-app.sh

# The app first, so its own ticket is stapled and it opens offline once copied out of the DMG.
if [ -z "${SKIP_NOTARIZE:-}" ]; then
  ditto -c -k --keepParent "$APP" build/Keepmum-notarize.zip
  notarize build/Keepmum-notarize.zip
  rm build/Keepmum-notarize.zip
  xcrun stapler staple -q "$APP"
fi

# The DMG: the app and a link to /Applications to drag it onto, on a background that says so,
# with the app's icon as the volume's (make-dmg.sh).
mkdir -p "$DIST"
./make-dmg.sh "$APP" "$DMG" "keepmum $VERSION"
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

# The update feed: the DMG's EdDSA signature, the release added, then the whole feed signed.
if [ -n "$UPDATES" ]; then
  BUILD=$(plutil -extract CFBundleVersion raw "$APP/Contents/Info.plist")
  SIGNATURE=$("$SPARKLE/sign_update" -p "$DMG")
  python3 appcast.py --version "$VERSION" --build "$BUILD" --dmg "$DMG" --signature "$SIGNATURE" \
    ${RELEASE_NOTES:+--notes "$RELEASE_NOTES"}
  "$SPARKLE/sign_update" appcast.xml
  "$SPARKLE/sign_update" --verify appcast.xml
fi

echo "next: git tag -s v$VERSION -m \"keepmum $VERSION\" && git push origin v$VERSION, then gh release create (see mac/README.md),"
echo "      then commit and push mac/appcast.xml: installed apps see the update, and keepmum.app/download"
echo "      (the website's download buttons) leads to the new DMG, once it is on main"
