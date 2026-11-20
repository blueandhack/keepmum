#!/usr/bin/env bash
# Delete what the Mac app's scripts build and cache. The next build makes it all again, online.
#   ./clean.sh       mac/.build    Swift's build and Sparkle's checkout (build-app.sh, tour-stills.sh)
#                    mac/build     the app, the engine and its cache (build-app.sh, bundle-runtime.sh),
#                                  SwiftLint (lint.sh), and builds made with APP_OUT
#   ./clean.sh all   that, and native/*/.work: pf-serve's and llama.cpp's sources and builds, which
#                    take longest to make again (fetched and compiled)
# dist/ (release.sh's DMGs) and uv's Pythons are left alone. To build only the engine afresh, no
# cleaning is needed: RUNTIME_CACHE=0 ./build-app.sh.
set -euo pipefail
cd "$(dirname "$0")"

case ${1:-} in
  "") paths=(.build build) ;;
  all) paths=(.build build ../native/*/.work) ;;
  *) echo "usage: $0 [all]" >&2; exit 2 ;;
esac
existing=()
for p in "${paths[@]}"; do [ -e "$p" ] && existing+=("$p"); done
if [ ${#existing[@]} -eq 0 ]; then echo "nothing to clean"; exit 0; fi
du -sh "${existing[@]}"
if pgrep -qf "^$(pwd)/build/Keepmum\.app/Contents/MacOS/Keepmum( |\$)"; then
  echo "note: build/Keepmum.app is running; it keeps running until you quit it" >&2
fi
rm -rf "${existing[@]}"
echo "cleaned"
