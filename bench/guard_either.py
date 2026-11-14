"""Two injection detectors together: a tool result is flagged when either one flags it.

Reads saved scores only (no models run): a guard_public.py result with both detectors' runs, and a
guard.py result with the same pair. Each detector's threshold is set to flag a share of the calibrate
half of the coding-agent output (SWE-chat and SWE-Hero), so the pair together stays near the budget of
one detector alone; the test half gives the false alarms. The same thresholds then go on guard.py's
hand-written set (the first detector's guard.py run is the original model, as the GGUF scores like it).

    uv run --group torch python bench/guard_either.py bench/results/guard-public-<stamp>.json \
        bench/results/guard-<stamp>.json --pair keepmum,shieldstral-3b --guard-pair horizon-small,shieldstral-3b
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import guard  # noqa: E402
import guard_public as gp  # noqa: E402

SOURCES = ("agentdojo", "injecagent", "llmail", "codeipi", "prodnull")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("public", type=Path)
    ap.add_argument("handwritten", type=Path, nargs="+", help="guard.py result(s) holding the --guard-pair runs")
    ap.add_argument("--pair", default="keepmum,shieldstral-3b", help="the two detectors in the public result")
    ap.add_argument("--guard-pair", default="horizon-small,shieldstral-3b", help="the same two in the guard.py result")
    ap.add_argument("--heldout", action="store_true", help="only documents fine-tuning left out (ft_data.heldout)")
    ap.add_argument("--public-extra", type=Path, nargs="*", default=[], help="more guard-public results with runs to add")
    ap.add_argument("--splits", default="0.01:0,0:0.01,0.005:0.005,0.007:0.003,0.003:0.007",
                    help="share of calibrate output each detector may flag, as first:second")
    a = ap.parse_args()
    one, two = a.pair.split(",")
    sc = {}
    for f in [a.public, *a.public_extra]:
        for r in json.loads(f.read_text())["runs"]:
            sc.setdefault(r["detector"], r["scores"])
    recs = [r for r in gp.load() if r["id"] in sc[one] and r["id"] in sc[two]]
    if a.heldout:
        from ft_data import heldout

        recs = [r for r in recs if heldout(r)]
    cal = [r for r in recs if r["source"] in gp.CODING and r["half"] == "calibrate"]
    test = [r for r in recs if r["source"] in gp.CODING and r["half"] == "test"]

    def threshold(name: str, share: float) -> float:
        if share <= 0:
            return float("inf")
        v = sorted((sc[name][r["id"]]["score"] for r in cal), reverse=True)
        return v[max(0, int(share * len(v)) - 1)] + 1e-12

    g = {}
    for f in a.handwritten:
        for r in json.loads(f.read_text())["runs"]:
            g.setdefault(r["detector"], r["scores"])
    g1, g2 = a.guard_pair.split(",")
    data = {sp: guard.load(sp) for sp in ("dev", "holdout")}
    print(f"| {one} : {two} (thresholds) | coding output flagged | "
          + " | ".join(f"{s} caught" for s in SOURCES) + " | hand-written holdout: caught / ordinary / security |")
    print("| --- " * (len(SOURCES) + 3) + "|")
    for split in a.splits.split(","):
        s1, s2 = (float(x) for x in split.split(":"))
        t1, t2 = threshold(one, s1), threshold(two, s2)

        def flag(r, t1=t1, t2=t2):
            return sc[one][r["id"]]["score"] >= t1 or sc[two][r["id"]]["score"] >= t2

        cells = [f"{sum(map(flag, test)) / len(test):.1%}"]
        for src in SOURCES:
            pos = [r for r in recs if r["source"] == src and r["label"] == 1]
            cells.append(f"{sum(map(flag, pos)) / len(pos):.1%}")
        hold = [r for r in data["holdout"] if r["id"] in g[g1] and r["id"] in g[g2]]

        def hflag(r, t1=t1, t2=t2):
            return g[g1][r["id"]]["score"] >= t1 or g[g2][r["id"]]["score"] >= t2

        groups = ([r for r in hold if r["group"] == "injected"],
                  [r for r in hold if r["label"] == 0 and r["group"] in guard.ORDINARY],
                  [r for r in hold if r["label"] == 0 and r["group"] in guard.SECURITY])
        cells.append(" / ".join(f"{sum(map(hflag, xs)) / len(xs):.0%}" for xs in groups))
        print(f"| {s1:.1%} : {s2:.1%} ({t1:.3f}, {t2:.3f}) | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    main()
