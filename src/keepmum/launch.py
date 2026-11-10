"""Run the proxy in the background and Claude Code in front of it (`keepmum claude`)."""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

from .config import Config
from .proxy import Proxy

def runtime_file() -> Path:
    return Path.home() / ".config" / "keepmum" / "runtime.json"


def log_file() -> Path:
    return Path.home() / ".config" / "keepmum" / "keepmum.log"

# Settings or environment that would send Claude Code's traffic somewhere other than keepmum.
BYPASS_KEYS = ("ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
               "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL")


def build_proxy(cfg: Config, pii: bool | None = None, rules_only: bool = False, upstream: str | None = None,
                mode: str = "serve", gateways: dict[str, str] | None = None) -> Proxy:
    """`gateways` (name -> URL) are added for this run to config [gateways], which can change."""
    from .config import LiveConfig
    from .detect.pipeline import default_pipeline
    from .redact import Redactor
    from .stats import stats_token
    from .vault import Vault

    from . import __version__


    fixed_pii = pii is not None
    pii = cfg.pii if pii is None else pii
    from .gateway import is_ours

    if upstream and is_ours(upstream, cfg.port):
        upstream = None  # protection points Claude Code at keepmum: never forward to ourselves
    from .detect.pipeline import ModelSwitch

    # `rules_only` (--rules-only) holds for this run; otherwise detect.model decides, and can change.
    pipeline = default_pipeline(pii=pii, rules_only=rules_only or not cfg.model, allow_strings=cfg.allow_strings,
                                allow_patterns=cfg.allow_patterns, pass_types=cfg.pass_types, mask_rules=cfg.mask_rules,
                                off_rules=cfg.off_rules)
    info = {"mode": mode, "pii": pii, "detector": pipeline.name, "version": __version__, "apis": ["messages", "responses", "gemini", "antigravity"],
            "checks": {"injection": cfg.guard_injection, "harm": cfg.guard_harm, "harm_model": cfg.guard_harm_model,
                       "commands": cfg.guard_commands}}
    from .stats import History, Stats

    from .audit import AuditLog
    from .tools import ToolRegistry

    audit = AuditLog(keep_days=cfg.audit_keep_days)
    if mode == "serve":
        audit.prune()  # the long-running service keeps the log to its retention
    # Settings changed while no keepmum was running show up now; later ones as the file changes.
    audit.note_config(cfg, "config file")

    def apply(new) -> None:  # the config file changed: settings apply on the next request
        audit.keep_days = new.audit_keep_days
        audit.note_config(new, "config file")
        pipeline.apply_settings(new.pass_types, new.mask_rules, new.allow_strings, new.allow_patterns, new.off_rules,
                                pii=None if fixed_pii else new.pii)
        if not fixed_pii:
            info["pii"] = new.pii
        info["detector"] = pipeline.name
        info["checks"] = {"injection": new.guard_injection, "harm": new.guard_harm, "harm_model": new.guard_harm_model,
                          "commands": new.guard_commands}
        harm_model.want(new.guard_harm_model, new.guard_harm_sensitivity)
        injection.threshold = new.guard_injection_threshold
        proxy.policy = new.restore
        proxy.gateways = {**new.gateways, **(gateways or {})}
        proxy.destinations = new.destinations
        if not rules_only:
            switch.want(new.model)

    observe = LiveConfig(cfg.path, cfg, on_change=apply)
    from .commands import CommandsCheck
    from .guard import SHORT_CHARS, Guard
    from .harm import HarmModel

    # Models load on their first check and unload when idle; modes follow the config file.
    injection = Guard(lambda: observe.config.guard_injection, threshold=cfg.guard_injection_threshold, name="injection",
                      short_chars=SHORT_CHARS)
    harm_model = HarmModel(cfg.guard_harm_model, cfg.guard_harm_sensitivity)
    harm = Guard(lambda: observe.config.guard_harm, harm_model, threshold=harm_model.threshold, name="harm",
                 min_chars=8)
    harm_model.guard = harm
    history = History()
    from .local import servers_for, skills_for

    proxy = Proxy(Redactor(pipeline, Vault()), upstream=upstream or cfg.upstream, policy=cfg.restore,
                  stats=Stats(history=history), stats_token=stats_token(), info=info, observe=observe,
                  previews=lambda: observe.config.log_previews,
                  context=lambda: observe.config.log_context, audit=audit, tools=ToolRegistry(), guards=[injection, harm],
                  commands=CommandsCheck(lambda: observe.config.guard_commands),
                  local=lambda check: servers_for(observe.config.guard_local_servers, check),
                  local_skills=lambda check: skills_for(observe.config.guard_local_skills, check))
    proxy.gateways = {**cfg.gateways, **(gateways or {})}
    proxy.destinations = cfg.destinations
    switch = ModelSwitch(pipeline, proxy._scan_lock)
    return proxy


