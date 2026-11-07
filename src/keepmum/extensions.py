"""What other apps added to your coding agents: plugins, plugin marketplaces, hooks and MCP servers.

Any program you run can write to ~/.claude or ~/.codex. In October 2026 the Postman desktop app,
on update, enabled its own plugin in Claude Code and Codex, added a hook that told every Codex
session to load its skill, marked that hook as trusted on the user's behalf, and connected a
remote MCP server, all without asking. keepmum cannot stop a program from writing these files,
but it reads them, so it can say when they change and take an entry back out.

`scan` lists what each agent has now. The first time keepmum looks, that list becomes what you
already have (~/.config/keepmum/extensions.json: names, commands and URLs, never a value); after
that, anything not in it is new until you keep it or remove it. Removing edits only that entry,
after a backup in ~/.config/keepmum/backups, and only if the file still reads the same otherwise.
Where an edit would not be safe (opencode's commented JSON, Gemini's extension folders, Claude
Code's own marketplace registry) keepmum says how to remove it with the agent itself.
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import json
import os
import shutil
import time
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Hook events that can add text to the model's context: at a session's start, or with each prompt.
CONTEXT_EVENTS = {"SessionStart", "UserPromptSubmit", "session_start", "user_prompt_submit"}


class ExtensionError(RuntimeError):
    pass


@dataclass
class Item:
    agent: str           # Claude Code, Codex, Cursor, Gemini CLI, Copilot CLI, opencode
    kind: str            # plugin, marketplace, hook, trusted hook, MCP server, extension, status line, skill,
                         # instructions (a global CLAUDE.md, AGENTS.md or GEMINI.md, by a hash of its text)
    name: str
    detail: str          # what it runs or where it connects; a plugin's hooks and MCP servers
    file: str
    # Runs at every session start or prompt, where its output reaches the model as instructions.
    adds_context: bool = False
    # How keepmum takes it out: (format, key path); None: with the agent itself (`how`).
    loc: list | None = None
    how: str = ""
    id: str = field(default="")

    def __post_init__(self):
        if not self.id:
            raw = "\0".join([self.agent, self.kind, self.name, self.detail])
            self.id = hashlib.sha256(raw.encode()).hexdigest()[:10]

    @property
    def removable(self) -> bool:
        return self.loc is not None

    def as_dict(self) -> dict:
        d = asdict(self)
        d.pop("loc")
        d["removable"] = self.removable
        return d


def state_file() -> Path:
    return Path.home() / ".config" / "keepmum" / "extensions.json"


def backup_dir() -> Path:
    return Path.home() / ".config" / "keepmum" / "backups"


# --- Reading each agent ----------------------------------------------------------------------

def _claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _claude_json() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base) / ".claude.json" if base else Path.home() / ".claude.json"


def _codex_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _read_json(path: Path, lenient: bool = False):
    if not path.is_file():
        return None
    text = path.read_text()
    if lenient:
        from .clients import _jsonc

        return _jsonc(text)
    return json.loads(text)


def _short(text: str, n: int = 140) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _server_detail(spec) -> str:
    if not isinstance(spec, dict):
        return ""
    if spec.get("url") or spec.get("httpUrl") or spec.get("serverUrl"):
        return str(spec.get("url") or spec.get("httpUrl") or spec.get("serverUrl"))
    cmd = spec.get("command")
    if isinstance(cmd, list):
        return _short(" ".join(map(str, cmd)))
    return _short(" ".join([str(cmd or "")] + [str(a) for a in spec.get("args", [])]))


def _hook_commands(hooks: dict):
    """(event, index of matcher group, index of hook, command) for Claude Code-style hooks."""
    for event, groups in (hooks or {}).items():
        for gi, group in enumerate(groups if isinstance(groups, list) else []):
            for hi, hook in enumerate((group or {}).get("hooks", []) if isinstance(group, dict) else []):
                if isinstance(hook, dict):
                    yield event, gi, hi, hook.get("command") or hook.get("prompt") or hook.get("type", "")


def _plugin_parts(install_path: str | None) -> str:
    """What a Claude Code plugin brings besides skills: its hook events and MCP servers."""
    if not install_path:
        return ""
    root = Path(install_path)
    parts = []
    with contextlib.suppress(OSError, ValueError):
        hooks = (_read_json(root / "hooks" / "hooks.json") or {}).get("hooks", {})
        if hooks:
            parts.append("hooks: " + ", ".join(sorted(hooks)))
    with contextlib.suppress(OSError, ValueError):
        servers = (_read_json(root / ".mcp.json") or {}).get("mcpServers", {})
        if servers:
            parts.append("MCP: " + ", ".join(sorted(servers)))
    return "; ".join(parts)


def _skills(out: list[Item], agent: str, root: Path) -> None:
    """Skills in a user-wide skills folder: each one's description is in every session's context."""
    with contextlib.suppress(OSError):
        for d in sorted(root.iterdir()):
            if (d / "SKILL.md").is_file():
                out.append(Item(agent, "skill", d.name, "", str(d), how=f"delete the folder {d}"))


def _instructions(out: list[Item], agent: str, path: Path) -> None:
    """A global instruction file every session reads. Its text is never kept: a hash, so an edit
    by anyone (an app appending a line, say) shows as a change."""
    with contextlib.suppress(OSError, UnicodeDecodeError):
        text = path.read_text()
        if text.strip():
            digest = hashlib.sha256(text.encode()).hexdigest()[:12]
            out.append(Item(agent, "instructions", path.name, f"{len(text.splitlines())} lines, sha256 {digest}",
                            str(path), adds_context=True, how=f"edit {path}"))


def _claude(out: list[Item], problems: list[str]) -> None:
    agent = "Claude Code"
    settings = _claude_dir() / "settings.json"
    try:
        s = _read_json(settings) or {}
    except (OSError, ValueError) as e:
        problems.append(f"{settings}: not readable ({type(e).__name__})")
        s = {}
    installed = {}
    with contextlib.suppress(OSError, ValueError, AttributeError):
        installed = (_read_json(_claude_dir() / "plugins" / "installed_plugins.json") or {}).get("plugins", {})
    for name, on in (s.get("enabledPlugins") or {}).items():
        if on is not True:
            continue
        entries = installed.get(name) or [{}]
        parts = _plugin_parts(entries[0].get("installPath") if isinstance(entries, list) and entries else None)
        out.append(Item(agent, "plugin", name, parts, str(settings), adds_context="SessionStart" in parts
                        or "UserPromptSubmit" in parts, loc=["json", "enabledPlugins", name]))
    for name, spec in (s.get("extraKnownMarketplaces") or {}).items():
        src = (spec or {}).get("source", {}) if isinstance(spec, dict) else {}
        where = (src.get("repo") or src.get("url") or src.get("path") or "") if isinstance(src, dict) else str(src)
        out.append(Item(agent, "marketplace", name, str(where), str(settings), loc=["json", "extraKnownMarketplaces", name]))
    for event, gi, hi, cmd in _hook_commands(s.get("hooks")):
        out.append(Item(agent, "hook", event, _short(cmd), str(settings), adds_context=event in CONTEXT_EVENTS,
                        loc=["claude-hook", event, gi, hi]))
    line = s.get("statusLine")
    if isinstance(line, dict) and line.get("command"):
        out.append(Item(agent, "status line", "statusLine", _short(line["command"]), str(settings), loc=["json", "statusLine"]))
    known = _claude_dir() / "plugins" / "known_marketplaces.json"
    with contextlib.suppress(OSError, ValueError, AttributeError):
        for name, spec in (_read_json(known) or {}).items():
            if name in (s.get("extraKnownMarketplaces") or {}) or name == "claude-plugins-official":
                continue
            src = (spec or {}).get("source", {}) if isinstance(spec, dict) else {}
            where = (src.get("repo") or src.get("url") or "") if isinstance(src, dict) else ""
            out.append(Item(agent, "marketplace", name, str(where), str(known),
                            how=f"in Claude Code: /plugin marketplace remove {name}"))
    _skills(out, agent, _claude_dir() / "skills")
    _instructions(out, agent, _claude_dir() / "CLAUDE.md")
    cj = _claude_json()
    try:
        servers = (_read_json(cj) or {}).get("mcpServers") or {}
    except (OSError, ValueError) as e:
        problems.append(f"{cj}: not readable ({type(e).__name__})")
        servers = {}
    for name, spec in servers.items():
        out.append(Item(agent, "MCP server", name, _server_detail(spec), str(cj), loc=["json", "mcpServers", name]))


def _codex(out: list[Item], problems: list[str]) -> None:
    agent = "Codex"
    _skills(out, agent, _codex_dir() / "skills")
    _instructions(out, agent, _codex_dir() / "AGENTS.md")
    path = _codex_dir() / "config.toml"
    if not path.is_file():
        return
    try:
        cfg = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        problems.append(f"{path}: not readable ({type(e).__name__})")
        return
    for name, spec in (cfg.get("plugins") or {}).items():
        if isinstance(spec, dict) and spec.get("enabled", True) is not False:
            out.append(Item(agent, "plugin", name, "", str(path), loc=["toml", "plugins", name]))
    for name, spec in (cfg.get("marketplaces") or {}).items():
        where = spec.get("source", "") if isinstance(spec, dict) else ""
        out.append(Item(agent, "marketplace", name, str(where), str(path), loc=["toml", "marketplaces", name]))
    for name, spec in (cfg.get("mcp_servers") or {}).items():
        out.append(Item(agent, "MCP server", name, _server_detail(spec), str(path), loc=["toml", "mcp_servers", name]))
    hooks = cfg.get("hooks") or {}
    for hook_id in (hooks.get("state") or {}):
        # "postman@postman:hooks/codex-hooks.json:session_start:0:0": its plugin, file and event.
        bits = hook_id.split(":")
        event = bits[2] if len(bits) > 2 else ""
        out.append(Item(agent, "trusted hook", bits[0], hook_id, str(path), adds_context=event in CONTEXT_EVENTS,
                        loc=["toml", "hooks", "state", hook_id]))
    for event, spec in hooks.items():
        if event != "state":
            out.append(Item(agent, "hook", event, _short(json.dumps(spec)), str(path), adds_context=event in CONTEXT_EVENTS,
                            how=f"edit [hooks.{event}] in {path}"))
    if cfg.get("notify"):
        out.append(Item(agent, "hook", "notify", _short(" ".join(map(str, cfg["notify"]))
                                                        if isinstance(cfg["notify"], list) else cfg["notify"]),
                        str(path), how=f"edit notify in {path}"))


def _mcp_json(out: list[Item], problems: list[str], agent: str, path: Path, key: str = "mcpServers") -> None:
    try:
        data = _read_json(path) or {}
    except (OSError, ValueError) as e:
        problems.append(f"{path}: not readable ({type(e).__name__})")
        return
    for name, spec in (data.get(key) or {}).items():
        out.append(Item(agent, "MCP server", name, _server_detail(spec), str(path), loc=["json", key, name]))


def _others(out: list[Item], problems: list[str]) -> None:
    home = Path.home()
    _mcp_json(out, problems, "Cursor", home / ".cursor" / "mcp.json")
    with contextlib.suppress(OSError, ValueError, AttributeError):
        hooks = (_read_json(home / ".cursor" / "hooks.json") or {}).get("hooks") or {}
        for event, entries in hooks.items():
            for entry in entries if isinstance(entries, list) else []:
                cmd = entry.get("command", "") if isinstance(entry, dict) else entry
                out.append(Item("Cursor", "hook", event, _short(cmd), str(home / ".cursor" / "hooks.json"),
                                adds_context=event in ("sessionStart", "beforeSubmitPrompt"),
                                how="edit ~/.cursor/hooks.json"))
    gemini = home / ".gemini"
    _mcp_json(out, problems, "Gemini CLI", gemini / "settings.json")
    _instructions(out, "Gemini CLI", gemini / "GEMINI.md")
    with contextlib.suppress(OSError):
        for d in sorted((gemini / "extensions").iterdir()):
            if d.is_dir() and (d / "gemini-extension.json").is_file():
                out.append(Item("Gemini CLI", "extension", d.name, "", str(d),
                                how=f"gemini extensions uninstall {d.name}"))
    _mcp_json(out, problems, "Copilot CLI", home / ".copilot" / "mcp-config.json")
    oc = home / ".config" / "opencode"
    for name in ("opencode.json", "opencode.jsonc"):
        path = oc / name
        try:
            data = _read_json(path, lenient=True) or {}
        except (OSError, ValueError) as e:
            problems.append(f"{path}: not readable ({type(e).__name__})")
            continue
        for server, spec in (data.get("mcp") or {}).items():
            out.append(Item("opencode", "MCP server", server, _server_detail(spec), str(path), how=f"edit mcp in {path}"))
        for plugin in data.get("plugin") or []:
            out.append(Item("opencode", "plugin", str(plugin), "", str(path), how=f"edit plugin in {path}"))


def scan() -> tuple[list[Item], list[str]]:
    """Everything each agent has now, and the files keepmum could not read."""
    items: list[Item] = []
    problems: list[str] = []
    for read in (_claude, _codex, _others):
        try:
            read(items, problems)
        except Exception as e:  # one agent's odd file never hides the others
            problems.append(f"{read.__name__.strip('_')}: {type(e).__name__}")
    return items, problems


# --- What you already have -------------------------------------------------------------------

@contextlib.contextmanager
def _locked():
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path.with_name(path.name + ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _load() -> dict | None:
    try:
        data = json.loads(state_file().read_text())
        return data if isinstance(data, dict) and isinstance(data.get("known"), dict) else None
    except (OSError, ValueError):
        return None


def _save(data: dict) -> None:
    path = state_file()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _entry(item: Item, since: float) -> dict:
    return {"agent": item.agent, "kind": item.kind, "name": item.name, "since": round(since)}


def ensure_baseline() -> bool:
    """The first look: what the agents have now is what you already have. True if it was taken now."""
    with _locked():
        if _load() is not None:
            return False
        items, _ = scan()
        now = time.time()
        _save({"version": 1, "since": round(now), "known": {i.id: _entry(i, now) for i in items}})
        return True


def check() -> dict:
    """Every item now, each marked new or not, and what went away since you last reviewed.
    Read-only: before the first look there is nothing to compare with, so nothing is new."""
    items, problems = scan()
    state = _load()
    known = (state or {}).get("known", {})
    current = {i.id for i in items}
    rows = []
    for i in items:
        d = i.as_dict()
        d["new"] = state is not None and i.id not in known
        if i.id in known:
            d["since"] = known[i.id].get("since")
        rows.append(d)
    gone = [dict(v, id=k) for k, v in known.items() if k not in current]
    return {"items": rows, "new": sum(r["new"] for r in rows), "gone": gone, "problems": problems,
            "watching": state is not None, "watching_since": (state or {}).get("since")}


def keep(ids: list[str] | None = None) -> int:
    """Mark items as yours (all new ones when `ids` is empty), and forget the ones that went away."""
    with _locked():
        state = _load() or {"version": 1, "since": round(time.time()), "known": {}}
        items, _ = scan()
        now = time.time()
        current = {i.id: i for i in items}
        added = 0
        for i in items:
            if i.id not in state["known"] and (not ids or i.id in ids):
                state["known"][i.id] = _entry(i, now)
                added += 1
        if not ids:
            state["known"] = {k: v for k, v in state["known"].items() if k in current}
        _save(state)
        return added


# --- Taking one out --------------------------------------------------------------------------

def _backup(path: Path) -> Path:
    d = backup_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = d / f"{path.name}.{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, dest)
    os.chmod(dest, 0o600)
    return dest


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".keepmum-tmp")
    tmp.write_text(text)
    os.chmod(tmp, path.stat().st_mode & 0o777)
    os.replace(tmp, path)


def _remove_json(path: Path, loc: list) -> str:
    data = json.loads(path.read_text())
    if loc[0] == "claude-hook":
        event, gi, hi = loc[1], loc[2], loc[3]
        groups = data["hooks"][event]
        del groups[gi]["hooks"][hi]
        if not groups[gi]["hooks"]:
            del groups[gi]
        if not groups:
            del data["hooks"][event]
    elif loc[1] == "enabledPlugins":
        data["enabledPlugins"][loc[2]] = False  # disabled, as Claude Code's own /plugin disable leaves it
    else:
        node = data
        for key in loc[1:-1]:
            node = node[key]
        del node[loc[-1]]
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _header_path(line: str) -> list | None:
    """The key path of a TOML table header line (`[hooks.state."a:b"]`), or None."""
    s = line.strip()
    if not s.startswith("["):
        return None
    try:
        node = tomllib.loads(s + "\n")
    except tomllib.TOMLDecodeError:
        return None
    path = []
    while isinstance(node, dict) and len(node) == 1:
        (key, node), = node.items()
        path.append(key)
        if isinstance(node, list):
            node = node[-1] if node else {}
    return path


def _pruned(node):
    if isinstance(node, dict):
        out = {k: _pruned(v) for k, v in node.items()}
        return {k: v for k, v in out.items() if v != {}}
    return node


def _remove_toml(path: Path, keys: list) -> str:
    text = path.read_text()
    before = tomllib.loads(text)
    out, skipping = [], False
    for line in text.splitlines(keepends=True):
        header = _header_path(line)
        if header is not None:
            skipping = header[: len(keys)] == keys
        if not skipping:
            out.append(line)
    new = "".join(out)
    after = tomllib.loads(new)
    expected = copy.deepcopy(before)
    node = expected
    for key in keys[:-1]:
        node = node[key]
    del node[keys[-1]]
    if _pruned(after) != _pruned(expected):
        raise ExtensionError(f"{path}: this entry is not a table of its own (an inline or dotted key); "
                             f"remove {'.'.join(keys)} by hand")
    return new


def remove(item_id: str) -> tuple[Item, Path]:
    """Take one item out of its agent's settings. Returns it and the backup made first."""
    items, _ = scan()
    item = next((i for i in items if i.id == item_id), None)
    if item is None:
        raise ExtensionError(f"no item {item_id} (`keepmum extensions` lists them)")
    if not item.removable:
        raise ExtensionError(f"keepmum does not edit {item.file} for this; {item.how}")
    path = Path(item.file)
    try:
        new = _remove_toml(path, item.loc[1:]) if item.loc[0] == "toml" else _remove_json(path, item.loc)
    except (OSError, ValueError, KeyError, IndexError, tomllib.TOMLDecodeError) as e:
        raise ExtensionError(f"{path}: could not take it out ({type(e).__name__}); nothing was changed") from e
    saved = _backup(path)
    _write(path, new)
    with _locked():
        state = _load()
        if state is not None:
            state["known"].pop(item.id, None)
            _save(state)
    return item, saved
