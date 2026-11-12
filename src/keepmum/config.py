"""~/.config/keepmum/config.toml. Every key is optional; unknown keys and bad values are errors.

    [server]
    port = 7788
    upstream = "https://api.anthropic.com"

    [detect]
    pii = false            # also mask personal data
    model = true           # the local model after the rules; false: the rules alone (faster, lighter, lower recall)
    observe = false        # count what would be masked, but forward every request unchanged
    pass = []              # types never masked, e.g. ["PERSON", "DATE"]

    [rules]                # your own patterns, always masked (Python regex; group 1 if present)
    mask = [{ name = "Employee ID", pattern = "EMP-\\d{6}", type = "ACCOUNT" }]
    off = []               # built-in rules turned off, by name, e.g. ["key-in-prose"] (`keepmum rules builtin`)

    [allowlist]            # added to the built-in allowlist, never replacing it
    strings = []
    patterns = []

    [gateways]             # other gateways keepmum forwards to (`keepmum protect on` adds them): name = URL
    local-3425 = "http://127.0.0.1:3425"

    [destinations]         # hosts a shell command may send a rule's tokens to, besides keepmum's list (destinations.py)
    gitlab-token = ["gitlab.corp.example"]

    [restore]              # per tool: "restore", "keep", "own" or a list of types; "mcp__*" style prefixes allowed
    default = "restore"
    WebFetch = "keep"
    "mcp__claude_ai_Gmail__*" = ["EMAIL", "PERSON"]   # its own values, and these types (never SECRET)

    [audit]
    keep_days = 90         # how long the audit log keeps entries (`keepmum audit`)

    [guard]                # optional checks (`keepmum guard setup injection|harm` first)
    injection = "off"      # tool results, for planted instructions (guard.py): "warn" logs and sends; "block" refuses
    harm = "off"           # what the user typed, for harmful requests (harm.py): "warn" or "block"
    harm_model = "small"   # "small" (Content Safety Guard, 0.4 GB while in use), or Qwen3Guard "0.6b" (1.1 GB) or "4b" (4.9 GB)
    injection_threshold = 0.8  # the injection check's score to flag (0.5-0.999): higher, fewer false alarms and fewer catches
                           # (a result under 200 characters needs more: 0.995 at 0.8)
    harm_sensitivity = "medium"  # "high", "medium", "low" or "lowest": lower, fewer false alarms and fewer catches
    commands = "off"       # shell commands in the model's replies, against a few rules (commands.py): "warn" or "block"
    # injection or harm = "local": a flagged request is answered by a model on this Mac (local.py),
    # the first of these that answers; checks: only for those (default both); api: "auto", "native" or "chat"
    local_servers = [{ url = "http://localhost:11434", model = "qwen3:8b" },
                     { url = "http://localhost:1234", model = "qwen/qwen3-8b", checks = ["harm"] }]
    # skills for those local turns, after keepmum's note: a SKILL.md, or a folder holding one, and its places (default all):
    # "injection" (answering a flagged tool result), "handback" (a summary or rewrite of it for the cloud), "harm"
    # (answering a flagged request); [] parks it. The older `checks` is read too. `keepmum guard local skill new`
    # and `add` keep them in ~/.config/keepmum/skills
    local_skills = [{ path = "~/.config/keepmum/skills/careful-answers", places = ["injection", "harm"] },
                    { path = "~/.config/keepmum/skills/summaries", places = ["handback"] }]
    # after a local turn of the injection check (handback.py): "stay" on the local model, or back to the cloud
    # with the flagged result replaced: "answer" (after the local answer), "summary", "rewrite" or "withhold"
    local_handback = "stay"
    local_handback_instructions = ""  # your own instruction for summary and rewrite, in place of keepmum's
    # look at a summary or rewrite before it goes to the cloud (review.py): "off", "rewrite" or "all"
    # (summaries too), the seconds to wait for an answer, and then "stay" (on this Mac) or "send"
    local_handback_review = "off"
    local_handback_review_wait = 60
    local_handback_review_timeout = "stay"

    [log]
    level = "info"
    previews = true        # the Logs page shows the first and last character of masked values (in memory only)
    context = true         # and the text around each, and what the checks flagged, every masked value shown as its type (in memory only)
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .restore import DEFAULT_POLICY


_SCHEMA = {
    "server": {"port": int, "upstream": str},
    "detect": {"pii": bool, "model": bool, "observe": bool, "pass": list},
    "allowlist": {"strings": list, "patterns": list},
    "rules": {"mask": list, "off": list},
    "log": {"level": str, "previews": bool, "context": bool},
    "audit": {"keep_days": int},
    "guard": {"injection": str, "injection_threshold": float, "harm": str, "harm_model": str, "harm_sensitivity": str, "commands": str, "local_url": str, "local_model": str,
              "local_servers": list, "local_skills": list, "local_handback": str, "local_handback_instructions": str,
              "local_handback_review": str, "local_handback_review_wait": int, "local_handback_review_timeout": str},
}
LOG_LEVELS = ("debug", "info", "warning", "error")
_HOST = re.compile(r"(?i)(?:[a-z0-9_-]+\.)*[a-z0-9-]+\.[a-z]{2,63}|localhost|\d{1,3}(?:\.\d{1,3}){3}")
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def gateway_problem(url: str, port: int | None = None) -> str | None:
    """Why keepmum cannot forward to `url` as a gateway, or None. Plain http only to this Mac:
    anywhere else it would carry the agent's key in the clear. Never keepmum itself."""
    from urllib.parse import urlsplit

    try:
        u = urlsplit(url)
        u.port  # noqa: B018 (raises on a bad port)
    except ValueError:
        return "is not a URL"
    if u.scheme not in ("http", "https") or not u.hostname:
        return "must be an http(s) URL"
    if u.query or u.fragment or u.username or u.password:
        return "must not have a query, a fragment or a user name"
    if u.scheme == "http" and u.hostname not in LOOPBACK:
        return "must use https (plain http only to this Mac: 127.0.0.1 or localhost)"
    if port is not None and u.hostname in LOOPBACK and (u.port or 80) == port:
        return f"is keepmum itself (port {port})"
    return None


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    port: int = 7788
    upstream: str = "https://api.anthropic.com"
    pii: bool = False
    model: bool = True
    observe: bool = False
    pass_types: list[str] = field(default_factory=list)
    mask_rules: list[dict] = field(default_factory=list)
    off_rules: list[str] = field(default_factory=list)
    allow_strings: list[str] = field(default_factory=list)
    allow_patterns: list[str] = field(default_factory=list)
    restore: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_POLICY))
    gateways: dict[str, str] = field(default_factory=dict)  # name -> URL (gateway.py)
    destinations: dict[str, list[str]] = field(default_factory=dict)  # rule id -> hosts (destinations.py)
    log_level: str = "info"
    log_previews: bool = True
    log_context: bool = True
    audit_keep_days: int = 90
    guard_injection: str = "off"
    guard_injection_threshold: float = 0.8
    guard_harm: str = "off"
    guard_harm_sensitivity: str = "medium"
    guard_harm_model: str = "small"
    guard_commands: str = "off"
    # local.Server entries, in the order tried. The older local_url and local_model are read as one.
    guard_local_servers: list = field(default_factory=list)
    # local.Skill entries: the user's skills for local mode's turns.
    guard_local_skills: list = field(default_factory=list)
    # What follows a local turn of the injection check (handback.py), and the user's own instruction.
    guard_local_handback: str = "stay"
    guard_local_handback_instructions: str = ""
    # Whether the user reviews a summary or rewrite before it goes (review.py): off, rewrite or all;
    # how long to wait for them; and what happens when they do not answer: stay or send.
    guard_local_handback_review: str = "off"
    guard_local_handback_review_wait: int = 60
    guard_local_handback_review_timeout: str = "stay"
    path: Path | None = None


