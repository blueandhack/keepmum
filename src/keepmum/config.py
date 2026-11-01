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

    [restore]              # per tool: "restore" or "keep"; "mcp__*" style prefixes allowed
    default = "restore"
    WebFetch = "keep"

    [audit]
    keep_days = 90         # how long the audit log keeps entries (`keepmum audit`)

    [log]
    level = "info"
    previews = true        # the Logs page shows the first and last character of masked values (in memory only)
    context = true         # and the text around each, every masked value in it shown as its type (in memory only)
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
}
LOG_LEVELS = ("debug", "info", "warning", "error")


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
    log_level: str = "info"
    log_previews: bool = True
    log_context: bool = True
    audit_keep_days: int = 90
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
        if section == "restore":
            continue
        if section not in _SCHEMA or not isinstance(values, dict):
            raise ConfigError(f"{path}: unknown section [{section}]")
        for key, value in values.items():
            want = _SCHEMA[section].get(key)
            if want is None:
                raise ConfigError(f"{path}: unknown key {section}.{key}")
            if not isinstance(value, want) or (want is int and isinstance(value, bool)):
                raise ConfigError(f"{path}: {section}.{key} must be {want.__name__}")
    server, detect, allow, log, rules, audit = (raw.get(k, {}) for k in ("server", "detect", "allowlist", "log", "rules", "audit"))
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
    for tool, action in restore.items():
        if action not in ("restore", "keep"):
            raise ConfigError(f"{path}: restore.{tool} must be \"restore\" or \"keep\"")
    cfg.restore = {**DEFAULT_POLICY, **restore}
    cfg.log_level = log.get("level", cfg.log_level)
    cfg.log_previews = log.get("previews", cfg.log_previews)
    cfg.log_context = log.get("context", cfg.log_context)
    if cfg.log_level not in LOG_LEVELS:
        raise ConfigError(f"{path}: log.level must be one of {', '.join(LOG_LEVELS)}")
    cfg.audit_keep_days = audit.get("keep_days", cfg.audit_keep_days)
    if not 1 <= cfg.audit_keep_days <= 3650:
        raise ConfigError(f"{path}: audit.keep_days must be 1-3650")
    return cfg


def toml_value(value) -> str:
    """One-line TOML for the values keepmum writes: bool, int, str, and lists of them or of tables.
    A JSON string is a valid TOML basic string."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {toml_value(v)}" for k, v in value.items()) + " }"
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    raise TypeError(f"cannot write {type(value).__name__} to config")


def set_value(section: str, key: str, value, path: Path | None = None) -> None:
    """Set one key in one section, keeping the rest of the file as written. The result must
    load cleanly, or the file is left as it was. Values keepmum writes are one line each."""
    path = path or config_file()
    text = path.read_text() if path.exists() else ""
    line = f"{key} = {toml_value(value)}"
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip() == f"[{section}]"), None)
    if start is None:
        lines += ([""] if lines and lines[-1].strip() else []) + [f"[{section}]", line]
    else:
        end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
        hit = next((i for i in range(start + 1, end) if re.match(rf"\s*{re.escape(key)}\s*=", lines[i])), None)
        if hit is None:
            lines.insert(start + 1, line)
        else:
            try:  # a multi-line value written by hand: replace all of its lines
                tomllib.loads(lines[hit])
                stop = hit + 1
            except tomllib.TOMLDecodeError:
                stop = next((k + 1 for k in range(hit + 1, end) if _parses("\n".join(lines[hit:k + 1]))), hit + 1)
            lines[hit:stop] = [line]
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