def bypass_risks(cwd: Path, port: int | None = None) -> list[str]:
    """Places that could route Claude Code around keepmum. Any hit means: do not start.
    An ANTHROPIC_BASE_URL pointing at keepmum itself on `port` (`keepmum protect`) is not a risk."""
    from .gateway import is_ours
    from .protect import is_excluded, settings_path

    risks = [f"environment variable {k} is set" for k in BYPASS_KEYS[1:] if os.environ.get(k)]
    excluded = is_excluded(cwd)  # reported by the callers as an exclusion, not a risk
    user = settings_path()
    files = [user, user.with_name("settings.local.json"),
             Path("/Library/Application Support/ClaudeCode/managed-settings.json")]
    for d in [cwd, *cwd.parents]:
        files += [d / ".claude" / "settings.json", d / ".claude" / "settings.local.json"]
    for f in files:
        try:
            env = json.loads(f.read_text()).get("env", {})
        except (OSError, ValueError, AttributeError):
            continue
        if not isinstance(env, dict):
            continue
        for k in BYPASS_KEYS:
            ours = k == "ANTHROPIC_BASE_URL" and port is not None and is_ours(env.get(k), port)
            ours = ours or (k == "ANTHROPIC_BASE_URL" and excluded is not None and f.name == "settings.local.json"
                            and env.get(k) == excluded)
            if k in env and not ours:
                gateway = f == user and k == "ANTHROPIC_BASE_URL" and str(env[k]).startswith(("http://", "https://"))
                risks.append(f"{f} sets env {k}" + (f" to {env[k]}; if that is a gateway, `keepmum protect on` "
                                                   "puts keepmum in front of it" if gateway else ""))
    return risks


def _pid_alive(pid: int) -> bool:
    if isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _write_runtime(port: int, mode: str | None = None) -> None:
    rf = runtime_file()
    if mode != "serve" and rf.exists():
        try:
            old = json.loads(rf.read_text())
            if old.get("mode") == "serve" and isinstance(old.get("pid"), int) and _pid_alive(old["pid"]):
                return  # keepmum serve is running: keep runtime.json pointed at the service
        except (OSError, ValueError):
            pass
    rf.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = rf.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"pid": os.getpid(), "port": port, "started": time.time(), **({"mode": mode} if mode else {})}, f)
    tmp.replace(rf)


def _clear_runtime() -> None:
    rf = runtime_file()
    try:
        if json.loads(rf.read_text()).get("pid") == os.getpid():
            rf.unlink()
    except (OSError, ValueError):
        pass


class BackgroundServer:
    """uvicorn in a thread, on a socket bound before it starts (port 0 picks a free one)."""

    def __init__(self, proxy: Proxy, port: int = 0):
        import uvicorn

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.port = self.sock.getsockname()[1]
        self.proxy = proxy
        config = uvicorn.Config(proxy.app, log_level="warning", access_log=False, lifespan="off")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.sock]}, daemon=True)

    def start(self, timeout: float = 10) -> None:
        self.thread.start()
        deadline = time.monotonic() + timeout
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("the proxy did not start")
            time.sleep(0.02)
        _write_runtime(self.port, getattr(self.proxy, "info", {}).get("mode"))
        self._audit("start")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)
        try:
            self.sock.close()
        except OSError:
            pass
        redactor = getattr(self.proxy, "redactor", None)
        if redactor is not None:
            redactor.vault.clear()
            pf = getattr(redactor.detector, "pf", None)
            if pf is not None and hasattr(pf, "close"):
                pf.close()
        _clear_runtime()
        self._audit("stop")

    def _audit(self, event: str) -> None:
        """A proxy began or ended: gaps in coverage show in the audit log. A stop that never
        comes (a crash, a kill) shows as a start with no stop."""
        audit = getattr(self.proxy, "audit", None)
        if audit is None:
            return
        info = self.proxy.info
        extra = {"port": self.port, "version": info.get("version"), "pii": info.get("pii"),
                 "detector": info.get("detector"), "observe": self.proxy._observe()} if event == "start" else {}
        audit.append(event, mode=info.get("mode"), pid=os.getpid(), **extra)


def log_to_file(level: str) -> None:
    """In `keepmum claude` the terminal belongs to Claude Code; keepmum logs to a private file."""
    lf = log_file()
    lf.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not lf.exists():
        os.close(os.open(lf, os.O_WRONLY | os.O_CREAT, 0o600))
    handler = logging.FileHandler(lf)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for name in ("httpx", "httpcore", "uvicorn", "uvicorn.error"):
        logging.getLogger(name).setLevel(logging.WARNING)


