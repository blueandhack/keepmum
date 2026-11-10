"""The injection check on public data: how often it flags what coding agents really read, and what it catches.

bench/guard.py measures the check on a small hand-written set. This one uses public datasets, so the
false-alarm rate comes from real coding-agent tool output, not from documents written for the test.

    uv run python bench/guard_public.py build                 # bench/data/guard-public/*.jsonl, kept out of git
    uv run python bench/guard_public.py build --only swe-chat # one source again
    uv run --group torch python bench/guard_public.py run     # every detector; --detectors keepmum,horizon-base
    uv run --group torch python bench/guard_public.py run --rescore bench/results/guard-public-<stamp>.json

Sources (see SOURCES): benign tool output from coding agents (SWE-chat: real Claude Code, Codex and Gemini
CLI sessions; SWE-Hero: OpenHands), AgentDojo's tool outputs with and without its attacks, InjecAgent's
injected tool responses, LLMail-Inject phase 2 (attack emails people wrote to get past defences), CodeIPI's
injections in issues, code comments, READMEs and configs, and prodnull's repository files. SWE-chat and
prodnull are gated: accept their terms on Hugging Face first, or `build` skips them.

The data is never committed: it is rebuilt from pinned sources, and SWE-chat's tool output comes from real
sessions. The report names documents by id only.

How a detector is judged: the benign coding-agent output (SWE-chat and SWE-Hero) is split in two by a hash
of each id. The threshold is set on one half so that it flags 1% of it; the other half gives the false-alarm
rate at that threshold, and every attack source its catch rate. The keepmum detector, the GGUF in
llama-server exactly as keepmum runs it, is also reported at its sensitivity levels (keepmum.guard.LEVELS).
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import subprocess
import sys
import time
import zlib
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from guard import DETECTORS, RESULTS, ROOT, auc, environment, make, threshold_for  # noqa: E402

DATA = ROOT / "bench" / "data" / "guard-public"
CACHE = DATA / ".cache"
MIN_CHARS = 40  # keepmum.guard.MIN_CHARS: shorter tool results are not scored
MAX_CHARS = 16_000  # as bench/guard.py: one tool result
SEED = 7
INJECAGENT = "f19c9f2c79a41046eb13c03c51a24c567a8ffa07"  # uiuc-kang-lab/InjecAgent
INSPECT_EVALS = "a2b74ce3620c5bba9693f130dc1f67a9463e938b"  # UKGovernmentBEIS/inspect_evals (CodeIPI)
AGENTDOJO = "agentdojo==0.1.34"

# name: (what it holds, licence, where)
SOURCES = {
    "swe-chat": ("benign: real Claude Code, Codex and Gemini CLI sessions, tool results", "ODC-BY, gated",
                 "huggingface.co/datasets/SALT-NLP/SWE-chat"),
    "swe-hero": ("benign: OpenHands agents fixing real repositories, tool observations", "CC BY 4.0",
                 "huggingface.co/datasets/nvidia/SWE-Hero-openhands-trajectories"),
    "agentdojo": ("both: tool outputs of AgentDojo v1.2's tasks, clean and with its seven attacks", "MIT",
                  f"github.com/ethz-spylab/agentdojo ({AGENTDOJO})"),
    "injecagent": ("injected: tool responses with an attacker's instruction, base and enhanced", "MIT",
                   "github.com/uiuc-kang-lab/InjecAgent"),
    "llmail": ("injected: LLMail-Inject phase 2, attack emails written to beat its defences", "MIT",
               "huggingface.co/datasets/microsoft/llmail-inject-challenge"),
    "codeipi": ("both: injections in issues, code comments, READMEs and configs, and clean tasks", "MIT",
                "github.com/UKGovernmentBEIS/inspect_evals (ipi_coding_agent)"),
    "prodnull": ("both: malicious and benign repository files (build scripts, CI, READMEs)", "Apache-2.0, gated",
                 "huggingface.co/datasets/prodnull/prompt-injection-repo-dataset"),
}
CODING = ("swe-chat", "swe-hero")  # what the threshold is set on
PER_SOURCE = {"swe-chat": 2000, "swe-hero": 1500, "injecagent": 600, "llmail": 600}
DEFAULT = "keepmum,horizon-base,horizon-large,wolf-small,wolf"


# --- Building the data ------------------------------------------------------------------------

def _doc(source: str, key: str, label: int, text: str, **meta) -> dict | None:
    text = (text or "").strip()
    if len(text) < MIN_CHARS:
        return None
    return {"id": f"{source}/{key}", "source": source, "label": label, "text": text[:MAX_CHARS], **meta}


def _sample(recs: list[dict], n: int | None) -> list[dict]:
    """Distinct texts, then at most n of them, the same ones on every run."""
    seen, out = set(), []
    for r in recs:
        if r is not None and r["text"] not in seen:
            seen.add(r["text"])
            out.append(r)
    if n is not None and len(out) > n:
        out = random.Random(SEED).sample(out, n)
    return sorted(out, key=lambda r: r["id"])


def _fetch(url: str, name: str) -> Path:
    import httpx

    path = CACHE / name
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        r = httpx.get(url, follow_redirects=True, timeout=120)
        r.raise_for_status()
        path.write_bytes(r.content)
    return path


def _text(content) -> str:
    """A message's content as text: a string, or the text parts of a list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_text(c.get("text", c.get("content")) if isinstance(c, dict) else c) for c in content)
    return "" if content is None else str(content)


