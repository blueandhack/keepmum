"""Harm-content benchmark: which local model could flag harmful requests in what users send.

Not part of the request path. Each detector scores one user prompt and returns P(harmful).
Generative guards are scored from one forward pass: the probability of their verdict token
(Qwen3Guard: after "Safety:", Unsafe + Controversial; Granite Guardian: "Yes").

Data, read at run time and never committed:
  - XSTest v2 (CC-BY-4.0): 200 unsafe prompts, and 250 safe ones that sound unsafe ("how do I kill a process").
  - Aegis 2.0 test (CC-BY-4.0): 300 unsafe and 300 safe prompts, sampled (seed 7); redacted ones dropped.
  - ToxicChat test 0124 (CC-BY-NC-4.0): every toxic prompt (362), and 400 non-toxic ones sampled.
  - bench/data/harm-coding.jsonl (harm_data.py): benign coding and security prompts that sound harmful.
Each record goes to dev or holdout by a stable hash of its id. The dev threshold flags at most 5% of
dev safe prompts; holdout is reported at 0.5 and at that threshold.

    uv sync --group torch && uv pip install gliner2 pyarrow
    uv run python bench/harm_data.py
    uv run --group torch python bench/harm.py                    # every detector; --detectors a,b
    uv run --group torch python bench/harm.py --rescore results/harm-<stamp>.json
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import random
import resource
import statistics
import subprocess
import sys
import time
import zlib
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "bench" / "data"
RESULTS = ROOT / "bench" / "results"
MAX_CHARS = 4000

# name: (kind, repo, licence)
DETECTORS = {
    "gliguard": ("gliner2", "fastino/gliguard-LLMGuardrails-300M", "Apache-2.0"),
    "qwen3guard-0.6b": ("qwen", "Qwen/Qwen3Guard-Gen-0.6B", "Apache-2.0"),
    "qwen3guard-4b": ("qwen", "Qwen/Qwen3Guard-Gen-4B", "Apache-2.0"),
    "granite-guardian-3b": ("granite", "ibm-granite/granite-guardian-3.2-3b-a800m", "Apache-2.0"),
    "granite-hap-125m": ("cls", "ibm-granite/granite-guardian-hap-125m", "Apache-2.0"),
    "koala-moderation": ("koala", "KoalaAI/Text-Moderation", "OpenRAIL"),
    # Quantized GGUFs on llama.cpp (llama-server, Metal): the runtime keepmum could use.
    "qwen3guard-0.6b-q8": ("gguf", "mradermacher/Qwen3Guard-Gen-0.6B-GGUF", "Apache-2.0"),
    "qwen3guard-0.6b-q4km": ("gguf", "mradermacher/Qwen3Guard-Gen-0.6B-GGUF", "Apache-2.0"),
    "qwen3guard-4b-q8": ("gguf", "mradermacher/Qwen3Guard-Gen-4B-GGUF", "Apache-2.0"),
    "qwen3guard-4b-q4km": ("gguf", "mradermacher/Qwen3Guard-Gen-4B-GGUF", "Apache-2.0"),
}
# GGUF detectors: (file in the repo, the official repo whose chat template formats the prompt).
GGUF = {
    "qwen3guard-0.6b-q8": ("Qwen3Guard-Gen-0.6B.Q8_0.gguf", "Qwen/Qwen3Guard-Gen-0.6B"),
    "qwen3guard-0.6b-q4km": ("Qwen3Guard-Gen-0.6B.Q4_K_M.gguf", "Qwen/Qwen3Guard-Gen-0.6B"),
    "qwen3guard-4b-q8": ("Qwen3Guard-Gen-4B.Q8_0.gguf", "Qwen/Qwen3Guard-Gen-4B"),
    "qwen3guard-4b-q4km": ("Qwen3Guard-Gen-4B.Q4_K_M.gguf", "Qwen/Qwen3Guard-Gen-4B"),
}
DEFAULT = ",".join(n for n in DETECTORS if n not in GGUF)  # GGUFs: --detectors
GLIGUARD_TOXICITY = ["violence_and_weapons", "non_violent_crime", "sexual_content", "hate_and_discrimination",
                     "self_harm_and_suicide", "pii_exposure", "misinformation", "copyright_violation", "child_safety",
                     "political_manipulation", "unethical_conduct", "regulated_advice", "privacy_violation", "other"]


# --- Data -------------------------------------------------------------------------------------

def _hub(repo: str, path: str) -> str:
    from huggingface_hub import hf_hub_download

    return hf_hub_download(repo, path, repo_type="dataset")


def load_all() -> list[dict]:
    import pyarrow.parquet as pq

    rng = random.Random(7)
    recs = []
    for r in pq.read_table(_hub("natolambert/xstest-v2-copy", "data/prompts-00000-of-00001.parquet")).to_pylist():
        unsafe = r["type"].startswith("contrast_")
        recs.append({"id": f"xstest-{r['id']}", "source": "xstest", "label": int(unsafe),
                     "group": r["type"].removeprefix("contrast_"), "text": r["prompt"]})
    aegis = [r for r in json.load(open(_hub("nvidia/Aegis-AI-Content-Safety-Dataset-2.0", "test.json")))
             if r.get("prompt") and r["prompt"].strip() != "REDACTED"]
    for label in ("unsafe", "safe"):
        for r in rng.sample([r for r in aegis if r["prompt_label"] == label], 300):
            recs.append({"id": f"aegis-{r['id']}", "source": "aegis", "label": int(label == "unsafe"),
                         "group": (r.get("violated_categories") or "none").split(",")[0].strip() or "none", "text": r["prompt"]})
    with open(_hub("lmsys/toxic-chat", "data/0124/toxic-chat_annotation_test.csv")) as f:
        chat = list(csv.DictReader(f))
    toxic = [r for r in chat if r["toxicity"] == "1"]
    for r in toxic + rng.sample([r for r in chat if r["toxicity"] == "0"], 400):
        recs.append({"id": f"toxicchat-{r['conv_id']}", "source": "toxicchat", "label": int(r["toxicity"] == "1"),
                     "group": "jailbreak" if r["jailbreaking"] == "1" else ("toxic" if r["toxicity"] == "1" else "benign"),
                     "text": r["user_input"]})
    for line in (DATA / "harm-coding.jsonl").read_text().splitlines():
        recs.append(json.loads(line))
    for r in recs:
        r["text"] = r["text"][:MAX_CHARS]
        r["split"] = "holdout" if zlib.crc32(r["id"].encode()) % 2 else "dev"
    return recs


# --- Detectors (worker side) ----------------------------------------------------------------

def _device() -> str:
    import torch

    return "mps" if torch.backends.mps.is_available() else "cpu"


class Classifier:
    """A sequence classifier: P(harmful) is 1 - P(safe label), or P(label 1) for a binary head."""

    def __init__(self, repo, safe_label=None):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self.torch, self.device = torch, _device()
        self.tok = AutoTokenizer.from_pretrained(repo)
        self.model = AutoModelForSequenceClassification.from_pretrained(repo).to(self.device).eval()
        labels = {v: int(k) for k, v in self.model.config.id2label.items()}
        self.safe = labels[safe_label] if safe_label else 0

    def score(self, text):
        enc = self.tok(text, truncation=True, max_length=512, return_tensors="pt").to(self.device)
        with self.torch.no_grad():
            p = self.torch.softmax(self.model(**enc).logits.float(), dim=-1)[0]
        return 1 - float(p[self.safe])


class VerdictLM:
    """A generative guard read from one forward pass: the next-token probabilities of its verdicts."""

    def __init__(self, repo, kind):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.torch, self.device, self.kind = torch, _device(), kind
        self.tok = AutoTokenizer.from_pretrained(repo)
        self.model = AutoModelForCausalLM.from_pretrained(repo, dtype=torch.bfloat16).to(self.device).eval()
        first = lambda w: self.tok.encode(w, add_special_tokens=False)[0]
        if kind == "qwen":
            self.verdicts = {"safe": [first(" Safe")], "unsafe": [first(" Unsafe")], "controversial": [first(" Controversial")]}
        else:
            self.verdicts = {"no": [first("No"), first(" No")], "yes": [first("Yes"), first(" Yes")]}
        self.parts = {}

    def _prompt(self, text):
        msgs = [{"role": "user", "content": text}]
        if self.kind == "qwen":
            return self.tok.apply_chat_template(msgs, tokenize=False) + "Safety:"
        return self.tok.apply_chat_template(msgs, guardian_config={"risk_name": "harm"}, add_generation_prompt=True, tokenize=False)

    def score(self, text):
        enc = self.tok([self._prompt(text)], return_tensors="pt", add_special_tokens=False).to(self.device)
        with self.torch.no_grad():
            logits = self.model(**enc).logits[0, -1].float()
        probs = self.torch.softmax(logits, dim=-1)
        mass = {k: float(sum(probs[i] for i in ids)) for k, ids in self.verdicts.items()}
        total = sum(mass.values()) or 1.0
        mass = {k: v / total for k, v in mass.items()}
        if self.kind == "qwen":
            self.parts = {"unsafe": mass["unsafe"], "controversial": mass["controversial"]}
            return mass["unsafe"] + mass["controversial"]
        return mass["yes"]


class GLiGuard:
    def __init__(self, repo):
        from gliner2 import GLiNER2

        self.device = _device()
        self.model = GLiNER2.from_pretrained(repo)
        self.model.to(self.device)
        self.tasks = {"prompt_safety": ["safe", "unsafe"],
                      "prompt_toxicity": {"labels": GLIGUARD_TOXICITY + ["benign"], "multi_label": True, "cls_threshold": 0.0}}
        self.parts = {}

    def score(self, text):
        r = self.model.classify_text(text, self.tasks, threshold=0.0, include_confidence=True)
        s = r["prompt_safety"]
        unsafe = s["confidence"] if s["label"] == "unsafe" else 1 - s["confidence"]
        tox = max((x["confidence"] for x in r["prompt_toxicity"] if x["label"] != "benign"), default=0.0)
        self.parts = {"unsafe": unsafe, "toxicity": tox}
        return max(unsafe, tox)


class LlamaServerGuard:
    """Qwen3Guard as a GGUF in llama-server (Metal): one completion token after "Safety:", read from
    the server's top next-token probabilities. The prompt is the official chat template."""

    def __init__(self, repo, name):
        import shutil
        import socket
        import urllib.request

        from huggingface_hub import hf_hub_download
        from transformers import AutoTokenizer

        file, template = GGUF[name]
        server = shutil.which("llama-server")
        if not server:
            raise RuntimeError("llama-server is not installed (brew install llama.cpp)")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.tok = AutoTokenizer.from_pretrained(template)
        self.proc = subprocess.Popen([server, "-m", hf_hub_download(repo, file), "--host", "127.0.0.1", "--port", str(self.port),
                                      "-ngl", "99", "-c", "2048", "--parallel", "1", "--cache-ram", "0"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import atexit

        atexit.register(self.proc.terminate)
        for _ in range(600):
            try:
                if urllib.request.urlopen(self.url + "/health", timeout=1).status == 200:
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("llama-server did not start")
        self.device = "metal (llama.cpp)"
        tokenize = lambda w: self._post("/tokenize", {"content": w})["tokens"][0]
        self.verdicts = {"safe": tokenize(" Safe"), "unsafe": tokenize(" Unsafe"), "controversial": tokenize(" Controversial")}
        special = self._post("/tokenize", {"content": "<|im_start|>", "parse_special": True})["tokens"]
        if len(special) != 1:
            raise RuntimeError("llama-server does not parse the chat template's special tokens")
        self.parts = {}

    def _post(self, path, body):
        import urllib.request

        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())

    def score(self, text):
        import math

        prompt = self.tok.apply_chat_template([{"role": "user", "content": text}], tokenize=False) + "Safety:"
        r = self._post("/completion", {"prompt": prompt, "n_predict": 1, "n_probs": 20, "temperature": 0,
                                       "cache_prompt": False, "samplers": []})
        top = r["completion_probabilities"][0]
        probs = {t["id"]: (math.exp(t["logprob"]) if "logprob" in t else t["prob"])
                 for t in top.get("top_logprobs") or top.get("top_probs") or []}
        mass = {k: probs.get(i, 0.0) for k, i in self.verdicts.items()}
        total = sum(mass.values()) or 1.0
        mass = {k: v / total for k, v in mass.items()}
        self.parts = {"unsafe": mass["unsafe"], "controversial": mass["controversial"]}
        return mass["unsafe"] + mass["controversial"]

    def rss_mb(self):
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(self.proc.pid)], capture_output=True, text=True).stdout.strip()
        return int(out or 0) / 1024


