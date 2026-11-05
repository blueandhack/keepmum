#!/usr/bin/env bash
# bundle-runtime.sh <Keepmum.app>: put keepmum's engine inside the app, so it needs no other install.
#
#   Contents/Resources/python/         a relocatable CPython (python-build-standalone, via uv),
#                                      with keepmum and its dependencies in site-packages
#   Contents/Resources/bin/pf-serve    the model runtime (native/pf-serve/build.sh)
#   Contents/Resources/bin/llama-server  the harm check's runtime (native/llama-server/build.sh)
#   Contents/Resources/keepmum           `keepmum` for the app: setup, protect, status
#   Contents/Resources/keepmum-agent     what the background service runs: `keepmum serve`
#   Contents/Library/LaunchAgents/     the service's launchd plist, registered with SMAppService
#
# The bundled Python runs with -I (no PYTHONPATH, no user site-packages) and -B (never writes
# .pyc files into the signed bundle; everything is compiled here). No model is bundled: `keepmum
# setup` downloads the privacy filter once into ~/.local/share/keepmum, and the optional checks'
# models come only with `keepmum guard setup`. Their runtimes are bundled: ONNX Runtime (the guard
# extra) and llama-server.
set -euo pipefail
APP=$(cd "$1" && pwd)
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PYTHON_VERSION=${PYTHON_VERSION:-3.12.15}
BUNDLE_ID=${BUNDLE_ID:-app.keepmum.Keepmum}
RES=$APP/Contents/Resources
work=$ROOT/mac/build/runtime

# A build with the same inputs as the last one reuses its engine: they are hashed (keepmum's
# source, the lock file, pf-serve's sources, this script, the Python version, the bundle id).
# RUNTIME_CACHE=0 builds it afresh; release.sh always does.
cache=$ROOT/mac/build/runtime-cache
key=$( (cd "$ROOT" && echo "$PYTHON_VERSION $BUNDLE_ID" \
  && find mac/bundle-runtime.sh pyproject.toml uv.lock src native/pf-serve native/llama-server \
       -path native/pf-serve/.work -prune -o -path native/llama-server/.work -prune \
       -o -name __pycache__ -prune -o -type f -print0 | LC_ALL=C sort -z | xargs -0 shasum) | shasum | cut -c1-16)
if [ "${RUNTIME_CACHE:-1}" != 0 ] && [ "$(cat "$cache/key" 2>/dev/null)" = "$key" ]; then
  rm -rf "$RES/python" "$RES/bin"
  ditto "$cache/Contents" "$APP/Contents"
  echo "engine unchanged since the last build ($key): reused"
  exit 0
fi
rm -rf "$work" "$RES/python" "$RES/bin" && mkdir -p "$work" "$RES/bin" "$APP/Contents/Library/LaunchAgents"

