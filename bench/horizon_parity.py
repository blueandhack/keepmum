"""Does Prompt Injection Guard Small as a GGUF in llama.cpp score like Horizon-Labs' original?

Scores every document of the injection benchmark (bench/guard.py's data) three ways, with keepmum's
windows (keepmum.guard.InjectionModel.windows) and the highest window's P(INJECTION) as the
document's score:

  torch        Horizon-Labs' weights and tokenizer (with its normalizer) in transformers: the original
  gguf-ids     each GGUF in llama-server, given the same token ids: llama.cpp's arithmetic
  keepmum      keepmum.guard.InjectionModel on each GGUF: keepmum's normalizer, llama.cpp's
               tokenizer, keepmum's windows and server settings, as the injection check runs

    uv run --group torch python bench/horizon_parity.py SRC OUT/prompt-injection-guard-small-v2.3.F32.gguf OUT/...Q8_0.gguf

SRC is the model's snapshot at the pinned revision; OUT is bench/horizon_gguf.py's output folder.
Writes bench/results/horizon-gguf-<stamp>.{md,json}: scores only, never text.
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


def p_injection(logits) -> float:
    a, b = logits
    m = max(a, b)
    ea, eb = math.exp(a - m), math.exp(b - m)
    return eb / (ea + eb)


class Server:
    """llama-server with keepmum's settings for this model (keepmum.guard)."""

    def __init__(self, gguf: Path, server: Path):
        from keepmum import guard

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        n = str(guard.WINDOW * guard.BATCH)
        self.proc = subprocess.Popen([str(server), "-m", str(gguf), "--host", "127.0.0.1", "--port", str(port), "--embedding",
                                      "-c", n, "-b", n, "-ub", n, "-ngl", "99", "--parallel", str(guard.BATCH),
                                      "--no-webui", "--cache-ram", "0"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(300):
            try:
                with urllib.request.urlopen(self.url + "/health", timeout=1) as r:
                    if r.status == 200:
                        return
            except OSError:
                time.sleep(0.1)
        raise SystemExit(f"llama-server did not start for {gguf}")

    def logits(self, rows: list[list[int]]) -> list[list[float]]:
        req = urllib.request.Request(self.url + "/embeddings", data=json.dumps({"content": rows, "embd_normalize": -1}).encode(),
                                     headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            out = sorted(json.loads(r.read()), key=lambda e: e["index"])
        return [e["embedding"][0] for e in out]

    def close(self):
        self.proc.terminate()
        self.proc.wait(timeout=10)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", type=Path)
    ap.add_argument("gguf", type=Path, nargs="+")
    ap.add_argument("--server", type=Path, default=SERVER)
    ap.add_argument("--device", default="auto", help="for torch: auto (MPS when available), mps or cpu")
    ap.add_argument("--out", type=Path, default=bench.RESULTS)
    a = ap.parse_args()

    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    from keepmum import guard

    data = {split: bench.load(split) for split in ("dev", "holdout")}
    records = [r for split in data.values() for r in split]

    device = bench._device(a.device)
    tok = AutoTokenizer.from_pretrained(str(a.src))
    model = AutoModelForSequenceClassification.from_pretrained(str(a.src)).to(device).eval()
    if model.config.id2label != {0: "SAFE", 1: "INJECTION"}:
        raise SystemExit(f"unexpected labels {model.config.id2label}")
    ids = lambda text: tok(text, add_special_tokens=False)["input_ids"]
    windows = {r["id"]: guard.InjectionModel.windows(ids(r["text"] or ".")) for r in records}
    wrap = lambda body: [guard.BOS, *body, guard.EOS]

    def torch_logits(rows):
        width = max(map(len, rows))
        x = torch.tensor([r + [tok.pad_token_id] * (width - len(r)) for r in rows], device=device)
        mask = torch.tensor([[1] * len(r) + [0] * (width - len(r)) for r in rows], device=device)
        with torch.no_grad():
            return model(input_ids=x, attention_mask=mask).logits.float().cpu().double().tolist()

    scorers = {"torch": torch_logits}
    servers, runtimes = {}, {}
    for g in a.gguf:
        tag = g.stem.split(".")[-1]  # F32, Q8_0
        servers[tag] = Server(g, a.server)
        scorers[f"gguf-ids {tag}"] = servers[tag].logits
        runtimes[f"keepmum {tag}"] = guard.InjectionModel(g, server=a.server)

    # per window: the logit margin (INJECTION - SAFE) of each scorer; per document: the top P(INJECTION)
    margins = {name: [] for name in scorers}
    scores = {name: {} for name in list(scorers) + list(runtimes)}
    tok_same = 0
    for r in records:
        rows = [wrap(b) for b in windows[r["id"]]]
        for name, fn in scorers.items():
            t0 = time.perf_counter()
            logits = [l for i in range(0, len(rows), guard.BATCH) for l in fn(rows[i:i + guard.BATCH])]
            ms = (time.perf_counter() - t0) * 1000
            margins[name].append([l[1] - l[0] for l in logits])
            scores[name][r["id"]] = {"score": max(p_injection(l) for l in logits), "ms": ms, "windows": len(rows),
                                     "chars": len(r["text"])}
        for i, (name, rt) in enumerate(runtimes.items()):
            text = r["text"] or "."
            t0 = time.perf_counter()
            score = rt.score(text)
            ms = (time.perf_counter() - t0) * 1000
            got = rt.tokens(text)
            if i == 0:  # one tokenizer, counted once
                tok_same += got == ids(text)
            scores[name][r["id"]] = {"score": score, "ms": ms, "windows": len(guard.InjectionModel.windows(got)),
                                     "chars": len(r["text"])}
    for s in servers.values():
        s.close()
    for rt in runtimes.values():
        rt.unload()

    def diff(x, y):
        d = [abs(p - q) for xs, ys in zip(margins[x], margins[y]) for p, q in zip(xs, ys)]
        return {"median": statistics.median(d), "p99": sorted(d)[int(0.99 * (len(d) - 1))], "max": max(d), "windows": len(d)}

    parity = {f"{x} vs torch": diff(x, "torch") for x in margins if x != "torch"}
    summaries = [bench.summarize({"detector": name, "device": device if name == "torch" else "metal", "load_s": 0,
                                  "max_rss_mb": 0, "scores": sc}, data) for name, sc in scores.items()]
    t_torch = next(s["dev_threshold"] for s in summaries if s["detector"] == "torch")
    agree = {}
    for name in scores:
        if name == "torch":
            continue
        differ = [r for r in records if (scores[name][r["id"]]["score"] >= guard.THRESHOLD) != (scores["torch"][r["id"]]["score"] >= t_torch)]
        agree[name] = {"same_verdict": len(records) - len(differ), "of": len(records),
                       "differ": [(r["id"], r["label"], round(scores["torch"][r["id"]]["score"], 3), round(scores[name][r["id"]]["score"], 3))
                                  for r in differ],
                       "max_score_diff": max(abs(scores[name][r["id"]]["score"] - scores["torch"][r["id"]]["score"]) for r in records)}
    for s in summaries:
        s["runtime_threshold"] = t_torch if s["detector"] == "torch" else guard.THRESHOLD
        s["holdout"]["at_runtime"] = bench.evaluate(data["holdout"], scores[s["detector"]], s["runtime_threshold"])

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = {"env": bench.environment(), "tokenizer": {"identical": tok_same, "documents": len(records)},
           "thresholds": {"torch": t_torch, "keepmum": guard.THRESHOLD}, "parity": parity, "agree": agree,
           "summaries": summaries, "runs": [{"detector": k, "scores": v} for k, v in scores.items()]}
    (a.out / f"horizon-gguf-{stamp}.json").write_text(json.dumps(out, indent=1))
    md = render(out)
    (a.out / f"horizon-gguf-{stamp}.md").write_text(md)
    print(md)


def render(out: dict) -> str:
    f = lambda x: "–" if x is None else f"{x:.3f}"
    t = out["thresholds"]
    lines = ["# Prompt Injection Guard Small: the original vs GGUF in llama.cpp", "",
             f"{out['env'].get('machine', '')} · keepmum's tokens (its normalizer, then llama.cpp's tokenizer) identical to the "
             f"model's tokenizer on {out['tokenizer']['identical']} of {out['tokenizer']['documents']} documents", "",
             "## Logit margin (INJECTION − SAFE), per window", "", "| | median | p99 | max | windows |", "| --- | --- | --- | --- | --- |"]
    lines += [f"| {k} | {v['median']:.4f} | {v['p99']:.4f} | {v['max']:.4f} | {v['windows']} |" for k, v in out["parity"].items()]
    lines += ["", f"## Verdicts against the original (torch at its dev threshold {t['torch']:.4f}, the others at keepmum's {t['keepmum']})", "",
              "| | same verdict | documents that differ (label, torch score → this score) | max score difference |", "| --- | --- | --- | --- |"]
    lines += [f"| {k} | {v['same_verdict']} / {v['of']} | {'; '.join(f'{i} ({l}, {a} → {b})' for i, l, a, b in v['differ']) or '–'} | "
              f"{v['max_score_diff']:.4f} |" for k, v in out["agree"].items()]
    lines += ["", "## Holdout", "",
              "| | dev threshold | AUC | recall at dev | ordinary FPR at dev | security FPR at dev | runtime threshold | recall | ordinary FPR | security FPR | p50 ms |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for s in out["summaries"]:
        h, k = s["holdout"]["at_dev"], s["holdout"]["at_runtime"]
        lines.append(f"| {s['detector']} | {s['dev_threshold']:.4f} | {f(h['auc'])} | {f(h['recall'])} | {f(h['fpr_ordinary'])} | "
                     f"{f(h['fpr_security'])} | {s['runtime_threshold']:.4f} | {f(k['recall'])} | {f(k['fpr_ordinary'])} | "
                     f"{f(k['fpr_security'])} | {s['ms_p50']:.1f} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
