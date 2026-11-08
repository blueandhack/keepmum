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

A note the tool policy collects while a call is restored (`policy.notes`: the script check's, about
a Codex script keepmum cannot fully see) goes to the user as an assistant message of its own, right
after that call, in the stream and in a whole reply; every later item's output_index moves up one.
"""

from __future__ import annotations

import json
import secrets
from collections import Counter

from .restore import DEFAULT_POLICY, StreamRestorer, _hold_from, next_run, restore_reply, restore_tool
from .vault import Vault


def _restore_args(raw: str, name: str, vault: Vault, policy: dict, counts: Counter) -> str:
    return restore_tool(name, raw, vault, policy, counts, parse=True)


def restore_item(item, vault: Vault, policy: dict = DEFAULT_POLICY, counts: Counter | None = None):
    """One output item: assistant text always, tool-call input per the tool policy."""
    counts = counts if counts is not None else Counter()
    if not isinstance(item, dict):
        return item
    kind = item.get("type")
    if kind == "message" and isinstance(item.get("content"), list):
        return {**item, "content": [{**c, "text": restore_reply(c["text"], vault, counts)}
                                    if isinstance(c, dict) and isinstance(c.get("text"), str) else c
                                    for c in item["content"]]}
    if kind == "function_call" and isinstance(item.get("arguments"), str):
        return {**item, "arguments": _restore_args(item["arguments"], item.get("name", ""), vault, policy, counts)}
    if kind == "custom_tool_call" and isinstance(item.get("input"), str):
        return {**item, "input": restore_tool(item.get("name", ""), item["input"], vault, policy, counts)}
    if kind in ("local_shell_call", "shell_call"):
        return {**item, "action": restore_tool(kind.removesuffix("_call"), item.get("action"), vault, policy, counts)}
    if kind == "mcp_call" and isinstance(item.get("arguments"), str):
        label = item.get("server_label") or ""
        fn = item.get("name") or ""
        mcp_name = f"mcp__{label}__{fn}" if label else f"mcp__{fn}"
        return {**item, "arguments": _restore_args(item["arguments"], mcp_name, vault, policy, counts)}
    return item


def note_item(text: str) -> dict:
    """keepmum's note to the user, as an assistant message (Codex shows it like its own commentary)."""
    return {"type": "message", "id": f"msg_keepmum_{secrets.token_hex(8)}", "role": "assistant", "status": "completed",
            "phase": "commentary", "content": [{"type": "output_text", "text": text, "annotations": []}]}


def _notes(policy) -> list[str] | None:
    notes = getattr(policy, "notes", None)
    return notes if isinstance(notes, list) else None


def restore_responses(body: dict, vault: Vault, policy: dict = DEFAULT_POLICY, counts: Counter | None = None) -> dict:
    """A non-streaming /responses reply with placeholders restored, and the policy's notes after the
    calls they are about."""
    if not isinstance(body.get("output"), list):
        return body
    notes, out = _notes(policy), []
    for item in body["output"]:
        before = len(notes) if notes is not None else 0
        out.append(restore_item(item, vault, policy, counts))
        out += [note_item(n) for n in (notes[before:] if notes is not None else [])]
    return {**body, "output": out}


