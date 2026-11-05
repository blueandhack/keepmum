"""Convert Horizon-Labs' Prompt Injection Guard Small (Apache-2.0) to a GGUF that llama.cpp runs.

The model is a ModernBertForSequenceClassification on mmBERT-small with mean pooling, which llama.cpp
supports (mean -> head dense -> GELU -> head norm -> classifier). Its weights are float safetensors,
so this only runs llama.cpp's converter, with four changes:

- the tokenizer is read as mmBERT's. Horizon-Labs' tokenizer.json is mmBERT-small's plus a
  normalizer (decode smuggled Unicode tag and variation-selector text, NFKC, strip invisible
  characters), which changes the converter's tokenizer fingerprint. Vocabulary, merges and
  pre-tokenizer are mmBERT's; check() verifies that. llama.cpp does not run the normalizer:
  keepmum.guard.normalize does, before tokenizing.
- the pre-tokenizer is written in mmBERT's form (add_prefix_space), which llama.cpp loads; the
  newer form of the same thing (prepend_scheme "always") is refused for a BPE vocabulary.
- pooling type rank, and an output as wide as the labels, so llama-server's /embeddings returns
  the two logits [SAFE, INJECTION].
- the name, licence and source are written into the GGUF.

  uv run --no-project --python 3.12 --with "numpy~=2.2.6" --with safetensors --with "torch==2.11.0" \\
      --with "transformers==4.57.6" --with "sentencepiece<0.3" --with "protobuf<5" \\
      python bench/horizon_gguf.py SRC MMBERT OUT [--type q8_0]

SRC is the model's snapshot at the pinned revision; MMBERT holds jhu-clsp/mmBERT-small's
tokenizer.json, to compare with. The versions are llama.cpp's converter requirements at the pinned
commit (native/llama-server/build.sh). Nothing in SRC is executed: its code/ folder is not used.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LLAMA = ROOT / "native" / "llama-server" / ".work" / "llama.cpp"
REPO = "Horizon-Labs/prompt-injection-guard-small"
REVISION = "9760ad5a613aa87b38c2e621189b9d06bfb6d12b"  # v2.3
SOURCE = f"https://huggingface.co/{REPO}/tree/{REVISION}"
FILES = ("config.json", "model.safetensors", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json",
         "README.md", "LICENSE", "NOTICE")
METADATA = {
    "general.name": "Prompt Injection Guard Small", "general.basename": "prompt-injection-guard-small",
    "general.version": "v2.3", "general.organization": "Horizon-Labs", "general.quantized_by": "keepmum",
    "general.description": "GGUF conversion of Horizon-Labs' Prompt Injection Guard Small (v2.3). Not made or endorsed "
                           "by Horizon-Labs. Apply its tokenizer normalizer before tokenizing (see the model card).",
    "general.license": "apache-2.0", "general.license.name": "Apache License 2.0",
    "general.license.link": "https://www.apache.org/licenses/LICENSE-2.0",
    "general.source.url": SOURCE, "general.source.repo_url": f"https://huggingface.co/{REPO}",
    "general.base_models": [{"name": "Prompt Injection Guard Small", "organization": "Horizon-Labs", "version": "v2.3",
                             "repo_url": f"https://huggingface.co/{REPO}"}],
}


def check(src: Path, mmbert: Path) -> None:
    """Horizon-Labs' tokenizer is mmBERT-small's with a normalizer in front; nothing else may differ."""
    config = json.loads((src / "config.json").read_text())
    if config.get("architectures") != ["ModernBertForSequenceClassification"] or \
            config.get("id2label") != {"0": "SAFE", "1": "INJECTION"} or config.get("classifier_pooling") != "mean":
        raise SystemExit("not Prompt Injection Guard Small's config")
    a, b = (json.loads((p / "tokenizer.json").read_text()) for p in (mmbert, src))
    merges = lambda m: [" ".join(x) if isinstance(x, list) else x for x in m["merges"]]
    if a["model"]["vocab"] != b["model"]["vocab"] or merges(a["model"]) != merges(b["model"]):
        raise SystemExit("vocabulary or merges differ from mmBERT-small's")
    pa, pb = a["pre_tokenizer"], b["pre_tokenizer"]
    if pa.get("type") != "Metaspace" or pb.get("type") != "Metaspace" or pa["replacement"] != pb["replacement"] or \
            not pa.get("add_prefix_space") or pb.get("prepend_scheme") != "always" or pb.get("split") is False:
        raise SystemExit("pre-tokenizer differs from mmBERT-small's")
    steps = b["normalizer"]["normalizers"]
    if steps[-1] != a["normalizer"]:
        raise SystemExit("the normalizer does not end in mmBERT's")
    print(f"tokenizer: mmBERT-small's, plus {len(steps) - 1} normalizer steps", flush=True)


def convert(src: Path, out: Path, outtype: str, metadata: Path) -> None:
    sys.path.insert(0, str(LLAMA))
    sys.path.insert(1, str(LLAMA / "gguf-py"))
    import convert_hf_to_gguf
    import gguf
    from conversion import base, bert

    base.TextModel.get_vocab_base_pre = lambda self, tokenizer: "mmbert"  # see check()
    params = bert.ModernBertModel.set_gguf_parameters

    def set_gguf_parameters(self):
        params(self)
        self.gguf_writer.add_pooling_type(gguf.PoolingType.RANK)
        self.gguf_writer.add_embedding_length_out(len(self.cls_out_labels))

    bert.ModernBertModel.set_gguf_parameters = set_gguf_parameters
    sys.argv = ["convert_hf_to_gguf.py", str(src), "--outfile", str(out), "--outtype", outtype,
                "--model-name", METADATA["general.name"], "--metadata", str(metadata)]
    convert_hf_to_gguf.main()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", type=Path, help=f"{REPO} at {REVISION[:8]}")
    ap.add_argument("mmbert", type=Path, help="a folder with jhu-clsp/mmBERT-small's tokenizer.json")
    ap.add_argument("out", type=Path, help="the folder to write into")
    ap.add_argument("--type", default="q8_0", help="f32, f16 or q8_0 (default)")
    a = ap.parse_args()
    check(a.src, a.mmbert)
    hf = a.out / "hf"  # only the model's files: the converter reads the folder it is given
    hf.mkdir(parents=True, exist_ok=True)
    for f in FILES:
        shutil.copyfile(a.src / f, hf / f)  # without the cache's read-only mode
    # The same pre-tokenizer in mmBERT's older form (add_prefix_space), as llama.cpp's mmbert vocab
    # expects: written as prepend_scheme "always", it is refused for a BPE vocab.
    tj = json.loads((hf / "tokenizer.json").read_text())
    tj["pre_tokenizer"] = json.loads((a.mmbert / "tokenizer.json").read_text())["pre_tokenizer"]
    (hf / "tokenizer.json").write_text(json.dumps(tj, ensure_ascii=False))
    meta = a.out / "metadata.json"
    meta.write_text(json.dumps(METADATA, indent=1))
    convert(hf, a.out / f"prompt-injection-guard-small-v2.3.{a.type.upper()}.gguf", a.type, meta)


if __name__ == "__main__":
    main()
