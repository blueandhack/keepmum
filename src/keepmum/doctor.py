"""`keepmum doctor`: read-only checks. It creates nothing (no salt, no token, no files)."""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path

OK, WARN, FAIL = "✓", "!", "✗"


def _check(results, status, text):
    results.append((status, text))


def run(as_json: bool = False) -> int:
    r = checks()
    failed = sum(1 for s, _ in r if s == FAIL)
    if as_json:
        names = {OK: "ok", WARN: "warn", FAIL: "fail"}
        print(json.dumps({"checks": [{"status": names[s], "text": t} for s, t in r], "failed": failed}))
    else:
        for status, text in r:
            print(f"  {status} {text}")
        print("all checks passed" if not failed else f"{failed} check(s) failed")
    return 1 if failed else 0


def _find(name: str) -> str | None:
    """On PATH, or where installers put it: the Mac app runs this with launchd's short PATH."""
    found = shutil.which(name)
    if found:
        return found
    home = Path.home()
    for d in (home / ".local/bin", home / ".claude/local", Path("/opt/homebrew/bin"), Path("/usr/local/bin"),
              home / ".npm-global/bin", home / ".bun/bin", home / ".opencode/bin"):
        if os.access(d / name, os.X_OK):
            return str(d / name)
    return None


def checks() -> list[tuple[str, str]]:
    """Every check, in order: (OK, WARN or FAIL, what was found)."""
    from . import launch
    from .config import ConfigError, load

    r: list[tuple[str, str]] = []
    mac = platform.mac_ver()[0]
    major = int(mac.split(".")[0]) if mac else 0
    _check(r, OK if major >= 14 else FAIL, f"macOS {mac or 'unknown'} (14 or later)")
    _check(r, OK if platform.machine() == "arm64" else FAIL, f"CPU {platform.machine()} (Apple silicon required)")
    _check(r, OK if sys.version_info >= (3, 12) else FAIL, f"Python {platform.python_version()}")

    try:
        cfg = load()
        _check(r, OK, f"config {cfg.path or '(none: defaults)'}; pii={'on' if cfg.pii else 'off'}")
    except ConfigError as e:
        cfg = None
        _check(r, FAIL, f"config: {e}")

    if cfg is not None:
        for line in _tool_warnings(cfg.restore):
            _check(r, WARN, line)

    from .detect.pfserve import PfServe, find_binary, find_model

    binary, model = find_binary(), find_model()
    needed = cfg is None or cfg.model  # with detect.model = false, the rules work alone
    if not needed:
        _check(r, OK, "model off: the rules work alone (`keepmum model on` adds the local model)")
    missing = FAIL if needed else WARN
    _check(r, OK if binary else missing, f"pf-serve {binary}" if binary else "pf-serve missing: run native/pf-serve/build.sh")
    _check(r, OK if model else missing, f"model {model}" if model else "model missing: run `keepmum setup`")
    if binary and model:
        pf = None
        try:
            pf = PfServe(binary, model)
            # Synthetic canary in a prose sentence, where only the model (not a rule) can catch it.
            canary = "Zq8vX2mN4pL7kR9t"
            text = f"the vpn password for the lab is {canary}, don't share it"
            found = any(text[s.start : s.end] == canary for s in pf.detect(text))
            _check(r, OK if found else FAIL, f"model runs on {pf.device} and catches a canary" if found
                   else "model loaded but missed the canary")
        except Exception as e:
            _check(r, FAIL, f"model did not run ({e})")
        finally:
            if pf:
                pf.close()

    from .vault import salt_file

    sf = salt_file()
    if sf.exists():
        mode = sf.stat().st_mode & 0o777
        _check(r, OK if mode == 0o600 else FAIL, f"install salt in {sf} (mode {mode:o})")
    else:
        _check(r, OK, "install salt not created yet (created on first run)")

    from . import audit
    from .config import config_file
    from .protect import excluded_file
    from .stats import history_file, token_file

    af = audit.audit_file()
    for f in (token_file(), launch.log_file(), launch.runtime_file(), af, af.with_name(af.stem + ".head.json"), history_file()):
        if f.exists() and f.stat().st_mode & 0o077:
            _check(r, FAIL, f"{f} is readable by other users (mode {f.stat().st_mode & 0o777:o})")
    for f in (config_file(), excluded_file()):
        if f.exists() and f.stat().st_mode & 0o077:
            _check(r, WARN, f"{f} is readable by other users (mode {f.stat().st_mode & 0o777:o}); chmod 600 recommended")
    if af.exists():
        checked = audit.verify()
        _check(r, OK if checked["ok"] else FAIL, f"audit log intact ({checked['entries']} entries)" if checked["ok"]
               else f"audit log changed outside keepmum: {checked['problems'][0]} (`keepmum audit verify`)")
    else:
        _check(r, OK, "audit log not started yet (keepmum starts it on first use)")

    claude = _find("claude")
    if claude:
        try:
            version = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
            _check(r, OK, f"Claude Code {version or 'found'}")
        except (subprocess.SubprocessError, OSError) as e:
            _check(r, WARN, f"`claude --version` failed ({e})")
    elif any(_find(c) for c in ("codex", "pi", "opencode")):
        _check(r, WARN, "`claude` not on PATH (other coding agents found)")
    else:
        _check(r, FAIL, "`claude` not on PATH")

    from .protect import is_excluded

    port = cfg.port if cfg else 7788
    if url := is_excluded(Path.cwd()):
        _check(r, WARN, f"this project is excluded: Claude Code here goes straight to {url}, unscanned (`keepmum include` undoes it)")
    risks = launch.bypass_risks(Path.cwd(), port)
    _check(r, FAIL if risks else OK, "routes around keepmum: " + "; ".join(risks) if risks else "nothing routes Claude Code around keepmum")
    if cfg and launch.protected(cfg):
        up = launch.shared_serve_port(cfg) is not None
        _check(r, OK if up else FAIL, "protection on: every Claude Code session goes through keepmum serve" if up
               else "protection on, but keepmum serve is not running: Claude Code cannot connect")
    else:
        _check(r, OK, "protection off: only `keepmum claude` and the VS Code wrapper go through keepmum")
    from .clients import protect_clients, via_phrase

    names = {"claude": "Claude Code", "codex": "Codex", "pi": "pi", "opencode": "opencode"}
    for client, st in protect_clients("status", port).items():
        if st.get("around"):
            _check(r, FAIL, f"{names[client]} goes to {st['around']} directly, around keepmum (a gateway's app may have "
                   "set it back); `keepmum protect on` puts keepmum in front again")
        elif st.get("on") and st.get("via"):
            _check(r, OK, f"{names[client]} goes through keepmum" + via_phrase(st))
    if os.environ.get("ANTHROPIC_BASE_URL"):
        _check(r, WARN, "ANTHROPIC_BASE_URL is set: `keepmum claude` will use it as its upstream")

    with socket.socket() as s:
        free = s.connect_ex(("127.0.0.1", port)) != 0
    try:
        running = json.loads(launch.runtime_file().read_text())
    except (OSError, ValueError):
        running = None
    if running:
        _check(r, OK, f"keepmum running (pid {running.get('pid')}, port {running.get('port')})")
    if not (running and running.get("port") == port):  # in use by keepmum itself is fine
        _check(r, OK if free else WARN, f"port {port} {'free' if free else 'in use'} for `keepmum serve`")
    if cfg is not None:
        _guard_checks(r, cfg)
    return r


