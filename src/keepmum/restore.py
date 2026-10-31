"""Put real values back where placeholders appear: whole responses and SSE streams."""

from __future__ import annotations

import json
from collections import Counter

from .detect import TYPES
from .vault import PLACEHOLDER, Vault

# Per tool: "restore" puts real values into the tool's input before it runs locally; "keep"
# leaves placeholders, so a prompt-injected request cannot send a real value over the network.
# MCP tools keep placeholders because keepmum cannot tell which ones reach the network.
DEFAULT_POLICY = {
    "default": "restore",
    "WebFetch": "keep",
    "WebSearch": "keep",
    "webfetch": "keep",
    "websearch": "keep",
    "web_fetch": "keep",
    "web_search": "keep",
    "codesearch": "keep",
    "mcp__*": "keep",
    "mcp_*": "keep",
}


def tool_policy(name: str, policy: dict[str, str]) -> str:
    if name in policy:
        return policy[name]
    for pattern, action in policy.items():
        if pattern.endswith("*") and name.startswith(pattern[:-1]):
            return action
    return policy.get("default", "restore")


def note_tool(counts: Counter | None, name: str, policy: dict[str, str], value) -> None:
    """Record a tool call whose input holds placeholders, and what the policy did with them, for
    the audit log: counts[("tool", name, "restore" | "keep")] += placeholders in the input."""
    if counts is None:
        return
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    n = len(PLACEHOLDER.findall(text))
    if n:
        counts[(TOOL, name, tool_policy(name, policy))] += n


def take_tools(counts: Counter) -> list[dict]:
    """Remove the tool decisions note_tool put in counts: [{tool, action, values}]."""
    keys = [k for k in counts if isinstance(k, tuple) and k[0] == TOOL]
    return [{"tool": k[1], "action": k[2], "values": counts.pop(k)} for k in sorted(keys)]


TOOL = "tool"

# Key in the counts Counter for placeholders the vault does not know: the model reused one this
# process never made (another keepmum with another salt, or one restarted since). They stay as
# they are, and the proxy reports them.
UNRESOLVED = "unresolved"


def restore_text(text: str, vault: Vault, counts: Counter | None = None) -> str:
    """Replace every placeholder the vault knows. Unknown placeholders stay as they are."""

    def swap(m):
        ph = m.group(0)
        value = vault.value(ph)
        suffix = ""
        # A 12-digit match may be an 8-digit placeholder followed by hex text.
        if value is None and len(m.group(2)) == 12:
            value, suffix = vault.value(ph[:-4]), ph[-4:]
        if value is None:
            if counts is not None:
                counts[UNRESOLVED] += 1
            return ph
        if counts is not None:
            counts[m.group(1)] += 1
        return value + suffix

    return PLACEHOLDER.sub(swap, text)


def _values(value, vault: Vault, counts: Counter | None):
    if isinstance(value, str):
        return restore_text(value, vault, counts)
    if isinstance(value, dict):
        return {(restore_text(k, vault, counts) if isinstance(k, str) else k): _values(v, vault, counts)
                for k, v in value.items()}
    if isinstance(value, list):
        return [_values(v, vault, counts) for v in value]
    return value


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
            note_tool(counts, block.get("name", ""), policy, block.get("input"))
            if tool_policy(block.get("name", ""), policy) == "restore":
                out.append({**block, "input": _values(block.get("input"), vault, counts)})
            else:
                out.append(block)
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
            partial = "".join(state["json"])
            note_tool(self.counts, state["name"], self.policy, partial)
            if tool_policy(state["name"], self.policy) == "restore":
                partial = self._restore_json(partial)
            delta = {"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": partial}}
            return self._emit("content_block_delta", delta)
        return b""

    def _restore_json(self, partial: str) -> str:
        try:
            return json.dumps(_values(json.loads(partial), self.vault, self.counts), ensure_ascii=False)
        except ValueError:
            # Not parseable as a whole: restore in place, JSON-escaping each value.
            def swap(m):
                ph = m.group(0)
                value, suffix = self.vault.value(ph), ""
                if value is None and len(m.group(2)) == 12:
                    value, suffix = self.vault.value(ph[:-4]), ph[-4:]
                if value is None:
                    self.counts[UNRESOLVED] += 1
                    return ph
                self.counts[m.group(1)] += 1
                return json.dumps(value + suffix, ensure_ascii=False)[1:-1]

            return PLACEHOLDER.sub(swap, partial)

    @staticmethod
    def _emit(name: str | None, data: dict) -> bytes:
        head = f"event: {name}\n" if name else ""
        return f"{head}data: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8", "replace")
