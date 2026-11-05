"""keepmum command line."""

from __future__ import annotations

import bisect
import sys
import time
from pathlib import Path

import typer

app = typer.Typer(help="Local secret and PII redaction for Claude Code.", no_args_is_help=True, add_completion=False)


@app.command()
def scan(
    paths: list[str] = typer.Argument(..., help="Files to scan, or - for stdin."),
    pii: bool = typer.Option(False, help="Also report personal data."),
    rules_only: bool = typer.Option(False, help="Rules only for this scan: fast, no model, lower recall."),
) -> None:
    """Report secrets in files without showing them. Exits 1 if anything is found."""
    import sys

    pipeline = _pipeline_or_exit(pii, rules_only, "scan")
    found = 0
    for path in paths:
        try:
            raw = sys.stdin.buffer.read() if path == "-" else Path(path).read_bytes()
        except OSError as e:
            typer.echo(f"keepmum scan: {path}: {e.strerror}", err=True)
            raise typer.Exit(2)
        if b"\0" in raw[:8192]:
            continue  # binary
        text = raw.decode("utf-8", errors="replace")
        line_starts = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]
        for s in pipeline.detect(text):
            line = bisect.bisect_right(line_starts, s.start)
            col = s.start - line_starts[line - 1] + 1
            # Never print the value, or any part of it: only where it is and what it is.
            typer.echo(f"{'<stdin>' if path == '-' else path}:{line}:{col}  {s.type}  {s.source}  [{s.end - s.start} chars]")
            found += 1
    if found:
        typer.echo(f"{found} finding{'s' if found != 1 else ''}", err=True)
        raise typer.Exit(1)


def _config():
    from .config import ConfigError, load

    try:
        return load()
    except ConfigError as e:
        typer.echo(f"keepmum: bad config: {e}", err=True)
        raise typer.Exit(2)


def _update_config(path, section: str, key: str, value, command: str) -> None:
    """Write a setting and record its audit entry under the audit lock, so a running proxy polling
    the file cannot record the change first as 'config file'."""
    from .audit import AuditLog, caller
    from .config import ConfigError, load, set_value

    log = AuditLog()
    try:
        with log._locked():
            set_value(section, key, value, path)
            try:
                cfg = load(path)
            except ConfigError:
                return
            log.keep_days = cfg.audit_keep_days
            log._note_config_unlocked(cfg, caller(command))
    except OSError:
        # Auditing never blocks a settings write: if only the lock/audit failed, ensure config wrote.
        set_value(section, key, value, path)


def _pipeline_or_exit(pii: bool, rules_only: bool, who: str):
    from .detect.pipeline import ModelUnavailable, default_pipeline

    cfg = _config()
    try:
        return default_pipeline(pii=pii or cfg.pii, rules_only=rules_only or not cfg.model, allow_strings=cfg.allow_strings,
                                allow_patterns=cfg.allow_patterns, pass_types=cfg.pass_types, mask_rules=cfg.mask_rules,
                                off_rules=cfg.off_rules)
    except ModelUnavailable as e:
        typer.echo(f"keepmum {who}: {e}", err=True)
        raise typer.Exit(2)


def _proxy_or_exit(pii, rules_only, upstream, who, gateways=None):  # who: "serve" or "claude", also the stats mode
    from .detect.pipeline import ModelUnavailable
    from .launch import build_proxy

    cfg = _config()
    try:
        return cfg, build_proxy(cfg, pii=pii, rules_only=rules_only, upstream=upstream, mode=who, gateways=gateways)
    except ModelUnavailable as e:
        typer.echo(f"keepmum {who}: {e}", err=True)
        raise typer.Exit(2)


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def claude(ctx: typer.Context) -> None:
    """Start Claude Code with every request scanned. Arguments go to `claude` unchanged."""
    import os
    import shutil

    from .launch import PROTECTED_BUT_DOWN, _run_child, bypass_risks, log_to_file, protected, run_claude, shared_serve_port
    from .protect import is_excluded

    cfg = _config()
    if url := is_excluded(Path.cwd()):
        typer.echo(f"keepmum claude: this project is excluded (Claude Code here goes straight to {url}); "
                   "run `claude`, or `keepmum include` to scan it again", err=True)
        raise typer.Exit(2)
    risks = bypass_risks(Path.cwd(), cfg.port)
    if risks:
        typer.echo("keepmum: refusing to start, Claude Code could bypass the proxy:\n  " + "\n  ".join(risks), err=True)
        raise typer.Exit(2)
    if protected(cfg):
        # Claude Code's settings send it to the keepmum service whatever we set, so use that.
        port = shared_serve_port(cfg)
        if port is None:
            typer.echo(f"keepmum claude: {PROTECTED_BUT_DOWN}", err=True)
            raise typer.Exit(2)
        if shutil.which("claude") is None:
            typer.echo("keepmum claude: `claude` is not on PATH", err=True)
            raise typer.Exit(2)
        raise typer.Exit(_run_child([shutil.which("claude"), *ctx.args],
                                    {**os.environ, "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}"}))
    # An existing ANTHROPIC_BASE_URL (another gateway) becomes keepmum's upstream, never a bypass.
    upstream = os.environ.get("ANTHROPIC_BASE_URL")
    cfg, proxy = _proxy_or_exit(None, False, upstream, "claude")
    log_to_file(cfg.log_level)
    try:
        code = run_claude(proxy, ctx.args)
    except (FileNotFoundError, RuntimeError) as e:
        typer.echo(f"keepmum claude: {e}", err=True)
        raise typer.Exit(2)
    raise typer.Exit(code)


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def codex(ctx: typer.Context) -> None:
    """Start Codex with every request scanned. Arguments go to `codex` unchanged."""
    import os
    import shutil

    from .clients import codex_args
    from .launch import BackgroundServer, _run_child, log_to_file, shared_serve_port

    binary = shutil.which("codex")
    if binary is None:
        typer.echo("keepmum codex: `codex` is not on PATH", err=True)
        raise typer.Exit(2)
    from .clients import codex_run_provider
    from .protect import ProtectError

    cfg = _config()
    try:
        # In front of the provider Codex's config selects, when that is another gateway.
        provider, extra = codex_run_provider(cfg.port, cfg.gateways)
    except ProtectError as e:
        typer.echo(f"keepmum codex: {e}", err=True)
        raise typer.Exit(2)
    # The service knows the gateways in the config; one it does not know needs a proxy of its own.
    port = None if extra else shared_serve_port(cfg, need="responses")
    if port is not None:
        raise typer.Exit(_run_child([binary, *codex_args(port, provider), *ctx.args], dict(os.environ)))
    cfg, proxy = _proxy_or_exit(None, False, None, "codex", gateways=extra)
    log_to_file(cfg.log_level)
    server = BackgroundServer(proxy)
    server.start()
    try:
        provider, _ = codex_run_provider(server.port, {**cfg.gateways, **extra})
        code = _run_child([binary, *codex_args(server.port, provider), *ctx.args], dict(os.environ))
    finally:
        server.stop()
    raise typer.Exit(code)


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def gemini(ctx: typer.Context) -> None:
    """Start Gemini CLI with every request scanned. Arguments go to `gemini` unchanged."""
    import os
    import shutil

    from .launch import (
        BackgroundServer,
        _run_child,
        gemini_env,
        gemini_upstreams,
        log_to_file,
        shared_serve_port,
    )

    binary = shutil.which("gemini")
    if binary is None:
        typer.echo("keepmum gemini: `gemini` is not on PATH (npm install -g @google/gemini-cli)", err=True)
        raise typer.Exit(2)
    cfg = _config()
    env = dict(os.environ)
    upstreams = gemini_upstreams(env, cfg.port)
    # The service forwards to Google; an address set here (another gateway) needs a proxy of its own.
    port = None if upstreams else shared_serve_port(cfg, need="gemini")
    if port is not None:
        raise typer.Exit(_run_child([binary, *ctx.args], gemini_env(port, env)))
    cfg, proxy = _proxy_or_exit(None, False, None, "gemini")
    proxy.prefixed.update(upstreams)
    log_to_file(cfg.log_level)
    server = BackgroundServer(proxy)
    server.start()
    try:
        code = _run_child([binary, *ctx.args], gemini_env(server.port, env))
    finally:
        server.stop()
    raise typer.Exit(code)


