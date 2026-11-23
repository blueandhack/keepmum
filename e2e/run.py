"""Run cases against real agents: `python -m e2e run`. See e2e/README.md."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import cost

REAL_HOME = os.environ["HOME"]
RESULTS = Path(__file__).parent / "results"
PROVIDERS = {"anthropic": "https://api.anthropic.com", "openai": "https://api.openai.com", "chatgpt": "https://chatgpt.com",
             "antigravity": "https://daily-cloudcode-pa.googleapis.com", "gemini": "https://generativelanguage.googleapis.com",
             "deepseek": "https://api.deepseek.com"}
# Local mode's server (Ollama by default) and model: the tap sends `/local/...` there in every mode, so
# what keepmum asks of it is recorded, apart from what goes to the provider.
LOCAL_URL = os.environ.get("KEEPMUM_E2E_LOCAL_URL", "http://localhost:11434").rstrip("/")
LOCAL_MODEL = os.environ.get("KEEPMUM_E2E_LOCAL_MODEL", "qwen3:0.6b")
GUARD_DIR = Path(REAL_HOME) / ".local" / "share" / "keepmum" / "guard"
SCRIPTED_TIMEOUT = 120  # seconds; a case's own `timeout` wins


def _environment() -> None:
    """Found where this Mac keeps them, read only, before HOME moves (as tests/conftest.py does)."""
    pf = Path(REAL_HOME) / ".local" / "share" / "keepmum" / "bin" / "pf-serve"
    if pf.exists():
        os.environ.setdefault("KEEPMUM_PF_SERVE", str(pf))
    # The model too: `keepmum setup` keeps it in keepmum's own folder under the real HOME, which a
    # run's HOME is not (the Hugging Face cache, set below, is read where it is).
    from keepmum.detect.pfserve import find_model

    if model := find_model():
        os.environ.setdefault("KEEPMUM_PF_MODEL", str(model))
    os.environ.setdefault("HF_HOME", str(Path(REAL_HOME) / ".cache" / "huggingface"))
    os.environ.setdefault("CODEX_HOME", str(Path(REAL_HOME) / ".codex"))


def model_ready() -> bool:
    from keepmum.detect.pipeline import ModelUnavailable, default_pipeline

    try:
        default_pipeline().pf.close()
        return True
    except (ModelUnavailable, Exception):
        return False


def missing_needs(case) -> str | None:
    """Why this Mac cannot run the case (`needs`), or None."""
    if "injection" in case.needs:
        from keepmum import guard
        from keepmum.llamacpp import find_server

        if not guard.installed():  # HOME is still this Mac's own here
            return "needs the injection check's model (keepmum guard setup injection)"
        if find_server() is None:
            return "needs llama-server (brew install llama.cpp)"
    if "harm" in case.needs:
        from keepmum import harm
        from keepmum.llamacpp import find_server

        if not harm.installed(harm.DEFAULT):  # HOME is still this Mac's own here
            return f"needs the harm check's model (keepmum guard setup harm --model {harm.DEFAULT})"
        if find_server() is None:
            return "needs llama-server (brew install llama.cpp)"
    if "local" in case.needs:
        from keepmum.local import models

        have = models(LOCAL_URL)
        if have is None:
            return f"needs a local model server at {LOCAL_URL} (KEEPMUM_E2E_LOCAL_URL)"
        if LOCAL_MODEL not in have:
            return f"needs {LOCAL_MODEL} at {LOCAL_URL} (KEEPMUM_E2E_LOCAL_MODEL)"
    return None


def run_one(case, agent, mode: str, rep: int, model: str | None, extra: list[str], keep: Path | None,
            keep_all: bool = True) -> dict:
    """keep: where to write the run's transcripts and requests; with keep_all False, only a run that
    failed is written (a failure that does not come back is otherwise lost)."""
    from keepmum import launch
    from keepmum.config import load

    from . import oracle
    from .scripted import ScriptedModel
    from .servers import Tap, ThreadServer

    scratch = Path(tempfile.mkdtemp(prefix="keepmum-e2e-")).resolve()
    khome = scratch / "home"
    cfg_dir = khome / ".config" / "keepmum"
    cfg_dir.mkdir(parents=True, mode=0o700)
    salt = secrets.token_bytes(32)
    (cfg_dir / "salt").write_text(salt.hex())
    os.chmod(cfg_dir / "salt", 0o600)
    ws = scratch / "w" / case.resolve(case.workspace_dir, salt)
    ws.mkdir(parents=True)
    if {"injection", "harm"} & set(case.needs) and GUARD_DIR.exists():  # the models, read only, from this Mac's own folder
        data = khome / ".local" / "share" / "keepmum"
        data.mkdir(parents=True)
        (data / "guard").symlink_to(GUARD_DIR)
    for rel, content in case.files.items():
        p = ws / case.resolve(rel, salt)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(case.resolve(content, salt))
    ws2 = scratch / "w2" / "ws"  # the companion's (case.companion): its own folder, the same keepmum
    for rel, content in case.companion.get("files", {}).items():
        p = ws2 / case.resolve(rel, salt)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(case.resolve(content, salt))
    # Commands of the case's own, before and after the agent: in the run's HOME, so the user's git
    # config (signing, hooks, their name) is not what a case's repository gets.
    shell_env = {**os.environ, "HOME": str(khome), "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    for cmd in case.setup:
        cmd = case.resolve(cmd, salt).replace("{{repo}}", str(Path(__file__).resolve().parent.parent))  # files of this repo
        done = subprocess.run(cmd, shell=True, cwd=ws, env=shell_env, capture_output=True, text=True)
        if done.returncode:
            raise RuntimeError(f"{case.id}: setup `{case.shown(cmd)}` failed: {case.shown(done.stderr or done.stdout)}")
    before = oracle.snapshot(ws)

    scripted = None
    live_vault = {}

    def seen(value: str) -> str | None:
        """The placeholder the model was shown for value: its own, else the shortest that holds it."""
        vault = live_vault.get("vault")
        if vault is None:
            return None
        items = list(vault._values.items())
        exact = [ph for ph, v in items if v == value]
        if exact:
            return exact[0]
        wider = sorted((ph for ph, v in items if value in v), key=lambda ph: len(vault._values[ph]))
        return wider[0] if wider else None

    def mask(text: str) -> str:
        """text as the model saw it: each value masked in this run replaced by its placeholder."""
        vault = live_vault.get("vault")
        known = vault.known_values() if vault is not None else None
        if known is None:
            return text
        return known.sub(lambda m: vault.placeholder(vault.kind_of(m.group()), m.group()), text)

    seen.mask = mask

    if mode == "scripted":
        scripted = ScriptedModel(case.script, lambda t, cwd: case.resolve(t, salt, seen=seen).replace("{{cwd}}", cwd))
        targets = dict.fromkeys(PROVIDERS, scripted.app)
    else:
        targets = dict(PROVIDERS)
    targets["local"] = LOCAL_URL
    tap = Tap(targets)
    tap_server = ThreadServer(tap.app).start()
    (cfg_dir / "config.toml").write_text(case.config.replace("{{local}}", f"{tap_server.url}/local")
                                         .replace("{{local_model}}", LOCAL_MODEL).replace("{{ws}}", str(ws)))
    servers = [tap_server]
    out = scratch / "out"
    out.mkdir()
    if mode == "scripted" and "timeout" not in case.raw:
        case.timeout = SCRIPTED_TIMEOUT  # a scripted run takes seconds: a hang should not cost five minutes
    started = time.monotonic()
    os.environ["HOME"] = str(khome)  # keepmum's own: salt, config, audit log
    proxy = None
    proxies = []  # every keepmum the run had: more than one after a restart (restart_after)

    def make_proxy():
        p = launch.build_proxy(load(), rules_only=not case.model, upstream=f"{tap_server.url}/anthropic", mode="serve")
        p.prefixed["/openai"] = f"{tap_server.url}/openai"
        p.prefixed["/chatgpt"] = f"{tap_server.url}/chatgpt"
        p.prefixed["/antigravity"] = f"{tap_server.url}/antigravity"
        p.prefixed["/gemini"] = f"{tap_server.url}/gemini"
        # DeepSeek is another gateway keepmum sits in front of (--provider deepseek; gateway.py).
        p.gateways["deepseek"] = f"{tap_server.url}/deepseek"
        proxies.append(p)
        return p

    try:
        built = time.monotonic()
        proxy = make_proxy()
        built = time.monotonic() - built
        live_vault["vault"] = proxy.redactor.vault
        restarted = threading.Event()

        class Switch:
            """keepmum as the agent reaches it. With restart_after, the first request after the Nth
            model reply finds a new keepmum, built then: its vault, caches and stats new, as after
            an update or a crash (what is on disk, the salt and the flagged list, carries over).
            A request already in flight finishes on the old one."""

            def __init__(self, first):
                self.current, self.info = first, getattr(first, "info", {})

                async def app(scope, receive, send):  # a function: uvicorn takes a bound method for ASGI 2
                    await self.dispatch(scope, receive, send)

                self.app = app

            def __getattr__(self, name):  # the rest is the current keepmum's
                if name == "current":
                    raise AttributeError(name)
                return getattr(self.current, name)

            async def dispatch(self, scope, receive, send):
                if (case.restart_after and not restarted.is_set() and scope["type"] == "http"
                        and sum(ex.method == "POST" and ex.meta.get("done", False) for ex in tap.exchanges)
                        >= case.restart_after):
                    restarted.set()
                    import asyncio

                    self.current = await asyncio.to_thread(make_proxy)
                    live_vault["vault"] = self.current.redactor.vault
                await self.current.app(scope, receive, send)

        server = launch.BackgroundServer(Switch(proxy))
        server.start()
        servers.append(server)
        mcp = None
        if case.mcp:
            mcp = {"name": "notes", "command": sys.executable, "args": [str(Path(__file__).parent / "mcp_notes.py")],
                   "env": {"KEEPMUM_E2E_MCP_LOG": str(out / "mcp.jsonl"),
                           "KEEPMUM_E2E_MCP_REPLY": case.resolve(case.raw.get("mcp_reply", "no record"), salt)}}
        argv, env = agent.command(ws, server.port, case.resolve(case.prompt, salt), REAL_HOME, out, model,
                                     [*extra, *case.agent_args.get(agent.name, [])], mcp, mode)
        # Where the agent works: some take it from PWD, which is the harness's own folder otherwise
        # (opencode read this repository as its project before this was set).
        env["PWD"] = str(ws)
        companion = None
        if case.companion:
            # A second agent at the same time, in its own folder, through the same keepmum: the user
            # with Claude Code in one window and Codex in another. "same": another session of this one.
            from .agents import AGENTS

            name = case.companion.get("agent", "same")
            other = agent if name == "same" else AGENTS[name]
            out2 = scratch / "out2"
            out2.mkdir()
            argv2, env2 = other.command(ws2, server.port, case.resolve(case.companion["prompt"], salt), REAL_HOME, out2,
                                        None, list(case.agent_args.get(other.name, [])), None, mode)
            env2["PWD"] = str(ws2)
            companion = subprocess.Popen(argv2, env=env2, cwd=ws2, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        session = time.monotonic()
        try:
            done = subprocess.run(argv, env=env, cwd=ws, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  timeout=case.timeout)
            code, stdout, stderr = done.returncode, done.stdout, done.stderr
        except subprocess.TimeoutExpired as e:
            code = None
            stdout = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            stderr = (e.stderr or b"").decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        companion_code = None
        if companion is not None:
            try:
                companion.communicate(timeout=case.timeout)
            except subprocess.TimeoutExpired:
                companion.kill()
                companion.communicate()
            companion_code = companion.returncode
        session = time.monotonic() - session
        # Warn mode checks after the request was sent: let those finish before reading what was found.
        drained = time.monotonic()
        deadline = time.monotonic() + 60
        while any(p._background for p in proxies) and time.monotonic() < deadline:
            time.sleep(0.1)
        drained = time.monotonic() - drained
        masked = {ph: (ph.split("_")[1], value) for p in proxies for ph, value in p.redactor.vault._values.items()}
        # Which detectors masked each value (rules:<id>, pf-serve, known, decoded): never the value, so a
        # false mask names its cause in results.json.
        sources = {ph: sorted(p.redactor.vault.sources_of(value) or ()) for p in proxies for ph, value in p.redactor.vault._values.items()}
        events = [e for p in proxies for e in p.stats.events]
        hits = [h for p in proxies for h in p.stats.snapshot()["guard"]["hits"]]
        posts = [ex for ex in tap.exchanges if ex.method == "POST" and "t1" in ex.meta and "t0" in ex.meta]
        scans = [e.get("scan_ms") for e in events if e.get("scan_ms") is not None]
        timing = {"build_s": round(built, 2), "session_s": round(session, 2), "drain_s": round(drained, 2),
                  "requests": len(posts), "provider_s": round(sum(ex.meta["t1"] - ex.meta["t0"] for ex in posts), 2),
                  "scan_s": round(sum(scans) / 1000, 2), "scan_max_ms": round(max(scans), 1) if scans else 0}
        keepmum = {"unresolved": sum(p.stats.unresolved for p in proxies), "timing": timing,
                   "restarted": restarted.is_set(),
                   "hits": [{k: h.get(k) for k in ("check", "action", "source", "score", "chars")} for h in hits],
                   "warned": sorted({h.get("check") for h in hits if h.get("action") == "warned"}),
                   # The Tools page: the network tools the agent offered, each with what it says it does.
                   "tools": [s for p in proxies if p.tools is not None for s in p.tools.snapshot(p.policy)],
                   # Refused: blocked, or meant for a local model that did not answer (sent nowhere).
                   "blocked": sorted({h.get("check") for h in hits if h.get("action") == "blocked"
                                      or (h.get("action") == "local" and h.get("local_error"))}),
                   "errors": [{"path": e.get("path"), "status": e.get("status"), "outcome": e.get("outcome")}
                              for e in events if (e.get("status") or 0) >= 400],
                   # Hits local mode acted on: whether a local model answered, and what was handed back.
                   "local": [{"check": h.get("check"), "answered": bool(h.get("answered_by")),
                              "handed_back": (h.get("handed_back") or {}).get("strategy"),
                              "kept": (h.get("handed_back") or {}).get("kept"), "error": h.get("local_error")}
                             for h in hits if h.get("action") == "local"]}
    finally:
        if (other := locals().get("companion")) is not None and other.poll() is None:
            other.kill()
        for s in reversed(servers):
            s.stop()
        # The checks' models run in llama-server, started on their first check and otherwise
        # stopped only after a while unused: stop them with the run, or each run leaves one behind.
        for g in (g for p in proxies for g in p.guards):
            for model in (g.model, getattr(g.model, "_model", None)):  # the harm check wraps its model
                with contextlib.suppress(Exception):
                    getattr(model, "unload", lambda: None)()
        os.environ["HOME"] = REAL_HOME
    after = oracle.snapshot(ws)
    outputs = []
    for want in case.expect.get("run", []):
        try:
            done = subprocess.run(case.resolve(want["cmd"], salt), shell=True, cwd=ws, env=shell_env,
                                  capture_output=True, text=True, timeout=want.get("timeout", 60))
            outputs.append((want, done.returncode, done.stdout + done.stderr))
        except subprocess.TimeoutExpired:
            outputs.append((want, None, ""))
    transcript = agent.transcript(stdout, out)
    mcp_log = (out / "mcp.jsonl").read_text() if (out / "mcp.jsonl").exists() else ("" if case.mcp else None)
    provider = [ex for ex in tap.exchanges if ex.target != "local"]
    local = [ex for ex in tap.exchanges if ex.target == "local"]  # stays on this Mac: real values belong there
    findings = oracle.judge(case, salt, provider, before, after, transcript, masked, keepmum, code,
                            scripted.served if scripted else None, mcp_log, local)
    findings += oracle.ran(case, salt, outputs)
    for rel, want in case.companion.get("expect_files", {}).items():
        path = ws2 / case.resolve(rel, salt)
        text = path.read_text() if path.exists() else None
        for how, wanted in (want.items() if isinstance(want, dict) else [("contains", want)]):
            wanted = case.resolve(wanted, salt)
            ok = text is not None and ((wanted in text) if how == "contains" else (wanted not in text))
            if not ok:
                findings.append(oracle.Finding("task", f"the companion's {rel} {'is missing' if text is None else how + ' failed'}: "
                                                       f"{case.shown(wanted)[:80]!r} (companion exit {companion_code})", "companion"))
    if case.restart_after and not keepmum.get("restarted"):
        findings.append(oracle.Finding("task", f"keepmum was never restarted: the agent finished before "
                                               f"{case.restart_after} model replies", "restart_after"))
    failed = [f for f in findings if f.failing()]
    # The provider refused to answer (a usage limit): the run says nothing about keepmum.
    limited = any(e["status"] == 429 for e in keepmum["errors"]) and not any(f.cls == "leak" for f in findings)
    result = {
        "case": case.id, "agent": agent.name, "mode": mode, "rep": rep, "seconds": round(time.monotonic() - started, 1),
        "status": "skip" if limited else ("gap" if failed else "gap-passed") if case.gap(agent.name, mode)
        else ("fail" if failed else "pass"),
        "reason": "the provider's usage limit (429): nothing was tested" if limited else None,
        "known_gap": case.gap(agent.name, mode), "findings": oracle.as_dicts(findings), "exit": code,
        "requests": len(provider), "local_requests": len(local), "masked": len(masked), "sources": sources,
        # KEEPMUM_E2E_VALUES=1: what each placeholder stood for, to debug a false mask. Off by default: a
        # case's secrets are made up, but a results folder is no place for values.
        **({"values": {ph: v for ph, (_, v) in masked.items()}} if os.environ.get("KEEPMUM_E2E_VALUES") else {}),
        "turns": [s["turn"] for s in scripted.served] if scripted else None,
        "tools": sorted(scripted.tools) if scripted else None,
        "code_mode": next((s.get("code_mode") for s in (scripted.served if scripted else []) if "code_mode" in s), None),
        "usage": cost.of(provider).as_dict() if mode == "live" else None,
        "timing": keepmum.get("timing"),
    }
    if keep is not None and (keep_all or result["status"] in ("fail", "gap-passed")):
        d = keep / f"{case.id}.{agent.name}.{mode}.{rep}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "stdout.jsonl").write_text(case.shown(stdout))
        (d / "stderr.txt").write_text(case.shown(stderr))
        (d / "requests.jsonl").write_text("".join(json.dumps({
            "target": ex.target, "method": ex.method, "path": ex.path, "status": ex.status,
            "headers": sorted(ex.headers),  # names only: values may be the agent's login
            "body": case.shown(ex.body.decode("utf-8", "replace")),
            "reply": case.shown(ex.reply.decode("utf-8", "replace"))}) + "\n" for ex in provider))
        if local:
            (d / "local.jsonl").write_text("".join(json.dumps({
                "path": ex.path, "status": ex.status, "body": case.shown(ex.body.decode("utf-8", "replace")),
                "reply": case.shown(ex.reply.decode("utf-8", "replace"))}) + "\n" for ex in local))
        if mcp_log:
            (d / "mcp.jsonl").write_text(case.shown(mcp_log))
        (d / "workspace.json").write_text(json.dumps({case.shown(k): case.shown(v.decode("utf-8", "replace"))
                                                      for k, v in after.items()}, indent=1))
        result["kept"] = str(d)
    shutil.rmtree(scratch, ignore_errors=True)
    return result


def _line(r: dict) -> str:
    mark = {"pass": "✓", "fail": "✗", "gap": "◌", "gap-passed": "!", "skip": "–"}[r["status"]]
    head = f"{mark} {r['case']:<34} {r['agent']:<7} {r['mode']:<9} #{r['rep']} {r.get('seconds', 0):>6}s"
    if r.get("usage"):
        head += f"  {cost.dollars(r['usage']):>9}"
    lines = [head]
    for f in r.get("findings", []):
        if f["cls"] != "shown" or r["status"] != "pass":
            lines.append(f"      {f['cls']:<10} {f['what']}" + (f"  [{f['where']}]" if f["where"] else ""))
    if r.get("reason"):
        lines.append(f"      {r['reason']}")
    return "\n".join(lines)


def uses_models(case) -> bool:
    """Whether a run uses a model on this Mac (a check's, the local model's or the Privacy Filter):
    such runs go one at a time, in this runner and across runners (a lock file)."""
    return bool({"injection", "harm", "local"} & set(case.needs)) or bool(case.model)


@contextlib.contextmanager
def _models_lock(case):
    if not uses_models(case):
        yield
        return
    import fcntl

    RESULTS.mkdir(parents=True, exist_ok=True)
    with open(RESULTS / ".models.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _run(case, agent, mode: str, rep: int, model, extra, keep, keep_all: bool = True) -> dict:
    """One run, its harness errors turned into a finding rather than a crash of the whole run."""
    try:
        with _models_lock(case):
            return run_one(case, agent, mode, rep, model, extra, keep, keep_all)
    except Exception as e:
        return {"case": case.id, "agent": agent.name, "mode": mode, "rep": rep, "status": "fail", "findings": [
            {"cls": "health", "what": f"harness error: {type(e).__name__}: {e}", "where": "e2e"}]}


def _job(case_id: str, agent_name: str, mode: str, rep: int, model, extra, keep, keep_all: bool, auth: dict) -> dict:
    """A run in a worker process of its own (`--jobs`): run_one moves HOME, which is process-wide."""
    from . import agents as agents_mod
    from .case import all_cases

    _environment()
    agents_mod.AUTH.update(auth)
    case = next(c for c in all_cases([case_id]) if c.id == case_id)
    return _run(case, agents_mod.AGENTS[agent_name], mode, rep, model, extra, keep, keep_all)


def _parallel(jobs: list, args, keep, auth: dict, report) -> None:
    """Runs jobs in `args.jobs` worker processes. Runs that use a model go one at a time. Live, an
    agent runs once at a time: its login (a token it refreshes, a session list) is the user's own."""
    import multiprocessing
    from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait

    pending, running = list(jobs), {}
    with ProcessPoolExecutor(max_workers=args.jobs, mp_context=multiprocessing.get_context("spawn")) as pool:
        while pending or running:
            agents_busy = {j[1].name for j in running.values()}
            models_busy = any(uses_models(j[0]) for j in running.values())
            for job in list(pending):
                if len(running) >= args.jobs:
                    break
                case, agent, rep = job
                if (args.mode == "live" and agent.name in agents_busy) or (uses_models(case) and models_busy):
                    continue
                pending.remove(job)
                agents_busy.add(agent.name)
                models_busy = models_busy or uses_models(case)
                running[pool.submit(_job, case.id, agent.name, args.mode, rep, args.model, args.agent_arg, keep,
                                    args.keep, auth)] = job
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for f in done:
                report(running.pop(f), f.result())


