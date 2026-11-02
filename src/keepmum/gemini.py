"""Restore placeholders in Gemini replies (Gemini CLI): whole responses and streams.

Two APIs carry the same GenerateContentResponse:

- the Gemini API (an API key): `/v1beta/models/<model>:generateContent`, and
  `:streamGenerateContent?alt=sse`, one `data:` event per response chunk;
- Code Assist ("Sign in with Google"): `/v1internal:generateContent` and
  `:streamGenerateContent?alt=sse`, the same chunks wrapped as `{"response": {...}, "traceId": ...}`.

In a chunk, each candidate's `content.parts` holds text in pieces (a tail buffer per candidate holds
back anything that could still become a placeholder, as for Anthropic text deltas) and whole
function calls (restored per the tool policy). Thought parts are signed model output: they keep
their placeholders, like thinking blocks. A stream asked for without `alt=sse` is a JSON array of
chunks, restored whole.
"""

from __future__ import annotations

import json
from collections import Counter

from .restore import (
    DEFAULT_POLICY,
    StreamRestorer,
    _hold_from,
    restore_text,
    restore_tool,
)
from .vault import Vault


def _part(part, vault: Vault, policy: dict, counts: Counter):
    if not isinstance(part, dict) or part.get("thought") is True:
        return part
    out = dict(part)
    if isinstance(out.get("text"), str):
        out["text"] = restore_text(out["text"], vault, counts)
    for key in ("functionCall", "function_call"):
        call = out.get(key)
        if isinstance(call, dict) and "args" in call:
            out[key] = {**call, "args": restore_tool(call.get("name", ""), call["args"], vault, policy, counts)}
    for key, field in (("executableCode", "code"), ("codeExecutionResult", "output")):
        if isinstance(out.get(key), dict) and isinstance(out[key].get(field), str):
            out[key] = {**out[key], field: restore_text(out[key][field], vault, counts)}
    return out


def _chunk(chunk, vault: Vault, policy: dict, counts: Counter):
    """One GenerateContentResponse, or Code Assist's wrapper around one."""
    if not isinstance(chunk, dict):
        return chunk
    if isinstance(chunk.get("response"), dict):
        return {**chunk, "response": _chunk(chunk["response"], vault, policy, counts)}
    candidates = chunk.get("candidates")
    if not isinstance(candidates, list):
        return chunk
    out = []
    for c in candidates:
        content = c.get("content") if isinstance(c, dict) else None
        if isinstance(content, dict) and isinstance(content.get("parts"), list):
            c = {**c, "content": {**content, "parts": [_part(p, vault, policy, counts) for p in content["parts"]]}}
        out.append(c)
    return {**chunk, "candidates": out}


def restore_gemini(body, vault: Vault, policy: dict = DEFAULT_POLICY, counts: Counter | None = None):
    """A whole reply: one response, Code Assist's wrapper, or a JSON array of stream chunks."""
    counts = counts if counts is not None else Counter()
    if isinstance(body, list):
        return [_chunk(c, vault, policy, counts) for c in body]
    return _chunk(body, vault, policy, counts)


class GeminiStreamRestorer(StreamRestorer):
    """Restores placeholders in a Gemini or Code Assist SSE stream, chunk by chunk."""

    def __init__(self, vault: Vault, policy: dict[str, str] = DEFAULT_POLICY, counts: Counter | None = None):
        super().__init__(vault, policy, counts)
        self._pending: dict[int, str] = {}  # candidate index -> held-back text
        self._wrapped = False  # Code Assist: chunks come as {"response": ...}

    def _event(self, raw: bytes) -> bytes:
        try:
            lines = [line[5:].lstrip(" ") for line in raw.decode("utf-8").splitlines() if line.startswith("data:")]
            if not lines:
                return raw
            data = json.loads("\n".join(lines))
        except (UnicodeDecodeError, ValueError):
            return raw  # not ours to interpret: placeholders stay, nothing leaks
        if not isinstance(data, dict):
            return raw
        self._wrapped = isinstance(data.get("response"), dict)
        chunk = data["response"] if self._wrapped else data
        candidates = chunk.get("candidates")
        if not isinstance(candidates, list):
            return raw
        out, kept_parts = [], False
        for i, c in enumerate(candidates):
            if not isinstance(c, dict):
                out.append(c)
                continue
            index = c.get("index", i)
            content = c.get("content")
            parts = content.get("parts") if isinstance(content, dict) else None
            new_parts = []
            for p in parts if isinstance(parts, list) else []:
                if isinstance(p, dict) and isinstance(p.get("text"), str) and p.get("thought") is not True:
                    text = self._pending.get(index, "") + p["text"]
                    cut = _hold_from(text)
                    self._pending[index] = text[cut:]
                    ready = restore_text(text[:cut], self.vault, self.counts)
                    if ready:
                        new_parts.append({**p, "text": ready})
                    elif "thoughtSignature" in p:
                        new_parts.append({**p, "text": ""})  # a signature is never dropped
                else:
                    new_parts.append(_part(p, self.vault, self.policy, self.counts))
            if c.get("finishReason") and self._pending.get(index):
                new_parts.append({"text": restore_text(self._pending.pop(index), self.vault, self.counts)})
            kept_parts = kept_parts or bool(new_parts)
            if isinstance(content, dict) and isinstance(parts, list):
                c = {**c, "content": {**content, "parts": new_parts}}
            out.append(c)
        # Only held-back text, nothing else to say yet: wait for the next chunk.
        if not kept_parts and not any(isinstance(c, dict) and c.get("finishReason") for c in out) \
                and not chunk.get("usageMetadata") and not chunk.get("promptFeedback"):
            return b""
        new = {**chunk, "candidates": out}
        return self._emit(None, {**data, "response": new} if self._wrapped else new)

    def close(self) -> bytes:
        """Text still held when the stream ends, as a last chunk, then whatever was left unparsed."""
        rest = super().close()
        tail = b""
        for index, text in sorted(self._pending.items()):
            if text:
                chunk = {"candidates": [{"index": index, "content": {"role": "model",
                                                                     "parts": [{"text": restore_text(text, self.vault, self.counts)}]}}]}
                tail += self._emit(None, {"response": chunk} if self._wrapped else chunk)
        self._pending.clear()
        return tail + rest