@app.command()
def serve(
    port: int = typer.Option(None, help="Port on 127.0.0.1 (default: config, else 7788)."),
    upstream: str = typer.Option(None, help="Where scanned requests go (default: config, else api.anthropic.com)."),
    pii: bool = typer.Option(None, "--pii/--no-pii", help="Also mask personal data (default: config)."),
    rules_only: bool = typer.Option(False, help="Rules only for this run, whatever detect.model says (`keepmum model`)."),
    log_file: bool = typer.Option(False, help="Log to ~/.config/keepmum/keepmum.log instead of stderr (for the background service)."),
) -> None:
    """Run the proxy on 127.0.0.1. Point Claude Code at it with ANTHROPIC_BASE_URL, or `keepmum protect`."""
    import logging
    import signal

    from .launch import BackgroundServer, log_to_file

    cfg, proxy = _proxy_or_exit(pii, rules_only, upstream, "serve")
    if log_file:
        log_to_file(cfg.log_level)
    else:
        logging.basicConfig(level=cfg.log_level.upper(), format="keepmum: %(message)s")
        # Only keepmum decides what is logged: libraries would print URLs and request details.
        for name in ("httpx", "httpcore", "uvicorn", "uvicorn.error"):
            logging.getLogger(name).setLevel(logging.WARNING)
    server = BackgroundServer(proxy, port or cfg.port)
    server.start()
    # launchd stops the service with SIGTERM: shut down cleanly and remove runtime.json.
    signal.signal(signal.SIGTERM, lambda *_: setattr(server.server, "should_exit", True))
    typer.echo(f"keepmum: listening on http://127.0.0.1:{server.port} -> {proxy.upstream}", err=True)
    try:
        server.thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


@app.command()
def selftest(rules_only: bool = typer.Option(False, help="Test with the rules only.")) -> None:
    """Send a canary secret through the proxy to a local mock upstream; fail if it leaks."""
    import anyio

    from . import selftest as st

    pipeline = _pipeline_or_exit(False, rules_only, "selftest")
    ok = anyio.run(st.run, pipeline)
    typer.echo("selftest passed" if ok else "SELFTEST FAILED", err=True)
    raise typer.Exit(0 if ok else 1)


@app.command()
def setup() -> None:
    """Download the Privacy Filter model (1.6 GB, once), checked against its pinned SHA-256."""
    from .detect.pfserve import MODEL_URL, PfServeError, download_model, find_binary

    typer.echo(f"model: {MODEL_URL}", err=True)
    last = [-1]

    def progress(done: int, total: int) -> None:
        pct = done * 100 // total
        if pct != last[0]:
            last[0] = pct
            typer.echo(f"\r  {pct}% of {total / 1e9:.1f} GB", nl=False, err=True)

    try:
        path = download_model(progress)
    except (PfServeError, OSError) as e:
        typer.echo(f"\nkeepmum setup: {e}", err=True)
        raise typer.Exit(1)
    typer.echo(f"\nmodel ready: {path}", err=True)
    if find_binary() is None:
        typer.echo("pf-serve is not installed yet: run native/pf-serve/build.sh", err=True)
        raise typer.Exit(1)


@app.command()
def protect(
    action: str = typer.Argument("status", help="on, off, or status"),
    client: list[str] = typer.Option(None, "--client", help="claude, codex, pi or opencode (default: every one installed)."),
    as_json: bool = typer.Option(False, "--json", help="Print status as JSON."),
) -> None:
    """Send every Claude Code, Codex, pi and opencode session through `keepmum serve` (on), stop (off), or show which."""
    import json

    from .clients import CLIENTS, protect_clients, via_phrase

    cfg = _config()
    if "gemini" in (client or []):
        # Gemini CLI reads its address only from the environment or a .env file, and the first
        # .env it finds walking up from the project wins: a project's own .env would quietly
        # undo a setting here. `keepmum gemini` sets the environment, which every .env yields to.
        typer.echo("keepmum protect: Gemini CLI has no setting keepmum can rely on (any project's .env file "
                   "would override it); run `keepmum gemini` instead", err=True)
        raise typer.Exit(2)
    if action not in ("on", "off", "status") or any(c not in CLIENTS for c in client or []):
        typer.echo("keepmum protect: use on, off or status, and --client claude|codex|pi|opencode", err=True)
        raise typer.Exit(2)
    from .config import config_file
    from .gateway import Adopter

    # An agent already pointed at another gateway gets keepmum in front of it; a new gateway is
    # added to [gateways] (and audited) before any agent is pointed at its route.
    adopt = Adopter(cfg.port, cfg.gateways, lambda name, url: _update_config(
        cfg.path or config_file(), "gateways", name, url, "keepmum protect on"))
    result = protect_clients(action, cfg.port, client or None, adopt=adopt if action == "on" else None)
    if action in ("on", "off"):
        from .audit import AuditLog, caller

        changed = [name for name, st in result.items() if st.get("installed") and not st.get("error")]
        if changed:
            AuditLog().append("protect", action=action, clients=changed, by=caller(f"keepmum protect {action}"),
                              **({"via": adopt.adopted} if adopt.adopted else {}))
    if as_json:
        typer.echo(json.dumps(result))
        return
    names = {"claude": "Claude Code", "codex": "Codex", "pi": "pi", "opencode": "opencode"}
    for name, st in result.items():
        if not st.get("installed"):
            line = "not installed"
        elif st.get("error"):
            line = f"error: {st['error']}"
        elif st.get("on"):
            line = "on: goes through keepmum" + (f" ({', '.join(st['providers'])})" if st.get("providers") else "")
            if st.get("via"):
                line += via_phrase(st)
        else:
            other = st.get("other_url") or st.get("other")
            other = ", ".join(other) if isinstance(other, list) else other
            line = "off" + (f": goes to {other} directly; `keepmum protect on` puts keepmum in front" if other else "")
        typer.echo(f"{names[name]:12} {line}", err=True)
        if st.get("warning"):
            typer.echo(f"{'':12} warning: {st['warning']}", err=True)
    if action == "on":
        typer.echo("Claude Code sessions switch now; Codex, pi and opencode pick it up when they next start.", err=True)
    elif action == "off":
        typer.echo("Sessions already running keep using keepmum until they restart.", err=True)
    if any(st.get("error") for st in result.values()):
        raise typer.Exit(1)


