"""Run cases against real agents: `python -m e2e run`. See e2e/README.md."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
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


def _environment() -> None:
    """Found where this Mac keeps them, read only, before HOME moves (as tests/conftest.py does)."""
    pf = Path(REAL_HOME) / ".local" / "share" / "keepmum" / "bin" / "pf-serve"
    if pf.exists():
        os.environ.setdefault("KEEPMUM_PF_SERVE", str(pf))
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
    if "local" in case.needs:
        from keepmum.local import models

        have = models(LOCAL_URL)
        if have is None:
            return f"needs a local model server at {LOCAL_URL} (KEEPMUM_E2E_LOCAL_URL)"
        if LOCAL_MODEL not in have:
            return f"needs {LOCAL_MODEL} at {LOCAL_URL} (KEEPMUM_E2E_LOCAL_MODEL)"
    return None


def run_one(case, agent, mode: str, rep: int, model: str | None, extra: list[str], keep: Path | None) -> dict:
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
    if "injection" in case.needs and GUARD_DIR.exists():  # the models, read only, from this Mac's own folder
        data = khome / ".local" / "share" / "keepmum"
        data.mkdir(parents=True)
        (data / "guard").symlink_to(GUARD_DIR)
    for rel, content in case.files.items():
        p = ws / case.resolve(rel, salt)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(case.resolve(content, salt))
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
    started = time.monotonic()
    os.environ["HOME"] = str(khome)  # keepmum's own: salt, config, audit log
    try:
        proxy = launch.build_proxy(load(), rules_only=not case.model, upstream=f"{tap_server.url}/anthropic", mode="serve")
        proxy.prefixed["/openai"] = f"{tap_server.url}/openai"
        proxy.prefixed["/chatgpt"] = f"{tap_server.url}/chatgpt"
        proxy.prefixed["/antigravity"] = f"{tap_server.url}/antigravity"
        proxy.prefixed["/gemini"] = f"{tap_server.url}/gemini"
        # DeepSeek is another gateway keepmum sits in front of (--provider deepseek; gateway.py).
        proxy.gateways["deepseek"] = f"{tap_server.url}/deepseek"
        live_vault["vault"] = proxy.redactor.vault
        server = launch.BackgroundServer(proxy)
        server.start()
        servers.append(server)
        mcp = None
        if case.mcp:
            mcp = {"name": "notes", "command": sys.executable, "args": [str(Path(__file__).parent / "mcp_notes.py")],
                   "env": {"KEEPMUM_E2E_MCP_LOG": str(out / "mcp.jsonl"),
                           "KEEPMUM_E2E_MCP_REPLY": case.resolve(case.raw.get("mcp_reply", "no record"), salt)}}
        argv, env = agent.command(ws, server.port, case.resolve(case.prompt, salt), REAL_HOME, out, model, extra, mcp, mode)
        # Where the agent works: some take it from PWD, which is the harness's own folder otherwise
        # (opencode read this repository as its project before this was set).
        env["PWD"] = str(ws)
        try:
            done = subprocess.run(argv, env=env, cwd=ws, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  timeout=case.timeout)
            code, stdout, stderr = done.returncode, done.stdout, done.stderr
        except subprocess.TimeoutExpired as e:
            code = None
            stdout = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            stderr = (e.stderr or b"").decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        vault = proxy.redactor.vault
        masked = {ph: (ph.split("_")[1], value) for ph, value in vault._values.items()}
        events = list(proxy.stats.events)
        keepmum = {"unresolved": proxy.stats.unresolved,
                   "warned": sorted({h.get("check") for h in proxy.stats.snapshot()["guard"]["hits"]
                                     if h.get("action") == "warned"}),
                   "errors": [{"path": e.get("path"), "status": e.get("status"), "outcome": e.get("outcome")}
                              for e in events if (e.get("status") or 0) >= 400],
                   # Hits local mode acted on: whether a local model answered, and what was handed back.
                   "local": [{"check": h.get("check"), "answered": bool(h.get("answered_by")),
                              "handed_back": (h.get("handed_back") or {}).get("strategy"),
                              "kept": (h.get("handed_back") or {}).get("kept"), "error": h.get("local_error")}
                             for h in proxy.stats.snapshot()["guard"]["hits"] if h.get("action") == "local"]}
    finally:
        for s in reversed(servers):
            s.stop()
        os.environ["HOME"] = REAL_HOME
    after = oracle.snapshot(ws)
    transcript = agent.transcript(stdout, out)
    mcp_log = (out / "mcp.jsonl").read_text() if (out / "mcp.jsonl").exists() else ("" if case.mcp else None)
    provider = [ex for ex in tap.exchanges if ex.target != "local"]
    local = [ex for ex in tap.exchanges if ex.target == "local"]  # stays on this Mac: real values belong there
    findings = oracle.judge(case, salt, provider, before, after, transcript, masked, keepmum, code,
                            scripted.served if scripted else None, mcp_log, local)
    failed = [f for f in findings if f.failing()]
    # The provider refused to answer (a usage limit): the run says nothing about keepmum.
    limited = any(e["status"] == 429 for e in keepmum["errors"]) and not any(f.cls == "leak" for f in findings)
    result = {
        "case": case.id, "agent": agent.name, "mode": mode, "rep": rep, "seconds": round(time.monotonic() - started, 1),
        "status": "skip" if limited else ("gap" if failed else "gap-passed") if case.gap(agent.name)
        else ("fail" if failed else "pass"),
        "reason": "the provider's usage limit (429): nothing was tested" if limited else None,
        "known_gap": case.gap(agent.name), "findings": oracle.as_dicts(findings), "exit": code,
        "requests": len(provider), "local_requests": len(local), "masked": len(masked),
        "turns": [s["turn"] for s in scripted.served] if scripted else None,
        "tools": sorted(scripted.tools) if scripted else None,
        "code_mode": next((s.get("code_mode") for s in (scripted.served if scripted else []) if "code_mode" in s), None),
        "usage": cost.of(provider).as_dict() if mode == "live" else None,
    }
    if keep is not None:
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
    keep = out if args.keep else None
    agents = [AGENTS[a] for a in (args.agent or AGENTS)]
    if args.cmd == "run" and args.mode == "live":
        from . import agents as agents_mod
        agents_mod.AUTH.update(mode=args.auth, cheap=args.cheap, provider=args.provider)
    model_ok = None
    results = []
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
            for rep in range(1, args.repeat + 1):
                try:
                    res = run_one(case, agent, args.mode, rep, args.model, args.agent_arg, keep)
                except Exception as e:  # the harness itself failed: a finding, not a crash of the whole run
                    res = {**base, "rep": rep, "status": "fail", "findings": [
                        {"cls": "health", "what": f"harness error: {type(e).__name__}: {e}", "where": "e2e"}]}
                results.append(res)
                print(_line(res), flush=True)
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
            seen.setdefault(r["agent"], set()).update(r["tools"])
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
