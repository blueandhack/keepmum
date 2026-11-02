"""Put real values back where placeholders appear: whole responses and SSE streams."""

from __future__ import annotations

import json
from collections import Counter

from .detect import TYPES
from .vault import PLACEHOLDER, Vault

# Per tool: "restore" puts real values into the tool's input before it runs locally; "keep"
# leaves placeholders, so a prompt-injected request cannot send a real value over the network;
# "own" lets an MCP server have back the values it returned itself, and a list of types adds
# those (tools.py has the rules every network tool is held to).
DEFAULT_POLICY = {
    "default": "restore",
    "WebFetch": "keep",
    "WebSearch": "keep",
    "webfetch": "keep",
    "websearch": "keep",
    "web_fetch": "keep",
    "web_search": "keep",
    "codesearch": "keep",
    "mcp__*": "own",
    "mcp_*": "own",
}
ACTIONS = ("restore", "keep", "own")


class ToolPolicy(dict):
    """The restore policy, with the registry of servers seen (tools.ToolRegistry) when there is one."""

    def __init__(self, policy: dict, registry=None):
        super().__init__(policy)
        self.registry = registry


def rule_for(name: str, policy: dict) -> tuple[str, object]:
    """(the key that decides, its action) for a tool: its own name, else the longest matching
    prefix pattern ("mcp__gmail__*" before "mcp__*"), else the default."""
    if name in policy:
        return name, policy[name]
    best = None
    for pattern in policy:
        if pattern.endswith("*") and name.startswith(pattern[:-1]) and (best is None or len(pattern) > len(best)):
            best = pattern
    if best is not None:
        return best, policy[best]
    return "default", policy.get("default", "restore")


def take_tools(counts: Counter) -> list[dict]:
    """Remove the tool decisions put in counts: [{tool, action, values, reason, types}], one per
    tool, action and reason. action is "restore" (real values went in) or "keep"; reason says why:
    local (the tool runs on this Mac), own (the server returned it), allowed (its type is let
    through), and for kept ones placeholders (the setting), secret, paused (the server changed),
    not_allowed, too_many."""
    keys = [k for k in counts if isinstance(k, tuple) and k[0] == TOOL]
    grouped: dict[tuple, dict] = {}
    for k in sorted(keys, key=lambda k: tuple(str(x) for x in k)):
        _, name, action, reason, kind = k
        n = counts.pop(k)
        g = grouped.setdefault((name, action, reason), {"tool": name, "action": action, "values": 0})
        g["values"] += n
        if reason:
            g["reason"] = reason
        if kind:
            g.setdefault("types", {})[kind] = g.get("types", {}).get(kind, 0) + n
    return list(grouped.values())


TOOL = "tool"

# Key in the counts Counter for placeholders the vault does not know: the model reused one this
# process never made (another keepmum with another salt, or one restarted since). They stay as
# they are, and the proxy reports them.
UNRESOLVED = "unresolved"


def resolve(m, vault: Vault) -> tuple[str, str | None, str]:
    """(the placeholder the vault knows, its value or None, text after it) for a PLACEHOLDER match.
    A 12-digit match may be an 8-digit placeholder followed by hex text."""
    ph = m.group(0)
    value = vault.value(ph)
    if value is None and len(m.group(2)) == 12 and vault.value(ph[:-4]) is not None:
        return ph[:-4], vault.value(ph[:-4]), ph[-4:]
    return ph, value, ""


def restore_text(text: str, vault: Vault, counts: Counter | None = None, only: set[str] | None = None) -> str:
    """Replace every placeholder the vault knows (with `only`, just those placeholders).
    Unknown placeholders stay as they are."""

    def swap(m):
        ph, value, suffix = resolve(m, vault)
        if value is None:
            if counts is not None:
                counts[UNRESOLVED] += 1
            return m.group(0)
        if only is not None and ph not in only:
            return m.group(0)
        if counts is not None:
            counts[ph.split("_")[1]] += 1
        return value + suffix

    return PLACEHOLDER.sub(swap, text)


def _values(value, vault: Vault, counts: Counter | None, only: set[str] | None = None):
    if isinstance(value, str):
        return restore_text(value, vault, counts, only)
    if isinstance(value, dict):
        return {(restore_text(k, vault, counts, only) if isinstance(k, str) else k): _values(v, vault, counts, only)
                for k, v in value.items()}
    if isinstance(value, list):
        return [_values(v, vault, counts, only) for v in value]
    return value


