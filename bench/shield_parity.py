"""Does Shield Small as a GGUF in llama.cpp score like the int8 ONNX keepmum runs today?

Scores every document of the injection benchmark (bench/guard.py's data) four ways, with the same
256-token windows (stride 192, at most 32) and the highest window's P(INJECTION) as the document's score:

  onnx         keepmum.guard.Shield: the int8 ONNX on ONNX Runtime, as keepmum runs it now
  torch        the weights bench/shield_gguf.py rebuilt, in PyTorch (fp32): what ONNX Runtime's
               int8 activations cost, apart from llama.cpp
  gguf-ids     each GGUF in llama-server, given the same token ids: llama.cpp's arithmetic
  gguf-tok     the same, with llama.cpp's own tokenizer (/tokenize): what a runtime without the
               tokenizers package would see

    uv run --group torch --extra guard python bench/shield_parity.py SRC OUT/shield-small.F32.gguf OUT/shield-small.Q8_0.gguf

SRC is Shield's pinned files; OUT is bench/shield_gguf.py's output folder (hf/ in it is used for torch).
Writes bench/results/shield-gguf-<stamp>.{md,json}: scores only, never text.
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
KEEPMUM_THRESHOLD = 0.92  # keepmum.guard.THRESHOLD today


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

    def tokenize(self, text: str) -> list[int]:
        return self.post("/tokenize", {"content": text, "add_special": False})["tokens"]

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

    import numpy as np
    import torch
    from safetensors.numpy import load_file
    from transformers import BertConfig, BertForSequenceClassification

    from keepmum.guard import Shield

    data = {split: bench.load(split) for split in ("dev", "holdout")}
    records = [r for split in data.values() for r in split]

    shield = Shield(a.src)
    shield._load()
    wrap = lambda body: shield._head + body + shield._tail
    windows = {r["id"]: shield.windows(shield._tok.encode(r["text"] or ".", add_special_tokens=False).ids) for r in records}

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
        ids = torch.tensor([r + [shield._pad] * (width - len(r)) for r in rows])
        mask = torch.tensor([[1] * len(r) + [0] * (width - len(r)) for r in rows])
        with torch.no_grad():
            return model(input_ids=ids, attention_mask=mask, token_type_ids=torch.zeros_like(ids)).logits.double().tolist()

    def onnx_logits(rows):
        width = max(map(len, rows))
        feed = {"input_ids": np.array([r + [shield._pad] * (width - len(r)) for r in rows], dtype=np.int64),
                "attention_mask": np.array([[1] * len(r) + [0] * (width - len(r)) for r in rows], dtype=np.int64)}
        return shield._session.run(None, {k: v for k, v in feed.items() if k in shield._inputs})[0].astype(np.float64).tolist()

    scorers = {"onnx": onnx_logits, "torch": torch_logits}
    servers = {}
    for g in a.gguf:
        tag = g.stem.split(".")[-1]  # F32, Q8_0
        servers[tag] = Server(g, a.server)
        scorers[f"gguf-ids {tag}"] = servers[tag].logits

    # per window: the logit margin (INJECTION - BENIGN) of each scorer; per document: the top P(INJECTION)
    margins = {name: [] for name in scorers}
    scores = {name: {} for name in list(scorers) + [f"gguf-tok {t}" for t in servers]}
    tok_same = tok_total = tok_trail = 0
    for r in records:
        rows = [wrap(b) for b in windows[r["id"]]]
        for name, fn in scorers.items():
            t0 = time.perf_counter()
            logits = [l for i in range(0, len(rows), 8) for l in fn(rows[i:i + 8])]
            ms = (time.perf_counter() - t0) * 1000
            margins[name].append([l[1] - l[0] for l in logits])
            scores[name][r["id"]] = {"score": max(p_injection(l) for l in logits), "ms": ms, "windows": len(rows),
                                     "chars": len(r["text"])}
        for tag, srv in servers.items():
            t0 = time.perf_counter()
            ids = srv.tokenize(r["text"] or ".")
            if tag == next(iter(servers)):  # one tokenizer, counted once
                ref = shield._tok.encode(r["text"] or ".", add_special_tokens=False).ids
                tok_total += 1
                tok_same += ids == ref
                tok_trail += ids != ref and ids == ref[:-1] and ref[-1:] == [shield._tok.token_to_id("\u2581")]
            own = [wrap(b) for b in shield.windows(ids)]
            logits = [l for i in range(0, len(own), 8) for l in srv.logits(own[i:i + 8])]
            scores[f"gguf-tok {tag}"][r["id"]] = {"score": max(p_injection(l) for l in logits),
                                                 "ms": (time.perf_counter() - t0) * 1000, "windows": len(own), "chars": len(r["text"])}
    for s in servers.values():
        s.close()

    # parity against onnx and against torch, per window
    def diff(x, y):
        d = [abs(p - q) for xs, ys in zip(margins[x], margins[y]) for p, q in zip(xs, ys)]
        return {"median": statistics.median(d), "p99": sorted(d)[int(0.99 * (len(d) - 1))], "max": max(d), "windows": len(d)}

    parity = {f"{x} vs {y}": diff(x, y) for x in margins for y in ("onnx", "torch") if x != y and not (x == "onnx" and y == "torch")}
    agree = {}
    for name in scores:
        if name == "onnx":
            continue
        same = sum((scores[name][r["id"]]["score"] >= KEEPMUM_THRESHOLD) == (scores["onnx"][r["id"]]["score"] >= KEEPMUM_THRESHOLD)
                   for r in records)
        agree[name] = {"same_verdict_at_0.92": same, "of": len(records),
                       "max_score_diff": max(abs(scores[name][r["id"]]["score"] - scores["onnx"][r["id"]]["score"]) for r in records)}

    summaries = []
    for name, sc in scores.items():
        s = bench.summarize({"detector": name, "device": "cpu" if name in ("onnx", "torch") else "metal",
                             "load_s": 0, "max_rss_mb": 0, "scores": sc}, data)
        s["holdout"]["at_0.92"] = bench.evaluate(data["holdout"], sc, KEEPMUM_THRESHOLD)
        summaries.append(s)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = {"env": bench.environment(), "tokenizer": {"identical": tok_same, "only_trailing_space_token": tok_trail, "documents": tok_total}, "parity": parity, "agree": agree,
           "summaries": summaries, "runs": [{"detector": k, "scores": v} for k, v in scores.items()]}
    (a.out / f"shield-gguf-{stamp}.json").write_text(json.dumps(out, indent=1))
    md = render(out)
    (a.out / f"shield-gguf-{stamp}.md").write_text(md)
    print(md)


def render(out: dict) -> str:
    f = lambda x: "–" if x is None else f"{x:.3f}"
    lines = [f"# Shield Small: int8 ONNX vs GGUF in llama.cpp", "",
             f"{out['env'].get('machine', '')} · llama.cpp's tokenizer: identical to Shield's on {out['tokenizer']['identical']} of "
             f"{out['tokenizer']['documents']} documents, and on {out['tokenizer']['only_trailing_space_token']} more apart from the final "
             "▁ that Shield's adds to text ending in whitespace", "",
             "## Logit margin (INJECTION − BENIGN), per window", "", "| | median | p99 | max | windows |", "| --- | --- | --- | --- | --- |"]
    lines += [f"| {k} | {v['median']:.3f} | {v['p99']:.3f} | {v['max']:.3f} | {v['windows']} |" for k, v in out["parity"].items()]
    lines += ["", "## Verdicts against ONNX at keepmum's threshold (0.92)", "", "| | same verdict | max score difference |", "| --- | --- | --- |"]
    lines += [f"| {k} | {v['same_verdict_at_0.92']} / {v['of']} | {v['max_score_diff']:.3f} |" for k, v in out["agree"].items()]
    lines += ["", "## Holdout", "",
              "| | dev threshold | AUC | recall at dev | ordinary FPR at dev | security FPR at dev | recall at 0.92 | ordinary FPR at 0.92 | security FPR at 0.92 | p50 ms |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for s in out["summaries"]:
        h, k = s["holdout"]["at_dev"], s["holdout"]["at_0.92"]
        lines.append(f"| {s['detector']} | {s['dev_threshold']:.4f} | {f(h['auc'])} | {f(h['recall'])} | {f(h['fpr_ordinary'])} | "
                     f"{f(h['fpr_security'])} | {f(k['recall'])} | {f(k['fpr_ordinary'])} | {f(k['fpr_security'])} | {s['ms_p50']:.1f} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
