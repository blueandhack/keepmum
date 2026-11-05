#!/usr/bin/env bash
# build.sh [out-dir]: llama-server for macOS on Apple silicon (Metal), from pinned sources.
# The default out-dir, ~/.local/share/keepmum/bin, is where keepmum looks for it (harm.py).
#
# llama-server (llama.cpp, MIT) runs the harm check's model, Qwen3Guard-Gen as a GGUF, in a process
# of its own. llama.cpp is fetched at LLAMA_COMMIT (tag v0.8.0, the version the benchmark ran) and
# built static, with the Metal library embedded and no network features, so the binary needs
# nothing else. cmake and ninja run through uvx, so nothing is installed system-wide.
set -euo pipefail

LLAMA_REPO=https://github.com/ggml-org/llama.cpp
LLAMA_COMMIT=8642541b255bfb5dc39cd351a178be09013a55d6

here=$(cd "$(dirname "$0")" && pwd)
dest=${1:-${XDG_DATA_HOME:-$HOME/.local/share}/keepmum/bin}
out=$(mkdir -p "$dest" && cd "$dest" && pwd)
work=${LLAMA_WORK:-$here/.work}
src=$work/llama.cpp
mkdir -p "$work"

if [ ! -d "$src/.git" ]; then
  git init -q "$src"
  git -C "$src" remote add origin "$LLAMA_REPO"
fi
if [ "$(git -C "$src" rev-parse -q --verify HEAD || true)" != "$LLAMA_COMMIT" ]; then
  git -C "$src" fetch -q --depth 1 origin "$LLAMA_COMMIT"
  git -C "$src" checkout -q --force FETCH_HEAD
fi
git -C "$src" checkout -q -- .

ninja=$(uvx --from ninja python -c "import ninja, os; print(os.path.join(ninja.BIN_DIR, 'ninja'))")
b=$work/build-darwin-arm64
uvx --from cmake cmake -S "$src" -B "$b" -G Ninja -DCMAKE_MAKE_PROGRAM="$ninja" \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=OFF -DGGML_OPENMP=OFF \
  -DCMAKE_OSX_ARCHITECTURES=arm64 -DCMAKE_OSX_DEPLOYMENT_TARGET=14.0 \
  -DGGML_METAL=ON -DGGML_METAL_EMBED_LIBRARY=ON -DGGML_BLAS=OFF \
  -DLLAMA_BUILD_SERVER=ON -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_TOOLS=ON \
  -DLLAMA_CURL=OFF -DLLAMA_OPENSSL=OFF
uvx --from cmake cmake --build "$b" -j "$(getconf _NPROCESSORS_ONLN)" --target llama-server

rm -f "$out/llama-server"
cp "$b/bin/llama-server" "$out/llama-server"
cp "$src/LICENSE" "$out/LICENSE-llama.cpp"
echo "built $out/llama-server"