@app.command()
def observe(action: str = typer.Argument("status", help="on, off, or status")) -> None:
    """Observe only (on): count what would be masked, but send every request unchanged. Off: mask."""
    from .config import ConfigError

    cfg = _config()
    if action in ("on", "off"):
        try:
            _update_config(cfg.path, "detect", "observe", action == "on", f"keepmum observe {action}")
        except (ConfigError, OSError) as e:
            typer.echo(f"keepmum observe: {e}", err=True)
            raise typer.Exit(1)
        on = action == "on"
    elif action == "status":
        on = cfg.observe
    else:
        typer.echo("keepmum observe: use on, off or status", err=True)
        raise typer.Exit(2)
    typer.echo("observe only: requests go to Anthropic UNMASKED; keepmum only counts what it would mask" if on
               else "masking: sensitive values are replaced before requests leave this Mac", err=True)
    if action != "status":
        typer.echo("Running proxies switch on their next request.", err=True)


@app.command()
def model(action: str = typer.Argument("status", help="on, off, or status")) -> None:
    """The local model after the rules (on, the default), or the rules alone (off): faster and about
    1.7 GB lighter, but they find only values with a known shape or a telling name."""
    from .config import ConfigError
    from .detect.pfserve import find_binary, find_model

    cfg = _config()
    if action == "on" and not (find_binary() and find_model()):
        typer.echo("keepmum model: the model is not downloaded: run `keepmum setup` first", err=True)
        raise typer.Exit(2)
    if action in ("on", "off"):
        try:
            _update_config(cfg.path, "detect", "model", action == "on", f"keepmum model {action}")
        except (ConfigError, OSError) as e:
            typer.echo(f"keepmum model: {e}", err=True)
            raise typer.Exit(1)
        on = action == "on"
    elif action == "status":
        on = cfg.model
    else:
        typer.echo("keepmum model: use on, off or status", err=True)
        raise typer.Exit(2)
    typer.echo("rules and the local model: secrets and personal data the rules alone would miss" if on
               else "rules only: names, addresses, and secrets with no known shape or telling name go out unmasked", err=True)
    if action != "status":
        typer.echo("Running proxies switch on their next request.", err=True)


@app.command()
def pii(action: str = typer.Argument("status", help="on, off, or status")) -> None:
    """Personal data (on): also mask names, emails, phones, addresses and the rest. Off, the default:
    only secrets and your own rules are masked."""
    from .config import ConfigError

    cfg = _config()
    if action in ("on", "off"):
        try:
            _update_config(cfg.path, "detect", "pii", action == "on", f"keepmum pii {action}")
        except (ConfigError, OSError) as e:
            typer.echo(f"keepmum pii: {e}", err=True)
            raise typer.Exit(1)
        on = action == "on"
    elif action == "status":
        on = cfg.pii
    else:
        typer.echo("keepmum pii: use on, off or status", err=True)
        raise typer.Exit(2)
    typer.echo("personal data: masked, along with secrets" if on
               else "personal data: not masked; names, emails, phones and addresses go out as they are", err=True)
    if action != "status":
        typer.echo("Running proxies switch on their next request.", err=True)


guard_app = typer.Typer(help="Optional checks: `injection` looks at what tools return for planted instructions; "
                        "`harm` looks at what you type for requests a policy may not allow; `commands` looks at the "
                        "shell commands in the model's replies. Set up injection or harm first with "
                        "`keepmum guard setup injection|harm`; commands needs no setup.", invoke_without_command=True)
app.add_typer(guard_app, name="guard")
CHECKS = ("injection", "harm")
_COMMANDS_TEXT = {"off": "off", "warn": "warn (a flagged command is logged, then run as the agent decides)",
                  "block": "block (a flagged command is replaced by one that says why and fails)"}
_MODE_TEXT = {"off": "off", "warn": "warn (flagged requests are logged, then sent)",
              "block": "block (flagged requests are refused)"}


def _guard_status() -> dict:
    from . import guard, harm
    from .commands import RULES, SENDS
    from .llamacpp import find_server

    cfg = _config()
    server = str(find_server() or "") or None  # both checks run in llama-server
    return {
        "injection": {"mode": cfg.guard_injection, "installed": guard.installed(),
                      "server": server, "model": f"{guard.REPO}@{guard.REVISION}", "source": f"{guard.SOURCE}@{guard.RELEASE}",
                      "licence": guard.LICENCE, "threshold": guard.THRESHOLD},
        "commands": {"mode": cfg.guard_commands, "rules": [label for _, label, _ in RULES + SENDS]},
        "harm": {"mode": cfg.guard_harm, "model": cfg.guard_harm_model,
                 "installed": {size: harm.installed(size) for size in harm.SIZES},
                 "server": server, "licence": harm.LICENCE,
                 "threshold": harm.MODELS[cfg.guard_harm_model][5]},
    }


def _missing(check: str, size: str | None = None) -> str | None:
    """Why `check` cannot run yet, or None."""
    from . import guard, harm
    from .llamacpp import find_server

    if check == "commands":  # rules, no model
        return None
    if check == "injection" and not guard.installed():
        return "its model is not set up: run `keepmum guard setup injection` first"
    if check == "harm" and not harm.installed(size):
        return f"the {size} model is not downloaded: run `keepmum guard setup harm --model {size}` first"
    if find_server() is None:
        return "llama-server is not installed: `brew install llama.cpp` (or set KEEPMUM_LLAMA_SERVER)"
    return None


@guard_app.callback()
def guard_status(ctx: typer.Context, as_json: bool = typer.Option(False, "--json", help="Print JSON.")) -> None:
    """Whether each check is on, and whether its model is set up."""
    if ctx.invoked_subcommand:
        return
    import json

    from . import guard, harm

    st = _guard_status()
    if as_json:
        typer.echo(json.dumps(st))
        return
    inj, hm = st["injection"], st["harm"]
    typer.echo(f"injection: {_MODE_TEXT[inj['mode']]}")
    if inj["installed"]:
        typer.echo(f"  model: {guard.ATTRIBUTION}")
    else:
        typer.echo("  model: not set up (`keepmum guard setup injection`)")
    typer.echo(f"harm: {_MODE_TEXT[hm['mode']]}")
    have = [size for size, ok in hm["installed"].items() if ok]
    typer.echo(f"  model: {harm.NAMES[hm['model']]} (Apache-2.0)"
               + ("" if hm["installed"][hm["model"]] else f", not downloaded (`keepmum guard setup harm --model {hm['model']}`)")
               + (f"; downloaded: {', '.join(have)}" if have else ""))
    typer.echo(f"runtime (both): {hm['server'] or 'llama-server not found (brew install llama.cpp)'}")
    cm = st["commands"]
    typer.echo(f"commands: {_COMMANDS_TEXT[cm['mode']]}")
    typer.echo(f"  rules ({len(cm['rules'])}, no model): {', '.join(cm['rules'])}")