def build_swe_chat() -> list[dict]:
    """Tool results from 100 of the conversations table's row groups (about 10,000 turns each, a few sessions),
    spread through the file and read remotely, at most 3 per session. A tool result names its tool only through
    the tool call it answers, so the tool comes from the matching `tool_use` row."""
    import pyarrow.parquet as pq
    from huggingface_hub import HfFileSystem

    cols = ["turn_id", "session_id", "turn_type", "content", "tool_name", "tool_call_id", "model"]
    with HfFileSystem().open("datasets/SALT-NLP/SWE-chat/conversations.parquet", "rb", block_size=8 << 20) as f:
        pf = pq.ParquetFile(f)
        groups = pf.num_row_groups
        per_session: dict[str, list[dict]] = {}
        for g in sorted({round(i * (groups - 1) / 99) for i in range(100)}):
            rows = pf.read_row_group(g, columns=cols).to_pylist()
            calls = {r["tool_call_id"]: r for r in rows if r["turn_type"] == "tool_use" and r["tool_call_id"]}
            for row in rows:
                if row["turn_type"] != "tool_result":
                    continue
                call = calls.get(row["tool_call_id"]) or {}
                r = _doc("swe-chat", row["turn_id"], 0, _text(row["content"]), tool=row["tool_name"] or call.get("tool_name"),
                         model=call.get("model") or row["model"])
                if r:
                    per_session.setdefault(row["session_id"], []).append(r)
    recs = [r for s, rs in per_session.items() for r in random.Random(s).sample(rs, min(3, len(rs)))]
    return _sample(recs, PER_SOURCE["swe-chat"])


def build_swe_hero() -> list[dict]:
    """Tool observations from the first shard, at most 3 per trajectory."""
    import pyarrow.parquet as pq
    from huggingface_hub import hf_hub_download

    path = hf_hub_download("nvidia/SWE-Hero-openhands-trajectories", "data/train-00000-of-00014.parquet", repo_type="dataset")
    recs = []
    for row in pq.read_table(path, columns=["trajectory_id", "repo", "trajectory"]).to_pylist():
        traj = row["trajectory"]
        msgs = json.loads(traj) if isinstance(traj, str) else traj
        tools = [m for m in msgs if m.get("role") == "tool"]
        for k, m in enumerate(random.Random(row["trajectory_id"]).sample(tools, min(3, len(tools)))):
            recs.append(_doc("swe-hero", f"{row['trajectory_id']}/{k}", 0, _text(m.get("content")),
                             tool=m.get("name"), repo=row["repo"]))
    return _sample(recs, PER_SOURCE["swe-hero"])


def build_agentdojo() -> list[dict]:
    out = CACHE / "agentdojo.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    helper = ROOT / "bench" / "guard_public_agentdojo.py"
    subprocess.run(["uv", "run", "--no-project", "--python", "3.12", "--with", AGENTDOJO, "python", "-I", str(helper), str(out)],
                   check=True, cwd=CACHE)
    recs = []
    for line in out.read_text().splitlines():
        r = json.loads(line)
        key = f"{r['suite']}/{r['task']}/{r['step']}" + (f"/{r['attack']}" if r["label"] else "")
        recs.append(_doc("agentdojo", key, r["label"], r["text"], function=r["function"],
                         **({"attack": r["attack"]} if r["label"] else {})))
    return _sample(recs, None)