def run_claude(proxy: Proxy, args: list[str]) -> int:
    claude = shutil.which("claude")
    if claude is None:
        raise FileNotFoundError("`claude` is not on PATH")
    return run_through(proxy, [claude, *args])


def run_through(proxy: Proxy, command: list[str]) -> int:
    """Start the proxy, run the command pointed at it, stop the proxy when it exits."""
    server = BackgroundServer(proxy)
    server.start()
    try:
        return _run_child(command, {**os.environ, "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.port}"})
    finally:
        server.stop()


def _run_child(command: list[str], env: dict[str, str]) -> int:
    """Run Claude with stdio inherited. Ctrl-C reaches it directly (same process group);
    a TERM or HUP sent to keepmum (an editor stopping the session) is passed on to it."""
    child = subprocess.Popen(command, env=env)

    def forward(signum, _frame):
        if child.poll() is None:
            child.send_signal(signum)

    previous = {sig: signal.signal(sig, handler) for sig, handler in
                ((signal.SIGINT, signal.SIG_IGN), (signal.SIGTERM, forward), (signal.SIGHUP, forward))}
    try:
        return child.wait()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


# Where Gemini CLI sends its requests, one variable per sign-in. Set in its environment, they win
# over every .env file it reads (a .env value applies only to a variable not already set).
GEMINI_VARS = {"GOOGLE_GEMINI_BASE_URL": "/gemini", "CODE_ASSIST_ENDPOINT": "/codeassist",
               "GOOGLE_VERTEX_BASE_URL": "/vertex"}


def gemini_env(port: int, env: dict[str, str]) -> dict[str, str]:
    """Gemini CLI's environment pointed at keepmum: the Gemini API (an API key), Code Assist
    ("Sign in with Google"), and Vertex AI, which keepmum refuses rather than let through."""
    return {**env, **{k: f"http://127.0.0.1:{port}{path}" for k, path in GEMINI_VARS.items()}}


def gemini_upstreams(env: dict[str, str], port: int) -> dict[str, str]:
    """A Gemini or Code Assist address already set (a company gateway): keepmum forwards there.
    Only those two: a Vertex address is refused like any Vertex request."""
    out = {}
    for var, prefix in (("GOOGLE_GEMINI_BASE_URL", "/gemini"), ("CODE_ASSIST_ENDPOINT", "/codeassist")):
        value = env.get(var, "").rstrip("/")
        if value and not value.startswith((f"http://127.0.0.1:{port}", f"http://localhost:{port}")):
            out[prefix] = value
    return out


# The Antigravity CLI (agy 1.5.3): signed in with Google, it sends every Code Assist call, the
# model's included, to CLOUD_CODE_URL. With a company gateway (AGY_LLM_GATEWAY_URL), it sends its
# model calls there in the Gemini API's format instead, authenticates with the gateway's key, and
# makes no Code Assist calls; with CLOUD_CODE_URL set too, its model calls go to CLOUD_CODE_URL.
ANTIGRAVITY_VAR = "CLOUD_CODE_URL"
ANTIGRAVITY_GATEWAY_VAR = "AGY_LLM_GATEWAY_URL"


def antigravity_env(port: int, env: dict[str, str]) -> dict[str, str]:
    """agy's environment pointed at keepmum: a gateway, when one is set, at /gemini; else Code
    Assist at /antigravity."""
    if env.get(ANTIGRAVITY_GATEWAY_VAR):
        return {**env, ANTIGRAVITY_GATEWAY_VAR: f"http://127.0.0.1:{port}/gemini"}
    return {**env, ANTIGRAVITY_VAR: f"http://127.0.0.1:{port}/antigravity"}


def antigravity_upstreams(env: dict[str, str], port: int) -> dict[str, str]:
    """Addresses already set for agy (a company gateway, a Code Assist endpoint): keepmum forwards
    there, as {route prefix: URL}."""
    out = {}
    for var, prefix in ((ANTIGRAVITY_VAR, "/antigravity"), (ANTIGRAVITY_GATEWAY_VAR, "/gemini")):
        value = env.get(var, "").rstrip("/")
        if value and not value.startswith((f"http://127.0.0.1:{port}", f"http://localhost:{port}")):
            out[prefix] = value
    return out