@guard_app.command("setup")
def guard_setup(check: str = typer.Argument(..., help="injection or harm"),
                model: str = typer.Option(None, "--model", help="harm: small (164 MB), 0.6b (805 MB) or 4b (4.7 GB); "
                                          "default: the one the check uses.")) -> None:
    """Download a check's model, checked against its pinned SHA-256."""
    from . import guard, harm
    from .audit import AuditLog, caller

    model = model or _config().guard_harm_model
    if check not in CHECKS or model not in harm.SIZES:
        typer.echo("keepmum guard setup: use `injection`, or `harm` with --model small, 0.6b or 4b", err=True)
        raise typer.Exit(2)
    last = [-1]

    def progress(done: int, total: int) -> None:
        pct = done * 100 // total
        if pct != last[0]:
            last[0] = pct
            typer.echo(f"\r  {pct}% of {total / 1e6:,.0f} MB", nl=False, err=True)

    if check == "injection":
        typer.echo(guard.INFO, err=True)
        try:
            path = guard.download(progress)
        except (guard.GuardError, OSError) as e:
            typer.echo(f"\nkeepmum guard setup: {e}", err=True)
            raise typer.Exit(1)
        AuditLog().append("guard-model", action="installed", model=f"{guard.REPO}@{guard.REVISION}", licence=guard.LICENCE,
                          by=caller("keepmum guard setup injection"))
    else:
        repo, commit, file, *_ = harm.MODELS[model]
        typer.echo(f"{harm.NAMES[model]} (Apache-2.0), {file} from {repo}", err=True)
        try:
            path = harm.download(model, progress)
        except (harm.HarmError, OSError) as e:
            typer.echo(f"\nkeepmum guard setup: {e}", err=True)
            raise typer.Exit(1)
        AuditLog().append("guard-model", action="installed", model=f"{repo}@{commit}/{file}", licence=harm.LICENCE,
                          by=caller("keepmum guard setup harm"))
        if model != _config().guard_harm_model:
            typer.echo(f"\nThe harm check uses {_config().guard_harm_model}: `keepmum guard harm warn --model {model}` switches.",
                       err=True)
    typer.echo(f"\nmodel ready: {path}", err=True)
    why = _missing(check, model)
    if why:
        typer.echo(why, err=True)
    typer.echo(f"Turn it on with `keepmum guard {check} warn` (log and send) or `keepmum guard {check} block` (refuse).", err=True)


def _harm_model_set(path) -> bool:
    import tomllib

    if path is None:  # no config file yet
        return False
    try:
        return "harm_model" in tomllib.loads(Path(path).read_text()).get("guard", {})
    except (OSError, ValueError):
        return False


def _set_mode(check: str, mode: str, model: str | None = None) -> None:
    from . import harm
    from .config import ConfigError

    if mode not in ("off", "warn", "block"):
        typer.echo(f"keepmum guard {check}: use warn, block or off", err=True)
        raise typer.Exit(2)
    cfg = _config()
    size = model or cfg.guard_harm_model
    if mode != "off" and (why := _missing(check, size)):
        typer.echo(f"keepmum guard {check}: {why}", err=True)
        raise typer.Exit(2)
    try:
        # Turning the harm check on writes its model down: a check on with no model set means the
        # 0.6B (config.py), the default before "small".
        if check == "harm" and (model and model != cfg.guard_harm_model or mode != "off" and not _harm_model_set(cfg.path)):
            _update_config(cfg.path, "guard", "harm_model", size, f"keepmum guard harm --model {size}")
        _update_config(cfg.path, "guard", check, mode, f"keepmum guard {check} {mode}")
    except (ConfigError, OSError) as e:
        typer.echo(f"keepmum guard {check}: {e}", err=True)
        raise typer.Exit(1)
    typer.echo(f"{check}: {(_COMMANDS_TEXT if check == 'commands' else _MODE_TEXT)[mode]}"
               + (f", {harm.NAMES[size]}" if check == "harm" and mode != "off" else ""),
               err=True)
    typer.echo("Running proxies switch on their next request.", err=True)


@guard_app.command("injection")
def guard_injection(mode: str = typer.Argument(..., help="warn, block or off")) -> None:
    """Check what tools return (web pages, files, MCP replies) for planted instructions."""
    _set_mode("injection", mode)


@guard_app.command("commands")
def guard_commands(mode: str = typer.Argument(..., help="warn, block or off")) -> None:
    """Check the shell commands in the model's replies (curl | sh, rm -rf ~, keys sent out, ...)."""
    _set_mode("commands", mode)


@guard_app.command("harm")
def guard_harm(mode: str = typer.Argument(..., help="warn, block or off"),
               model: str = typer.Option(None, "--model", help="small, 0.6b or 4b (default: the current one).")) -> None:
    """Check what you type for requests a policy may not allow."""
    from . import harm

    if model is not None and model not in harm.SIZES:
        typer.echo("keepmum guard harm: --model is small, 0.6b or 4b", err=True)
        raise typer.Exit(2)
    _set_mode("harm", mode, model)


@guard_app.command("remove")
def guard_remove(check: str = typer.Argument(..., help="injection or harm")) -> None:
    """Turn a check off and delete its model."""
    from . import guard, harm
    from .audit import AuditLog, caller

    if check not in CHECKS:
        typer.echo("keepmum guard remove: use injection or harm", err=True)
        raise typer.Exit(2)
    cfg = _config()
    if (cfg.guard_injection if check == "injection" else cfg.guard_harm) != "off":
        _set_mode(check, "off")
    gone = guard.remove() if check == "injection" else harm.remove()
    for path in gone:
        AuditLog().append("guard-model", action="removed", model=path.name, by=caller(f"keepmum guard remove {check}"),
                          licence=guard.LICENCE if check == "injection" else harm.LICENCE)
        typer.echo(f"removed {path}", err=True)
    if not gone:
        typer.echo("nothing to remove", err=True)


log_app = typer.Typer(help="What the Logs page of the Mac app may show about masked values (kept in memory only).",
                      no_args_is_help=True)
app.add_typer(log_app, name="log")


def _log_switch(key: str, action: str, on_text: str, off_text: str) -> None:
    from .config import ConfigError

    cfg = _config()
    if action in ("on", "off"):
        try:
            _update_config(cfg.path, "log", key, action == "on", f"keepmum log {key} {action}")
        except (ConfigError, OSError) as e:
            typer.echo(f"keepmum log: {e}", err=True)
            raise typer.Exit(1)
        on = action == "on"
    elif action == "status":
        on = getattr(cfg, f"log_{key}")
    else:
        typer.echo(f"keepmum log {key}: use on, off or status", err=True)
        raise typer.Exit(2)
    typer.echo(on_text if on else off_text, err=True)
    if action != "status":
        typer.echo("Running proxies switch on their next request.", err=True)


@log_app.command("previews")
def log_previews(action: str = typer.Argument("status", help="on, off, or status")) -> None:
    """The first and last character of each long masked value (on, the default), or types and rules only."""
    _log_switch("previews", action, "previews: the Logs page shows the first and last character of long masked values",
                "previews: off; the Logs page shows types and rules only")


@log_app.command("context")
def log_context(action: str = typer.Argument("status", help="on, off, or status")) -> None:
    """The text around each masked value, every masked value in it shown as its type (on, the default)."""
    _log_switch("context", action, "context: the Logs page shows the text around each masked value",
                "context: off; no text around masked values is kept")


destinations_app = typer.Typer(help="Hosts a shell command may send a token to, besides the ones its issuer uses.",
                               invoke_without_command=True)
app.add_typer(destinations_app, name="destinations")