def tool_values(name: str, value, vault: Vault, policy: dict) -> tuple[set[str] | None, list[tuple[str, str, str, int]]]:
    """Which placeholders in a tool call's input get their real values: (None for all, or the set
    allowed; and the decisions as (action, reason, type, occurrences)) for the audit log."""
    action = rule_for(name, policy)[1]
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    found = Counter()
    for m in PLACEHOLDER.finditer(text):
        ph, real, _ = resolve(m, vault)
        if real is not None:
            found[ph] += 1
    if action == "restore":
        return None, [("restore", "local", ph.split("_")[1], n) for ph, n in found.items()]
    from .tools import BULK, server_of

    server = server_of(name)
    registry = getattr(policy, "registry", None)
    types = set(action) if isinstance(action, (list, tuple)) else set()
    paused = action != "keep" and registry is not None and registry.paused(server)
    allowed, decisions = set(), []
    for ph, n in found.items():
        kind = ph.split("_")[1]
        if action == "keep":
            reason = "placeholders"
        elif kind == "SECRET":
            reason = "secret"
        elif paused:
            reason = "paused"
        elif server in vault.origins_of(vault.value(ph)):
            reason = "own"
        elif kind in types:
            reason = "allowed"
        else:
            reason = "not_allowed"
        if reason in ("own", "allowed"):
            allowed.add(ph)
        decisions.append(("restore" if reason in ("own", "allowed") else "keep", reason, kind, n))
    if len(allowed) > BULK:
        allowed = set()
        decisions = [("keep", "too_many" if a == "restore" else r, k, n) for a, r, k, n in decisions]
    if registry is not None:
        registry.note_kept(server, [k for a, r, k, n in decisions if r == "not_allowed"])
    return allowed, decisions


def restore_tool(name: str, value, vault: Vault, policy: dict, counts: Counter | None, parse: bool = False):
    """A tool call's input with the real values its tool may receive, and its decisions in counts.
    `parse`: value is a JSON document in a string (Responses API arguments), restored as JSON."""
    tree = value
    if parse and isinstance(value, str):
        try:
            tree = json.loads(value)
        except ValueError:
            parse = False
    only, decisions = tool_values(name, tree, vault, policy)
    if counts is not None:
        for action, reason, kind, n in decisions:
            counts[(TOOL, name, action, reason, kind)] += n
    if only is not None and not only:
        return value  # nothing to put back: the input goes out exactly as the model wrote it
    if parse:
        return json.dumps(_values(tree, vault, counts, only), ensure_ascii=False)
    return _values(value, vault, counts, only)


def restore_response(body: dict, vault: Vault, policy: dict[str, str] = DEFAULT_POLICY, counts: Counter | None = None) -> dict:
    """A copy of a non-streaming /v1/messages response with placeholders restored.

    Text is restored. Tool inputs follow the per-tool policy. Thinking blocks are signed and
    must be resent unchanged, so they keep their placeholders.
    """
    content = body.get("content")
    if not isinstance(content, list):
        return body
    out = []
    for block in content:
        if not isinstance(block, dict):
            out.append(block)
        elif block.get("type") == "text":
            out.append({**block, "text": restore_text(block.get("text", ""), vault, counts)})
        elif block.get("type") == "tool_use":
            out.append({**block, "input": restore_tool(block.get("name", ""), block.get("input"), vault, policy, counts)})
        else:
            out.append(block)
    return {**body, "content": out}


# --- Streaming (server-sent events) -------------------------------------------------

_PREFIX = "KEEPMUM_"
_TYPE_TAILS = tuple(f"{t}_" for t in TYPES)
_HEX = frozenset("0123456789abcdef")


def _could_be_placeholder_start(s: str) -> bool:
    """True when more text could still turn s (a suffix of the stream) into a placeholder."""
    if len(s) <= len(_PREFIX):
        return _PREFIX.startswith(s)
    if not s.startswith(_PREFIX):
        return False
    rest = s[len(_PREFIX):]
    for tail in _TYPE_TAILS:
        if len(rest) <= len(tail):
            if tail.startswith(rest):
                return True
        elif rest.startswith(tail):
            digits = rest[len(tail):]
            # 8 digits is complete, but 4 more would make it a 12-digit placeholder.
            return len(digits) < 12 and all(c in _HEX for c in digits)
    return False


def _hold_from(text: str) -> int:
    """Index from which text must be held back, or len(text) when it can all go out now."""
    longest = len(_PREFIX) + max(len(t) for t in _TYPE_TAILS) + 12
    for i in range(max(0, len(text) - longest), len(text)):
        if text[i] == _PREFIX[0] and _could_be_placeholder_start(text[i:]):
            return i
    return len(text)


