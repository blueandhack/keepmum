"""Convert Horizon-Labs' guards (Apache-2.0) to GGUFs that llama.cpp runs: Prompt Injection Guard
Small (`--model injection`, keepmum's injection check) and Content Safety Guard Small
(`--model safety`, the harm check's small model).

Both are ModernBertForSequenceClassification on mmBERT-small with mean pooling, which llama.cpp
supports (mean -> head dense -> GELU -> head norm -> classifier). Their weights are float
safetensors, so this only runs llama.cpp's converter, with four changes:

- the tokenizer is read as mmBERT's. Vocabulary, merges and pre-tokenizer are mmBERT-small's
  (check() verifies that), but the injection model's tokenizer.json adds a normalizer (decode
  smuggled Unicode tag and variation-selector text, NFKC, strip invisible characters), and both are
  written by a newer tokenizers, which change the converter's tokenizer fingerprint. llama.cpp
  does not run that normalizer: keepmum.guard.normalize does, before tokenizing.
- the pre-tokenizer is written in mmBERT's form (add_prefix_space), which llama.cpp loads; the
  newer form of the same thing (prepend_scheme "always") is refused for a BPE vocabulary.
- pooling type rank, and an output as wide as the labels, so llama-server's /embeddings returns
  the logits: [SAFE, INJECTION], or the safety model's 16 (unsafe, then 15 categories).
- the name, licence and source are written into the GGUF.

  uv run --no-project --python 3.12 --with "numpy~=2.2.6" --with safetensors --with "torch==2.11.0" \\
      --with "transformers==4.57.6" --with "sentencepiece<0.3" --with "protobuf<5" \\
      python bench/horizon_gguf.py SRC MMBERT OUT [--model injection|safety] [--type q8_0]

SRC is the model's snapshot at its pinned revision (MODELS); MMBERT holds jhu-clsp/mmBERT-small's
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
SAFETY_LABELS = ["unsafe", "hate", "harassment", "violence", "weapons", "sexual", "sexual_minors", "self_harm",
                 "criminal_planning", "drugs", "privacy", "profanity", "fraud_manipulation", "misinformation",
                 "unauthorized_advice", "other"]
# name: (repo, revision, version, display name, labels, what to do before tokenizing)
MODELS = {
    "injection": ("Horizon-Labs/prompt-injection-guard-small", "9760ad5a613aa87b38c2e621189b9d06bfb6d12b", "v2.3",
                  "Prompt Injection Guard Small", ["SAFE", "INJECTION"],
                  "Apply its tokenizer normalizer before tokenizing (see the model card)."),
    # keepmum's fine-tune of the injection model (bench/ft_train.py): SRC is its local folder.
    "injection-ft": ("Horizon-Labs/prompt-injection-guard-small", "9760ad5a613aa87b38c2e621189b9d06bfb6d12b", "v2.3-km1",
                     "Prompt Injection Guard Small, keepmum fine-tune", ["SAFE", "INJECTION"],
                     "Apply its tokenizer normalizer before tokenizing (see the model card). Fine-tuned by keepmum on "
                     "agent tool output (bench/ft_data.py, bench/ft_train.py)."),
    "safety": ("Horizon-Labs/content-safety-guard-small", "e2995329cff04dd2d3bcb5f97a02c6bf77d341ef", "v1.2",
               "Content Safety Guard Small", SAFETY_LABELS, "Logits: unsafe, then 15 categories; a sigmoid each."),
}
FILES = ("config.json", "model.safetensors", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json",
         "README.md", "LICENSE", "NOTICE")


def metadata(model: str) -> dict:
    repo, revision, version, name, _, note = MODELS[model]
    basename = repo.split("/")[1]
    return {
        "general.name": name, "general.basename": basename, "general.version": version,
        "general.organization": "keepmum" if model.endswith("-ft") else "Horizon-Labs", "general.quantized_by": "keepmum",
        "general.description": (f"keepmum's fine-tune of Horizon-Labs' Prompt Injection Guard Small (v2.3), as a GGUF. "
                                 f"Not made or endorsed by Horizon-Labs. {note}") if model.endswith("-ft") else
                               f"GGUF conversion of Horizon-Labs' {name} ({version}). Not made or endorsed by Horizon-Labs. {note}",
        "general.license": "apache-2.0", "general.license.name": "Apache License 2.0",
        "general.license.link": "https://www.apache.org/licenses/LICENSE-2.0",
        "general.source.url": f"https://huggingface.co/{repo}/tree/{revision}", "general.source.repo_url": f"https://huggingface.co/{repo}",
        "general.base_models": [{"name": name, "organization": "Horizon-Labs", "version": version,
                                 "repo_url": f"https://huggingface.co/{repo}"}],
    }


def check(src: Path, mmbert: Path, model: str) -> None:
    """Horizon-Labs' tokenizer is mmBERT-small's, perhaps with a normalizer in front; nothing else may differ."""
    config = json.loads((src / "config.json").read_text())
    labels = {str(i): label for i, label in enumerate(MODELS[model][4])}
    if config.get("architectures") != ["ModernBertForSequenceClassification"] or \
            config.get("id2label") != labels or config.get("classifier_pooling") != "mean":
        raise SystemExit(f"not {MODELS[model][3]}'s config")
    a, b = (json.loads((p / "tokenizer.json").read_text()) for p in (mmbert, src))
    merges = lambda m: [" ".join(x) if isinstance(x, list) else x for x in m["merges"]]
    if a["model"]["vocab"] != b["model"]["vocab"] or merges(a["model"]) != merges(b["model"]):
        raise SystemExit("vocabulary or merges differ from mmBERT-small's")
    pa, pb = a["pre_tokenizer"], b["pre_tokenizer"]
    if pa.get("type") != "Metaspace" or pb.get("type") != "Metaspace" or pa["replacement"] != pb["replacement"] or \
            not pa.get("add_prefix_space") or pb.get("prepend_scheme") != "always" or pb.get("split") is False:
        raise SystemExit("pre-tokenizer differs from mmBERT-small's")
    steps = b["normalizer"]["normalizers"] if b["normalizer"]["type"] == "Sequence" else [b["normalizer"]]
    if steps[-1] != a["normalizer"]:
        raise SystemExit("the normalizer does not end in mmBERT's")
    print(f"tokenizer: mmBERT-small's, plus {len(steps) - 1} normalizer steps", flush=True)


def convert(src: Path, out: Path, outtype: str, metadata: Path, name: str) -> None:
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
                "--model-name", name, "--metadata", str(metadata)]
    convert_hf_to_gguf.main()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", type=Path, help="the model's snapshot at its pinned revision")
    ap.add_argument("mmbert", type=Path, help="a folder with jhu-clsp/mmBERT-small's tokenizer.json")
    ap.add_argument("out", type=Path, help="the folder to write into")
    ap.add_argument("--model", default="injection", choices=MODELS, help="which of Horizon-Labs' guards (default: injection)")
    ap.add_argument("--type", default="q8_0", help="f32, f16 or q8_0 (default)")
    a = ap.parse_args()
    check(a.src, a.mmbert, a.model)
    hf = a.out / "hf"  # only the model's files: the converter reads the folder it is given
    hf.mkdir(parents=True, exist_ok=True)
    for f in FILES:
        if (a.src / f).exists():  # a fine-tune's folder has no README
            shutil.copyfile(a.src / f, hf / f)  # without the cache's read-only mode
    # The same pre-tokenizer in mmBERT's older form (add_prefix_space), as llama.cpp's mmbert vocab
    # expects: written as prepend_scheme "always", it is refused for a BPE vocab.
    tj = json.loads((hf / "tokenizer.json").read_text())
    tj["pre_tokenizer"] = json.loads((a.mmbert / "tokenizer.json").read_text())["pre_tokenizer"]
    (hf / "tokenizer.json").write_text(json.dumps(tj, ensure_ascii=False))
    meta = a.out / "metadata.json"
    meta.write_text(json.dumps(metadata(a.model), indent=1))
    repo, _, version, name, *_ = MODELS[a.model]
    convert(hf, a.out / f"{repo.split('/')[1]}-{version}.{a.type.upper()}.gguf", a.type, meta, name)
    return None


if __name__ == "__main__":
    main()