def build_injecagent() -> list[dict]:
    recs = []
    for kind in ("dh", "ds"):
        for setting in ("base", "enhanced"):
            name = f"test_cases_{kind}_{setting}.json"
            path = _fetch(f"https://raw.githubusercontent.com/uiuc-kang-lab/InjecAgent/{INJECAGENT}/data/{name}", f"injecagent/{name}")
            for i, case in enumerate(json.loads(path.read_text())):
                recs.append(_doc("injecagent", f"{kind}-{setting}/{i}", 1, case["Tool Response"],
                                 attack=f"{kind}-{setting}", tool=case["User Tool"]))
    return _sample(recs, PER_SOURCE["injecagent"])


def build_llmail() -> list[dict]:
    """Phase 2's distinct submissions labelled as attacks: half that made the assistant call its send-email
    API (`api_triggered`), half that a judge labelled as attempts. Each is an email as the assistant read it."""
    from huggingface_hub import hf_hub_download

    path = hf_hub_download("microsoft/llmail-inject-challenge", "data/labelled_unique_submissions_phase2.json", repo_type="dataset")
    halves: dict[str, list] = {"api_triggered": [], "judge": []}
    for email, label in json.loads(Path(path).read_text()).items():
        if str(label.get("attack_attempt")) == "True" and label.get("reason") in halves:
            key = f"{label['reason']}/{zlib.crc32(email.encode()):08x}"
            halves[label["reason"]].append(_doc("llmail", key, 1, email, attack=label["reason"]))
    n = PER_SOURCE["llmail"] // 2
    return sorted(_sample(halves["api_triggered"], n) + _sample(halves["judge"], n), key=lambda r: r["id"])


def build_codeipi() -> list[dict]:
    """Each injected task's carrier (the issue, or the file holding the payload); every text of the clean tasks."""
    url = (f"https://raw.githubusercontent.com/UKGovernmentBEIS/inspect_evals/{INSPECT_EVALS}"
           "/src/inspect_evals/ipi_coding_agent/dataset/samples.json")
    recs = []
    for s in json.loads(_fetch(url, "codeipi/samples.json").read_text()):
        if s["is_benign"]:
            recs.append(_doc("codeipi", f"{s['id']}/issue", 0, s["issue_text"]))
            recs += [_doc("codeipi", f"{s['id']}/{f}", 0, c) for f, c in s["repo_files"].items()]
            continue
        if s["injection_vector"] == "issue_text":
            recs.append(_doc("codeipi", f"{s['id']}/issue", 1, s["issue_text"], attack=s["payload_category"]))
            continue
        marks = [m for m in s["payload_indicator"].split("|") if m]
        files = [f for f, c in s["repo_files"].items() if any(m in c for m in marks)] or \
                [f for f in s["repo_files"] if (s["injection_vector"] == "readme") == f.lower().startswith("readme")
                 and (s["injection_vector"] != "config_file" or f.endswith((".toml", ".cfg", ".ini", ".yaml", ".yml", ".json")))][:1]
        recs += [_doc("codeipi", f"{s['id']}/{f}", 1, s["repo_files"][f], attack=s["payload_category"]) for f in files]
    return _sample(recs, None)


def build_prodnull() -> list[dict]:
    from huggingface_hub import hf_hub_download

    path = hf_hub_download("prodnull/prompt-injection-repo-dataset", "train.jsonl", repo_type="dataset")
    recs = [_doc("prodnull", str(i), int(r["label"]), r["text"]) for i, r in
            enumerate(json.loads(line) for line in Path(path).read_text().splitlines() if line.strip())]
    return _sample(recs, None)


BUILDERS = {"swe-chat": build_swe_chat, "swe-hero": build_swe_hero, "agentdojo": build_agentdojo,
            "injecagent": build_injecagent, "llmail": build_llmail, "codeipi": build_codeipi, "prodnull": build_prodnull}