class StreamRestorer:
    """Restores placeholders in a /v1/messages SSE stream, chunk by chunk.

    - text_delta: a tail buffer holds back anything that could still become a placeholder.
    - input_json_delta (tool_use): the whole input is held until content_block_stop, then
      parsed, restored per the tool policy, and sent as one delta, so values are JSON-escaped.
    - Everything else (thinking and signatures, server tools, ping, message_*, error) passes
      through byte for byte, in order.
    """

    def __init__(self, vault: Vault, policy: dict[str, str] = DEFAULT_POLICY, counts: Counter | None = None):
        self.vault = vault
        self.policy = policy
        self.counts = counts if counts is not None else Counter()
        self._buf = b""
        self._blocks: dict[int, dict] = {}  # index -> {"type", "name", "pending", "json"}

    def feed(self, chunk: bytes) -> bytes:
        self._buf += chunk
        out = []
        while True:
            end, sep = self._event_end()
            if end < 0:
                break
            raw, self._buf = self._buf[: end + sep], self._buf[end + sep :]
            out.append(self._event(raw))
        return b"".join(out)

    def close(self) -> bytes:
        """Whatever is left when the upstream closes: an unterminated event goes out as it came."""
        rest, self._buf = self._buf, b""
        return rest

    def _event_end(self) -> tuple[int, int]:
        best = (-1, 0)
        for sep in (b"\n\n", b"\r\n\r\n"):
            i = self._buf.find(sep)
            if i >= 0 and (best[0] < 0 or i < best[0]):
                best = (i, len(sep))
        return best

    def _event(self, raw: bytes) -> bytes:
        try:
            text = raw.decode("utf-8")
            name, data_lines = None, []
            for line in text.splitlines():
                if line.startswith("event:"):
                    name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip(" "))
            if not data_lines:
                return raw
            data = json.loads("\n".join(data_lines))
        except (UnicodeDecodeError, ValueError):
            return raw  # not ours to interpret: placeholders stay, nothing leaks
        kind = data.get("type")
        index = data.get("index")
        if kind == "content_block_start":
            block = data.get("content_block") or {}
            self._blocks[index] = {"type": block.get("type"), "name": block.get("name", ""), "pending": "", "json": []}
            return raw
        state = self._blocks.get(index)
        if kind == "content_block_delta" and state:
            delta = data.get("delta") or {}
            if delta.get("type") == "text_delta" and state["type"] == "text":
                text = state["pending"] + delta.get("text", "")
                cut = _hold_from(text)
                state["pending"] = text[cut:]
                ready = restore_text(text[:cut], self.vault, self.counts)
                return self._emit(name, {**data, "delta": {**delta, "text": ready}}) if ready else b""
            if delta.get("type") == "input_json_delta" and state["type"] == "tool_use":
                state["json"].append(delta.get("partial_json", ""))
                return b""
            return raw
        if kind == "content_block_stop" and state:
            del self._blocks[index]
            return self._flush(index, state) + raw
        return raw

    def _flush(self, index: int, state: dict) -> bytes:
        if state["type"] == "text" and state["pending"]:
            text = restore_text(state["pending"], self.vault, self.counts)
            delta = {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": text}}
            return self._emit("content_block_delta", delta)
        if state["type"] == "tool_use" and state["json"]:
            partial = self._restore_json("".join(state["json"]), state["name"])
            delta = {"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": partial}}
            return self._emit("content_block_delta", delta)
        return b""

    def _restore_json(self, partial: str, name: str) -> str:
        try:
            tree = json.loads(partial)
        except ValueError:
            # Not parseable as a whole: restore in place, JSON-escaping each value.
            only, decisions = tool_values(name, partial, self.vault, self.policy)
            for action, reason, kind, n in decisions:
                self.counts[(TOOL, name, action, reason, kind)] += n

            def swap(m):
                ph, value, suffix = resolve(m, self.vault)
                if value is None:
                    self.counts[UNRESOLVED] += 1
                    return m.group(0)
                if only is not None and ph not in only:
                    return m.group(0)
                self.counts[m.group(1)] += 1
                return json.dumps(value + suffix, ensure_ascii=False)[1:-1]

            return PLACEHOLDER.sub(swap, partial)
        restored = restore_tool(name, tree, self.vault, self.policy, self.counts)
        return partial if restored is tree else json.dumps(restored, ensure_ascii=False)

    @staticmethod
    def _emit(name: str | None, data: dict) -> bytes:
        head = f"event: {name}\n" if name else ""
        return f"{head}data: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8", "replace")
