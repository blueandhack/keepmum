#!/usr/bin/env bash
# build.sh [out-dir]: pf-serve for macOS on Apple silicon (Metal), from pinned sources.
# The default out-dir, ~/.local/share/keepmum/bin, is where keepmum looks for it.
#
# pf-serve runs OpenAI Privacy Filter on privacy-filter.cpp (ggml) for keepmum's L2 detector
# (src/keepmum/detect/pfserve.py). pf-serve.cpp, CMakeLists.txt and patches/ come from magpie
# (MIT, LICENSE-magpie) at commit MAGPIE_COMMIT, except patches/classify-reset-error.patch,
# keepmum's own (one failed request no longer fails every later one); privacy-filter.cpp (MIT) is fetched at
# PF_COMMIT with its pinned ggml submodule. cmake and ninja run through uvx, so nothing is
# installed system-wide. Windows and Linux targets (Vulkan, CPU) come later; magpie's
# build.sh shows how.
set -euo pipefail

MAGPIE_COMMIT=642d430ecb076319b51930f4c13576ce25a8a534
PF_REPO=https://github.com/localai-org/privacy-filter.cpp
PF_COMMIT=735a6c28607ee82afc3a670383f41b55266a3b9a

here=$(cd "$(dirname "$0")" && pwd)
dest=${1:-${XDG_DATA_HOME:-$HOME/.local/share}/keepmum/bin}
out=$(mkdir -p "$dest" && cd "$dest" && pwd)
work=${PF_WORK:-$here/.work}
src=$work/privacy-filter.cpp
mkdir -p "$work"

if [ ! -d "$src/.git" ]; then
  git init -q "$src"
  git -C "$src" remote add origin "$PF_REPO"
fi
if [ "$(git -C "$src" rev-parse -q --verify HEAD || true)" != "$PF_COMMIT" ]; then
  git -C "$src" fetch -q --depth 1 origin "$PF_COMMIT"
  git -C "$src" checkout -q --force FETCH_HEAD
fi
git -C "$src" submodule update -q --init --depth 1 ggml
git -C "$src" checkout -q -- .
git -C "$src/ggml" checkout -q -- .
for p in "$here"/patches/*.patch; do
  git -C "$src" apply "$p"
done

ninja=$(uvx --from ninja python -c "import ninja, os; print(os.path.join(ninja.BIN_DIR, 'ninja'))")
b=$work/build-darwin-arm64
uvx --from cmake cmake -S "$here" -B "$b" -G Ninja -DCMAKE_MAKE_PROGRAM="$ninja" \
  -DCMAKE_BUILD_TYPE=Release -DPF_SRC="$src" -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=OFF -DGGML_OPENMP=OFF \
  -DCMAKE_OSX_ARCHITECTURES=arm64 -DCMAKE_OSX_DEPLOYMENT_TARGET=14.0 \
  -DGGML_METAL=ON -DGGML_METAL_EMBED_LIBRARY=ON -DGGML_BLAS=OFF
uvx --from cmake cmake --build "$b" -j "$(getconf _NPROCESSORS_ONLN)" --target pf-serve

rm -f "$out/pf-serve"
cp "$b/pf-serve" "$out/pf-serve"
cp "$src/LICENSE" "$out/LICENSE-privacy-filter.cpp"
cp "$src/ggml/LICENSE" "$out/LICENSE-ggml"
cp "$here/LICENSE-magpie" "$out/LICENSE-magpie"
echo "built $out/pf-serve"
