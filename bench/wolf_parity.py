"""Does Wolf Defender Small as a GGUF in llama.cpp veto keepmum's hits like Patronus' original?

bench/guard_both.py found that a hit of keepmum's injection check, confirmed only when Wolf Defender Small
also scores it at a small threshold (0.001-0.003), keeps most catches and drops nearly every false alarm.
Thresholds that small sit where safe text scores, so this checks the GGUF there. Documents: every one of
guard_public.py's that keepmum flags at High (the widest level; a saved run gives keepmum's scores), and a
sample of the rest of the coding-agent output. Each is scored:

  torch        Patronus' weights and tokenizer in transformers: the original
  gguf-ids     each GGUF in llama-server, given the same token ids: llama.cpp's arithmetic
  keepmum      keepmum.guard.InjectionModel on each GGUF: keepmum's normalizer, llama.cpp's tokenizer,
               keepmum's windows and server settings, as a second check would run

All in keepmum's windows, a document scoring its highest window's P(INJECTION).

    uv run --group torch python bench/wolf_parity.py SRC bench/results/guard-public-<stamp>.json OUT/*.F16.gguf OUT/*.Q8_0.gguf

SRC is the model's snapshot (bench/horizon_gguf.py MODELS["wolf-small"]). Writes
bench/results/wolf-gguf-<stamp>.{md,json}: scores only, never text.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bench"))
sys.path.insert(0, str(ROOT / "src"))
import guard as bench  # noqa: E402
import guard_public as gp  # noqa: E402
from horizon_parity import SERVER, Server, p_injection  # noqa: E402

VETOES = (0.01, 0.003, 0.001)
SAMPLE = 600  # unflagged coding-agent output, for the low end in general


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", type=Path)
    ap.add_argument("public", type=Path, help="a guard_public.py result with a keepmum run")
    ap.add_argument("gguf", type=Path, nargs="+")
    ap.add_argument("--server", type=Path, default=SERVER)
    ap.add_argument("--device", default="auto")
    ap.add_argument("--out", type=Path, default=bench.RESULTS)
    a = ap.parse_args()

    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    from keepmum import guard

    km = next(r["scores"] for r in json.loads(a.public.read_text())["runs"] if r["detector"] == "keepmum")
    recs = [r for r in gp.load() if r["id"] in km]

    def flags(r, level):
        lv = guard.LEVELS[level]
        return km[r["id"]]["score"] >= (lv.short if len(r["text"]) < guard.SHORT_CHARS else lv.threshold)

    hits = [r for r in recs if flags(r, "high")]
    rest = [r for r in recs if not flags(r, "high") and r["source"] in gp.CODING]
    docs = hits + random.Random(7).sample(rest, min(SAMPLE, len(rest)))
    print(f"{len(hits)} flagged at High, {len(docs) - len(hits)} sampled", flush=True)

    device = bench._device(a.device)
    tok = AutoTokenizer.from_pretrained(str(a.src))
    model = AutoModelForSequenceClassification.from_pretrained(str(a.src)).to(device).eval()
    if model.config.id2label != {0: "BENIGN", 1: "INJECTION"}:
        raise SystemExit(f"unexpected labels {model.config.id2label}")
    ids = lambda text: tok(text, add_special_tokens=False)["input_ids"]
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
        tag = g.stem.split(".")[-1]  # F16, Q8_0
        servers[tag] = Server(g, a.server)
        scorers[f"gguf-ids {tag}"] = servers[tag].logits
        runtimes[f"keepmum {tag}"] = guard.InjectionModel(g, server=a.server)

    margins = {name: [] for name in scorers}  # per window: INJECTION - BENIGN
    scores = {name: {} for name in list(scorers) + list(runtimes)}
    tok_same = 0
    t0 = time.perf_counter()
    for n, r in enumerate(docs, 1):
        text = r["text"] or "."
        rows = [wrap(b) for b in guard.InjectionModel.windows(ids(text))]
        for name, fn in scorers.items():
            logits = [l for i in range(0, len(rows), 4) for l in fn(rows[i:i + 4])]
            margins[name].append([l[1] - l[0] for l in logits])
            scores[name][r["id"]] = max(p_injection(l) for l in logits)
        for i, (name, rt) in enumerate(runtimes.items()):
            scores[name][r["id"]] = rt.score(text)
            if i == 0:
                tok_same += rt.tokens(text) == ids(text)
        if n % 500 == 0:
            print(f"{n} / {len(docs)}, {time.perf_counter() - t0:.0f} s", flush=True)
    for s in servers.values():
        s.close()
    for rt in runtimes.values():
        rt.unload()

    def diff(x):
        d = [abs(p - q) for xs, ys in zip(margins[x], margins["torch"]) for p, q in zip(xs, ys)]
        return {"median": statistics.median(d), "p99": sorted(d)[int(0.99 * (len(d) - 1))], "max": max(d), "windows": len(d)}

    def logit(p):
        p = min(max(p, 1e-12), 1 - 1e-12)
        return math.log(p / (1 - p))

    parity = {name: diff(name) for name in scorers if name != "torch"}
    # The veto's verdict on keepmum's hits: does each scorer confirm the same ones as the original?
    verdicts = {}
    for name in scores:
        if name == "torch":
            continue
        row = {}
        for v in VETOES:
            differ = [r for r in hits if (scores[name][r["id"]] >= v) != (scores["torch"][r["id"]] >= v)]
            row[str(v)] = {"same": len(hits) - len(differ), "of": len(hits),
                           "differ": [(r["id"], r["label"], round(scores["torch"][r["id"]], 5), round(scores[name][r["id"]], 5))
                                      for r in differ][:20]}
        lo = [abs(logit(scores[name][r["id"]]) - logit(scores["torch"][r["id"]])) for r in docs]
        row["doc_logit_diff"] = {"median": statistics.median(lo), "p99": sorted(lo)[int(0.99 * (len(lo) - 1))], "max": max(lo)}
        verdicts[name] = row

    # guard_both.py's table, with each scorer as the second model (documents keepmum does not flag cannot be flagged)
    benign = {s: [r for r in recs if r["source"] == s and r["label"] == 0] for s in ("swe-chat", "swe-hero", "prodnull")}
    injected = {s: [r for r in recs if r["source"] == s and r["label"] == 1] for s in ("agentdojo", "injecagent", "llmail", "codeipi", "prodnull")}
    table = []
    for level in ("high", "medium"):
        for name in scores:
            for v in VETOES:
                flag = lambda r: flags(r, level) and scores[name].get(r["id"], 0.0) >= v
                table.append({"level": level, "second": name, "veto": v,
                              "benign": {s: sum(map(flag, xs)) for s, xs in benign.items()},
                              "caught": {s: sum(map(flag, xs)) for s, xs in injected.items()}})
    counts = {"benign": {s: len(x) for s, x in benign.items()}, "injected": {s: len(x) for s, x in injected.items()}}

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = {"env": bench.environment(), "documents": {"flagged_high": len(hits), "sampled": len(docs) - len(hits)},
           "tokenizer": {"identical": tok_same, "documents": len(docs)}, "parity": parity, "verdicts": verdicts,
           "table": table, "counts": counts, "runs": [{"detector": k, "scores": v} for k, v in scores.items()]}
    (a.out / f"wolf-gguf-{stamp}.json").write_text(json.dumps(out, indent=1))
    md = render(out)
    (a.out / f"wolf-gguf-{stamp}.md").write_text(md)
    print(md)


def render(out: dict) -> str:
    d = out["documents"]
    lines = ["# Wolf Defender Small: the original vs GGUF in llama.cpp, as a veto on keepmum's hits", "",
             f"{out['env'].get('machine', '')} · {d['flagged_high']} documents keepmum flags at High, {d['sampled']} sampled "
             f"from the rest of the coding-agent output · keepmum's tokens identical to the model's tokenizer on "
             f"{out['tokenizer']['identical']} of {out['tokenizer']['documents']}", "",
             "## Logit margin (INJECTION − BENIGN) against torch, per window, same token ids", "",
             "| | median | p99 | max | windows |", "| --- | --- | --- | --- | --- |"]
    lines += [f"| {k} | {v['median']:.4f} | {v['p99']:.4f} | {v['max']:.4f} | {v['windows']} |" for k, v in out["parity"].items()]
    lines += ["", "## The veto's verdict on keepmum's High hits, against torch", "",
              "| | " + " | ".join(f"same at {v}" for v in VETOES) + " | document log-odds difference: median / p99 / max |",
              "| --- " * (len(VETOES) + 2) + "|"]
    for k, row in out["verdicts"].items():
        cells = [f"{row[str(v)]['same']} / {row[str(v)]['of']}" for v in VETOES]
        lo = row["doc_logit_diff"]
        lines.append(f"| {k} | " + " | ".join(cells) + f" | {lo['median']:.3f} / {lo['p99']:.3f} / {lo['max']:.3f} |")
    lines += ["", "Documents whose verdict differs (id, label, torch → this), up to 20 per cell:", ""]
    for k, row in out["verdicts"].items():
        for v in VETOES:
            if row[str(v)]["differ"]:
                lines.append(f"- {k} at {v}: " + "; ".join(f"`{i}` ({l}, {a} → {b})" for i, l, a, b in row[str(v)]["differ"]))
    c = out["counts"]
    lines += ["", "## Both must flag: keepmum at its level, and the second model at the veto", "",
              "| keepmum level | second | veto | " + " | ".join(f"{s} benign flagged ({n})" for s, n in c["benign"].items()) + " | "
              + " | ".join(f"{s} caught ({n})" for s, n in c["injected"].items()) + " |",
              "| --- " * (3 + len(c["benign"]) + len(c["injected"])) + "|"]
    for t in out["table"]:
        lines.append(f"| {t['level']} | {t['second']} | {t['veto']} | "
                     + " | ".join(str(t["benign"][s]) for s in c["benign"]) + " | "
                     + " | ".join(f"{t['caught'][s] / c['injected'][s]:.1%}" for s in c["injected"]) + " |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