# CPython: a pinned uv-managed build (no shims added to ~/.local/bin), copied whole, minus what
# keepmum never uses. The interpreter names its own install directory.
uv python install --no-bin "$PYTHON_VERSION" >/dev/null
src_py=$(uv python find --managed-python --no-project "$PYTHON_VERSION")
prefix=$("$src_py" -I -c 'import sys; print(sys.base_prefix)')
case $prefix in */cpython-"$PYTHON_VERSION"-macos-aarch64-*) ;; *) echo "unexpected Python prefix: $prefix" >&2; exit 1 ;; esac
ditto "$prefix" "$RES/python"
py=$RES/python/bin/python3
stdlib=$("$py" -I -c 'import sysconfig; print(sysconfig.get_paths()["stdlib"])')
site=$("$py" -I -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
rm -rf "$stdlib"/{test,idlelib,tkinter,turtledemo,ensurepip,lib2to3,pydoc_data} "$stdlib"/lib-dynload/_tkinter* \
       "$RES/python"/lib/{libtcl,libtk,tcl,tk,itcl,thread}* "$RES/python"/{include,share} "$RES/python/lib/pkgconfig"
find "$RES/python" -name __pycache__ -type d -prune -exec rm -rf {} +
find "$RES/python/bin" -mindepth 1 ! -name 'python3*' -delete
rm -f "$stdlib"/EXTERNALLY-MANAGED

# keepmum and its locked dependencies (no dev or benchmark groups). Not rich and what it pulls in
# (about 14 MB): typer's boxed help and tracebacks, off with TYPER_USE_RICH=0 below. Plain errors
# also suit the app, which shows a failed command's last line.
(cd "$ROOT" && uv export --frozen --no-dev --extra guard --no-emit-project --no-hashes --format requirements-txt -o "$work/all.txt" >/dev/null)
# The guard extra's packages come without what only their own tooling uses: tokenizers' Hugging Face
# hub client (keepmum loads a tokenizer from a file) and onnxruntime's protobuf and flatbuffers.
grep -vE '^(rich|pygments|markdown-it-py|mdurl|huggingface-hub|hf-xet|fsspec|filelock|tqdm|pyyaml|httpx2|httpx2-jsfetch|httpcore2|truststore|protobuf|flatbuffers)==' \
  "$work/all.txt" > "$work/requirements.txt"
(cd "$ROOT" && uv build --wheel -q -o "$work")
uv pip install -q --python "$py" --target "$site" --no-deps -r "$work/requirements.txt"
uv pip install -q --python "$py" --target "$site" --no-deps "$work"/keepmum-*.whl
rm -rf "$site"/bin "$site"/pip "$site"/pip-*.dist-info
# onnxruntime's model tooling (transformer optimizers, quantization, converters): the runtime never imports it.
rm -rf "$site"/onnxruntime/{transformers,quantization,tools}
# Packages' own test suites (numpy's alone is ~12 MB).
find "$site" -type d -name tests -prune -exec rm -rf {} +
# arm64 only: a universal binary's other slice is dead weight, and codesign fails on some of them.
find "$RES/python" -type f \( -name '*.so' -o -name '*.dylib' \) -print0 | while IFS= read -r -d '' f; do
  if [ "$(lipo -archs "$f" 2>/dev/null)" != arm64 ] && lipo -archs "$f" 2>/dev/null | grep -qw arm64; then
    lipo -thin arm64 "$f" -output "$f.thin" && mv "$f.thin" "$f"
  fi
done
# The interpreter is linked statically: the shared libpython (17 MB) is never loaded.
otool -L "$RES/python/bin/python3" | grep -q libpython || rm -f "$RES/python/lib"/libpython3*.dylib "$RES/python/bin"/python3*-config
"$py" -I -m compileall -q -j 0 --invalidation-mode unchecked-hash "$RES/python/lib" >/dev/null

# pf-serve and llama-server, built from pinned sources.
"$ROOT/native/pf-serve/build.sh" "$RES/bin" >/dev/null
"$ROOT/native/llama-server/build.sh" "$RES/bin" >/dev/null

cat > "$RES/keepmum" <<'SH'
#!/bin/sh
# keepmum, as bundled in Keepmum.app.
here=$(cd "$(dirname "$0")" && pwd)
TYPER_USE_RICH=0 KEEPMUM_PF_SERVE="$here/bin/pf-serve" KEEPMUM_LLAMA_SERVER="$here/bin/llama-server" \
  exec "$here/python/bin/python3" -I -B -m keepmum "$@"
SH
cat > "$RES/keepmum-agent" <<'SH'
#!/bin/sh
# The background service (launchd, via SMAppService): the proxy on 127.0.0.1, logging to a file.
here=$(cd "$(dirname "$0")" && pwd)
exec "$here/keepmum" serve --log-file
SH
chmod 755 "$RES/keepmum" "$RES/keepmum-agent"

cat > "$APP/Contents/Library/LaunchAgents/app.keepmum.agent.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>app.keepmum.agent</string>
  <key>BundleProgram</key><string>Contents/Resources/keepmum-agent</string>
  <key>AssociatedBundleIdentifiers</key><array><string>${BUNDLE_ID}</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Interactive</string>
</dict>
</plist>
PLIST

"$py" -I -B -c 'import keepmum, keepmum.cli, keepmum.proxy, keepmum.harm, uvicorn, httpx, onnxruntime, tokenizers, numpy; print("runtime ok:", keepmum.__version__)'
# Help and a usage error, which are what would need rich.
HOME=$work "$RES/keepmum" --help >/dev/null
HOME=$work "$RES/keepmum" tools set >/dev/null 2>&1 && exit 1 || [ $? -eq 2 ]
du -sh "$RES/python" "$RES/bin"

if [ "${RUNTIME_CACHE:-1}" != 0 ]; then  # unsigned: build-app.sh signs the copy in the app
  rm -rf "$cache" && mkdir -p "$cache/Contents/Resources" "$cache/Contents/Library"
  for f in python bin keepmum keepmum-agent; do ditto "$RES/$f" "$cache/Contents/Resources/$f"; done
  ditto "$APP/Contents/Library/LaunchAgents" "$cache/Contents/Library/LaunchAgents"
  echo "$key" > "$cache/key"
fi