def _destinations_json(cfg) -> dict:
    from .destinations import ISSUERS
    from .detect.rules import builtin_rules

    return {"added": cfg.destinations, "issuers": {r: list(h) for r, h in ISSUERS.items()},
            "rules": [r for r, t in builtin_rules().items() if t == "SECRET"]}


@destinations_app.callback()
def destinations_list(ctx: typer.Context, as_json: bool = typer.Option(False, "--json", help="Print JSON.")) -> None:
    """The hosts you added, per rule. With no hosts added, each token goes only to its issuer's hosts."""
    import json

    if ctx.invoked_subcommand:
        return
    cfg = _config()
    if as_json:
        typer.echo(json.dumps(_destinations_json(cfg)))
        return
    if not cfg.destinations:
        typer.echo("no hosts added: each token goes only to its issuer's hosts (`keepmum destinations add`)", err=True)
    for rule, hosts in cfg.destinations.items():
        typer.echo(f"{rule}: {', '.join(hosts)}", err=True)


def _save_destinations(cfg, rule: str, hosts: list[str], command: str) -> None:
    from .config import ConfigError

    try:
        _update_config(cfg.path, "destinations", rule, hosts or None, f"keepmum destinations {command}")
    except (ConfigError, OSError) as e:
        typer.echo(f"keepmum destinations: {e}", err=True)
        raise typer.Exit(1)
    typer.echo(f"{rule}: {', '.join(hosts) if hosts else 'only its issuer’s hosts'}", err=True)
    typer.echo("Running proxies switch on their next request.", err=True)


@destinations_app.command("add")
def destinations_add(
    rule: str = typer.Argument(..., help="A rule that finds secrets, e.g. gitlab-token (`keepmum rules builtin`)."),
    hosts: list[str] = typer.Argument(..., help="Host names, e.g. gitlab.corp.example (subdomains included)."),
    yes: bool = typer.Option(False, "--yes", help="Confirm: shell commands may then send this rule's tokens there."),
) -> None:
    """Let shell commands send a rule's tokens to more hosts: your company's GitLab, a GitHub Enterprise server."""
    from .config import _HOST
    from .detect.rules import builtin_rules

    cfg = _config()
    hosts = [h.strip().lower().rstrip(".") for h in hosts]
    bad = [h for h in hosts if not _HOST.fullmatch(h)]
    if bad:
        typer.echo(f"keepmum destinations: {bad[0]!r} is not a host name; give one like gitlab.corp.example, "
                   "without https:// or a path", err=True)
        raise typer.Exit(2)
    if builtin_rules().get(rule) != "SECRET":
        typer.echo(f"keepmum destinations: {rule!r} is not a built-in rule that finds secrets; see `keepmum rules builtin`",
                   err=True)
        raise typer.Exit(2)
    if not yes:
        typer.echo(f"keepmum destinations: shell commands could then send {rule} tokens to {', '.join(hosts)} and its "
                   "subdomains. Add --yes if those hosts are yours", err=True)
        raise typer.Exit(2)
    current = cfg.destinations.get(rule, [])
    _save_destinations(cfg, rule, [*current, *(h for h in dict.fromkeys(hosts) if h not in current)], "add")


@destinations_app.command("remove")
def destinations_remove(
    rule: str = typer.Argument(..., help="The rule the hosts were added for."),
    hosts: list[str] = typer.Argument(None, help="Hosts to remove (default: every host added for the rule)."),
) -> None:
    """Remove hosts you added: the rule's tokens go only to its issuer's hosts again."""
    cfg = _config()
    if rule not in cfg.destinations:
        typer.echo(f"keepmum destinations: no hosts added for {rule!r}", err=True)
        raise typer.Exit(2)
    gone = {h.lower().rstrip(".") for h in hosts or []}
    _save_destinations(cfg, rule, [h for h in cfg.destinations[rule] if hosts and h not in gone], "remove")