def main(argv: list[str] | None = None) -> int:
    from .agents import AGENTS
    from .case import all_cases

    p = argparse.ArgumentParser(prog="python -m e2e")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    r = sub.add_parser("run")
    r.add_argument("cases", nargs="*", help="case ids or prefixes (default: all)")
    r.add_argument("--agent", action="append", choices=sorted(AGENTS), help="repeatable (default: each installed one)")
    r.add_argument("--mode", choices=["scripted", "live"], default="scripted")
    r.add_argument("--repeat", type=int, default=1, help="runs per case and agent (live: measures flakiness)")
    r.add_argument("--model", help="the agent's model (live)")
    r.add_argument("--auth", choices=["login", "keys"], default="login",
                   help="live: the user's logins, or API keys from KEEPMUM_OPENAI_API_KEY and KEEPMUM_CLAUDE_API_KEY "
                        "(op run --env-file e2e/keys.env -- ...)")
    r.add_argument("--cheap", action="store_true", help="--auth keys: each agent's smallest model")
    r.add_argument("--provider", choices=["deepseek"],
                   help="--auth keys: DeepSeek (KEEPMUM_DEEPSEEK_API_KEY) through keepmum as a gateway, for the "
                        "agents that speak the Messages API (Claude Code, pi, opencode)")
    r.add_argument("--agent-arg", action="append", default=[], help="extra argument for the agent, repeatable")
    r.add_argument("--keep", action="store_true", help="keep transcripts and requests (values shown as {{v:name}})")
    r.add_argument("-j", "--jobs", type=int, default=1,
                   help="runs at once, each in a process of its own; runs that use a model on this Mac still go one "
                        "at a time, and live, each agent runs once at a time (default 1)")
    r.add_argument("--check-tools", action="store_true",
                   help="fail on a tool an agent offers that e2e/tools.toml does not list (scripted mode)")
    args = p.parse_args(argv)
    _environment()
    cases = all_cases(getattr(args, "cases", None))
    if args.cmd == "list":
        for c in cases:
            gap = "  [known gap]" if c.known_gap else ""
            print(f"{c.id:<36} {','.join(c.agents):<14} {','.join(c.modes):<16} {c.title}{gap}")
        return 0
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = RESULTS / f"{args.mode}-{stamp}"
    # Two runners started in the same second (release-check.sh runs agy beside the others) each get a folder.
    for n in range(2, 100):
        try:
            out.mkdir(parents=True)
            break
        except FileExistsError:
            out = RESULTS / f"{args.mode}-{stamp}-{n}"
    keep = out  # every run with --keep; without it, the runs that failed
    agents = [AGENTS[a] for a in (args.agent or AGENTS)]
    if args.cmd == "run" and args.mode == "live":
        from . import agents as agents_mod
        agents_mod.AUTH.update(mode=args.auth, cheap=args.cheap, provider=args.provider)
    model_ok = None
    results, jobs = [], []
    how = (f" ({'API keys' + (', DeepSeek' if args.provider else '') + (', cheapest models' if args.cheap else '')})"
           if args.mode == "live" and args.auth == "keys" else "")
    print(f"keepmum e2e, {args.mode}{how}: " + ", ".join(f"{a.name} {a.version()}" for a in agents if a.available()))
    for case in cases:
        for agent in agents:
            base = {"case": case.id, "agent": agent.name, "mode": args.mode, "rep": 0}
            reason = None
            if agent.name not in case.agents:
                continue
            if args.mode not in case.modes or args.mode not in agent.modes:
                continue
            if not agent.available():
                reason = f"{agent.binary} is not installed"
            elif args.mode == "live" and args.auth == "keys" and args.provider == "deepseek" and not agent.deepseek:
                reason = f"{agent.name} does not speak the Messages API, DeepSeek's format keepmum scans"
            elif args.mode == "live" and args.auth == "keys" and not agent.key_auth:
                reason = f"{agent.name} signs in with Google, not an OpenAI or Anthropic key"
            elif case.mcp and args.mode not in agent.mcp_modes:
                reason = f"{agent.name} gets no MCP server in {args.mode} runs"
            elif missing := missing_needs(case):
                reason = missing
            elif case.model:
                model_ok = model_ready() if model_ok is None else model_ok
                if not model_ok:
                    reason = "needs pf-serve and the Privacy Filter model (keepmum setup)"
            if reason:
                results.append({**base, "status": "skip", "reason": reason})
                print(_line(results[-1]), flush=True)
                continue
            jobs.extend((case, agent, rep) for rep in range(1, args.repeat + 1))
    order = {(c.id, a.name, rep): i for i, (c, a, rep) in enumerate(jobs)}

    def report(job, res: dict) -> None:
        results.append({**res, "_order": order[(job[0].id, job[1].name, job[2])]})
        print(_line(res), flush=True)

    if args.jobs > 1:
        from . import agents as agents_mod
        _parallel(jobs, args, keep, dict(agents_mod.AUTH), report)
    else:
        for job in jobs:
            report(job, _run(*job[:2], args.mode, job[2], args.model, args.agent_arg, keep, args.keep))
    # Skips first, as before; then the runs in case order, however they finished.
    results.sort(key=lambda r: r.pop("_order", -1))
    tools_report = check_tools(results) if args.check_tools else None
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))
    (out / "summary.md").write_text(summary(results, args.mode, agents) + (tools_report["text"] if tools_report else "")
                                    + (cost_report(results) if args.mode == "live" else ""))
    # A known gap that passes scripted fails the run, so its marker is removed and the case guards the
    # fix. Live, a real model may simply not fall into the trap that run: reported, not failed.
    failed = [r for r in results if r["status"] == "fail" or (r["status"] == "gap-passed" and r["mode"] == "scripted")]
    counts = {s: sum(r["status"] == s for r in results) for s in ("pass", "fail", "gap", "gap-passed", "skip")}
    if tools_report:
        print("\n" + tools_report["text"].strip())
        if tools_report["new"]:
            failed.append({"tools": tools_report["new"]})
    if args.mode == "live":
        print("\n" + cost_report(results).strip())
    print(f"\n{counts['pass']} passed, {counts['fail']} failed, {counts['gap']} known gaps still open, "
          f"{counts['gap-passed']} known gaps now passing (remove their known_gap), {counts['skip']} skipped"
          f"  ->  {out / 'summary.md'}")
    return 1 if failed else 0