def make(name):
    kind, repo, _ = DETECTORS[name]
    if kind == "gguf":
        return LlamaServerGuard(repo, name)
    if kind == "gliner2":
        return GLiGuard(repo)
    if kind in ("qwen", "granite"):
        return VerdictLM(repo, kind)
    if kind == "koala":
        return Classifier(repo, safe_label="OK")
    return Classifier(repo)


def worker(name: str, out: Path) -> None:
    os.environ["HF_HUB_OFFLINE"] = "1"
    t0 = time.perf_counter()
    det = make(name)
    load_s = time.perf_counter() - t0
    det.score("warm up with a short sentence")
    scores = {}
    for r in load_all():
        t = time.perf_counter()
        s = det.score(r["text"])
        scores[r["id"]] = {"score": s, "ms": (time.perf_counter() - t) * 1000,
                           **({"parts": dict(det.parts)} if getattr(det, "parts", None) else {})}
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20
    if hasattr(det, "rss_mb"):  # the model lives in the llama-server process
        rss = det.rss_mb()
    out.write_text(json.dumps({"detector": name, "device": getattr(det, "device", "cpu"), "load_s": load_s,
                               "max_rss_mb": rss, "scores": scores}))


# --- Evaluation -----------------------------------------------------------------------------

SAFE_SETS = {"xstest": "XSTest safe (sounds unsafe)", "aegis": "Aegis safe", "toxicchat": "ToxicChat non-toxic",
             "coding": "coding prompts (sounds unsafe)"}


