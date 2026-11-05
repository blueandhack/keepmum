"""Does Shield Small as a GGUF in llama.cpp score like ZeroLeaks' int8 ONNX?

Scores every document of the injection benchmark (bench/guard.py's data) four ways, with the same
256-token windows (stride 192, at most 32) and the highest window's P(INJECTION) as the document's score:

  onnx         ZeroLeaks' int8 ONNX on ONNX Runtime (CPU), as keepmum ran it before the GGUF
  torch        the weights bench/shield_gguf.py rebuilt, in PyTorch (fp32): what ONNX Runtime's
               int8 activations cost, apart from llama.cpp
  gguf-ids     each GGUF in llama-server, given the same token ids: llama.cpp's arithmetic
  keepmum      keepmum.guard.Shield on each GGUF: llama.cpp's tokenizer, keepmum's windows and
               server settings, as the injection check runs now

    uv run --group torch --group onnx python bench/shield_parity.py SRC OUT/shield-small-s15e.F32.gguf OUT/shield-small-s15e.Q8_0.gguf

SRC is Shield's pinned files; OUT is bench/shield_gguf.py's output folder (hf/ in it is used for torch).
Writes bench/results/shield-gguf-<stamp>.{md,json}: scores only, never text.

keepmum's injection check has since moved to Prompt Injection Guard Small (bench/horizon_parity.py),
and keepmum.guard.Shield is gone: run this at commit f04428d, the last with it.
"""

from __future__ import annotations

import argparse
import json
import math
import socket
import statistics
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bench"))
sys.path.insert(0, str(ROOT / "src"))
import guard as bench  # noqa: E402

SERVER = ROOT / "native" / "llama-server" / ".work" / "build-darwin-arm64" / "bin" / "llama-server"
ONNX_THRESHOLD = 0.92  # the int8 ONNX's own dev threshold, which keepmum used with it


class OnnxShield:
    """ZeroLeaks' int8 ONNX on ONNX Runtime, with the tokenizers package: keepmum's runtime before the GGUF."""

    def __init__(self, path: Path):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.np = np
        self.tok = Tokenizer.from_file(str(path / "tokenizer.json"))
        self.tok.no_padding()
        self.tok.no_truncation()
        plain = self.tok.encode("a", add_special_tokens=False).ids
        wrapped = self.tok.encode("a").ids
        at = next(i for i in range(len(wrapped)) if wrapped[i:i + len(plain)] == plain)
        self.head, self.tail = wrapped[:at], wrapped[at + len(plain):]
        pad = json.loads((path / "tokenizer_config.json").read_text())["pad_token"]
        self.pad = self.tok.token_to_id(pad["content"] if isinstance(pad, dict) else pad)
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        opts.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(path / "onnx" / "model_quantized.onnx"), opts, providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.session.get_inputs()}

    def ids(self, text: str) -> list[int]:
        return self.tok.encode(text, add_special_tokens=False).ids

    def logits(self, rows):
        np = self.np
        width = max(map(len, rows))
        feed = {"input_ids": np.array([r + [self.pad] * (width - len(r)) for r in rows], dtype=np.int64),
                "attention_mask": np.array([[1] * len(r) + [0] * (width - len(r)) for r in rows], dtype=np.int64)}
        return self.session.run(None, {k: v for k, v in feed.items() if k in self.inputs})[0].astype(np.float64).tolist()


def p_injection(logits) -> float:
    a, b = logits
    m = max(a, b)
    ea, eb = math.exp(a - m), math.exp(b - m)
    return eb / (ea + eb)