def cost_report(results: list[dict]) -> str:
    """What the run cost per agent and in all, from the usage each provider reported (cost.py)."""
    per: dict[str, cost.Usage] = {}
    for r in results:
        if r.get("usage") and r["usage"]["calls"]:  # none: a login whose replies carry no usage keepmum reads
            per.setdefault(r["agent"], cost.Usage()).merge(cost.Usage.from_dict(r["usage"]))
    if not per:
        return ""
    total = cost.Usage()
    lines = ["", "## Cost", "", "| Agent | Models | Calls | Input | Cached | Cache writes | Output | Cost |",
             "|---|---|---|---|---|---|---|---|"]
    for agent, u in sorted(per.items(), key=lambda kv: -kv[1].cost):
        total.merge(u)
        lines.append(f"| {agent} | {', '.join(sorted(u.models)) or '?'} | {u.calls} | {u.input:,} | {u.cached:,} | "
                     f"{u.cache_write:,} | {u.output:,} | {cost.dollars(u)} |")
    lines.append(f"| **all** | | {total.calls} | {total.input:,} | {total.cached:,} | {total.cache_write:,} | "
                 f"{total.output:,} | **{cost.dollars(total)}** |")
    if total.unpriced:
        lines.append(f"\n`+`: no price for {', '.join(sorted(total.unpriced))} (e2e/cost.py); counted in tokens only.")
    return "\n".join(lines) + "\n"