class ResponsesStreamRestorer(StreamRestorer):
    """Restores placeholders in a /responses SSE stream, chunk by chunk."""

    def __init__(self, vault: Vault, policy: dict[str, str] = DEFAULT_POLICY, counts: Counter | None = None):
        super().__init__(vault, policy, counts)
        self._text: dict[tuple, str] = {}    # (item_id, content_index) -> held-back tail
        self._runs: dict[tuple, str] = {}    # (item_id, content_index) -> the run its text ends in
        self._calls: dict[str, dict] = {}    # item_id -> {"name", "parts", "last"}
        self._pending: dict[str, list[str]] = {}  # item_id -> notes to show once that call is done
        self._sent: dict[str, list[dict]] = {}    # item_id -> the note items shown after it
        self._after: list[int] = []               # output_index (as sent upstream) of each call a note follows

    def _at(self, index: int) -> int:
        """An upstream output_index as the client sees it: each note before it moves it up one."""
        return index + sum(1 for i in self._after if i < index)

    def _note_events(self, name, after: int, sequence, text: str) -> tuple[bytes, dict]:
        item = note_item(text)
        at, part = after + 1, item["content"][0]
        base = {"item_id": item["id"], "output_index": at, "content_index": 0}
        if sequence is not None:
            base["sequence_number"] = sequence
        events = [
            ("response.output_item.added", {"output_index": at, "item": {**item, "status": "in_progress", "content": []}}),
            ("response.content_part.added", {**base, "part": {**part, "text": ""}}),
            ("response.output_text.delta", {**base, "delta": part["text"]}),
            ("response.output_text.done", {**base, "text": part["text"]}),
            ("response.content_part.done", {**base, "part": part}),
            ("response.output_item.done", {"output_index": at, "item": item}),
        ]
        out = b"".join(self._emit(name and kind, {"type": kind, **({"sequence_number": sequence} if sequence is not None
                                                                    and "sequence_number" not in data else {}), **data})
                       for kind, data in events)
        return out, item

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
        upstream_index = data.get("output_index")
        if self._after and isinstance(upstream_index, int) and self._at(upstream_index) != upstream_index:
            data = {**data, "output_index": self._at(upstream_index)}
            raw = self._emit(name, data)
        # Counted once, where a value first comes out (text deltas, a finished tool call); the
        # same value repeated in .done events, items and response.completed is not counted again.
        v, p, c, again = self.vault, self.policy, self.counts, Counter()

        if kind == "response.output_text.delta":
            key = (data.get("item_id"), data.get("content_index"))
            text = self._text.get(key, "") + data.get("delta", "")
            cut = _hold_from(text)
            self._text[key] = text[cut:]
            before = self._runs.get(key, "")
            ready = restore_reply(text[:cut], v, c, before)
            self._runs[key] = next_run(before, text[:cut])
            return self._emit(name, {**data, "delta": ready}) if ready else b""
        if kind == "response.output_text.done":
            key = (data.get("item_id"), data.get("content_index"))
            tail = self._text.pop(key, "")
            flush = self._emit(name and "response.output_text.delta", {
                "type": "response.output_text.delta", "item_id": key[0], "content_index": key[1],
                "output_index": data.get("output_index"), "delta": restore_reply(tail, v, c, self._runs.pop(key, "")),
                "sequence_number": data.get("sequence_number")}) if tail else b""
            return flush + self._emit(name, {**data, "text": restore_reply(data.get("text", ""), v, again)})

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
            notes = _notes(p)
            before = len(notes) if notes is not None else 0
            if field == "arguments":
                whole = _restore_args(whole, call["name"], v, p, c)
            else:
                whole = restore_tool(call["name"], whole, v, p, c)
            if notes is not None and len(notes) > before:
                self._pending.setdefault(data.get("item_id"), []).extend(notes[before:])
            delta = b""
            if call["parts"]:
                last = call["last"] or {}
                delta = self._emit(name and last.get("type"), {**last, "delta": whole})
            return delta + self._emit(name, {**data, field: whole})

        if kind in ("response.content_part.added", "response.content_part.done"):
            part = data.get("part") or {}
            if isinstance(part.get("text"), str):
                return self._emit(name, {**data, "part": {**part, "text": restore_reply(part["text"], v, again)}})
            return raw
        if kind == "response.output_item.done":
            item = data.get("item")
            item_counts = c if isinstance(item, dict) and item.get("type") in ("local_shell_call", "shell_call") else again
            out = self._emit(name, {**data, "item": restore_item(item, v, p, item_counts)})
            item_id = item.get("id") if isinstance(item, dict) else None
            if item_id in self._pending and isinstance(upstream_index, int):
                for text in self._pending.pop(item_id):
                    events, note = self._note_events(name, self._at(upstream_index), data.get("sequence_number"), text)
                    out += events
                    self._sent.setdefault(item_id, []).append(note)
                    self._after.append(upstream_index)
            return out
        if kind in ("response.completed", "response.done", "response.incomplete"):
            resp = data.get("response")
            if isinstance(resp, dict):
                # The whole reply again: restored without collecting notes twice, and with the notes
                # this stream showed, each after its call.
                quiet = _quiet(p)
                resp = restore_responses(resp, v, quiet, again)
                if self._sent and isinstance(resp.get("output"), list):
                    out_items = []
                    for item in resp["output"]:
                        out_items.append(item)
                        out_items += self._sent.get(item.get("id") if isinstance(item, dict) else None, [])
                    resp = {**resp, "output": out_items}
                return self._emit(name, {**data, "response": resp})
        return raw


def _quiet(policy):
    """The policy without its notes and review: for restoring what was already reviewed."""
    if _notes(policy) is None:
        return policy
    from .restore import ToolPolicy

    return ToolPolicy(policy, getattr(policy, "registry", None), getattr(policy, "destinations", None))