def config_file() -> Path:
    """~/.config/keepmum/config.toml, for the current HOME."""
    return Path.home() / ".config" / "keepmum" / "config.toml"


def load(path: Path | None = None) -> Config:
    path = path or config_file()
    cfg = Config(path=path if path.exists() else None)
    if not path.exists():
        return cfg
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    for section, values in raw.items():
        if section in ("restore", "gateways", "destinations"):
            continue
        if section not in _SCHEMA or not isinstance(values, dict):
            raise ConfigError(f"{path}: unknown section [{section}]")
        for key, value in values.items():
            want = _SCHEMA[section].get(key)
            if want is None:
                raise ConfigError(f"{path}: unknown key {section}.{key}")
            if want is float and isinstance(value, int) and not isinstance(value, bool):
                value = values[key] = float(value)
            if not isinstance(value, want) or (want is int and isinstance(value, bool)):
                raise ConfigError(f"{path}: {section}.{key} must be {want.__name__}")
    server, detect, allow, log, rules, audit, guard = (raw.get(k, {}) for k in ("server", "detect", "allowlist", "log",
                                                                                "rules", "audit", "guard"))
    cfg.port = server.get("port", cfg.port)
    if not 1 <= cfg.port <= 65535:
        raise ConfigError(f"{path}: server.port must be 1-65535")
    cfg.upstream = server.get("upstream", cfg.upstream)
    if not re.match(r"https?://", cfg.upstream):
        raise ConfigError(f"{path}: server.upstream must be an http(s) URL")
    cfg.pii = detect.get("pii", cfg.pii)
    cfg.model = detect.get("model", cfg.model)
    cfg.observe = detect.get("observe", cfg.observe)
    from .detect import TYPES

    cfg.pass_types = [str(t) for t in detect.get("pass", [])]
    for t in cfg.pass_types:
        if t not in TYPES:
            raise ConfigError(f"{path}: detect.pass: unknown type {t!r} (types: {', '.join(TYPES)})")
    for i, rule in enumerate(rules.get("mask", [])):
        if not isinstance(rule, dict) or not isinstance(rule.get("pattern"), str):
            raise ConfigError(f"{path}: rules.mask[{i}] needs a pattern")
        unknown = set(rule) - {"name", "pattern", "type"}
        if unknown:
            raise ConfigError(f"{path}: rules.mask[{i}]: unknown key {sorted(unknown)[0]}")
        kind = rule.get("type", "SECRET")
        if kind not in TYPES:
            raise ConfigError(f"{path}: rules.mask[{i}]: unknown type {kind!r}")
        try:
            re.compile(rule["pattern"])
        except re.error as e:
            raise ConfigError(f"{path}: rules.mask[{i}] pattern: {e}") from e
        cfg.mask_rules.append({"name": str(rule.get("name") or f"rule {i + 1}"), "pattern": rule["pattern"], "type": kind})
    from .detect.rules import builtin_rules

    cfg.off_rules = [str(r) for r in rules.get("off", [])]
    for r in cfg.off_rules:
        if r not in builtin_rules():
            raise ConfigError(f"{path}: rules.off: no built-in rule {r!r} (`keepmum rules builtin` lists them)")
    cfg.allow_strings = [str(s) for s in allow.get("strings", [])]
    cfg.allow_patterns = [str(p) for p in allow.get("patterns", [])]
    for p in cfg.allow_patterns:
        try:
            re.compile(p)
        except re.error as e:
            raise ConfigError(f"{path}: allowlist pattern {p!r}: {e}") from e
    restore = raw.get("restore", {})
    if not isinstance(restore, dict):
        raise ConfigError(f"{path}: [restore] must be a table")
    from .detect import TYPES

    for tool, action in restore.items():
        if isinstance(action, list):
            bad = [t for t in action if t not in TYPES]
            if bad:
                raise ConfigError(f"{path}: restore.{tool}: unknown type {bad[0]!r}")
            if "SECRET" in action:
                raise ConfigError(f"{path}: restore.{tool}: SECRET cannot be listed: a tool that may not have every "
                                  "value never gets a secret. \"restore\" gives a tool every value, secrets included")
        elif action not in ("restore", "keep", "own"):
            raise ConfigError(f"{path}: restore.{tool} must be \"restore\", \"keep\", \"own\" or a list of types")
    cfg.restore = {**DEFAULT_POLICY, **restore}
    gateways = raw.get("gateways", {})
    if not isinstance(gateways, dict):
        raise ConfigError(f"{path}: [gateways] must be a table")
    for name, url in gateways.items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", name):
            raise ConfigError(f"{path}: gateways.{name}: a name is lowercase letters, digits and dashes")
        why = "must be a string" if not isinstance(url, str) else gateway_problem(url, cfg.port)
        if why:
            raise ConfigError(f"{path}: gateways.{name} {why}")
        cfg.gateways[name] = url.rstrip("/")
    destinations = raw.get("destinations", {})
    if not isinstance(destinations, dict):
        raise ConfigError(f"{path}: [destinations] must be a table")
    from .detect.rules import builtin_rules

    for rule, hosts in destinations.items():
        if rule not in builtin_rules():
            raise ConfigError(f"{path}: destinations.{rule}: no built-in rule has that name (`keepmum rules builtin`)")
        if not isinstance(hosts, list) or not all(isinstance(h, str) and _HOST.fullmatch(h) for h in hosts):
            raise ConfigError(f"{path}: destinations.{rule} must be a list of host names, like [\"gitlab.corp.example\"]")
        cfg.destinations[rule] = sorted({h.lower() for h in hosts})
    cfg.log_level = log.get("level", cfg.log_level)
    cfg.log_previews = log.get("previews", cfg.log_previews)
    cfg.log_context = log.get("context", cfg.log_context)
    if cfg.log_level not in LOG_LEVELS:
        raise ConfigError(f"{path}: log.level must be one of {', '.join(LOG_LEVELS)}")
    cfg.audit_keep_days = audit.get("keep_days", cfg.audit_keep_days)
    if not 1 <= cfg.audit_keep_days <= 3650:
        raise ConfigError(f"{path}: audit.keep_days must be 1-3650")
    cfg.guard_injection = guard.get("injection", cfg.guard_injection)
    cfg.guard_harm = guard.get("harm", cfg.guard_harm)
    cfg.guard_commands = guard.get("commands", cfg.guard_commands)
    for key, value in (("injection", cfg.guard_injection), ("harm", cfg.guard_harm)):
        if value not in ("off", "warn", "block", "local"):
            raise ConfigError(f"{path}: guard.{key} must be \"off\", \"warn\", \"block\" or \"local\"")
    cfg.guard_injection_threshold = guard.get("injection_threshold", cfg.guard_injection_threshold)
    if not 0.5 <= cfg.guard_injection_threshold <= 0.999:
        raise ConfigError(f"{path}: guard.injection_threshold must be 0.5-0.999 (0.8 is the default)")
    cfg.guard_harm_sensitivity = guard.get("harm_sensitivity", cfg.guard_harm_sensitivity)
    if cfg.guard_harm_sensitivity not in ("high", "medium", "low", "lowest"):
        raise ConfigError(f"{path}: guard.harm_sensitivity must be \"high\", \"medium\", \"low\" or \"lowest\"")
    if cfg.guard_commands not in ("off", "warn", "block"):
        raise ConfigError(f"{path}: guard.commands must be \"off\", \"warn\" or \"block\"")
    from .local import DEFAULT_URL, parse_servers, parse_skills

    # local_servers wins; before it there was one server, local_url and local_model (doctor says so).
    servers = guard.get("local_servers")
    if servers is None and ("local_url" in guard or "local_model" in guard):
        servers, where = [{"url": guard.get("local_url", DEFAULT_URL), "model": guard.get("local_model", "")}], "guard.local_url"
    else:
        where = "guard.local_servers"
    try:
        cfg.guard_local_servers = parse_servers(servers or [], where)
    except ValueError as e:
        raise ConfigError(f"{path}: {str(e).replace('guard.local_url[1].url', 'guard.local_url').replace('guard.local_url[1].', 'guard.local_')}")
    try:
        cfg.guard_local_skills = parse_skills(guard.get("local_skills", []), "guard.local_skills")
    except ValueError as e:
        raise ConfigError(f"{path}: {e}")
    from .handback import parse as parse_handback

    try:
        cfg.guard_local_handback, cfg.guard_local_handback_instructions = parse_handback(
            guard.get("local_handback", cfg.guard_local_handback),
            guard.get("local_handback_instructions", cfg.guard_local_handback_instructions))
    except ValueError as e:
        raise ConfigError(f"{path}: {e}")
    from .review import parse as parse_review

    try:
        cfg.guard_local_handback_review, cfg.guard_local_handback_review_wait, cfg.guard_local_handback_review_timeout = parse_review(
            guard.get("local_handback_review", cfg.guard_local_handback_review),
            guard.get("local_handback_review_wait", cfg.guard_local_handback_review_wait),
            guard.get("local_handback_review_timeout", cfg.guard_local_handback_review_timeout))
    except ValueError as e:
        raise ConfigError(f"{path}: {e}")
    # A harm check turned on before "small" existed, without a model set, keeps the default it had.
    cfg.guard_harm_model = guard.get("harm_model", "0.6b" if cfg.guard_harm != "off" else cfg.guard_harm_model)
    if cfg.guard_harm_model not in ("small", "0.6b", "4b"):
        raise ConfigError(f"{path}: guard.harm_model must be \"small\", \"0.6b\" or \"4b\"")
    return cfg