TOOLS = Path(__file__).parent / "tools.toml"


def check_tools(results: list[dict]) -> dict:
    """The tools each agent offered, against the reviewed list in tools.toml: new ones fail the run."""
    import tomllib

    known = tomllib.loads(TOOLS.read_text())
    seen: dict[str, set[str]] = {}
    for r in results:
        if r.get("tools") is not None:
            # MCP tools are held to their server's [restore] setting, whatever they are (tools.toml):
            # Claude Code declares the stub server's once ToolSearch loads them (mcp-06).
            seen.setdefault(r["agent"], set()).update(t for t in r["tools"] if not t.startswith("mcp__"))
    new = {a: sorted(t - set(known.get(a, {}))) for a, t in seen.items() if t - set(known.get(a, {}))}
    gone = {a: sorted(set(known.get(a, {})) - t) for a, t in seen.items() if set(known.get(a, {})) - t}
    lines = ["", "## Tools the agents offer", ""]
    for agent, tools in sorted(seen.items()):
        lines.append(f"- {agent}: {len(tools)} tools, " + ("all reviewed" if agent not in new else
                     f"**not reviewed: {', '.join(new[agent])}**"))
        if agent in gone:
            lines.append(f"  - no longer offered: {', '.join(gone[agent])}")
    if new:
        lines += ["", "A tool not in e2e/tools.toml may run commands, write files or reach the network in a way "
                      "keepmum does not handle yet. Find out what it does, then list it there (see the file)."]
    return {"new": new, "gone": gone, "text": "\n".join(lines) + "\n"}


def summary(results: list[dict], mode: str, agents) -> str:
    lines = [f"# keepmum e2e, {mode}, {time.strftime('%Y-%m-%d %H:%M')}", "",
             "Agents: " + ", ".join(f"{a.name} {a.version()}" for a in agents if a.available()), "",
             "| Case | Agent | Runs | Passed | Status | Findings |", "|---|---|---|---|---|---|"]
    groups: dict[tuple, list] = {}
    for r in results:
        groups.setdefault((r["case"], r["agent"]), []).append(r)
    for (case, agent), rs in groups.items():
        passed = sum(r["status"] in ("pass", "gap-passed") for r in rs)
        statuses = sorted({r["status"] for r in rs})
        found = sorted({f"{f['cls']}: {f['what']}" for r in rs for f in r.get("findings", []) if f["cls"] != "shown"})
        lines.append(f"| {case} | {agent} | {len(rs)} | {passed} | {', '.join(statuses)} | "
                     + "<br>".join(x.replace("|", "\\|") for x in found[:6]) + " |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