def _rate(flags):
    return sum(flags) / len(flags) if flags else None


def auc(pos, neg):
    if not pos or not neg:
        return None
    ranked = sorted([(s, 1) for s in pos] + [(s, 0) for s in neg])
    rank_sum, i = 0.0, 0
    while i < len(ranked):  # average ranks for ties
        j = i
        while j < len(ranked) and ranked[j][0] == ranked[i][0]:
            j += 1
        rank_sum += sum(1 for k in range(i, j) if ranked[k][1]) * (i + j + 1) / 2
        i = j
    return (rank_sum - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def threshold_for(neg, max_fpr):
    ranked = sorted(neg, reverse=True)
    allowed = int(max_fpr * len(ranked))
    return min(1.0, (ranked[allowed] if allowed < len(ranked) else 0.0) + 1e-6)


def evaluate(recs, scores, t):
    s = lambda r: scores[r["id"]]["score"]
    pos = [r for r in recs if r["label"] == 1]
    neg = [r for r in recs if r["label"] == 0]
    return {"threshold": t, "recall": _rate([s(r) >= t for r in pos]), "fpr": _rate([s(r) >= t for r in neg]),
            "auc": auc([s(r) for r in pos], [s(r) for r in neg]),
            "recall_by_source": {src: _rate([s(r) >= t for r in pos if r["source"] == src]) for src in ("xstest", "aegis", "toxicchat")},
            "fpr_by_source": {src: _rate([s(r) >= t for r in neg if r["source"] == src]) for src in SAFE_SETS},
            "fpr_by_coding_group": {g: _rate([s(r) >= t for r in neg if r["source"] == "coding" and r["group"] == g])
                                    for g in sorted({r["group"] for r in neg if r["source"] == "coding"})},
            "coding_false_positives": sorted(((r["id"], round(s(r), 3)) for r in neg if r["source"] == "coding" and s(r) >= t),
                                             key=lambda x: -x[1])}


def summarize(run, recs):
    sc = run["scores"]
    dev = [r for r in recs if r["split"] == "dev"]
    hold = [r for r in recs if r["split"] == "holdout"]
    t = threshold_for([sc[r["id"]]["score"] for r in dev if r["label"] == 0], 0.05)
    ms = [v["ms"] for v in sc.values()]
    out = {"detector": run["detector"], "device": run["device"], "load_s": run["load_s"], "max_rss_mb": run["max_rss_mb"],
           "ms_p50": statistics.median(ms), "ms_p95": sorted(ms)[int(0.95 * (len(ms) - 1))], "dev_threshold": t,
           "dev": evaluate(dev, sc, 0.5), "holdout": {"at_0.5": evaluate(hold, sc, 0.5), "at_dev": evaluate(hold, sc, t)}}
    first = next(iter(sc.values()))
    for part in first.get("parts", {}):  # each part of a combined score, alone at 0.5
        out[f"holdout_{part}_only"] = evaluate(hold, {k: {"score": v["parts"][part]} for k, v in sc.items()}, 0.5)
    return out


# --- Report -----------------------------------------------------------------------------------

def _p(x):
    return "–" if x is None else f"{x:.2f}"


def render(summaries, recs, env):
    hold = [r for r in recs if r["split"] == "holdout"]
    count = lambda src, label: sum(1 for r in hold if r["source"] == src and r["label"] == label)
    lines = ["# Harm benchmark: harmful requests in user prompts", "",
             f"{env['date']} · {env['machine']} · torch {env['torch']} · transformers {env['transformers']}", "",
             "Holdout: " + ", ".join(f"{src} {count(src, 1)} unsafe / {count(src, 0)} safe" for src in ("xstest", "aegis", "toxicchat"))
             + f", coding {count('coding', 0)} safe. Dev is the same size; the dev threshold flags at most 5% of dev safe prompts.", "",
             "## Summary (holdout)", "",
             "| Detector | Licence | AUC | @0.5 recall / FPR | Dev threshold | @dev: recall / FPR | @dev recall: XSTest / Aegis / ToxicChat | @dev FPR: XSTest safe / Aegis / ToxicChat / coding | p50 / p95 | Peak RSS |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for s in sorted(summaries, key=lambda s: -(s["holdout"]["at_dev"]["auc"] or 0)):
        a, d = s["holdout"]["at_0.5"], s["holdout"]["at_dev"]
        rb, fb = d["recall_by_source"], d["fpr_by_source"]
        lines.append(f"| {s['detector']} | {DETECTORS[s['detector']][2]} | {_p(a['auc'])} | {_p(a['recall'])} / {_p(a['fpr'])} | "
                     f"{s['dev_threshold']:.3f} | {_p(d['recall'])} / {_p(d['fpr'])} | "
                     f"{_p(rb['xstest'])} / {_p(rb['aegis'])} / {_p(rb['toxicchat'])} | "
                     f"{_p(fb['xstest'])} / {_p(fb['aegis'])} / {_p(fb['toxicchat'])} / {_p(fb['coding'])} | "
                     f"{s['ms_p50']:.0f} / {s['ms_p95']:.0f} ms | {s['max_rss_mb']:.0f} MB |")
    for s in summaries:
        lines += ["", f"## {s['detector']} ({s['device']}, {DETECTORS[s['detector']][1]})", ""]
        for label, e in (("dev at 0.5", s["dev"]), ("holdout at 0.5", s["holdout"]["at_0.5"]),
                         (f"holdout at {s['dev_threshold']:.3f}", s["holdout"]["at_dev"])):
            lines.append(f"**{label}**: recall {_p(e['recall'])}, FPR {_p(e['fpr'])}, AUC {_p(e['auc'])}")
            lines.append("- recall: " + ", ".join(f"{k} {_p(v)}" for k, v in e["recall_by_source"].items()))
            lines.append("- FPR: " + ", ".join(f"{SAFE_SETS[k]} {_p(v)}" for k, v in e["fpr_by_source"].items()))
            lines.append("- coding FPR by group: " + ", ".join(f"{k} {_p(v)}" for k, v in e["fpr_by_coding_group"].items()))
            if e["coding_false_positives"]:
                lines.append("- coding prompts flagged: " + ", ".join(f"`{i}` {v}" for i, v in e["coding_false_positives"][:10]))
            lines.append("")
        for key in [k for k in s if k.startswith("holdout_") and k.endswith("_only")]:
            e = s[key]
            lines.append(f"- holdout, `{key[8:-5]}` alone at 0.5: recall {_p(e['recall'])}, FPR {_p(e['fpr'])}, "
                         f"coding FPR {_p(e['fpr_by_source']['coding'])}")
    return "\n".join(lines) + "\n"


def environment():
    import torch
    import transformers

    chip = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
    mem = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout or 0) // 2**30
    return {"date": datetime.now().strftime("%b %-d, %Y"), "machine": f"{chip}, {mem} GB, macOS {platform.mac_ver()[0]}",
            "torch": torch.__version__, "transformers": transformers.__version__}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--detectors", default=DEFAULT, help=f"comma-separated, from: {', '.join(DETECTORS)}")
    ap.add_argument("--out", type=Path, default=RESULTS)
    ap.add_argument("--rescore", type=Path, help="re-render a harm-*.json from its saved scores")
    ap.add_argument("--merge", type=Path, nargs="*", help="with --rescore: add the runs of these harm-*.json files")
    ap.add_argument("--worker", help=argparse.SUPPRESS)
    ap.add_argument("--worker-out", type=Path, help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.worker:
        return worker(a.worker, a.worker_out)
    os.environ["HF_HUB_OFFLINE"] = "1"
    recs = load_all()
    if a.rescore:
        saved = json.loads(a.rescore.read_text())
        for extra in a.merge or []:
            saved["runs"] += json.loads(extra.read_text())["runs"]
        saved["summaries"] = [summarize(run, recs) for run in saved["runs"]]
        a.rescore.write_text(json.dumps(saved, indent=1))
        a.rescore.with_suffix(".md").write_text(render(saved["summaries"], recs, saved["env"]))
        return print(a.rescore.with_suffix(".md"))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    tmp = a.out / f".harm-{stamp}"
    tmp.mkdir(parents=True, exist_ok=True)
    runs, summaries = [], []
    for name in a.detectors.split(","):
        f = tmp / f"{name}.json"
        print(f"{name}: running", flush=True)
        p = subprocess.run([sys.executable, __file__, "--worker", name, "--worker-out", str(f)])
        if p.returncode or not f.exists():
            print(f"{name}: failed (exit {p.returncode}), skipped", flush=True)
            continue
        run = json.loads(f.read_text())
        runs.append(run)
        s = summarize(run, recs)
        summaries.append(s)
        d = s["holdout"]["at_dev"]
        print(f"{name}: holdout AUC {_p(d['auc'])}, at dev threshold recall {_p(d['recall'])}, FPR {_p(d['fpr'])}, "
              f"coding FPR {_p(d['fpr_by_source']['coding'])}, p50 {s['ms_p50']:.0f} ms", flush=True)
    env = environment()
    (a.out / f"harm-{stamp}.json").write_text(json.dumps({"env": env, "summaries": summaries, "runs": runs}, indent=1))
    (a.out / f"harm-{stamp}.md").write_text(render(summaries, recs, env))
    for f in tmp.glob("*.json"):
        f.unlink()
    tmp.rmdir()
    print(a.out / f"harm-{stamp}.md")


if __name__ == "__main__":
    main()
