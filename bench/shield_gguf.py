"""Convert Shield Small (ZeroLeaks, CC BY-NC 4.0) from its int8 ONNX to a GGUF that llama.cpp runs.

The release has no original weights, only onnx/model_quantized.onnx: per-channel int8 MatMul weights
and uint8 embeddings. This rebuilds float weights from them ((q - zero_point) * scale, the values
ONNX Runtime multiplies by), writes a Hugging Face BertForSequenceClassification folder, and runs
llama.cpp's converter on it, with two changes to how it treats BERT:

- the pooler is kept: Shield's head is pooler.dense -> tanh -> classifier, and the converter drops
  the pooler (it expects BERT for embeddings). Named classifier.dense and classifier.out_proj, they
  map to llama.cpp's cls and cls_out, whose default activation is tanh.
- the vocabulary is XLM-R's sentencepiece (unigram), not WordPiece.

  uv run --no-project --python 3.12 --with onnx --with "numpy~=2.2.6" --with safetensors --with "torch==2.11.0" \\
      --with "transformers==4.57.6" --with "sentencepiece<0.3" --with "protobuf<5" \\
      python -I bench/shield_gguf.py SRC OUT [--type q8_0]

The versions are llama.cpp's converter requirements at the pinned commit (native/llama-server/build.sh).

SRC is a folder with Shield's pinned files (keepmum guard setup puts them in
~/.local/share/keepmum/guard/shield-small-s15e). Nothing in SRC is executed: the ONNX file is read as data.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LLAMA = ROOT / "native" / "llama-server" / ".work" / "llama.cpp"
SOURCE = "https://huggingface.co/ZeroLeaks/shield-small/tree/f6d60339a424e17b60272f2d17c4e68995e933cf"  # release s15e
# Written into the GGUF: whose model it is, its licence and where it came from.
METADATA = {
    "general.name": "Shield Small S15e", "general.basename": "shield-small", "general.version": "s15e",
    "general.organization": "ZeroLeaks", "general.quantized_by": "keepmum",
    "general.description": "Unofficial GGUF conversion of ZeroLeaks' Shield Small (S15e) prompt-injection classifier. "
                           "Not endorsed by ZeroLeaks. Non-commercial use only (CC BY-NC 4.0).",
    "general.license.name": "CC BY-NC 4.0", "general.license.link": "https://creativecommons.org/licenses/by-nc/4.0/",
    "general.source.url": SOURCE, "general.source.repo_url": "https://huggingface.co/ZeroLeaks/shield-small",
    "general.base_models": [{"name": "Shield Small", "organization": "ZeroLeaks", "version": "s15e",
                             "repo_url": "https://huggingface.co/ZeroLeaks/shield-small"}],
}


def dequantize(onnx_path: Path) -> dict:
    import numpy as np
    import onnx
    from onnx import numpy_helper

    g = onnx.load(str(onnx_path)).graph
    init = {t.name: numpy_helper.to_array(t) for t in g.initializer}
    users = defaultdict(list)
    for n in g.node:
        for i in n.input:
            users[i].append(n)

    def deq(base: str):
        q, s, z = init[base + "_quantized"], init[base + "_scale"], init[base + "_zero_point"]
        return ((q.astype(np.float32) - z.astype(np.float32)) * s.astype(np.float32)).astype(np.float32)

    out = {}
    for k, v in init.items():  # biases and norms are float and keep their names
        if v.dtype == np.float32 and v.ndim >= 1 and not k.endswith("_scale") and not k.startswith("onnx::"):
            out[k] = v
    for part in ("word", "position", "token_type"):
        out[f"bert.embeddings.{part}_embeddings.weight"] = deq(f"bert.embeddings.{part}_embeddings.weight")

    # Each MatMulInteger's weight is anonymous; its layer is the named bias the result is added to.
    for n in g.node:
        if n.op_type != "MatMulInteger":
            continue
        base = n.input[1].removesuffix("_quantized")
        seen, frontier, bias = set(), list(n.output), None
        while frontier and bias is None:
            nxt = []
            for t in frontier:
                for u in users[t]:
                    if id(u) in seen:
                        continue
                    seen.add(id(u))
                    named = [i for i in u.input if i.endswith(".bias") and i in init]
                    if u.op_type == "Add" and named:
                        bias = named[0]
                        break
                    nxt += list(u.output)
                if bias:
                    break
            frontier = nxt
        if bias is None:
            raise SystemExit(f"no bias found after {base}")
        w = bias.removesuffix(".bias") + ".weight"
        if w in out:
            raise SystemExit(f"two weights map to {w}")
        out[w] = np.ascontiguousarray(deq(base).T)  # ONNX keeps (in, out); torch Linear is (out, in)

    head = {"bert.pooler.dense.weight": "classifier.dense.weight", "bert.pooler.dense.bias": "classifier.dense.bias",
            "classifier.weight": "classifier.out_proj.weight", "classifier.bias": "classifier.out_proj.bias"}
    for old, new in head.items():
        out[new] = out.pop(old)
    return out


def check(weights: dict, config: dict) -> None:
    L, H, I = config["num_hidden_layers"], config["hidden_size"], config["intermediate_size"]
    want = {"bert.embeddings.word_embeddings.weight": (config["vocab_size"], H),
            "classifier.dense.weight": (H, H), "classifier.out_proj.weight": (len(config["id2label"]), H)}
    for i in range(L):
        p = f"bert.encoder.layer.{i}."
        want |= {p + "attention.self.query.weight": (H, H), p + "attention.self.key.weight": (H, H),
                 p + "attention.self.value.weight": (H, H), p + "attention.output.dense.weight": (H, H),
                 p + "intermediate.dense.weight": (I, H), p + "output.dense.weight": (H, I)}
    for k, shape in want.items():
        if k not in weights or tuple(weights[k].shape) != shape:
            raise SystemExit(f"{k}: expected {shape}, got {getattr(weights.get(k), 'shape', None)}")
    n = sum(v.size for v in weights.values())
    print(f"{len(weights)} tensors, {n / 1e6:.1f}M parameters", flush=True)


def convert(hf: Path, out: Path, outtype: str, metadata: Path) -> None:
    sys.path.insert(0, str(LLAMA))
    sys.path.insert(1, str(LLAMA / "gguf-py"))
    import types

    import convert_hf_to_gguf
    from conversion import bert

    bert.BertModel.set_vocab = bert.BertModel._xlmroberta_set_vocab  # XLM-R sentencepiece, positions from 0 (BERT)
    params = bert.BertModel.set_gguf_parameters

    def set_gguf_parameters(self):
        # A classifier: rank pooling (CLS token -> cls -> tanh -> cls.output), and an output as wide
        # as its labels, which llama-server's /embeddings reads (else it reads hidden_size floats).
        params(self)
        self.gguf_writer.add_pooling_type(gguf.PoolingType.RANK)
        self.gguf_writer.add_embedding_length_out(len(self.cls_out_labels))

    import gguf
    bert.BertModel.set_gguf_parameters = set_gguf_parameters

    # The converter reads the charsmap from tokenizer.json's normalizer, which here is a Sequence:
    # Precompiled, collapse runs of spaces, Metaspace. llama.cpp's sentencepiece tokenizer does the
    # last two itself (remove_extra_whitespaces, add_space_prefix), so it needs only the charsmap.
    def load(fp, **kw):
        d = json.load(fp, **kw)
        n = d.get("normalizer") if isinstance(d, dict) else None
        if isinstance(n, dict) and n.get("type") == "Sequence":
            pc = [x for x in n["normalizers"] if x.get("type") == "Precompiled"]
            if pc:
                d["normalizer"] = dict(n, precompiled_charsmap=pc[0]["precompiled_charsmap"])
        return d

    shim = types.ModuleType("json")
    shim.__dict__.update(json.__dict__)
    shim.load = load
    bert.json = shim
    sys.argv = ["convert_hf_to_gguf.py", str(hf), "--outfile", str(out), "--outtype", outtype,
                "--model-name", METADATA["general.name"], "--metadata", str(metadata)]
    convert_hf_to_gguf.main()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", type=Path, help="Shield Small's files: onnx/model_quantized.onnx, config.json, tokenizer*.json")
    ap.add_argument("out", type=Path, help="the folder to write into")
    ap.add_argument("--type", default="q8_0", help="f32, f16 or q8_0 (default)")
    a = ap.parse_args()
    from safetensors.numpy import save_file

    config = json.loads((a.src / "config.json").read_text())
    if config.get("architectures") != ["BertForSequenceClassification"] or config.get("id2label") != {"0": "BENIGN", "1": "INJECTION"}:
        raise SystemExit("not Shield Small's config")
    hf = a.out / "hf"
    hf.mkdir(parents=True, exist_ok=True)
    weights = dequantize(a.src / "onnx" / "model_quantized.onnx")
    check(weights, config)
    save_file(weights, str(hf / "model.safetensors"))
    for f in ("config.json", "tokenizer.json", "tokenizer_config.json"):
        shutil.copy(a.src / f, hf / f)
    (hf / "README.md").write_text("---\nlicense: cc-by-nc-4.0\n---\n")  # general.license is read from a model card only
    meta = a.out / "metadata.json"
    meta.write_text(json.dumps(METADATA, indent=1))
    convert(hf, a.out / f"shield-small-s15e.{a.type.upper()}.gguf", a.type, meta)


if __name__ == "__main__":
    main()
