#!/bin/bash
# lint.sh: SwiftLint on the Mac app, with mac/.swiftlint.yml. --strict: a warning is a failure.
#
#   ./lint.sh         lint
#   ./lint.sh --fix   first apply the fixes SwiftLint can make itself, then lint
#
# The pinned release, downloaded once into build/tools (git ignores build/) and checked against the
# SHA-256 GitHub records for it before it is unpacked. To move to a new version: its tag and the
# digest of its portable_swiftlint.zip, from
#   gh api repos/realm/SwiftLint/releases/tags/<tag> --jq '.assets[] | select(.name == "portable_swiftlint.zip") | .digest'
set -euo pipefail
cd "$(dirname "$0")"

VERSION=0.65.1
SHA256=c1e429b0599cf1b516f369a2d9ec04eaf0e436f3c12b637df8851fa52ff694d0

dir=build/tools/swiftlint-$VERSION
bin=$dir/swiftlint
if [ ! -x "$bin" ]; then
  mkdir -p build/tools
  zip=build/tools/portable_swiftlint-$VERSION.zip
  curl -fsSL --retry 3 -o "$zip.part" "https://github.com/realm/SwiftLint/releases/download/$VERSION/portable_swiftlint.zip"
  if ! echo "$SHA256  $zip.part" | shasum -a 256 -c --quiet; then
    rm -f "$zip.part"
    echo "SwiftLint $VERSION: the download does not match its pinned SHA-256; not run" >&2
    exit 1
  fi
  mv "$zip.part" "$zip"
  rm -rf "$dir"
  unzip -q "$zip" -d "$dir"
fi

if [ "${1:-}" = "--fix" ]; then
  "$bin" lint --fix --quiet
fi
"$bin" lint --strict --quiet
echo "SwiftLint $VERSION: no findings"
