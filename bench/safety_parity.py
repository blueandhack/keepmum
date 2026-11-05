"""Does Content Safety Guard Small as a GGUF in llama.cpp score like Horizon-Labs' original?

Scores every prompt of the harm benchmark (bench/harm.py's data) three ways, with keepmum's windows
(keepmum.harm.SafetyGuard.windows) and the highest window's P(unsafe) as the prompt's score:

  torch        Horizon-Labs' weights and tokenizer in transformers: the original
  gguf-ids     each GGUF in llama-server, given the same token ids: llama.cpp's arithmetic
  keepmum      keepmum.harm.SafetyGuard on each GGUF: llama.cpp's tokenizer, keepmum's windows and
               server settings, as the harm check runs

and compares the categories a flagged prompt gets (each category at Horizon-Labs' threshold).

    uv run --group torch python bench/safety_parity.py SRC OUT/content-safety-guard-small-v1.2.F32.gguf OUT/...Q8_0.gguf

SRC is the model's snapshot at the pinned revision; OUT is bench/horizon_gguf.py's output folder
(`--model safety`). Writes bench/results/safety-gguf-<stamp>.{md,json}: scores only, never text.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bench"))
sys.path.insert(0, str(ROOT / "src"))
import harm as bench  # noqa: E402
from horizon_parity import SERVER, Server  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", type=Path)
    ap.add_argument("gguf", type=Path, nargs="+")
    ap.add_argument("--server", type=Path, default=SERVER)
    ap.add_argument("--out", type=Path, default=bench.RESULTS)
    a = ap.parse_args()

    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    from keepmum import harm
    from keepmum.guard import sigmoid

    recs = bench.load_all()
    device = bench._device()
    tok = AutoTokenizer.from_pretrained(str(a.src))
    model = AutoModelForSequenceClassification.from_pretrained(str(a.src)).to(device).eval()
    if model.config.id2label[0] != "unsafe" or [model.config.id2label[i + 1] for i in range(15)] != [c for c, _, _ in harm.SAFETY_CATEGORIES]:
        raise SystemExit(f"unexpected labels {model.config.id2label}")
    ids = lambda text: tok(text, add_special_tokens=False)["input_ids"]
    windows = {r["id"]: harm.SafetyGuard.windows(ids(r["text"])) for r in recs}

    def torch_logits(rows):
        with torch.no_grad():
            return model(input_ids=torch.tensor(rows, device=device)).logits.float().cpu().double().tolist()

    def cats(logits):
        return [name for (_, name, t), x in zip(harm.SAFETY_CATEGORIES, logits[1:]) if sigmoid(x) >= t]

    servers, scorers, runtimes = {}, {"torch": torch_logits}, {}
    for g in a.gguf:
        tag = g.stem.split(".")[-1]
        servers[tag] = _server(g, a.server)  # one window per request, as SafetyGuard runs
        scorers[f"gguf-ids {tag}"] = servers[tag].logits
        runtimes[f"keepmum {tag}"] = harm.SafetyGuard(server=a.server, model=g)

    margins = {name: [] for name in scorers}  # per window: the unsafe logit
    scores = {name: {} for name in list(scorers) + list(runtimes)}
    categories = {name: {} for name in scores}
    tok_same = 0
    for r in recs:
        rows = [[harm.BOS, *b, harm.EOS] for b in windows[r["id"]]]
        for name, fn in scorers.items():
            t0 = time.perf_counter()
            logits = [fn([row])[0] for row in rows]
            ms = (time.perf_counter() - t0) * 1000
            margins[name].append([l[0] for l in logits])
            top = max(logits, key=lambda l: l[0])
            scores[name][r["id"]] = {"score": sigmoid(top[0]), "ms": ms}
            categories[name][r["id"]] = cats(top)
        for i, (name, rt) in enumerate(runtimes.items()):
            t0 = time.perf_counter()
            s = rt.score(r["text"])
            ms = (time.perf_counter() - t0) * 1000
            scores[name][r["id"]] = {"score": s, "ms": ms}
            categories[name][r["id"]] = rt.categories(r["text"])
            if i == 0:
                tok_same += rt.tokens(r["text"]) == ids(r["text"])
    for s in servers.values():
        s.close()
    for rt in runtimes.values():
        rt.unload()

    def diff(x, y):
        d = [abs(p - q) for xs, ys in zip(margins[x], margins[y]) for p, q in zip(xs, ys)]
        return {"median": statistics.median(d), "p99": sorted(d)[int(0.99 * (len(d) - 1))], "max": max(d), "windows": len(d)}

    parity = {f"{x} vs torch": diff(x, "torch") for x in margins if x != "torch"}
    summaries = [bench.summarize({"detector": name, "device": device if name == "torch" else "metal", "load_s": 0,
                                  "max_rss_mb": 0, "scores": sc}, recs) for name, sc in scores.items()]
    t_torch = next(s["dev_threshold"] for s in summaries if s["detector"] == "torch")
    agree = {}
    for name in scores:
        if name == "torch":
            continue
        flag = lambda n, r, t: scores[n][r["id"]]["score"] >= t
        differ = [r for r in recs if flag(name, r, t_torch) != flag("torch", r, t_torch)]  # the same threshold: the runtime alone
        flagged = [r for r in recs if flag("torch", r, t_torch)]
        agree[name] = {"same_verdict": len(recs) - len(differ), "of": len(recs),
                       "differ": [(r["id"], r["label"], round(scores["torch"][r["id"]]["score"], 3), round(scores[name][r["id"]]["score"], 3))
                                  for r in differ],
                       "same_categories": sum(categories[name][r["id"]] == categories["torch"][r["id"]] for r in flagged),
                       "flagged": len(flagged),
                       "max_score_diff": max(abs(scores[name][r["id"]]["score"] - scores["torch"][r["id"]]["score"]) for r in recs)}
    for s in summaries:
        s["runtime_threshold"] = t_torch if s["detector"] == "torch" else harm.MODELS["small"][5]
        hold = [r for r in recs if r["split"] == "holdout"]
        s["holdout"]["at_runtime"] = bench.evaluate(hold, scores[s["detector"]], s["runtime_threshold"])
        s["coding_flagged_at_runtime"] = sum(scores[s["detector"]][r["id"]]["score"] >= s["runtime_threshold"]
                                             for r in recs if r["source"] == "coding")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = {"env": bench.environment(), "tokenizer": {"identical": tok_same, "prompts": len(recs)},
           "thresholds": {"torch": t_torch, "keepmum": harm.MODELS["small"][5]}, "parity": parity, "agree": agree,
           "summaries": summaries, "runs": [{"detector": k, "scores": v} for k, v in scores.items()]}
    (a.out / f"safety-gguf-{stamp}.json").write_text(json.dumps(out, indent=1))
    md = render(out)
    (a.out / f"safety-gguf-{stamp}.md").write_text(md)
    print(md)


def _server(gguf: Path, server: Path) -> Server:
    """llama-server with keepmum.harm.SafetyGuard's settings."""
    import socket
    import subprocess
    import urllib.request

    from keepmum import harm

    s = Server.__new__(Server)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    s.url = f"http://127.0.0.1:{port}"
    n = str(harm.WINDOW)
    s.proc = subprocess.Popen([str(server), "-m", str(gguf), "--host", "127.0.0.1", "--port", str(port), "--embedding",
                               "-c", n, "-b", n, "-ub", n, "-ngl", "99", "--parallel", "1", "--no-webui", "--cache-ram", "0"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(300):
        try:
            with urllib.request.urlopen(s.url + "/health", timeout=1) as r:
                if r.status == 200:
                    return s
        except OSError:
            time.sleep(0.1)
    raise SystemExit(f"llama-server did not start for {gguf}")


def render(out: dict) -> str:
    f = lambda x: "–" if x is None else f"{x:.3f}"
    t = out["thresholds"]
    lines = ["# Content Safety Guard Small: the original vs GGUF in llama.cpp", "",
             f"{out['env'].get('machine', '')} · keepmum's tokens (llama.cpp's tokenizer) identical to the model's tokenizer on "
             f"{out['tokenizer']['identical']} of {out['tokenizer']['prompts']} prompts", "",
             "## The unsafe logit, per window", "", "| | median difference | p99 | max | windows |", "| --- | --- | --- | --- | --- |"]
    lines += [f"| {k} | {v['median']:.4f} | {v['p99']:.4f} | {v['max']:.4f} | {v['windows']} |" for k, v in out["parity"].items()]
    lines += ["", f"## Verdicts against the original, all at the original's dev threshold ({t['torch']:.4f})", "",
              "| | same verdict | same categories, of the prompts the original flags | prompts that differ (label, torch → this) | max score difference |",
              "| --- | --- | --- | --- | --- |"]
    lines += [f"| {k} | {v['same_verdict']} / {v['of']} | {v['same_categories']} / {v['flagged']} | "
              f"{'; '.join(f'{i} ({l}, {a} → {b})' for i, l, a, b in v['differ']) or '–'} | {v['max_score_diff']:.4f} |"
              for k, v in out["agree"].items()]
    lines += ["", "## Holdout", "",
              "| | dev threshold | AUC | recall at dev | FPR at dev | runtime threshold | recall | FPR | XSTest safe flagged | coding flagged (all) | p50 ms |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for s in out["summaries"]:
        h, k = s["holdout"]["at_dev"], s["holdout"]["at_runtime"]
        lines.append(f"| {s['detector']} | {s['dev_threshold']:.4f} | {f(h['auc'])} | {f(h['recall'])} | {f(h['fpr'])} | "
                     f"{s['runtime_threshold']:.4f} | {f(k['recall'])} | {f(k['fpr'])} | {f(k['fpr_by_source']['xstest'])} | "
                     f"{s['coding_flagged_at_runtime']} | {s['ms_p50']:.1f} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