def antigravity_refusal(env: dict[str, str], args: list[str]) -> str | None:
    """Why agy, run this way, would send requests keepmum never sees or cannot scan; None when it
    would not."""
    if env.get(ANTIGRAVITY_GATEWAY_VAR):
        if env.get(ANTIGRAVITY_VAR):
            return ("both AGY_LLM_GATEWAY_URL and CLOUD_CODE_URL are set: agy would send its model calls to "
                    "CLOUD_CODE_URL. Unset one of them")
        if env.get("AGY_LLM_GATEWAY_WIRE_PROTOCOL"):
            return ("AGY_LLM_GATEWAY_WIRE_PROTOCOL is set: agy would talk to the gateway in another API (openai is "
                    "Chat Completions), which keepmum does not scan. Unset it to use the Gemini API")
        if env.get("AGY_LLM_GATEWAY_PROXY_URL"):
            return "AGY_LLM_GATEWAY_PROXY_URL is set: agy would reach the gateway through that proxy, around keepmum"
        if env.get("AGY_LLM_GATEWAY_CLIENT_CERT"):
            return ("AGY_LLM_GATEWAY_CLIENT_CERT is set: the gateway wants agy's client certificate, which keepmum, "
                    "in between, cannot present")
    if any(a == "--remote-agent" or a.startswith("--remote-agent=") for a in args):
        return "--remote-agent runs the conversation on Google's machines, where keepmum cannot scan it"
    return None


PROTECTED_BUT_DOWN = ("protection is on, so Claude Code goes to the keepmum service, but it is not running: "
                      "open Keepmum.app or run `keepmum serve` (or `keepmum protect off`)")


def protected(cfg: Config) -> bool:
    """`keepmum protect` is on: Claude Code's settings send every session to `keepmum serve`."""
    from .protect import ProtectError, status

    try:
        return status(cfg.port).on
    except ProtectError:
        return False


def shared_serve_port(cfg: Config, need: str = "messages") -> int | None:
    """The port of a running `keepmum serve`, if there is one. It must answer the stats endpoint
    with this user's private token and say it is in serve mode: something else listening on
    the port gets nothing."""
    import httpx

    from .stats import token_file

    try:
        token = token_file().read_text().strip()
        r = httpx.get(f"http://127.0.0.1:{cfg.port}/_keepmum/stats", headers={"authorization": f"Bearer {token}"}, timeout=1.5)
        info = r.json() if r.status_code == 200 else {}
        # A service started before Responses API support only scans the Messages API.
        return cfg.port if info.get("mode") == "serve" and need in info.get("apis", ["messages"]) else None
    except (OSError, ValueError, httpx.HTTPError):
        return None


def wrap_main() -> int:
    """`keepmum-claude-wrapper <claude binary> [args...]`: for editors that launch Claude Code
    through a wrapper (VS Code's claudeCode.claudeProcessWrapper setting).

    Uses a running `keepmum serve` when there is one, so every conversation shares one model;
    otherwise starts a proxy for this conversation. Never writes to stdout: the editor talks
    to Claude over it. Refusals go to stderr, and Claude is not started.
    """
    import sys

    from .config import ConfigError, load
    from .detect.pipeline import ModelUnavailable

    def refuse(message: str) -> int:
        print(f"keepmum: {message}", file=sys.stderr)
        return 2

    if len(sys.argv) < 2:
        return refuse("usage: keepmum-claude-wrapper <claude binary> [args...]")
    command = sys.argv[1:]
    try:
        cfg = load()
    except ConfigError as e:
        return refuse(f"bad config: {e}")
    risks = bypass_risks(Path.cwd(), cfg.port)
    if risks:
        return refuse("refusing to start, Claude Code could bypass the proxy: " + "; ".join(risks))
    log_to_file(cfg.log_level)
    if not os.access(command[0], os.X_OK):
        return refuse(f"cannot run {command[0]}: not an executable file")
    from .protect import is_excluded

    if is_excluded(Path.cwd()):
        # `keepmum exclude` sends Claude Code in this project straight to the API: run it as is.
        logging.getLogger("keepmum").info("wrapper: project excluded, running Claude Code without keepmum")
        try:
            return _run_child(command, dict(os.environ))
        except OSError as e:
            return refuse(f"cannot run {command[0]}: {e.strerror}")
    port = shared_serve_port(cfg)
    if port is None and protected(cfg):
        return refuse(PROTECTED_BUT_DOWN)
    try:
        if port is not None:
            logging.getLogger("keepmum").info("wrapper: using keepmum serve on port %d", port)
            return _run_child(command, {**os.environ, "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}"})
        try:
            proxy = build_proxy(cfg, upstream=os.environ.get("ANTHROPIC_BASE_URL"), mode="vscode")
        except ModelUnavailable as e:
            return refuse(str(e))
        return run_through(proxy, command)
    except OSError as e:
        return refuse(f"cannot run {command[0]}: {e.strerror}")