# Memory each optional check's model takes while loaded (measured on an M5 Pro), next to pf-serve's 1.7 GB.
GUARD_MEMORY = {"injection": "0.4 GB", "small": "0.4 GB", "0.6b": "1.1 GB", "4b": "4.9 GB"}


def _guard_checks(r, cfg) -> None:
    """The optional checks: whether each that is on can run, and what it costs in memory."""
    from . import guard, harm
    from .llamacpp import find_server

    gb = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout or 0) / 2**30
    if cfg.guard_injection != "off":
        why = ("model not set up (`keepmum guard setup injection`)" if not guard.installed()
               else "llama-server not found (brew install llama.cpp)" if find_server() is None else None)
        _check(r, FAIL if why else OK, f"injection check {cfg.guard_injection}: " + (why or
               f"Prompt Injection Guard Small, Apache-2.0; {GUARD_MEMORY['injection']} while in use"))
    if "local" in (cfg.guard_injection, cfg.guard_harm):
        from .local import models

        have = models(cfg.guard_local_url)
        why = ("no local model set (`keepmum guard local --model <name>`)" if not cfg.guard_local_model
               else f"nothing answers at {cfg.guard_local_url} (open Ollama, or `ollama serve`)" if have is None
               else f"{cfg.guard_local_url} has no {cfg.guard_local_model} (`ollama pull {cfg.guard_local_model}`)"
               if cfg.guard_local_model not in have else None)
        _check(r, FAIL if why else OK, "local mode: " + (why or f"flagged requests go to {cfg.guard_local_model} at "
               f"{cfg.guard_local_url}, without tools") + ("; until then they are refused" if why else ""))
    if cfg.guard_commands != "off":
        from .commands import RULES, SENDS

        _check(r, OK, f"commands check {cfg.guard_commands}: {len(RULES) + len(SENDS)} rules on the shell commands in "
               "replies; no model")
    if cfg.guard_harm != "off":
        size = cfg.guard_harm_model
        why = (f"{size} model not downloaded (`keepmum guard setup harm --model {size}`)" if not harm.installed(size)
               else "llama-server not found (brew install llama.cpp)" if find_server() is None else None)
        heavy = size == "4b" and 0 < gb < 32
        _check(r, FAIL if why else WARN if heavy else OK, f"harm check {cfg.guard_harm}: " + (why or
               f"{harm.NAMES[size]}; {GUARD_MEMORY[size]} while in use" +
               (f", a lot for this Mac's {gb:.0f} GB: `keepmum guard harm {cfg.guard_harm} --model small`" if heavy else "")))


def _tool_warnings(policy: dict) -> list[str]:
    """Restore settings that reach further than one server, and servers paused because their tools changed."""
    from .tools import ToolRegistry

    out = []
    for key, action in policy.items():
        if key.endswith("*") and key.count("__") < 2 and (action == "restore" or isinstance(action, list)):
            what = "every value, secrets included" if action == "restore" else ", ".join(action)
            out.append(f"restore.{key} gives {what} to every tool it matches, including servers added later; "
                       "set it per server (`keepmum tools set`)")
    for s in ToolRegistry().snapshot(policy):
        if s["paused"]:
            out.append(f"{s['label']}: its tools changed ({', '.join(s['changed'])}), so it gets placeholders only; "
                       f"`keepmum tools trust {s['label']}` once you have checked them")
    return out
