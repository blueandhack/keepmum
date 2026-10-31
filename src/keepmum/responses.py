"""Restore placeholders in OpenAI Responses API replies (Codex, pi): whole responses and streams.

Verified against Codex 0.147 on chatgpt.com/backend-api/codex (Oct 27, 2026). A stream is
`response.*` events, each one `data:` line of JSON:

- output_text.delta: text in pieces. A tail buffer per (item, part) holds back anything that
  could still become a placeholder, as for Anthropic text deltas.
- function_call_arguments.delta / custom_tool_call_input.delta: a tool call in pieces. Held
  until the matching .done, then restored per the tool policy and sent as one delta.
- output_text.done, content_part.*, output_item.*, function_call_arguments.done and
  response.completed carry whole texts and items: restored in place.
- Everything else (reasoning, metadata, errors) passes through byte for byte.
"""

from __future__ import annotations

import json
from collections import Counter

from .restore import DEFAULT_POLICY, StreamRestorer, _hold_from, _values, note_tool, restore_text, tool_policy
from .vault import Vault


def _restore_args(raw: str, name: str, vault: Vault, policy: dict, counts: Counter) -> str:
    note_tool(counts, name, policy, raw)
    if tool_policy(name, policy) != "restore":
        return raw
    try:
        return json.dumps(_values(json.loads(raw), vault, counts), ensure_ascii=False)
    except ValueError:
        return restore_text(raw, vault, counts)


def restore_item(item, vault: Vault, policy: dict = DEFAULT_POLICY, counts: Counter | None = None):
    """One output item: assistant text always, tool-call input per the tool policy."""
    counts = counts if counts is not None else Counter()
    if not isinstance(item, dict):
        return item
    kind = item.get("type")
    if kind == "message" and isinstance(item.get("content"), list):
        return {**item, "content": [{**c, "text": restore_text(c["text"], vault, counts)}
                                    if isinstance(c, dict) and isinstance(c.get("text"), str) else c
                                    for c in item["content"]]}
    if kind == "function_call" and isinstance(item.get("arguments"), str):
        return {**item, "arguments": _restore_args(item["arguments"], item.get("name", ""), vault, policy, counts)}
    if kind == "custom_tool_call" and isinstance(item.get("input"), str):
        note_tool(counts, item.get("name", ""), policy, item["input"])
        if tool_policy(item.get("name", ""), policy) == "restore":
            return {**item, "input": restore_text(item["input"], vault, counts)}
    if kind in ("local_shell_call", "shell_call"):
        tool = kind.removesuffix("_call")
        note_tool(counts, tool, policy, item.get("action"))
        if tool_policy(tool, policy) == "restore":
            return {**item, "action": _values(item.get("action"), vault, counts)}
    if kind == "mcp_call" and isinstance(item.get("arguments"), str):
        label = item.get("server_label") or ""
        fn = item.get("name") or ""
        mcp_name = f"mcp__{label}__{fn}" if label else f"mcp__{fn}"
        return {**item, "arguments": _restore_args(item["arguments"], mcp_name, vault, policy, counts)}
    return item


def restore_responses(body: dict, vault: Vault, policy: dict = DEFAULT_POLICY, counts: Counter | None = None) -> dict:
    """A non-streaming /responses reply with placeholders restored."""
    if isinstance(body.get("output"), list):
        return {**body, "output": [restore_item(i, vault, policy, counts) for i in body["output"]]}
    return body


class ResponsesStreamRestorer(StreamRestorer):
    """Restores placeholders in a /responses SSE stream, chunk by chunk."""

    def __init__(self, vault: Vault, policy: dict[str, str] = DEFAULT_POLICY, counts: Counter | None = None):
        super().__init__(vault, policy, counts)
        self._text: dict[tuple, str] = {}    # (item_id, content_index) -> held-back tail
        self._calls: dict[str, dict] = {}    # item_id -> {"name", "parts", "last"}

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
            return raw
        if not isinstance(data, dict):
            return raw
        kind = data.get("type", "")
        # Counted once, where a value first comes out (text deltas, a finished tool call); the
        # same value repeated in .done events, items and response.completed is not counted again.
        v, p, c, again = self.vault, self.policy, self.counts, Counter()

        if kind == "response.output_text.delta":
            key = (data.get("item_id"), data.get("content_index"))
            text = self._text.get(key, "") + data.get("delta", "")
            cut = _hold_from(text)
            self._text[key] = text[cut:]
            ready = restore_text(text[:cut], v, c)
            return self._emit(name, {**data, "delta": ready}) if ready else b""
        if kind == "response.output_text.done":
            key = (data.get("item_id"), data.get("content_index"))
            tail = self._text.pop(key, "")
            flush = self._emit(name and "response.output_text.delta", {
                "type": "response.output_text.delta", "item_id": key[0], "content_index": key[1],
                "output_index": data.get("output_index"), "delta": restore_text(tail, v, c),
                "sequence_number": data.get("sequence_number")}) if tail else b""
            return flush + self._emit(name, {**data, "text": restore_text(data.get("text", ""), v, again)})

        if kind == "response.output_item.added":
            item = data.get("item") or {}
            if item.get("type") in ("function_call", "custom_tool_call", "mcp_call"):
                label = item.get("server_label") or ""
                fn = item.get("name", "")
                tool_name = (f"mcp__{label}__{fn}" if label else f"mcp__{fn}") if item.get("type") == "mcp_call" else fn
                self._calls[item.get("id")] = {"name": tool_name, "parts": [], "last": None}
            return self._emit(name, {**data, "item": restore_item(item, v, p, again)})
        if kind in ("response.function_call_arguments.delta", "response.custom_tool_call_input.delta",
                    "response.mcp_call_arguments.delta"):
            call = self._calls.setdefault(data.get("item_id"), {"name": "mcp__" if "mcp_call" in kind else "", "parts": [], "last": None})
            call["parts"].append(data.get("delta", ""))
            call["last"] = data
            return b""
        if kind in ("response.function_call_arguments.done", "response.custom_tool_call_input.done",
                    "response.mcp_call_arguments.done"):
            field = "arguments" if "arguments" in kind else "input"
            call = self._calls.pop(data.get("item_id"), {"name": "mcp__" if "mcp_call" in kind else "", "parts": [], "last": None})
            whole = data.get(field) if isinstance(data.get(field), str) else "".join(call["parts"])
            if field == "arguments":
                whole = _restore_args(whole, call["name"], v, p, c)
            else:
                note_tool(c, call["name"], p, whole)
                if tool_policy(call["name"], p) == "restore":
                    whole = restore_text(whole, v, c)
            delta = b""
            if call["parts"]:
                last = call["last"] or {}
                delta = self._emit(name and last.get("type"), {**last, "delta": whole})
            return delta + self._emit(name, {**data, field: whole})

        if kind in ("response.content_part.added", "response.content_part.done"):
            part = data.get("part") or {}
            if isinstance(part.get("text"), str):
                return self._emit(name, {**data, "part": {**part, "text": restore_text(part["text"], v, again)}})
            return raw
        if kind == "response.output_item.done":
            item = data.get("item")
            item_counts = c if isinstance(item, dict) and item.get("type") in ("local_shell_call", "shell_call") else again
            return self._emit(name, {**data, "item": restore_item(item, v, p, item_counts)})
        if kind in ("response.completed", "response.done", "response.incomplete"):
            resp = data.get("response")
            if isinstance(resp, dict):
                return self._emit(name, {**data, "response": restore_responses(resp, v, p, again)})
        return raw
