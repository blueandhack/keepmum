"""Memory and CPU cost of an injection GGUF in llama-server, as keepmum runs it.

    uv run python bench/guard_cost.py NAME GGUF [N]

Scores the first N SWE-chat tool results (bench/data/guard-public/swe-chat.jsonl, from guard_public.py
build) in keepmum.guard.InjectionModel, then prints llama-server's resident memory and macOS footprint,
idle and after scoring, its CPU time, and the time per document. Any GGUF whose labels are [benign,
injection] runs: keepmum's own, or bench/horizon_gguf.py's Wolf Defender conversions.
"""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from keepmum import guard  # noqa: E402

name, gguf = sys.argv[1], Path(sys.argv[2])
n = int(sys.argv[3]) if len(sys.argv) > 3 else 600
docs = []
for line in open(ROOT / "bench/data/guard-public/swe-chat.jsonl"):
    t = json.loads(line)["text"][:16_000]
    if len(t) >= guard.MIN_CHARS:
        docs.append(t)
    if len(docs) == n:
        break


def server_pid() -> int:
    out = subprocess.run(["pgrep", "-f", re.escape(gguf.name)], capture_output=True, text=True).stdout.split()
    for pid in out:  # not keepmum's /bin/sh supervisor, whose command line names the model too
        comm = subprocess.run(["ps", "-o", "comm=", "-p", pid], capture_output=True, text=True).stdout.strip()
        if comm.endswith("llama-server"):
            return int(pid)
    raise SystemExit(f"no llama-server for {gguf.name}")


def cpu_s(pid: int) -> float:
    t = subprocess.run(["ps", "-o", "time=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    m, s = t.split(":")[-2:]
    h = t.split(":")[0] if t.count(":") == 2 else 0
    return int(h) * 3600 + int(m) * 60 + float(s)


def rss_mb(pid: int) -> float:
    return int(subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout) / 1024


def footprint_mb(pid: int) -> str:
    out = subprocess.run(["footprint", "-p", str(pid)], capture_output=True, text=True).stdout
    m = re.search(r"Footprint:\s*([\d.]+\s*[KMG]B)", out)
    return m.group(1) if m else "?"


model = guard.InjectionModel(gguf)
t0 = time.perf_counter()
model.score("warm up the kernels with a short sentence")
load_s = time.perf_counter() - t0
pid = server_pid()
idle = (rss_mb(pid), footprint_mb(pid))
c0 = cpu_s(pid)
ms, chars = [], sum(map(len, docs))
t0 = time.perf_counter()
for d in docs:
    t = time.perf_counter()
    model.score(d)
    ms.append((time.perf_counter() - t) * 1000)
wall = time.perf_counter() - t0
cpu = cpu_s(pid) - c0
ms.sort()
print(json.dumps({
    "model": name, "file_mb": round(gguf.stat().st_size / 1e6), "docs": len(docs), "chars": chars,
    "start_s": round(load_s, 2), "idle_rss_mb": round(idle[0]), "idle_footprint": idle[1],
    "busy_rss_mb": round(rss_mb(pid)), "busy_footprint": footprint_mb(pid),
    "p50_ms": round(ms[len(ms) // 2], 1), "p95_ms": round(ms[int(len(ms) * .95)], 1),
    "ms_per_10k_chars": round(wall * 1000 / chars * 10_000, 1),
    "server_cpu_s": round(cpu, 2), "wall_s": round(wall, 2), "cpu_per_wall": round(cpu / wall, 2),
    "cpu_ms_per_10k_chars": round(cpu * 1000 / chars * 10_000, 1),
}))
model.unload()