class Server:
    def __init__(self, gguf: Path, server: Path):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.proc = subprocess.Popen([str(server), "-m", str(gguf), "--host", "127.0.0.1", "--port", str(port), "--embedding",
                                      "-c", "4096", "-b", "512", "-ub", "512", "-ngl", "99", "--parallel", "8",
                                      "--no-webui", "--cache-ram", "0"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(300):
            try:
                with urllib.request.urlopen(self.url + "/health", timeout=1) as r:
                    if r.status == 200:
                        return
            except OSError:
                time.sleep(0.1)
        raise SystemExit(f"llama-server did not start for {gguf}")

    def post(self, path: str, body: dict):
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())

    def logits(self, rows: list[list[int]]) -> list[list[float]]:
        out = self.post("/embeddings", {"content": rows, "embd_normalize": -1})
        out = sorted(out, key=lambda e: e["index"])
        return [e["embedding"][0] for e in out]

    def close(self):
        self.proc.terminate()
        self.proc.wait(timeout=10)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", type=Path)
    ap.add_argument("gguf", type=Path, nargs="+")
    ap.add_argument("--server", type=Path, default=SERVER)
    ap.add_argument("--out", type=Path, default=bench.RESULTS)
    a = ap.parse_args()

    import torch
    from safetensors.numpy import load_file
    from transformers import BertConfig, BertForSequenceClassification

    from keepmum import guard

    data = {split: bench.load(split) for split in ("dev", "holdout")}
    records = [r for split in data.values() for r in split]

    onnx = OnnxShield(a.src)
    wrap = lambda body: onnx.head + body + onnx.tail
    windows = {r["id"]: guard.Shield.windows(onnx.ids(r["text"] or ".")) for r in records}

    # torch: the rebuilt weights under their original names
    hf = a.gguf[0].parent / "hf"
    sd = load_file(str(hf / "model.safetensors"))
    back = {"classifier.dense.weight": "bert.pooler.dense.weight", "classifier.dense.bias": "bert.pooler.dense.bias",
            "classifier.out_proj.weight": "classifier.weight", "classifier.out_proj.bias": "classifier.bias"}
    sd = {back.get(k, k): torch.from_numpy(v) for k, v in sd.items()}
    model = BertForSequenceClassification(BertConfig.from_pretrained(str(hf)))
    missing, unexpected = model.load_state_dict(sd, strict=False)
    missing = [m for m in missing if not m.endswith("position_ids")]
    if missing or unexpected:
        raise SystemExit(f"torch weights: missing {missing}, unexpected {unexpected}")
    model.eval()

    def torch_logits(rows):
        width = max(map(len, rows))
        ids = torch.tensor([r + [onnx.pad] * (width - len(r)) for r in rows])
        mask = torch.tensor([[1] * len(r) + [0] * (width - len(r)) for r in rows])
        with torch.no_grad():
            return model(input_ids=ids, attention_mask=mask, token_type_ids=torch.zeros_like(ids)).logits.double().tolist()

    scorers = {"onnx": onnx.logits, "torch": torch_logits}
    servers, runtimes = {}, {}
    for g in a.gguf:
        tag = g.stem.split(".")[-1]  # F32, Q8_0
        servers[tag] = Server(g, a.server)
        scorers[f"gguf-ids {tag}"] = servers[tag].logits
        runtimes[f"keepmum {tag}"] = guard.Shield(g, server=a.server)

    # per window: the logit margin (INJECTION - BENIGN) of each scorer; per document: the top P(INJECTION)
    margins = {name: [] for name in scorers}
    scores = {name: {} for name in list(scorers) + list(runtimes)}
    tok_same = tok_total = 0
    for r in records:
        rows = [wrap(b) for b in windows[r["id"]]]
        for name, fn in scorers.items():
            t0 = time.perf_counter()
            logits = [l for i in range(0, len(rows), 8) for l in fn(rows[i:i + 8])]
            ms = (time.perf_counter() - t0) * 1000
            margins[name].append([l[1] - l[0] for l in logits])
            scores[name][r["id"]] = {"score": max(p_injection(l) for l in logits), "ms": ms, "windows": len(rows),
                                     "chars": len(r["text"])}
        for i, (name, rt) in enumerate(runtimes.items()):
            text = r["text"] or "."
            t0 = time.perf_counter()
            score = rt.score(text)
            ms = (time.perf_counter() - t0) * 1000
            ids = rt.tokens(text)
            if i == 0:  # one tokenizer, counted once
                tok_total += 1
                tok_same += ids == onnx.ids(text)
            scores[name][r["id"]] = {"score": score, "ms": ms, "windows": len(guard.Shield.windows(ids)), "chars": len(r["text"])}
    for s in servers.values():
        s.close()
    for rt in runtimes.values():
        rt.unload()

    # parity against onnx and against torch, per window
    def diff(x, y):
        d = [abs(p - q) for xs, ys in zip(margins[x], margins[y]) for p, q in zip(xs, ys)]
        return {"median": statistics.median(d), "p99": sorted(d)[int(0.99 * (len(d) - 1))], "max": max(d), "windows": len(d)}

    parity = {f"{x} vs {y}": diff(x, y) for x in margins for y in ("onnx", "torch") if x != y and not (x == "onnx" and y == "torch")}
    # verdicts, each runtime at its own threshold: the ONNX at 0.92, the GGUF at keepmum's
    at = lambda name: ONNX_THRESHOLD if name == "onnx" else guard.THRESHOLD
    agree = {}
    for name in scores:
        if name == "onnx":
            continue
        differ = [r for r in records if (scores[name][r["id"]]["score"] >= at(name)) != (scores["onnx"][r["id"]]["score"] >= ONNX_THRESHOLD)]
        agree[name] = {"same_verdict": len(records) - len(differ), "of": len(records),
                       "differ": [(r["id"], r["label"], round(scores["onnx"][r["id"]]["score"], 3), round(scores[name][r["id"]]["score"], 3))
                                  for r in differ],
                       "max_score_diff": max(abs(scores[name][r["id"]]["score"] - scores["onnx"][r["id"]]["score"]) for r in records)}

    summaries = []
    for name, sc in scores.items():
        s = bench.summarize({"detector": name, "device": "cpu" if name in ("onnx", "torch") else "metal",
                             "load_s": 0, "max_rss_mb": 0, "scores": sc}, data)
        s["runtime_threshold"] = at(name)
        s["holdout"]["at_runtime"] = bench.evaluate(data["holdout"], sc, at(name))
        summaries.append(s)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = {"env": bench.environment(), "tokenizer": {"identical": tok_same, "documents": tok_total},
           "thresholds": {"onnx": ONNX_THRESHOLD, "keepmum": guard.THRESHOLD}, "parity": parity, "agree": agree,
           "summaries": summaries, "runs": [{"detector": k, "scores": v} for k, v in scores.items()]}
    (a.out / f"shield-gguf-{stamp}.json").write_text(json.dumps(out, indent=1))
    md = render(out)
    (a.out / f"shield-gguf-{stamp}.md").write_text(md)
    print(md)


def render(out: dict) -> str:
    f = lambda x: "–" if x is None else f"{x:.3f}"
    t = out["thresholds"]
    lines = ["# Shield Small: int8 ONNX vs GGUF in llama.cpp", "",
             f"{out['env'].get('machine', '')} · keepmum's tokens (llama.cpp's tokenizer, plus the final ▁ Shield's adds to text "
             f"ending in whitespace) identical to Shield's tokenizer on {out['tokenizer']['identical']} of {out['tokenizer']['documents']} documents", "",
             "## Logit margin (INJECTION − BENIGN), per window", "", "| | median | p99 | max | windows |", "| --- | --- | --- | --- | --- |"]
    lines += [f"| {k} | {v['median']:.3f} | {v['p99']:.3f} | {v['max']:.3f} | {v['windows']} |" for k, v in out["parity"].items()]
    lines += ["", f"## Verdicts against ONNX (ONNX at {t['onnx']}, the others at keepmum's {t['keepmum']})", "",
              "| | same verdict | documents that differ (label, ONNX score → this score) | max score difference |", "| --- | --- | --- | --- |"]
    lines += [f"| {k} | {v['same_verdict']} / {v['of']} | {'; '.join(f'{i} ({l}, {a} → {b})' for i, l, a, b in v['differ']) or '–'} | "
              f"{v['max_score_diff']:.3f} |" for k, v in out["agree"].items()]
    lines += ["", "## Holdout", "",
              "| | dev threshold | AUC | recall at dev | ordinary FPR at dev | security FPR at dev | runtime threshold | recall | ordinary FPR | security FPR | p50 ms |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for s in out["summaries"]:
        h, k = s["holdout"]["at_dev"], s["holdout"]["at_runtime"]
        lines.append(f"| {s['detector']} | {s['dev_threshold']:.4f} | {f(h['auc'])} | {f(h['recall'])} | {f(h['fpr_ordinary'])} | "
                     f"{f(h['fpr_security'])} | {s['runtime_threshold']} | {f(k['recall'])} | {f(k['fpr_ordinary'])} | {f(k['fpr_security'])} | {s['ms_p50']:.1f} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
