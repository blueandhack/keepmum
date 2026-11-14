"""Sensitivity levels for a new injection model, set to flag what the current levels flag.

keepmum.guard.LEVELS gives each level two thresholds: one for tool results of SHORT_CHARS or more,
a higher one for shorter results. For each level, this counts how many of SWE-chat's 2,000 real tool
results the current model flags, long and short apart, then finds the new model's two thresholds that
flag the same number of each. Both models are then compared at each level on the documents fine-tuning
left out (bench/ft_data.py), as keepmum runs them.

    uv run --group torch python bench/guard_levels.py bench/results/guard-public-<old>.json \\
        bench/results/guard-public-<new>.json --old keepmum --new keepmum-ft
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bench"))
sys.path.insert(0, str(ROOT / "src"))
import guard_public as gp  # noqa: E402
from ft_data import heldout  # noqa: E402
from keepmum.guard import LEVELS, SHORT_CHARS  # noqa: E402

SOURCES = ("swe-chat", "swe-hero", "agentdojo", "injecagent", "llmail", "codeipi", "prodnull")


def flags(sc, r, t, s):
    return sc[r["id"]]["score"] >= (s if len(r["text"]) < SHORT_CHARS else t)


def bar_for(scores: list[float], count: int) -> float:
    """The lowest threshold that flags at most `count` of these scores (three decimals)."""
    v = sorted(scores, reverse=True)
    if count >= len(v):
        return 0.5
    return round(min(0.999, v[count] + 0.0005), 3) if count < len(v) else 0.5


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("old", type=Path)
    ap.add_argument("new", type=Path)
    ap.add_argument("--old-name", default="keepmum")
    ap.add_argument("--new-name", default="keepmum-ft")
    a = ap.parse_args()
    old = next(r["scores"] for r in json.loads(a.old.read_text())["runs"] if r["detector"] == a.old_name)
    new = next(r["scores"] for r in json.loads(a.new.read_text())["runs"] if r["detector"] == a.new_name)
    recs = [r for r in gp.load() if r["id"] in old and r["id"] in new]
    chat = [r for r in recs if r["source"] == "swe-chat"]
    long_, short = [r for r in chat if len(r["text"]) >= SHORT_CHARS], [r for r in chat if len(r["text"]) < SHORT_CHARS]
    print(f"SWE-chat: {len(long_)} results of {SHORT_CHARS}+ characters, {len(short)} shorter")
    test = [r for r in recs if heldout(r)]
    levels = {}
    head = "| Level | model (thresholds) | " + " | ".join(
        f"{s} {'flagged' if s in ('swe-chat', 'swe-hero') else 'caught'}" for s in SOURCES) + " | prodnull benign flagged |"
    print(head)
    print("| --- " * (len(SOURCES) + 3) + "|")
    for name, lv in LEVELS.items():
        n_long = sum(flags(old, r, lv.threshold, lv.short) for r in long_)
        n_short = sum(flags(old, r, lv.threshold, lv.short) for r in short)
        t = bar_for([new[r["id"]]["score"] for r in long_], n_long)
        s = max(t, bar_for([new[r["id"]]["score"] for r in short], n_short))
        levels[name] = (t, s)
        for label, sc, tt, ss in (("now", old, lv.threshold, lv.short), ("new", new, t, s)):
            cells = []
            for src in SOURCES:
                rs = [r for r in test if r["source"] == src and r["label"] == (0 if src in ("swe-chat", "swe-hero") else 1)]
                cells.append(f"{sum(flags(sc, r, tt, ss) for r in rs) / len(rs):.1%}")
            neg = [r for r in test if r["source"] == "prodnull" and r["label"] == 0]
            cells.append(f"{sum(flags(sc, r, tt, ss) for r in neg) / len(neg):.1%}")
            print(f"| {name} | {label} ({tt}, {ss}) | " + " | ".join(cells) + " |")
    print(json.dumps(levels))


if __name__ == "__main__":
    main()