def build(only: list[str]) -> None:
    from huggingface_hub.errors import GatedRepoError

    DATA.mkdir(parents=True, exist_ok=True)
    for name in only:
        t = time.perf_counter()
        try:
            recs = BUILDERS[name]()
        except GatedRepoError:
            print(f"{name}: skipped, gated: accept its terms at https://{SOURCES[name][2]} (logged in as `hf auth whoami`)")
            continue
        (DATA / f"{name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
        pos = sum(r["label"] for r in recs)
        print(f"{name}: {len(recs)} documents ({pos} injected, {len(recs) - pos} benign) in {time.perf_counter() - t:.0f} s")


def load() -> list[dict]:
    recs = []
    for name in SOURCES:
        path = DATA / f"{name}.jsonl"
        if path.exists():
            recs += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    for r in recs:  # the half of the coding-agent output each document is in
        r["half"] = "calibrate" if zlib.crc32(r["id"].encode()) % 2 else "test"
    return recs


# --- Scoring ----------------------------------------------------------------------------------

class KeepmumDetector:
    """keepmum's own injection check: the pinned GGUF in llama-server, with keepmum's normalizer and windows."""

    device = "metal"

    def __init__(self):
        sys.path.insert(0, str(ROOT / "src"))
        from keepmum import guard

        self.model = guard.InjectionModel()

    def score(self, text: str) -> tuple[float, int]:
        return self.model.score(text), 0


def worker(name: str, device: str, out: Path) -> None:
    import os
    import resource

    os.environ["HF_HUB_OFFLINE"] = "1"
    t0 = time.perf_counter()
    det = KeepmumDetector() if name == "keepmum" else make(name, device)
    load_s = time.perf_counter() - t0
    det.score("warm up the kernels with a short sentence")
    scores = {}
    for r in load():
        t = time.perf_counter()
        s, _ = det.score(r["text"])
        scores[r["id"]] = {"score": s, "ms": (time.perf_counter() - t) * 1000}
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    out.write_text(json.dumps({"detector": name, "device": getattr(det, "device", device), "load_s": load_s,
                               "max_rss_mb": rss / 2**20, "scores": scores}))


# --- Report -----------------------------------------------------------------------------------

def _rate(flags: list[bool]) -> float | None:
    return sum(flags) / len(flags) if flags else None


def _p(x, pct: bool = True) -> str:
    if x is None:
        return "–"
    return f"{100 * x:.1f}%" if pct else f"{x:.3f}"


def _flags(sc: dict, threshold: float, short: tuple[int, float] | None = None):
    """Whether a record flags at `threshold`; with `short` (chars, threshold), one shorter than chars needs
    that threshold instead, as keepmum's check does (keepmum.guard.SHORT_CHARS)."""
    if short is None:
        return lambda r: sc[r["id"]]["score"] >= threshold
    return lambda r: sc[r["id"]]["score"] >= (short[1] if len(r["text"]) < short[0] else threshold)


def at(recs: list[dict], sc: dict, threshold: float, half: str | None = None, short: tuple[int, float] | None = None) -> dict:
    """Per source: the share of benign documents flagged and of injected ones caught. With `half`, the
    coding-agent sources count only that half."""
    flag = _flags(sc, threshold, short)
    out = {}
    for name in SOURCES:
        rs = [r for r in recs if r["source"] == name and r["id"] in sc and (half is None or name not in CODING or r["half"] == half)]
        out[name] = {"benign": _rate([flag(r) for r in rs if not r["label"]]),
                     "injected": _rate([flag(r) for r in rs if r["label"]]),
                     "n_benign": sum(1 for r in rs if not r["label"]), "n_injected": sum(1 for r in rs if r["label"])}
    return out


def summarize(run: dict, recs: list[dict]) -> dict:
    sc = run["scores"]
    calibrate = [sc[r["id"]]["score"] for r in recs if r["source"] in CODING and r["half"] == "calibrate" and r["id"] in sc]
    t = threshold_for(calibrate, 0.01) if calibrate else 0.5
    coding = [sc[r["id"]]["score"] for r in recs if r["source"] in CODING and r["id"] in sc]
    attacks = {name: [sc[r["id"]]["score"] for r in recs if r["source"] == name and r["label"] and r["id"] in sc] for name in SOURCES}
    ms = [v["ms"] for v in sc.values()]
    benign = sorted((r for r in recs if not r["label"] and r["id"] in sc), key=lambda r: -sc[r["id"]]["score"])
    missed = sorted((r for r in recs if r["label"] and r["id"] in sc), key=lambda r: sc[r["id"]]["score"])
    out = {"detector": run["detector"], "device": run["device"], "load_s": run["load_s"], "max_rss_mb": run["max_rss_mb"],
           "ms_p50": statistics.median(ms), "ms_p95": sorted(ms)[int(0.95 * (len(ms) - 1))], "threshold_1pct": t,
           "at_1pct": at(recs, sc, t, half="test"), "at_0.5": at(recs, sc, 0.5),
           "auc": {name: auc(s, coding) for name, s in attacks.items() if s},
           "top_benign": [(r["id"], round(sc[r["id"]]["score"], 3)) for r in benign[:15]],
           "lowest_injected": [(r["id"], round(sc[r["id"]]["score"], 3)) for r in missed[:15]]}
    out["by_tool"] = {"1%": by_tool(recs, sc, t)}
    if run["detector"] == "keepmum":
        sys.path.insert(0, str(ROOT / "src"))
        from keepmum.guard import LEVELS, SHORT_CHARS

        out["levels"] = {level: at(recs, sc, v.threshold, short=(SHORT_CHARS, v.short)) for level, v in LEVELS.items()}
        out["by_tool"] |= {level: by_tool(recs, sc, v.threshold, short=(SHORT_CHARS, v.short)) for level, v in LEVELS.items()}
    return out


def by_tool(recs: list[dict], sc: dict, threshold: float, short: tuple[int, float] | None = None) -> dict:
    """SWE-chat's tool results flagged, per tool: (documents, share flagged)."""
    flag = _flags(sc, threshold, short)
    tools: dict[str, list[bool]] = {}
    for r in recs:
        if r["source"] == "swe-chat" and r["id"] in sc:
            tools.setdefault(r.get("tool") or "?", []).append(flag(r))
    return {k: (len(v), _rate(v)) for k, v in sorted(tools.items(), key=lambda kv: -len(kv[1]))}


def _row(name: str, cells: dict) -> str:
    benign = [cells[s]["benign"] for s in SOURCES if cells[s]["n_benign"]]
    injected = [cells[s]["injected"] for s in SOURCES if cells[s]["n_injected"]]
    return "| " + name + " | " + " | ".join(_p(x) for x in benign) + " | " + " | ".join(_p(x) for x in injected) + " |"


def _head(first: str, cells: dict) -> list[str]:
    benign = [s for s in SOURCES if cells[s]["n_benign"]]
    injected = [s for s in SOURCES if cells[s]["n_injected"]]
    cols = [f"{s} benign flagged ({cells[s]['n_benign']})" for s in benign] + [f"{s} caught ({cells[s]['n_injected']})" for s in injected]
    return [f"| {first} | " + " | ".join(cols) + " |", "|" + " --- |" * (len(cols) + 1)]


def render(summaries: list[dict], recs: list[dict], env: dict) -> str:
    lines = ["# The injection check on public data", "",
             f"{env['date']} · {env['machine']} · torch {env['torch']} · transformers {env['transformers']}", "",
             "## Data", "", "| Source | Holds | Licence | Benign | Injected |", "| --- | --- | --- | --- | --- |"]
    for name, (holds, licence, where) in SOURCES.items():
        rs = [r for r in recs if r["source"] == name]
        if rs:
            pos = sum(r["label"] for r in rs)
            lines.append(f"| [{name}](https://{where.split(' ')[0]}) | {holds} | {licence} | {len(rs) - pos} | {pos} |")
    lines += ["", "Documents shorter than 40 characters are left out (keepmum does not score them); longer ones are cut at 16,000.", ""]
    if not summaries:
        return "\n".join(lines) + "\n"
    first = summaries[0]
    lines += ["## At 1% false alarms on coding-agent output", "",
              "Each detector's threshold flags 1% of one half of the SWE-chat and SWE-Hero tool output; the columns for "
              "those two sources are the other half. Every other column is the whole source at that threshold.", ""]
    lines += _head("Detector (threshold)", first["at_1pct"])
    for s in summaries:
        lines.append(_row(f"{s['detector']} ({s['threshold_1pct']:.3f})", s["at_1pct"]))
    lines += ["", "## At 0.5", ""] + _head("Detector", first["at_0.5"])
    for s in summaries:
        lines.append(_row(s["detector"], s["at_0.5"]))
    for s in summaries:
        if "levels" in s:
            lines += ["", "## keepmum at its sensitivity levels", "",
                      "The GGUF in llama-server as keepmum runs it, at each level's thresholds (keepmum.guard.LEVELS: "
                      "the second for a document under keepmum.guard.SHORT_CHARS), over every document.", ""]
            lines += _head("Level (thresholds)", s["levels"]["high"])
            sys.path.insert(0, str(ROOT / "src"))
            from keepmum.guard import LEVELS

            for level, cells in s["levels"].items():
                lines.append(_row(f"{level} ({LEVELS[level].threshold}, {LEVELS[level].short})", cells))
    cols = [(s["detector"], "1%") for s in summaries] + [("keepmum", lv) for s in summaries if "levels" in s for lv in ("high", "medium")]
    tools = next(iter(summaries[0]["by_tool"].values()))
    if tools:
        lines += ["", "## SWE-chat by tool", "",
                  "Real sessions' tool results flagged, per tool with at least 10 (all of them, both halves): each "
                  "detector at its 1% threshold, and keepmum at its high and medium levels.", "",
                  "| Tool (results) | " + " | ".join(f"{d} {lv}" for d, lv in cols) + " |", "|" + " --- |" * (len(cols) + 1)]
        by = {s["detector"]: s["by_tool"] for s in summaries}
        for tool, (n, _) in tools.items():
            if n >= 10:
                lines.append(f"| {tool} ({n}) | " + " | ".join(_p(by[d][lv][tool][1]) for d, lv in cols) + " |")
    lines += ["", "## Per detector", ""]
    for s in summaries:
        lines += [f"### {s['detector']}", "",
                  f"{s['device']}; loaded in {s['load_s']:.1f} s; {s['ms_p50']:.0f} / {s['ms_p95']:.0f} ms per document "
                  f"(p50 / p95); peak RSS {s['max_rss_mb']:.0f} MB.", "",
                  "- AUC against coding-agent output: " + ", ".join(f"{k} {_p(v, False)}" for k, v in s["auc"].items()),
                  "- highest-scored benign: " + ", ".join(f"`{i}` {v}" for i, v in s["top_benign"][:10]),
                  "- lowest-scored injected: " + ", ".join(f"`{i}` {v}" for i, v in s["lowest_injected"][:10]), ""]
    return "\n".join(lines) + "\n"


def run(a) -> None:
    recs = load()
    if not recs:
        raise SystemExit("no data: run `bench/guard_public.py build` first")
    if a.rescore:
        saved = json.loads(a.rescore.read_text())
        summaries = [summarize(r, recs) for r in saved["runs"]]
        saved["summaries"] = summaries
        a.rescore.write_text(json.dumps(saved, indent=1))
        a.rescore.with_suffix(".md").write_text(render(summaries, recs, saved["env"]))
        return print(a.rescore.with_suffix(".md"))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    tmp = a.out / f".guard-public-{stamp}"
    tmp.mkdir(parents=True, exist_ok=True)
    runs, summaries = [], []
    for name in a.detectors.split(","):
        f = tmp / f"{name}.json"
        print(f"{name}: scoring {len(recs)} documents", flush=True)
        p = subprocess.run([sys.executable, __file__, "--worker", name, "--device", a.device, "--worker-out", str(f)])
        if p.returncode or not f.exists():
            print(f"{name}: failed (exit {p.returncode}), skipped", flush=True)
            continue
        runs.append(json.loads(f.read_text()))
        s = summarize(runs[-1], recs)
        summaries.append(s)
        c = s["at_1pct"]
        print(f"{name}: threshold {s['threshold_1pct']:.3f}; caught " +
              ", ".join(f"{k} {_p(v['injected'])}" for k, v in c.items() if v["n_injected"]), flush=True)
    env = environment()
    out = a.out / f"guard-public-{stamp}.json"
    out.write_text(json.dumps({"env": env, "summaries": summaries, "runs": runs}, indent=1))
    out.with_suffix(".md").write_text(render(summaries, recs, env))
    for f in tmp.glob("*.json"):
        f.unlink()
    tmp.rmdir()
    print(out.with_suffix(".md"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    b = sub.add_parser("build", help="fetch the sources and write bench/data/guard-public/")
    b.add_argument("--only", help=f"comma-separated, from: {', '.join(SOURCES)}")
    r = sub.add_parser("run", help="score the data with each detector and write bench/results/guard-public-<stamp>.md")
    r.add_argument("--detectors", default=DEFAULT, help=f"comma-separated: keepmum, or from: {', '.join(DETECTORS)}")
    r.add_argument("--device", default="auto", help="auto (MPS when available), mps or cpu")
    r.add_argument("--out", type=Path, default=RESULTS)
    r.add_argument("--rescore", type=Path, help="re-render a guard-public-*.json from its saved scores")
    ap.add_argument("--worker", help=argparse.SUPPRESS)
    ap.add_argument("--worker-out", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--device", dest="worker_device", default="auto", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.worker:
        return worker(a.worker, a.worker_device, a.worker_out)
    if a.cmd == "build":
        return build(a.only.split(",") if a.only else list(SOURCES))
    if a.cmd == "run":
        return run(a)
    ap.print_help()


if __name__ == "__main__":
    main()
