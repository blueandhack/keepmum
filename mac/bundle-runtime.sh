#!/usr/bin/env bash
# bundle-runtime.sh <Keepmum.app>: put keepmum's engine inside the app, so it needs no other install.
#
#   Contents/Resources/python/         a relocatable CPython (python-build-standalone, via uv),
#                                      with keepmum and its dependencies in site-packages
#   Contents/Resources/bin/pf-serve    the model runtime (native/pf-serve/build.sh)
#   Contents/Resources/keepmum           `keepmum` for the app: setup, protect, status
#   Contents/Resources/keepmum-agent     what the background service runs: `keepmum serve`
#   Contents/Library/LaunchAgents/     the service's launchd plist, registered with SMAppService
#
# The bundled Python runs with -I (no PYTHONPATH, no user site-packages) and -B (never writes
# .pyc files into the signed bundle; everything is compiled here). The model itself is not
# bundled: `keepmum setup` downloads it once into ~/.local/share/keepmum.
set -euo pipefail
APP=$(cd "$1" && pwd)
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PYTHON_VERSION=${PYTHON_VERSION:-3.12.15}
BUNDLE_ID=${BUNDLE_ID:-app.keepmum.Keepmum}
RES=$APP/Contents/Resources
work=$ROOT/mac/build/runtime
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
rm -rf "$stdlib"/{test,idlelib,tkinter,turtledemo,ensurepip,lib2to3} "$stdlib"/lib-dynload/_tkinter* \
       "$RES/python"/lib/{libtcl,libtk,tcl,tk,itcl,thread}* "$RES/python"/{include,share} "$RES/python/lib/pkgconfig"
find "$RES/python" -name __pycache__ -type d -prune -exec rm -rf {} +
find "$RES/python/bin" -mindepth 1 ! -name 'python3*' -delete
rm -f "$stdlib"/EXTERNALLY-MANAGED

# keepmum and its locked dependencies (no dev or benchmark groups).
(cd "$ROOT" && uv export --frozen --no-dev --no-emit-project --no-hashes --format requirements-txt -o "$work/requirements.txt" >/dev/null)
(cd "$ROOT" && uv build --wheel -q -o "$work")
uv pip install -q --python "$py" --target "$site" --no-deps -r "$work/requirements.txt"
uv pip install -q --python "$py" --target "$site" --no-deps "$work"/keepmum-*.whl
rm -rf "$site"/bin
"$py" -I -m compileall -q -j 0 --invalidation-mode unchecked-hash "$RES/python/lib" >/dev/null

# pf-serve, built from pinned sources.
"$ROOT/native/pf-serve/build.sh" "$RES/bin" >/dev/null

cat > "$RES/keepmum" <<'SH'
#!/bin/sh
# keepmum, as bundled in Keepmum.app.
here=$(cd "$(dirname "$0")" && pwd)
KEEPMUM_PF_SERVE="$here/bin/pf-serve" exec "$here/python/bin/python3" -I -B -m keepmum "$@"
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

"$py" -I -B -c 'import keepmum, keepmum.cli, keepmum.proxy, uvicorn, httpx; print("runtime ok:", keepmum.__version__)'
du -sh "$RES/python" "$RES/bin"
