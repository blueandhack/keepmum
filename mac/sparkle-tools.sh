#!/usr/bin/env bash
# sparkle-tools.sh: Sparkle's command-line tools (generate_keys, sign_update), pinned like the
# framework in Package.swift, downloaded once into mac/.sparkle and checked against their SHA-256.
# Prints the folder holding them.
set -euo pipefail
cd "$(dirname "$0")"
VERSION=2.10.1
SHA256=1a856d2a9edb8e628de88121d7be04cf0e2167f8641650b78fca00b14244cf27  # GitHub's digest of the release asset
DIR=.sparkle/$VERSION
if [ ! -x "$DIR/bin/sign_update" ]; then
  mkdir -p "$DIR"
  curl -fsSL -o "$DIR.tar.xz" "https://github.com/sparkle-project/Sparkle/releases/download/$VERSION/Sparkle-$VERSION.tar.xz"
  echo "$SHA256  $DIR.tar.xz" | shasum -a 256 -c --quiet - || { rm -f "$DIR.tar.xz"; echo "Sparkle $VERSION does not match its SHA-256" >&2; exit 1; }
  tar -xf "$DIR.tar.xz" -C "$DIR" bin LICENSE
  rm "$DIR.tar.xz"
fi
echo "$PWD/$DIR/bin"