@app.command()
def types(
    action: str = typer.Argument("list", help="list, mask or pass"),
    names: list[str] = typer.Argument(None, help="Types: SECRET, ACCOUNT, PERSON, EMAIL, PHONE, ADDRESS, URL, DATE."),
    yes: bool = typer.Option(False, "--yes", help="Confirm letting secrets through unmasked."),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """Which types are masked. `pass` lets a type through unmasked (e.g. names); `mask` masks it again."""
    import json

    from .config import ConfigError
    from .detect import TYPES

    cfg = _config()
    wanted = [n.upper() for n in names or []]
    if action not in ("list", "mask", "pass") or any(n not in TYPES for n in wanted) or (action != "list" and not wanted):
        typer.echo(f"keepmum types: use list, or mask|pass with types from {', '.join(TYPES)}", err=True)
        raise typer.Exit(2)
    if action == "pass" and "SECRET" in wanted and not yes:
        typer.echo("keepmum types: letting secrets through sends API keys and passwords to the provider unmasked; "
                   "add --yes if you mean it", err=True)
        raise typer.Exit(2)
    passed = set(cfg.pass_types)
    if action != "list":
        passed = passed | set(wanted) if action == "pass" else passed - set(wanted)
        try:
            _update_config(cfg.path, "detect", "pass", [t for t in TYPES if t in passed], f"keepmum types {action}")
        except (ConfigError, OSError) as e:
            typer.echo(f"keepmum types: {e}", err=True)
            raise typer.Exit(1)
    personal = [t for t in TYPES if t != "SECRET"]
    state = {t: ("pass" if t in passed else "mask" if t == "SECRET" or cfg.pii else "off: personal data is off")
             for t in TYPES}
    if as_json:
        typer.echo(json.dumps({"pii": cfg.pii, "types": state}))
        return
    for t in TYPES:
        typer.echo(f"{t:8} {state[t]}", err=True)
    if not cfg.pii and any(t in personal for t in wanted):
        typer.echo("Personal data is off ([detect] pii = false), so personal types are not masked anyway.", err=True)


rules_app = typer.Typer(help="Your own rules (patterns always masked, exceptions never masked), and built-in rules turned off.",
                        no_args_is_help=True)
app.add_typer(rules_app, name="rules")


def _rules_json(cfg) -> dict:
    return {"mask": cfg.mask_rules, "allow_strings": cfg.allow_strings, "allow_patterns": cfg.allow_patterns,
            "off": cfg.off_rules}


@rules_app.command("list")
def rules_list(as_json: bool = typer.Option(False, "--json", help="Print JSON.")) -> None:
    """Show your rules."""
    import json

    cfg = _config()
    if as_json:
        typer.echo(json.dumps(_rules_json(cfg)))
        return
    for i, r in enumerate(cfg.mask_rules):
        typer.echo(f"mask  {i}  {r['name']}  {r['type']}  /{r['pattern']}/", err=True)
    for i, v in enumerate(cfg.allow_strings):
        typer.echo(f"allow {i}  {v!r}", err=True)
    for i, v in enumerate(cfg.allow_patterns):
        typer.echo(f"allow-pattern {i}  /{v}/", err=True)
    for r in cfg.off_rules:
        typer.echo(f"off   {r}  (built-in; `keepmum rules on {r}` turns it back on)", err=True)


def _save_rules(cfg, section: str, key: str, value, command: str) -> None:
    from .config import ConfigError

    try:
        _update_config(cfg.path, section, key, value, f"keepmum rules {command}")
    except (ConfigError, OSError) as e:
        typer.echo(f"keepmum rules: {e}", err=True)
        raise typer.Exit(1)
    typer.echo("saved; running proxies apply it on their next request", err=True)


@rules_app.command("mask")
def rules_mask(
    pattern: str = typer.Argument(..., help="A Python regular expression; group 1 is masked if it has a group."),
    name: str = typer.Option(None, help="A name shown in the dashboard."),
    kind: str = typer.Option("SECRET", "--type", help="The type its matches are masked as."),
) -> None:
    """Always mask what PATTERN matches."""
    import re

    from .detect import TYPES

    cfg = _config()
    try:
        re.compile(pattern)
    except re.error as e:
        typer.echo(f"keepmum rules: bad pattern: {e}", err=True)
        raise typer.Exit(2)
    if kind.upper() not in TYPES:
        typer.echo(f"keepmum rules: --type must be one of {', '.join(TYPES)}", err=True)
        raise typer.Exit(2)
    rule = {"name": name or f"rule {len(cfg.mask_rules) + 1}", "pattern": pattern, "type": kind.upper()}
    _save_rules(cfg, "rules", "mask", [*cfg.mask_rules, rule], "mask")


@rules_app.command("allow")
def rules_allow(
    value: str = typer.Argument(..., help="Exact text that is never masked."),
    pattern: bool = typer.Option(False, "--pattern", help="VALUE is a regular expression matching the whole value."),
) -> None:
    """Never mask VALUE (an exception)."""
    import re

    cfg = _config()
    if pattern:
        try:
            re.compile(value)
        except re.error as e:
            typer.echo(f"keepmum rules: bad pattern: {e}", err=True)
            raise typer.Exit(2)
        _save_rules(cfg, "allowlist", "patterns", [*cfg.allow_patterns, value], "allow")
    else:
        _save_rules(cfg, "allowlist", "strings", [*cfg.allow_strings, value], "allow")


@rules_app.command("remove")
def rules_remove(
    kind: str = typer.Argument(..., help="mask, allow or allow-pattern"),
    index: int = typer.Argument(..., help="The number `keepmum rules list` shows."),
) -> None:
    """Remove one of your rules."""
    cfg = _config()
    table = {"mask": ("rules", "mask", cfg.mask_rules), "allow": ("allowlist", "strings", cfg.allow_strings),
             "allow-pattern": ("allowlist", "patterns", cfg.allow_patterns)}
    if kind not in table or not 0 <= index < len(table[kind][2]):
        typer.echo("keepmum rules: no such rule; see `keepmum rules list`", err=True)
        raise typer.Exit(2)
    section, key, items = table[kind]
    _save_rules(cfg, section, key, [x for i, x in enumerate(items) if i != index], "remove")


@rules_app.command("builtin")
def rules_builtin(as_json: bool = typer.Option(False, "--json", help="Print JSON.")) -> None:
    """List the built-in rules and checks, with their type and whether they are on."""
    import json

    from .detect.rules import builtin_rules

    cfg = _config()
    rules = [{"id": r, "type": t, "on": r not in cfg.off_rules} for r, t in builtin_rules().items()]
    if as_json:
        typer.echo(json.dumps(rules))
        return
    for r in rules:
        typer.echo(f"{'on ' if r['on'] else 'OFF'}  {r['type']:8} {r['id']}", err=True)


@rules_app.command("off")
def rules_off(
    names: list[str] = typer.Argument(..., help="Built-in rule names, as `keepmum rules builtin` lists them."),
    yes: bool = typer.Option(False, "--yes", help="Confirm turning off a rule that finds secrets."),
) -> None:
    """Turn off built-in rules that mask something they should not. Other rules and the local
    model can still find the same value; for one value, `keepmum rules allow` is narrower."""
    from .detect.rules import builtin_rules

    cfg = _config()
    unknown = [n for n in names if n not in builtin_rules()]
    if unknown:
        typer.echo(f"keepmum rules: no built-in rule {unknown[0]!r}; see `keepmum rules builtin`", err=True)
        raise typer.Exit(2)
    secret = [n for n in names if builtin_rules()[n] == "SECRET"]
    if secret and not yes:
        typer.echo(f"keepmum rules: {', '.join(secret)} finds secrets; with it off, what only it finds reaches the "
                   "provider unmasked. Add --yes if you mean it, or `keepmum rules allow` one value instead", err=True)
        raise typer.Exit(2)
    _save_rules(cfg, "rules", "off", [*cfg.off_rules, *(n for n in dict.fromkeys(names) if n not in cfg.off_rules)], "off")


@rules_app.command("on")
def rules_on(names: list[str] = typer.Argument(..., help="Built-in rule names turned off earlier.")) -> None:
    """Turn built-in rules back on."""
    cfg = _config()
    _save_rules(cfg, "rules", "off", [r for r in cfg.off_rules if r not in names], "on")


tools_app = typer.Typer(help="Tools that reach the network (MCP servers, web search): which real values each may receive.",
                        invoke_without_command=True)
app.add_typer(tools_app, name="tools")

_SETTINGS_HELP = {"placeholders": "placeholders only", "own": "values it gave you",
                  "contacts": "values it gave you, and contact details", "custom": "values it gave you, and",
                  "restore": "every value, secrets included"}


def _registry_server(name: str) -> str:
    from .tools import ToolRegistry

    key = ToolRegistry().find(name)
    if key is None:
        typer.echo(f"keepmum tools: no server {name!r} seen yet; `keepmum tools` lists the ones agents offered", err=True)
        raise typer.Exit(2)
    return key


@tools_app.callback()
def tools_list(ctx: typer.Context, as_json: bool = typer.Option(False, "--json", help="Print JSON.")) -> None:
    """The servers agents offered, what each may receive, and the calls that kept placeholders this week."""
    if ctx.invoked_subcommand is not None:
        return
    import json

    from .tools import ToolRegistry

    servers = ToolRegistry().snapshot(_config().restore)
    if as_json:
        typer.echo(json.dumps({"servers": servers}))
        return
    if not servers:
        typer.echo("no network tools seen yet: they show up after an agent's first request through keepmum", err=True)
    for x in servers:
        setting = _SETTINGS_HELP.get(x["setting"], x["setting"])
        if x["setting"] == "custom":
            setting += " " + ", ".join(x["types"])
        flags = [f for f, on in (("new", x["new"]), ("PAUSED: tools changed", x["paused"])) if on]
        if not x["own_rule"] and not x["default"]:
            flags.append(f"from {x['rule']}")
        kept = sum(x["kept_week"].values())
        typer.echo(f"{x['label']:20} {setting}" + (f"  ({'; '.join(flags)})" if flags else "")
                   + (f"  · {kept} kept this week: {', '.join(sorted(x['kept_week']))}" if kept else ""), err=True)


@tools_app.command("set")
def tools_set(
    server: str = typer.Argument(..., help="A server, as `keepmum tools` names it (gmail, mcp__gmail)."),
    setting: str = typer.Argument(..., help="placeholders, own, contacts, or default (back to keepmum's default)."),
    yes: bool = typer.Option(False, "--yes", help="Confirm giving real values to a public tool (web search, web fetch)."),
) -> None:
    """Choose what one server may receive."""
    from .tools import CONTACTS

    values = {"placeholders": "keep", "own": "own", "contacts": list(CONTACTS), "default": None}
    if setting not in values:
        typer.echo("keepmum tools: SETTING must be placeholders, own, contacts or default", err=True)
        raise typer.Exit(2)
    _save_tool(_registry_server(server), values[setting], yes, "set")


@tools_app.command("allow")
def tools_allow(
    server: str = typer.Argument(..., help="A server, as `keepmum tools` names it."),
    types: list[str] = typer.Argument(..., help="Types it may also receive: PERSON, EMAIL, PHONE, ADDRESS, DATE, URL, ACCOUNT."),
    yes: bool = typer.Option(False, "--yes", help="Confirm giving real values to a public tool (web search, web fetch)."),
) -> None:
    """Let a server also receive values of these types (never secrets)."""
    from .detect import TYPES
    from .restore import rule_for
    from .tools import rule_key

    wanted = [t.upper() for t in types]
    bad = [t for t in wanted if t not in TYPES or t == "SECRET"]
    if bad:
        typer.echo(f"keepmum tools: {bad[0]} cannot be allowed" + (": a network tool never gets a secret" if bad[0] == "SECRET"
                   else f"; types are {', '.join(t for t in TYPES if t != 'SECRET')}"), err=True)
        raise typer.Exit(2)
    key = _registry_server(server)
    current = rule_for(rule_key(key), _config().restore)[1]
    have = list(current) if isinstance(current, list) else []
    _save_tool(key, [*have, *(t for t in dict.fromkeys(wanted) if t not in have)], yes, "allow")


def _save_tool(server: str, value, yes: bool, command: str) -> None:
    from .audit import _lets_out_more
    from .config import ConfigError
    from .restore import rule_for
    from .tools import ToolRegistry, pinned, rule_key

    cfg = _config()
    key = rule_key(server)
    was = rule_for(key, cfg.restore)[1]
    now = value if value is not None else rule_for(key, {k: v for k, v in cfg.restore.items() if k != key})[1]
    if not pinned(server) and _lets_out_more(was, now) and not yes:
        typer.echo(f"keepmum tools: {server} sends what it gets to the public internet (a search engine, any web "
                   "page). Add --yes if you mean it", err=True)
        raise typer.Exit(2)
    try:
        _update_config(cfg.path, "restore", key, value, f"keepmum tools {command}")
    except (ConfigError, OSError) as e:
        typer.echo(f"keepmum tools: {e}", err=True)
        raise typer.Exit(1)
    ToolRegistry().reviewed(server)
    typer.echo("saved; running proxies apply it on their next request", err=True)


@tools_app.command("trust")
def tools_trust(server: str = typer.Argument(..., help="A server whose tools changed, as `keepmum tools` names it.")) -> None:
    """Trust a server's tools as they are now, after they changed (until then it gets placeholders only)."""
    from .audit import AuditLog, caller
    from .tools import ToolRegistry

    key = _registry_server(server)
    changed = ToolRegistry().trust(key)
    AuditLog().append("tools", action="trust", server=key, changed=changed or None, by=caller("keepmum tools trust"))
    typer.echo(f"trusted {key}" + (f"; changed: {', '.join(changed)}" if changed else ""), err=True)


@tools_app.command("reviewed")
def tools_reviewed(server: str = typer.Argument(None, help="A server; every server when left out.")) -> None:
    """Mark new servers as seen, keeping their setting, so the menu bar stops pointing at them."""
    from .tools import ToolRegistry

    ToolRegistry().reviewed(_registry_server(server) if server else None)


@app.command()
def exclude(
    path: Path = typer.Argument(None, help="A folder in the project (default: the current folder)."),
    list_: bool = typer.Option(False, "--list", help="List excluded projects."),
) -> None:
    """Let Claude Code in one project skip keepmum and go straight to the API. `keepmum include` undoes it."""
    from .protect import ProtectError, excluded_projects
    from .protect import exclude as do_exclude

    if list_:
        for p in excluded_projects():
            typer.echo(f"{p['path']}  -> {p['url']}" + ("" if p["active"] else "  (setting removed: goes through keepmum)"))
        return
    from .protect import status

    cfg = _config()
    # Where keepmum would have forwarded: the gateway it is in front of, if any, else the upstream.
    try:
        to = status(cfg.port).via or cfg.upstream
    except ProtectError:
        to = cfg.upstream
    try:
        root = do_exclude(path or Path.cwd(), to)
    except ProtectError as e:
        typer.echo(f"keepmum exclude: {e}", err=True)
        raise typer.Exit(1)
    from .audit import AuditLog, caller

    AuditLog().append("exclude", project=str(root), to=to, by=caller("keepmum exclude"))
    typer.echo(f"excluded {root}: Claude Code there goes straight to {to}, unscanned.\n"
               "It applies once the folder is trusted in Claude Code; sessions already open there switch now.", err=True)


@app.command()
def include(path: Path = typer.Argument(None, help="A folder in the project (default: the current folder).")) -> None:
    """Scan an excluded project again."""
    from .protect import ProtectError
    from .protect import include as do_include

    try:
        root = do_include(path or Path.cwd())
    except ProtectError as e:
        typer.echo(f"keepmum include: {e}", err=True)
        raise typer.Exit(1)
    from .audit import AuditLog, caller

    AuditLog().append("include", project=str(root), by=caller("keepmum include"))
    typer.echo(f"included {root} again. Restart Claude Code sessions open there: a running session keeps "
               "going straight to the API until it restarts.", err=True)


@app.command()
def status(as_json: bool = typer.Option(True, "--json/--text", help="Print JSON (for the Mac app).")) -> None:
    """Setup and service state in one place: model, pf-serve, protection, running proxy."""
    import json

    from .detect.pfserve import MODEL_SIZE, find_binary, find_model
    from .launch import shared_serve_port
    from .protect import ProtectError, excluded_projects
    from .protect import status as protect_status

    cfg = _config()
    from .clients import protect_clients

    try:
        prot = protect_status(cfg.port).as_dict()
    except ProtectError as e:
        prot = {"error": str(e)}
    clients = protect_clients("status", cfg.port)
    binary, model = find_binary(), find_model()
    out = {"model": str(model) if model else None, "model_size": MODEL_SIZE, "pf_serve": str(binary) if binary else None,
           "protect": prot, "port": cfg.port, "serve_running": shared_serve_port(cfg) is not None,
           "observe": cfg.observe, "model_on": cfg.model, "clients": clients, "pii": cfg.pii, "pass": cfg.pass_types, "rules": _rules_json(cfg),
           "excluded": excluded_projects(), "gateways": cfg.gateways, "destinations": cfg.destinations,
           "previews": cfg.log_previews, "context": cfg.log_context, "keep_days": cfg.audit_keep_days,
           "guard": _guard_status()}
    typer.echo(json.dumps(out) if as_json else "\n".join(f"{k}: {v}" for k, v in out.items()))


@app.command()
def doctor(as_json: bool = typer.Option(False, "--json", help="Print JSON (for the Mac app).")) -> None:
    """Check that this Mac can run keepmum, and that nothing routes around it."""
    from .doctor import run

    raise typer.Exit(run(as_json))


audit_app = typer.Typer(help="The audit log: what keepmum did with each request, and every change to its protection. "
                             "Counts and names only, never values.", no_args_is_help=True)
app.add_typer(audit_app, name="audit")


def _since(text: str | None) -> float | None:
    """`--since`: 30d, 12h, 90m, or a date (2026-10-01)."""
    import re
    import time
    from datetime import datetime

    if not text:
        return None
    m = re.fullmatch(r"(\d+)([dhm])", text.strip())
    if m:
        return time.time() - int(m.group(1)) * {"d": 86400, "h": 3600, "m": 60}[m.group(2)]
    try:
        return datetime.fromisoformat(text.strip()).timestamp()
    except ValueError:
        typer.echo("keepmum audit: --since takes 30d, 12h, 90m or a date such as 2026-10-01", err=True)
        raise typer.Exit(2)


def _audit_entries(since: str | None, weakening: bool, requests: bool = True) -> list[dict]:
    from .audit import read, weakens

    start = _since(since)
    return [e for e in read() if (start is None or e.get("ts", 0) >= start) and (not weakening or weakens(e))
            and (requests or e.get("event") != "request")]


@audit_app.command("list")
def audit_list(
    since: str = typer.Option("7d", help="How far back: 30d, 12h, 90m, or a date."),
    weakening: bool = typer.Option(False, "--weakening", help="Only changes that turned protection down."),
    requests: bool = typer.Option(True, "--requests/--no-requests", help="Include each request, or only the other events."),
    limit: int = typer.Option(50, help="The newest N entries (0: all)."),
    as_json: bool = typer.Option(False, "--json", help="Print JSON (for the Mac app)."),
) -> None:
    """The newest entries, oldest first."""
    import json
    from datetime import datetime

    from .audit import summary, weakens

    entries = _audit_entries(since, weakening, requests)
    if limit:
        entries = entries[-limit:]
    if as_json:
        typer.echo(json.dumps([{**e, "summary": summary(e), "weakens": weakens(e)} for e in entries]))
        return
    if not entries:
        typer.echo("no audit entries in that period", err=True)
    for e in entries:
        when = datetime.fromtimestamp(e.get("ts", 0)).strftime("%Y-%m-%d %H:%M:%S")
        flag = "!" if weakens(e) else " "
        by = f"  [{e['by']}]" if e.get("by") else ""
        typer.echo(f"{e.get('seq', '?'):>7} {when} {flag} {e.get('event', '?'):8} {summary(e)}{by}")


@audit_app.command("verify")
def audit_verify(
    anchor: str = typer.Option(None, help="<seq>:<hash> from an earlier `keepmum audit head`: the log must still hold it."),
    file: Path = typer.Option(None, "--file", help="Check a JSONL file from `keepmum audit export` instead of the log."),
    as_json: bool = typer.Option(False, "--json", help="Print JSON (for the Mac app)."),
) -> None:
    """Check that no entry was edited, added, removed or reordered. Exits 1 if any was."""
    import json

    from .audit import verify

    result = verify(file, anchor=anchor, export=file is not None)
    if as_json:
        typer.echo(json.dumps(result))
    else:
        last = result["last"]
        if result["ok"]:
            typer.echo(f"audit log intact: {result['entries']} entries"
                       + (f", seq {result['first']['seq']} to {last['seq']}, head {last['seq']}:{last['hash'][:16]}" if last else ""),
                       err=True)
        else:
            typer.echo(f"AUDIT LOG BROKEN: {len(result['problems'])} problem(s)", err=True)
            for p in result["problems"][:20]:
                typer.echo(f"  {p}", err=True)
            if len(result["problems"]) > 20:
                typer.echo(f"  … and {len(result['problems']) - 20} more", err=True)
        typer.echo("The chain shows edits and removals inside the log. A rewrite of the whole log is caught only "
                   "against a head saved elsewhere: `keepmum audit head`, then `verify --anchor`.", err=True)
    raise typer.Exit(0 if result["ok"] else 1)


@audit_app.command("head")
def audit_head(as_json: bool = typer.Option(False, "--json", help="Print JSON.")) -> None:
    """The newest entry's <seq>:<hash>. Keep it somewhere else (a note, a ticket, another machine);
    `keepmum audit verify --anchor` then shows whether the log was rewritten since."""
    import json

    from .audit import read

    entries = read()
    if not entries:
        typer.echo("the audit log is empty", err=True)
        raise typer.Exit(1)
    last = entries[-1]
    if as_json:
        typer.echo(json.dumps({"seq": last["seq"], "hash": last["hash"], "ts": last.get("ts")}))
    else:
        typer.echo(f"{last['seq']}:{last['hash']}")


@audit_app.command("keep")
def audit_keep(days: int = typer.Argument(None, help="Days the log keeps entries (default 90); leave out to show it.")) -> None:
    """How long the audit log keeps entries. A shorter time is recorded as weakening protection."""
    from .config import ConfigError

    cfg = _config()
    if days is None:
        typer.echo(f"the audit log keeps entries for {cfg.audit_keep_days} days", err=True)
        return
    if days < 1:
        typer.echo("keepmum audit keep: at least 1 day", err=True)
        raise typer.Exit(2)
    try:
        _update_config(cfg.path, "audit", "keep_days", days, f"keepmum audit keep {days}")
    except (ConfigError, OSError) as e:
        typer.echo(f"keepmum audit: {e}", err=True)
        raise typer.Exit(1)
    typer.echo(f"the audit log keeps entries for {days} days; the background service removes older ones", err=True)


@audit_app.command("export")
def audit_export(
    since: str = typer.Option("30d", help="How far back: 30d, 12h, 90m, or a date."),
    fmt: str = typer.Option("jsonl", "--format", help="jsonl (the entries as written, checkable) or csv (one row each)."),
    output: Path = typer.Option(None, "--output", "-o", help="Write to a file instead of standard output."),
    weakening: bool = typer.Option(False, "--weakening", help="Only changes that turned protection down."),
) -> None:
    """Export entries for a review or an incident ticket. JSONL keeps each entry exactly as written,
    so its hashes still check; CSV is for reading."""
    import csv
    import io
    import json
    from datetime import datetime, timezone

    from .audit import summary, verify, weakens

    if fmt not in ("jsonl", "csv"):
        typer.echo("keepmum audit: --format is jsonl or csv", err=True)
        raise typer.Exit(2)
    entries = _audit_entries(since, weakening)
    buf = io.StringIO()
    if fmt == "jsonl":
        for e in entries:
            buf.write(json.dumps(e, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n")
    else:
        w = csv.writer(buf)
        w.writerow(["seq", "time_utc", "event", "weakens", "client", "provider", "outcome", "masked", "new", "summary", "hash"])
        for e in entries:
            w.writerow([e.get("seq"), datetime.fromtimestamp(e.get("ts", 0), timezone.utc).isoformat(timespec="seconds"),
                        e.get("event"), "yes" if weakens(e) else "", e.get("client", ""), e.get("provider", ""),
                        e.get("outcome", ""), sum((e.get("masked") or {}).values()) if e.get("event") == "request" else "",
                        e.get("new", ""), summary(e), e.get("hash")])
    if output:
        import os

        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(buf.getvalue())
        result = verify()
        typer.echo(f"wrote {len(entries)} entries to {output}; the log is "
                   + ("intact" if result["ok"] else f"BROKEN ({len(result['problems'])} problems: `keepmum audit verify`)"),
                   err=True)
    else:
        typer.echo(buf.getvalue(), nl=False)
