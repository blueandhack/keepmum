"""Two injection detectors together: a tool result is flagged only when both flag it.

guard_either.py asks whether a second model catches what keepmum misses; this asks whether a second
model can veto keepmum's false alarms. keepmum's check stays at its sensitivity levels (keepmum.guard.LEVELS,
the short bar for a result under SHORT_CHARS), and a hit stands only if the second model also scores at
least its veto threshold. Reads saved scores only (no models run): a guard_public.py result with both
detectors' runs, and a guard.py result with the same pair (keepmum's model there is the fine-tune in
PyTorch, which scores like its GGUF).

    uv run python bench/guard_both.py bench/results/guard-public-<stamp>.json bench/results/guard-<stamp>.json \
        --second wolf-edge,wolf-small --vetoes 0.5,0.1,0.01,0.003
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "src"))
import guard  # noqa: E402
import guard_public as gp  # noqa: E402
from keepmum.guard import LEVELS, SHORT_CHARS  # noqa: E402

SOURCES = ("agentdojo", "injecagent", "llmail", "codeipi", "prodnull")
TOOLS = ("Read", "Agent", "Task", "AskUserQuestion", "TaskCreate", "Bash")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("public", type=Path)
    ap.add_argument("handwritten", type=Path, help="guard.py result holding --guard-first and the second detectors")
    ap.add_argument("--first", default="keepmum", help="keepmum's detector in the public result")
    ap.add_argument("--guard-first", default="horizon-small-ft", help="the same model in the guard.py result")
    ap.add_argument("--second", default="wolf-edge,wolf-small", help="detectors that may veto, comma-separated")
    ap.add_argument("--vetoes", default="0.5,0.1,0.01,0.003", help="the second detector's thresholds to try")
    ap.add_argument("--levels", default="medium,lowest", help="keepmum's levels")
    ap.add_argument("--heldout", action="store_true", help="only documents fine-tuning left out (ft_data.heldout)")
    a = ap.parse_args()
    sc = {r["detector"]: r["scores"] for r in json.loads(a.public.read_text())["runs"]}
    hw = {r["detector"]: r["scores"] for r in json.loads(a.handwritten.read_text())["runs"]}
    seconds = a.second.split(",")
    recs = [r for r in gp.load() if all(r["id"] in sc[d] for d in (a.first, *seconds))]
    if a.heldout:
        from ft_data import heldout

        recs = [r for r in recs if heldout(r)]
    hold = [r for r in guard.load("holdout") if all(r["id"] in hw[d] for d in (a.guard_first, *seconds))]
    groups = {"caught": [r for r in hold if r["group"] == "injected"],
              "ordinary": [r for r in hold if r["label"] == 0 and r["group"] in guard.ORDINARY],
              "security": [r for r in hold if r["label"] == 0 and r["group"] in guard.SECURITY]}
    benign = {s: [r for r in recs if r["source"] == s and r["label"] == 0] for s in ("swe-chat", "swe-hero", "prodnull")}
    injected = {s: [r for r in recs if r["source"] == s and r["label"] == 1] for s in SOURCES}
    tools = {t: [r for r in benign["swe-chat"] if r.get("tool") == t] for t in TOOLS}

    def share(xs, flag):
        n = sum(map(flag, xs))
        return f"{n / len(xs):.1%} ({n})" if xs else "-"

    head = (["Rule"] + [f"{s} benign flagged ({len(v)})" for s, v in benign.items()]
            + [f"{s} caught ({len(v)})" for s, v in injected.items()]
            + [f"swe-chat {t} flagged ({len(v)})" for t, v in tools.items()]
            + [f"hand-written holdout: caught ({len(groups['caught'])}) / ordinary ({len(groups['ordinary'])}) "
               f"/ security ({len(groups['security'])})"])
    print("| " + " | ".join(head) + " |")
    print("| --- " * len(head) + "|")
    for level in a.levels.split(","):
        lv = LEVELS[level]

        def first(scores, r, lv=lv):
            return scores[r["id"]]["score"] >= (lv.short if len(r["text"]) < SHORT_CHARS else lv.threshold)

        rules = [(f"keepmum {level} alone", lambda r: first(sc[a.first], r), lambda r: first(hw[a.guard_first], r))]
        for d in seconds:
            for v in (float(x) for x in a.vetoes.split(",")):
                rules.append((f"keepmum {level} and {d} ≥ {v:g}",
                              lambda r, d=d, v=v: first(sc[a.first], r) and sc[d][r["id"]]["score"] >= v,
                              lambda r, d=d, v=v: first(hw[a.guard_first], r) and hw[d][r["id"]]["score"] >= v))
        for name, flag, hflag in rules:
            cells = [share(v, flag) for v in benign.values()] + [share(v, flag) for v in injected.values()]
            cells += [share(v, flag) for v in tools.values()]
            cells.append(" / ".join(f"{sum(map(hflag, xs)) / len(xs):.0%}" for xs in groups.values()))
            print(f"| {name} | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    main()
