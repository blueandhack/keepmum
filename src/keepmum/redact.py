"""Replace sensitive spans with placeholders, in plain text and in Messages API payloads."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, OrderedDict

from .detect import Span
from .detect.merge import merge
from .vault import PLACEHOLDER, Vault

# Signed or binary blocks are resent exactly as the API produced them.
PASS_THROUGH = frozenset({"thinking", "redacted_thinking", "image"})
# For block types keepmum does not know, only these fields are scanned. Ids, names, signatures
# and every other protocol field stay as they are.
CONTENT_KEYS = ("text", "content", "input", "title", "context", "source", "output", "error")

# Appended to every request's system prompt, always the same text, so the prompt cache stays
# valid. Without it Claude sees KEEPMUM_SECRET_... and concludes the file holds a fake value.
PLACEHOLDER_HINT = (
    "Note from keepmum, a local privacy proxy on this machine: values of the form "
    "KEEPMUM_<TYPE>_<8 hex digits> (KEEPMUM_SECRET_xxxxxxxx, KEEPMUM_PERSON_xxxxxxxx, KEEPMUM_EMAIL_xxxxxxxx, "
    "and so on) are placeholders for real values that were redacted before this request left "
    "the user's computer. Treat each one as the real value: copy it exactly when a value is "
    "needed, and do not call it fake, masked or a placeholder. Real values are restored locally "
    "in your replies and tool calls."
)


# Responses API items that are encrypted or signed, or carry no user content.
RESPONSES_PASS_THROUGH = frozenset({"reasoning", "compaction", "item_reference", "web_search_call", "image_generation_call"})
RESPONSES_HINT = {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": PLACEHOLDER_HINT}]}

# Below this length the first and last characters give too much of a value away: show none.
PREVIEW_MIN = 8


def preview(value: str) -> str:
    """The first and last character of a masked value, for the Logs page: `g…w`. Shorter
    values show nothing but dots."""
    return f"{value[0]}…{value[-1]}" if len(value) >= PREVIEW_MIN else "•••"


# Characters of text kept on each side of a masked value for the Logs page's context.
CONTEXT = 80


def context(text: str, spans: list[Span], start: int, end: int) -> tuple[str, str]:
    """The text before and after text[start:end], CONTEXT characters each way, as the provider
    saw it but with every masked value in it, and every placeholder, shown as its type: `‹SECRET›`.
    A value or placeholder cut by the window is shown whole as its type, never in part."""
    marks = sorted([(s.start, s.end, s.type) for s in spans]
                   + [(m.start(), m.end(), m.group(1)) for m in PLACEHOLDER.finditer(text)])

    def render(a: int, b: int) -> str:
        out, pos = [], a
        for s, e, kind in marks:
            if e <= a or s >= b or e <= pos:
                continue
            if s > pos:
                out.append(text[pos:s])
            out.append(f"‹{kind}›")
            pos = e
        if pos < b:
            out.append(text[pos:b])
        return "".join(out)

    a, b = max(0, start - CONTEXT), min(len(text), end + CONTEXT)
    return ("…" if a > 0 else "") + render(a, start), render(end, b) + ("…" if b < len(text) else "")


def detector_of(source: str) -> tuple[str, str]:
    """(detector, rule) for a span's source, preferring your rule, then a built-in rule, then the
    model: rules:github-token|pf-serve -> ("rules", "github-token")."""
    parts = source.split("|")
    for prefix, name in (("user:", "user"), ("rules:", "rules")):
        hit = next((x for x in parts if x.startswith(prefix)), None)
        if hit:
            return name, hit[len(prefix):]
    if "known" in parts and len(parts) == 1:
        return "known", ""
    return "model", ""


class Redactor:
    def __init__(self, detector, vault: Vault, cache_size: int = 4096):
        self.detector = detector
        self.vault = vault
        self._cache: OrderedDict[bytes, tuple[Span, ...]] = OrderedDict()
        self._cache_size = cache_size
        self._generation = getattr(detector, "generation", 0)
        self.counts: Counter[str] = Counter()  # spans masked per type since the last reset
        # Spans masked in text this redactor had not seen before, since the last reset: what a
        # request brought that is new. Claude Code resends the whole conversation every turn.
        self.fresh = 0
        # Those spans for the Logs page: type, detector, rule, length, and with `previews` the
        # first and last character. Never the value. Reset with `fresh`.
        self.findings: list[dict] = []
        self.previews = True
        self.context = True

    def _detect(self, text: str) -> tuple[tuple[Span, ...], bool]:
        """Detector spans, cached per text: the conversation history is resent every turn.
        Also says whether the text was seen before."""
        generation = getattr(self.detector, "generation", 0)
        if generation != self._generation:  # the settings changed: detect again
            self._cache.clear()
            self._generation = generation
        key = hashlib.sha256(text.encode("utf-8", "surrogatepass")).digest()
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
            return hit, True
        spans = tuple(self.detector.detect(text))
        self._cache[key] = spans
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return spans, False

    def spans(self, text: str) -> list[Span]:
        """Detector spans plus every exact occurrence of a value masked before."""
        return self._spans(text)[0]

    def _spans(self, text: str) -> tuple[list[Span], bool]:
        found, seen = self._detect(text)
        spans = list(found)
        known = self.vault.known_values()
        if known is not None:
            passed = getattr(self.detector, "pass_types", frozenset())
            released = getattr(self.detector, "lets_through", None)
            spans += [Span(m.start(), m.end(), kind, 1.0, "known") for m in known.finditer(text)
                      if (kind := self.vault.kind_of(m.group())) not in passed
                      and not (released and released(m.group(), self.vault.sources_of(m.group()), kind))]
        return merge(spans), seen

    def redact_text(self, text: str) -> str:
        if not text:
            return text
        spans, seen = self._spans(text)
        if not seen:
            self.fresh += len(spans)
            for s in spans:
                value = text[s.start : s.end]
                detector, rule = detector_of(s.source)
                before, after = context(text, spans, s.start, s.end) if self.context else (None, None)
                self.findings.append({"type": s.type, "detector": detector, "rule": rule, "chars": len(value),
                                      "preview": preview(value) if self.previews else None,
                                      "before": before, "after": after})
        if not spans:
            return text
        out, pos = [], 0
        for s in spans:
            out.append(text[pos : s.start])
            out.append(self.vault.placeholder(s.type, text[s.start : s.end], s.source.split("|")))
            self.counts[s.type] += 1
            pos = s.end
        out.append(text[pos:])
        return "".join(out)

    # --- Messages API payloads ----------------------------------------------------

    def redact_request(self, body: dict) -> dict:
        """A copy of a /v1/messages (or count_tokens) body with system and messages masked.

        `tools`, `metadata` and every other top-level field pass through unchanged.
        """
        out = dict(body)
        if "system" in out:
            out["system"] = self._content(out["system"])
        out["system"] = self._with_hint(out.get("system"))
        if isinstance(out.get("messages"), list):
            out["messages"] = [
                {**m, "content": self._content(m.get("content"))} if isinstance(m, dict) else m
                for m in out["messages"]
            ]
        return out

    # --- Responses API payloads (OpenAI: Codex, pi) ------------------------------------

    def redact_responses_request(self, body: dict) -> dict:
        """A copy of a /responses body with `instructions` and every `input` item masked.

        Reasoning items are encrypted and signed: they pass through, like thinking blocks. The
        placeholder hint goes first in `input` as a developer message, stable across turns.
        """
        out = dict(body)
        if isinstance(out.get("instructions"), str):
            out["instructions"] = self.redact_text(out["instructions"])
        items = out.get("input")
        if isinstance(items, str):
            items = [{"type": "message", "role": "user", "content": items}]
        if isinstance(items, list):
            out["input"] = [RESPONSES_HINT, *(self._responses_item(i) for i in items)]
        return out

    def _responses_item(self, item):
        if not isinstance(item, dict):
            return item
        kind = item.get("type", "message")
        if kind in RESPONSES_PASS_THROUGH:
            return item
        if kind == "message":
            content = item.get("content")
            if isinstance(content, str):
                return {**item, "content": self.redact_text(content)}
            if isinstance(content, list):
                def _part(c):
                    if not isinstance(c, dict):
                        return c
                    if isinstance(c.get("text"), str):
                        return {**c, "text": self.redact_text(c["text"])}
                    if isinstance(c.get("refusal"), str):
                        return {**c, "refusal": self.redact_text(c["refusal"])}
                    return c

                return {**item, "content": [_part(c) for c in content]}
            return item
        out = {**item, "arguments": self._json_string(item["arguments"])} if isinstance(item.get("arguments"), str) else dict(item)
        for key in ("output", "input", "content", "text", "action", "error"):
            if key in out:
                out[key] = self._values(out[key])
        return out

    def _json_string(self, raw: str) -> str:
        """Tool-call arguments are a JSON document in a string: mask its values, keep its keys."""
        try:
            parsed = json.loads(raw)
        except ValueError:
            return self.redact_text(raw)
        masked = self._values(parsed)
        return raw if masked == parsed else json.dumps(masked, ensure_ascii=False)

    @staticmethod
    def _with_hint(system):
        """The system prompt with the placeholder hint as a last, separate text block."""
        hint = {"type": "text", "text": PLACEHOLDER_HINT}
        if isinstance(system, list):
            return [*system, hint]
        if isinstance(system, str) and system:
            return [{"type": "text", "text": system}, hint]
        return [hint]

    def _content(self, content):
        if isinstance(content, str):
            return self.redact_text(content)
        if isinstance(content, list):
            return [self._block(b) for b in content]
        return content

    def _block(self, block):
        if not isinstance(block, dict):
            return self._content(block)
        kind = block.get("type")
        if kind in PASS_THROUGH:
            return block
        if kind == "text":
            return {**block, "text": self.redact_text(block.get("text", ""))}
        if kind == "tool_use":
            return {**block, "input": self._values(block.get("input"))}
        if kind == "tool_result":
            return {**block, "content": self._content(block.get("content"))}
        if kind == "document":
            out = dict(block)
            for meta in ("title", "context"):
                if isinstance(out.get(meta), str):
                    out[meta] = self.redact_text(out[meta])
            source = out.get("source")
            if isinstance(source, dict) and source.get("type") == "text":
                out["source"] = {**source, "data": self.redact_text(source.get("data", ""))}
            elif isinstance(source, dict) and source.get("type") == "content":
                out["source"] = {**source, "content": self._content(source.get("content"))}
            return out  # PDFs and other binary sources pass through (a v1 risk)
        # Unknown block type: scan its content fields, leave protocol fields alone.
        return {k: (self._values(v) if k in CONTENT_KEYS else v) for k, v in block.items()}

    def _values(self, value):
        """Every string value in a JSON tree, recursively. Keys are never changed."""
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            return {k: self._values(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._values(v) for v in value]
        return value