def toml_value(value) -> str:
    """One-line TOML for the values keepmum writes: bool, int, str, and lists of them or of tables.
    A JSON string is a valid TOML basic string."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {toml_value(v)}" for k, v in value.items()) + " }"
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    raise TypeError(f"cannot write {type(value).__name__} to config")


def set_value(section: str, key: str, value, path: Path | None = None) -> None:
    """Set one key in one section, keeping the rest of the file as written; None removes it. The
    result must load cleanly, or the file is left as it was. Values keepmum writes are one line
    each. A key other than a bare word is written quoted ("mcp__gmail__*")."""
    path = path or config_file()
    text = path.read_text() if path.exists() else ""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
        key = json.dumps(key)
    line = None if value is None else f"{key} = {toml_value(value)}"
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip() == f"[{section}]"), None)
    if start is None:
        if line is None:
            return
        lines += ([""] if lines and lines[-1].strip() else []) + [f"[{section}]", line]
    else:
        end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
        hit = next((i for i in range(start + 1, end) if re.match(rf"\s*{re.escape(key)}\s*=", lines[i])), None)
        if hit is None:
            if line is None:
                return
            lines.insert(start + 1, line)
        else:
            try:  # a multi-line value written by hand: replace all of its lines
                tomllib.loads(lines[hit])
                stop = hit + 1
            except tomllib.TOMLDecodeError:
                stop = next((k + 1 for k in range(hit + 1, end) if _parses("\n".join(lines[hit:k + 1]))), hit + 1)
            lines[hit:stop] = [line] if line is not None else []
    new = "\n".join(lines) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(new)
    try:
        load(tmp)
    except ConfigError:
        tmp.unlink()
        raise
    tmp.replace(path)


def _parses(text: str) -> bool:
    try:
        tomllib.loads(text)
        return True
    except tomllib.TOMLDecodeError:
        return False


def set_detect_flag(key: str, value: bool, path: Path | None = None) -> None:
    set_value("detect", key, value, path)


class LiveConfig:
    """The config file, re-read whenever it changes, so settings apply without a restart.

    Called per request, it returns whether observe mode is on (an invalid file means masking:
    the safe side). Each valid change is passed to `on_change`, which re-applies detection
    settings; an invalid file keeps the last valid detection settings.
    """

    def __init__(self, path: Path | None = None, initial: Config | None = None, on_change=None):
        self.path = path or config_file()
        self.config = initial or Config()
        self.observe = self.config.observe
        self.on_change = on_change
        self._stamp = self._mtime()

    def _mtime(self):
        try:
            st = self.path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def __call__(self) -> bool:
        stamp = self._mtime()
        if stamp != self._stamp:
            self._stamp = stamp
            try:
                self.config = load(self.path)
                self.observe = self.config.observe
                if self.on_change:
                    self.on_change(self.config)
            except ConfigError:
                self.observe = False
        return self.observe


def ObserveFlag(path: Path | None = None, initial: bool = False) -> LiveConfig:
    """`detect.observe`, re-read when the file changes (a LiveConfig without detection settings)."""
    return LiveConfig(path, Config(observe=initial))
