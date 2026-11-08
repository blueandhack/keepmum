"""Tools that reach the network: which real values each may receive, and the servers keepmum has seen.

The restore policy (config `[restore]`) gives each tool one of:

- "restore": every value (the default, for tools that run on this Mac: file edits, shell);
- "keep": placeholders only;
- "own": the values that server itself returned earlier in the conversation (the default for
  MCP servers: Gmail may open the thread it found, Linear never sees Gmail's addresses);
- a list of types, e.g. ["EMAIL", "PERSON"]: its own values, plus values of those types.

Whatever the setting, a tool that is not "restore" never gets a SECRET, never gets more than
BULK real values in one call (a search needs one or two; dumping a contact list needs dozens),
and gets none while its server's tools differ from the ones keepmum saw first or you last
trusted (a server that rewrites its tool descriptions, or another server taking its name).

The servers seen, a fingerprint of each tool's definition, and how often a call kept
placeholders a setting could have let through live in ~/.config/keepmum/tools.json (0600):
tool names and hashes, never a value.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import threading
import time
import re
from collections import Counter
from pathlib import Path

# The types "Also contact details" lets through.
CONTACTS = ("PERSON", "EMAIL", "PHONE", "ADDRESS")
# The most real values one call to a network tool may receive.
BULK = 5
# How long kept-call counts are kept, in days (Settings shows the last 7).
KEEP_DAYS = 30


def tools_file() -> Path:
    return Path.home() / ".config" / "keepmum" / "tools.json"


def server_of(name: str) -> str:
    """The server a tool belongs to: `mcp__gmail__search` -> `mcp__gmail`. Any other name is its
    own server (WebFetch, web_search)."""
    if name.startswith("mcp__"):
        server = name[5:].split("__", 1)[0]
        if server:
            return f"mcp__{server}"
    return name


# Antigravity (agy 1.5.3) reaches an MCP server's lazily loaded tools and resources through tools
# of its own that name the server in their arguments: call_mcp_tool {ServerName, ToolName,
# Arguments}, list_resources and read_resource {ServerName, Uri}.
SERVER_IN_ARGS = frozenset({"call_mcp_tool", "list_resources", "read_resource"})
_SERVER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def call_name(name: str, args) -> str:
    """The name a tool call is decided by, and its result credited to: its own, or for a call that
    names its MCP server in its arguments, that server's tool, `call_mcp_tool {ServerName: notes,
    ToolName: send_note}` -> `mcp__notes__send_note`. A server name that would not read back as
    one (`__` in it) leaves the call as it is: decided by its own name."""
    if name in SERVER_IN_ARGS and isinstance(args, dict):
        server = args.get("ServerName")
        tool = args.get("ToolName") if name == "call_mcp_tool" else name
        if isinstance(server, str) and _SERVER_NAME.fullmatch(server) and "__" not in server \
                and isinstance(tool, str) and tool:
            return f"mcp__{server}__{tool}"
    return name


def rule_key(server: str) -> str:
    """The `[restore]` key that covers every tool of a server."""
    return f"{server}__*" if server.startswith("mcp__") else server


def label_of(server: str) -> str:
    """A name to show: `mcp__claude_ai_Gmail` -> "Gmail", `mcp__linear` -> "linear"."""
    name = server.removeprefix("mcp__").removeprefix("claude_ai_")
    return name.replace("_", " ").strip() or server


def pinned(server: str) -> bool:
    """Whether a server's tool definitions are checked against the ones trusted. MCP servers
    come from anywhere; WebFetch and web search are the agent's own and change with it."""
    return server.startswith("mcp_")


def setting_of(action) -> str:
    """The app's name for a policy value."""
    if isinstance(action, (list, tuple)):
        return "contacts" if set(action) == set(CONTACTS) else "custom"
    return {"keep": "placeholders"}.get(action, action)


def definitions(body: dict, api: str | None) -> list[tuple[str, str, object]]:
    """(name, description, input schema) of each tool a request offers the model."""
    out = []
    if api in ("gemini", "gemini-plain"):
        for key in ("request", "generateContentRequest", "generate_content_request"):
            if isinstance(body.get(key), dict):
                out += definitions(body[key], api)
        for group in body.get("tools") or []:
            if not isinstance(group, dict):
                continue
            for d in group.get("functionDeclarations") or group.get("function_declarations") or []:
                if isinstance(d, dict) and isinstance(d.get("name"), str):
                    out.append((d["name"], d.get("description") or "",
                                d.get("parameters", d.get("parametersJsonSchema"))))
        return out
    for t in body.get("tools") or []:
        if not isinstance(t, dict):
            continue
        if isinstance(t.get("function"), dict):  # Chat Completions shape
            t = t["function"]
        if api == "responses" and t.get("type") == "mcp" and t.get("server_label"):
            out.append((f"mcp__{t['server_label']}__*", t.get("server_description") or "", t.get("server_url")))
        elif isinstance(t.get("name"), str):
            out.append((t["name"], t.get("description") or "",
                        t.get("input_schema", t.get("parameters", t.get("format")))))
    return out


def _hash(description: str, schema) -> str:
    raw = json.dumps([description, schema], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _today(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now or time.time()))


class ToolRegistry:
    """The servers seen in agents' tool lists, shared by every keepmum process through tools.json.

    Per server: its label, the agents that offered it, whether it is new (not reviewed yet), each
    tool's fingerprint as last seen and as trusted, and per day the calls that kept placeholders
    of a type the setting could let through. A server's tools are trusted when first seen; a tool
    whose definition later changes pauses the server until `trust`. A tool added later is trusted
    when it first appears: Claude Code loads deferred tools one at a time.
    """

    def __init__(self, path: Path | None = None):
        self.path = path or tools_file()
        self._lock = threading.Lock()
        self._stamp = None
        self._state: dict = {"servers": {}}
        self._reload()

    # --- File -------------------------------------------------------------------------

    def _mtime(self):
        try:
            st = self.path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _reload(self) -> None:
        stamp = self._mtime()
        if stamp == self._stamp:
            return
        self._stamp = stamp
        try:
            state = json.loads(self.path.read_text())
            self._state = state if isinstance(state.get("servers"), dict) else {"servers": {}}
        except (OSError, ValueError, AttributeError):
            self._state = {"servers": {}}

    @contextlib.contextmanager
    def _update(self):
        """Read, change and write tools.json under a file lock: the service, `keepmum claude` and
        the CLI share it. Yields the servers table; written only if it changed."""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock = os.open(self.path.with_suffix(".lock"), os.O_WRONLY | os.O_CREAT, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX)
                self._stamp = None
                self._reload()
                before = json.dumps(self._state, sort_keys=True)
                yield self._state["servers"]
                if json.dumps(self._state, sort_keys=True) != before:
                    tmp = self.path.with_suffix(".tmp")
                    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "w") as f:
                        json.dump(self._state, f, indent=1, sort_keys=True)
                    tmp.replace(self.path)
                    self._stamp = self._mtime()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
                os.close(lock)

    # --- What the proxy tells it --------------------------------------------------------

    def observe(self, defs: list[tuple[str, str, object]], client: str | None, network) -> None:
        """Record the network tools a request offers. `network(name)` says whether a tool is one
        (its restore setting is not "restore"). Writes only when something is new or changed."""
        seen = {}
        for name, description, schema in defs:
            if network(name):
                seen[name] = _hash(description, schema)
        if not seen:
            return
        with self._lock:
            self._reload()
            servers = self._state["servers"]
            fresh = any(
                (s := servers.get(server_of(n))) is None or (t := s["tools"].get(n)) is None or t["hash"] != h
                or (client and client not in s.get("agents", []))
                for n, h in seen.items())
        if not fresh:
            return
        try:
            with self._update() as servers:
                now = round(time.time())
                for name, h in seen.items():
                    key = server_of(name)
                    s = servers.setdefault(key, {"label": label_of(key), "agents": [], "first_seen": now,
                                                 "new": True, "tools": {}, "kept": {}})
                    if client and client not in s["agents"]:
                        s["agents"] = sorted([*s["agents"], client])
                    t = s["tools"].setdefault(name, {"hash": h, "trusted": h})
                    t["hash"] = h
        except OSError:
            pass  # a read-only home: decisions still use what was read

    def paused(self, server: str) -> bool:
        """True while one of the server's tools differs from the definition trusted."""
        if not pinned(server):
            return False
        with self._lock:
            self._reload()
            s = self._state["servers"].get(server)
            return bool(s) and any(t["hash"] != t["trusted"] for t in s["tools"].values())

    def note_kept(self, server: str, kinds) -> None:
        """A call kept placeholders of these types, which a setting could let through."""
        kinds = sorted(set(kinds))
        if not kinds:
            return
        try:
            with self._update() as servers:
                s = servers.get(server)
                if s is None:
                    s = servers[server] = {"label": label_of(server), "agents": [], "first_seen": round(time.time()),
                                           "new": True, "tools": {}, "kept": {}}
                day = s["kept"].setdefault(_today(), {})
                for k in kinds:
                    day[k] = day.get(k, 0) + 1
                cutoff = _today(time.time() - KEEP_DAYS * 86400)
                s["kept"] = {d: v for d, v in s["kept"].items() if d >= cutoff}
        except OSError:
            pass

    # --- What you tell it -----------------------------------------------------------------

    def find(self, name: str) -> str | None:
        """A server by key (`mcp__gmail`), rule key (`mcp__gmail__*`) or label (`gmail`)."""
        self._reload()
        servers = self._state["servers"]
        name = name.strip()
        if name in servers:
            return name
        for key in servers:
            if rule_key(key) == name or f"mcp__{name}" == key:
                return key
        hits = [k for k, s in servers.items() if s.get("label", "").lower() == name.lower()]
        return hits[0] if len(hits) == 1 else None

    def trust(self, server: str) -> list[str]:
        """Trust the server's tools as they are now. Returns the tools that had changed."""
        with self._update() as servers:
            s = servers.get(server)
            if s is None:
                return []
            changed = sorted(n for n, t in s["tools"].items() if t["hash"] != t["trusted"])
            for t in s["tools"].values():
                t["trusted"] = t["hash"]
            s["new"] = False
            return changed

    def reviewed(self, server: str | None = None) -> None:
        """Mark a server (or every server) as seen, so the menu bar stops pointing at it."""
        with self._update() as servers:
            for key, s in servers.items():
                if server is None or key == server:
                    s["new"] = False

    def snapshot(self, policy: dict, now: float | None = None) -> list[dict]:
        """Every server, for the app and `keepmum tools`: its setting, whether it is new or
        paused (and which tools changed), and the last 7 days' kept calls by type."""
        from .restore import DEFAULT_POLICY, rule_for

        with self._lock:
            self._reload()
            servers = json.loads(json.dumps(self._state["servers"]))
        week = _today((now or time.time()) - 6 * 86400)
        out = []
        for key, s in sorted(servers.items(), key=lambda kv: kv[1].get("label", kv[0]).lower()):
            pattern, action = rule_for(next(iter(s["tools"]), rule_key(key)), policy)
            kept = Counter()
            for day, kinds in s.get("kept", {}).items():
                if day >= week:
                    kept.update(kinds)
            changed = sorted(n for n, t in s["tools"].items() if t["hash"] != t["trusted"]) if pinned(key) else []
            out.append({"server": key, "label": s.get("label", label_of(key)), "agents": s.get("agents", []),
                        "tools": sorted(s["tools"]), "new": bool(s.get("new")), "paused": bool(changed), "changed": changed,
                        "setting": setting_of(action), "types": list(action) if isinstance(action, (list, tuple)) else [],
                        "rule": pattern, "own_rule": pattern == rule_key(key) or pattern in s["tools"],
                        "default": DEFAULT_POLICY.get(pattern) == action,
                        "kept_week": dict(kept), "public": not key.startswith("mcp_")})
        return out
