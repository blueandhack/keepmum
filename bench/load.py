"""Load on keepmum serve: how its memory and CPU go over many requests.

    .venv/bin/python bench/load.py --requests 20000 --concurrency 8 [--rules-only] [--profile agent|unique]

Starts a fake Anthropic provider (a short streamed reply to every request) and `keepmum serve` in a
HOME of its own, pointed at it, then sends requests shaped like an agent's. `agent`: conversations
of 30 turns, each resent whole with the last one's tool call and its result added, as agents do;
`unique`: a new conversation every request, nothing ever seen twice (the worst case for keepmum's
caches). The tool results hold code, logs and a secret of their own, so detection, masking and
restore all run. Every second it samples keepmum's resident memory and CPU time, and its children's
(pf-serve), and prints a line every 10%; at the end, CPU per request and memory against requests.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- the provider -----------------------------------------------------------------------------

def provider_app():
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, StreamingResponse
    from starlette.routing import Route

    def events(model: str):
        yield ("message_start", {"type": "message_start", "message": {
            "id": "msg_load", "type": "message", "role": "assistant", "model": model, "content": [],
            "stop_reason": None, "usage": {"input_tokens": 1000, "output_tokens": 1}}})
        yield ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
        for piece in ("Done: ", "I read the file ", "and ran the tests."):
            yield ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": piece}})
        yield ("content_block_stop", {"type": "content_block_stop", "index": 0})
        yield ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 12}})
        yield ("message_stop", {"type": "message_stop"})

    async def messages(request):
        body = json.loads(await request.body())
        if not body.get("stream"):
            return JSONResponse({"id": "msg_load", "type": "message", "role": "assistant", "model": body.get("model", "m"),
                                 "content": [{"type": "text", "text": "Done."}], "stop_reason": "end_turn",
                                 "usage": {"input_tokens": 1000, "output_tokens": 2}})

        async def stream():
            for name, data in events(body.get("model", "m")):
                yield f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()
        return StreamingResponse(stream(), media_type="text/event-stream")

    return Starlette(routes=[Route("/v1/messages", messages, methods=["POST"])])


def run_provider(port: int) -> None:
    import uvicorn

    uvicorn.run(provider_app(), host="127.0.0.1", port=port, log_level="warning", access_log=False, lifespan="off")


# --- the traffic ------------------------------------------------------------------------------

WORDS = ("config", "deploy", "render", "parse", "client", "token", "cache", "build", "route", "query", "worker",
         "session", "handler", "schema", "index", "payload", "retry", "buffer", "stream", "export")


def secret(rng: random.Random) -> str:
    return "ghp_" + "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789") for _ in range(36))


def code(rng: random.Random, lines: int) -> str:
    out = []
    for i in range(lines):
        a, b = rng.choice(WORDS), rng.choice(WORDS)
        out.append(f"def {a}_{b}_{rng.randrange(10**6)}(x):\n    return {b}(x) + {i}  # {rng.choice(WORDS)} {rng.choice(WORDS)}")
    return "\n".join(out)


def tool_result(rng: random.Random, n: int) -> str:
    kind = n % 3
    if kind == 0:
        return code(rng, 40)
    if kind == 1:
        return "\n".join(f"2026-11-17T10:{rng.randrange(60):02d}:00Z INFO {rng.choice(WORDS)} {rng.randrange(10**9)} ok"
                         for _ in range(60))
    return f"GITHUB_TOKEN={secret(rng)}\nREGION=eu-west-{rng.randrange(3)}\nDB_URL=postgres://app:{secret(rng)[4:20]}@db.internal/app\n"


class Conversation:
    """One agent session: a growing list of messages, resent whole each turn."""

    def __init__(self, rng: random.Random, sid: int):
        self.rng, self.sid, self.turn = rng, sid, 0
        self.messages: list[dict] = [{"role": "user", "content": f"Session {sid}: fix the failing tests in the {rng.choice(WORDS)} module "
                                                                 f"and tell me what you changed."}]

    def body(self) -> dict:
        return {"model": "claude-load", "max_tokens": 1024, "stream": True,
                "system": [{"type": "text", "text": "You are a coding agent. " * 40}],
                "tools": [{"name": "Bash", "description": "Run a command", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}},
                          {"name": "Read", "description": "Read a file", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}],
                "messages": self.messages}

    def advance(self) -> None:
        """The model asked for a tool; its result comes back with the next request."""
        self.turn += 1
        tid = f"toolu_{self.sid}_{self.turn}"
        self.messages.append({"role": "assistant", "content": [
            {"type": "text", "text": f"Let me look at step {self.turn}."},
            {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": f"cat src/{self.rng.choice(WORDS)}.py"}}]})
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": tool_result(self.rng, self.turn)}]})


# --- measuring --------------------------------------------------------------------------------

def tree(pid: int) -> list[int]:
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True).stdout.split("\n")
    kids: dict[int, list[int]] = {}
    for line in out:
        parts = line.split()
        if len(parts) == 2:
            kids.setdefault(int(parts[1]), []).append(int(parts[0]))
    pids, todo = [], [pid]
    while todo:
        p = todo.pop()
        pids.append(p)
        todo.extend(kids.get(p, []))
    return pids


def _cpu(text: str) -> float:
    """ps TIME, as [[dd-]hh:]mm:ss.cc, in seconds."""
    days, _, rest = text.rpartition("-")
    parts = [float(x) for x in rest.split(":")]
    secs = sum(v * 60 ** i for i, v in enumerate(reversed(parts)))
    return secs + (int(days) * 86400 if days else 0)


def sample(pid: int) -> dict:
    """keepmum's own and its children's resident memory (MB) and CPU time (s)."""
    pids = tree(pid)
    out = subprocess.run(["ps", "-o", "pid=,rss=,time=,comm=", "-p", ",".join(map(str, pids))], capture_output=True, text=True).stdout
    me = {"rss": 0.0, "cpu": 0.0}
    kids = {"rss": 0.0, "cpu": 0.0}
    names = []
    for line in out.strip().split("\n"):
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        p, rss, cpu, comm = int(parts[0]), int(parts[1]) / 1024, _cpu(parts[2]), parts[3]
        target = me if p == pid else kids
        target["rss"] += rss
        target["cpu"] += cpu
        if p != pid:
            names.append(Path(comm).name)
    return {"rss": me["rss"], "cpu": me["cpu"], "kids_rss": kids["rss"], "kids_cpu": kids["cpu"], "kids": sorted(set(names))}


async def drive(url: str, total: int, concurrency: int, profile: str, seed: int, on_done) -> dict:
    import httpx

    rng = random.Random(seed)
    sid = 0
    sent = ok = 0
    errors: dict[str, int] = {}
    lock = asyncio.Lock()
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(base_url=url, timeout=120, limits=limits) as client:

        async def worker(w: int) -> None:
            nonlocal sid, sent, ok
            conv = None
            while True:
                async with lock:
                    if sent >= total:
                        return
                    sent += 1
                    if conv is None or profile == "unique" or conv.turn >= 30:
                        sid += 1
                        conv = Conversation(random.Random(rng.random()), sid)
                body = conv.body()
                try:
                    r = await client.post("/v1/messages", json=body, headers={"x-api-key": "sk-ant-load", "anthropic-version": "2023-06-01"})
                    text = r.text
                    if r.status_code == 200 and "message_stop" in text:
                        ok += 1
                    else:
                        key = f"{r.status_code}: {text[:80]}"
                        errors[key] = errors.get(key, 0) + 1
                except httpx.HTTPError as e:
                    errors[type(e).__name__] = errors.get(type(e).__name__, 0) + 1
                conv.advance()
                on_done()

        await asyncio.gather(*(worker(w) for w in range(concurrency)))
    return {"ok": ok, "errors": errors}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--requests", type=int, default=5000)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--profile", choices=["agent", "unique"], default="agent")
    p.add_argument("--rules-only", action="store_true", help="no privacy filter model")
    p.add_argument("--keepmum", default=str(ROOT / ".venv" / "bin" / "keepmum"), help="the keepmum to run")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--out", help="write the samples here, as JSON")
    p.add_argument("--keep-home", action="store_true")
    p.add_argument("--signal", action="store_true", help="send keepmum SIGUSR1 at the end (for a --keepmum wrapper)")
    args = p.parse_args()

    home = Path(tempfile.mkdtemp(prefix="keepmum-load-"))
    (home / ".config" / "keepmum").mkdir(parents=True)
    pport, kport = free_port(), free_port()
    (home / ".config" / "keepmum" / "config.toml").write_text(
        f'[server]\nport = {kport}\nupstream = "http://127.0.0.1:{pport}"\n\n[detect]\nmodel = {"false" if args.rules_only else "true"}\n')
    env = {**os.environ, "HOME": str(home)}
    try:
        from keepmum.detect.pfserve import find_binary, find_model

        if find_model():
            env["KEEPMUM_PF_MODEL"] = str(find_model())
        if find_binary():
            env["KEEPMUM_PF_SERVE"] = str(find_binary())
    except Exception:
        pass

    threading.Thread(target=run_provider, args=(pport,), daemon=True).start()
    proc = subprocess.Popen([args.keepmum, "serve", "--port", str(kport)] + (["--rules-only"] if args.rules_only else []),
                            env=env, stdout=subprocess.DEVNULL, stderr=open(home / "serve.log", "w"))
    url = f"http://127.0.0.1:{kport}"
    import httpx

    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            if httpx.get(url + "/_keepmum/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if proc.poll() is not None:
            print((home / "serve.log").read_text()[-2000:], file=sys.stderr)
            return 1
        time.sleep(0.3)
    else:
        print("keepmum did not answer", file=sys.stderr)
        proc.kill()
        return 1
    time.sleep(1)

    done = 0
    samples = []
    start = time.time()
    base = sample(proc.pid)
    samples.append({"t": 0.0, "n": 0, **base})
    print(f"started: keepmum {base['rss']:.0f} MB, children {base['kids_rss']:.0f} MB {base['kids']}", flush=True)
    stop = threading.Event()

    def sampler():
        while not stop.wait(1.0):
            s = sample(proc.pid)
            samples.append({"t": time.time() - start, "n": done, **s})

    def on_done():
        nonlocal done
        done += 1
        if done % max(1, args.requests // 10) == 0:
            s = sample(proc.pid)
            el = time.time() - start
            print(f"{done:>9} requests  {done / el:6.0f}/s  keepmum {s['rss']:6.0f} MB {s['cpu'] - base['cpu']:7.1f} s CPU"
                  f"  children {s['kids_rss']:6.0f} MB {s['kids_cpu'] - base['kids_cpu']:7.1f} s CPU", flush=True)

    threading.Thread(target=sampler, daemon=True).start()
    result = asyncio.run(drive(url, args.requests, args.concurrency, args.profile, args.seed, on_done))
    elapsed = time.time() - start
    stop.set()
    time.sleep(1.5)
    idle0 = sample(proc.pid)
    time.sleep(10)
    idle1 = sample(proc.pid)
    end = idle0
    print(f"\n{result['ok']} of {args.requests} ok in {elapsed:.1f} s ({args.requests / elapsed:.0f}/s); errors: {result['errors'] or 'none'}")
    print(f"CPU per request: keepmum {1000 * (end['cpu'] - base['cpu']) / args.requests:.2f} ms, "
          f"children {1000 * (end['kids_cpu'] - base['kids_cpu']) / args.requests:.2f} ms")
    print(f"memory: keepmum {base['rss']:.0f} -> {end['rss']:.0f} MB (peak {max(s['rss'] for s in samples):.0f}), "
          f"children {base['kids_rss']:.0f} -> {end['kids_rss']:.0f} MB")
    half = [s for s in samples if s["n"] >= args.requests // 2]
    if half and half[-1]["n"] > half[0]["n"]:
        slope = (half[-1]["rss"] - half[0]["rss"]) / (half[-1]["n"] - half[0]["n"]) * 1e6
        print(f"memory growth over the second half: {slope:.0f} MB per million requests")
    print(f"idle for 10 s after: keepmum {idle1['cpu'] - idle0['cpu']:.2f} s CPU, children {idle1['kids_cpu'] - idle0['kids_cpu']:.2f} s CPU")
    if args.out:
        Path(args.out).write_text(json.dumps({"args": vars(args), "samples": samples, "result": result}, indent=1))
    if args.signal:
        import signal

        os.kill(proc.pid, signal.SIGUSR1)  # a --keepmum wrapper's cue, such as a tracemalloc dump
        time.sleep(20)
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()
    if args.keep_home:
        print(f"HOME: {home}")
    else:
        shutil.rmtree(home, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
