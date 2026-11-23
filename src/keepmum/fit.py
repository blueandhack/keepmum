"""A long conversation made to fit what the local model reads, for a turn it answers on this Mac.

A coding agent's conversation outgrows a local model's context: Claude Code's runs to hundreds of
thousands of tokens, and the picks run at 64k (Ollama copies, Splash by default). A turn over it
was refused by the server, and the agent got keepmum's 403. And reading costs time even where it
fits: Qwen3.6 35B-A3B on Splash 1.7.1 (M5 Pro, Nov 22, 2026) read 26k tokens in 9 s, 65k in 31 s
and 134k in 98 s before its first word; an M4 Pro reads at about half that speed.

So a turn the local model answers is cut to `[guard] local_answer_context` tokens (32k by default;
"full": the server's own context), and never more than the server's context less what it writes:

1. each message longer than a share of that (CAP of it) keeps its start and its end, with a line
   saying how much was left out between them: an old file read or log is mostly what it is;
2. then, while it is still over, the oldest messages go, after the instructions and the user's first
   message (the task), never the newest one; a line where they were says how many.

Messages go in steps of STEP of the budget, so the point the conversation starts again from stays
the same for several turns: a server's prompt cache (Splash's, llama.cpp's) then reads only what is
new. A server's context is what it says of itself (`context_of`); a request it still refuses as too
long is cut again to half.

Tokens are estimated from characters (CHARS_PER_TOKEN: a Claude Code session of docs and code ran at
3.8 a token on Qwen3.6, Nov 22, 2026); a server that still refuses it as too long gets it cut again.
"""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass

CHARS_PER_TOKEN = 3.5
CAP = 0.4  # of the budget: the most one message keeps
STEP = 0.25  # of the budget: how far the start moves at a time
HEAD = 0.7  # of what a cut message keeps: its start; the rest its end
DEFAULT = 32768
CONTEXT_TTL = 300.0  # seconds a server's context is taken as known


@dataclass
class Fitted:
    messages: list[dict]
    left_out: int = 0  # messages that went
    cut: int = 0  # characters left out of messages that stayed
    tokens: int = 0  # estimated, after

    def note(self) -> str:
        parts = []
        if self.left_out:
            parts.append(f"{self.left_out} earlier message{'s' if self.left_out != 1 else ''} left out")
        if self.cut:
            parts.append(f"{self.cut:,} characters cut from long messages")
        return "; ".join(parts)


def tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN) + 4


def size(messages: list[dict]) -> int:
    return sum(tokens(m.get("content") or "") for m in messages)


def _cut(text: str, keep: int) -> tuple[str, int]:
    """`text` in at most about `keep` characters: its start and end, a line between saying how much went."""
    if len(text) <= keep:
        return text, 0
    head = int(keep * HEAD)
    tail = keep - head
    gone = len(text) - head - tail
    return (f"{text[:head]}\n\n[keepmum: {gone:,} characters of this message are left out here, so that the "
            f"conversation fits the local model's context.]\n\n{text[len(text) - tail:]}"), gone


def fit(messages: list[dict], budget: int) -> Fitted:
    """Chat Completions `messages` (the system message first, then turns that alternate) in about
    `budget` tokens. Unchanged if they fit."""
    if size(messages) <= budget:
        return Fitted(messages, tokens=size(messages))
    cap = max(2000, int(budget * CAP * CHARS_PER_TOKEN))
    out, cut = [], 0
    for m in messages:
        text, gone = _cut(m.get("content") or "", cap)
        out.append({**m, "content": text})
        cut += gone
    total = size(out)
    left_out = 0
    # Kept whatever happens: the system message, the user's first message (the task), and the newest.
    first = 1 + (len(out) > 2 and out[1].get("role") == "user")
    if total > budget and len(out) - 1 > first:
        step = max(1, int(budget * STEP))
        need = math.ceil((total - budget) / step) * step
        end, dropped = first, 0
        while end < len(out) - 1 and dropped < need:
            dropped += tokens(out[end].get("content") or "")
            end += 1
        left_out = end - first
        line = (f"[keepmum: {left_out} earlier message{'s' if left_out != 1 else ''} of this conversation "
                "are left out here, so that it fits the local model's context.]")
        rest = out[end:]
        out = out[:first] + _joined(out[first - 1]["role"] if first > 1 else None, line, rest)
        total = size(out)
    if total > budget:  # the instructions and the newest message alone are over it: cut those too
        over = (total - budget) * CHARS_PER_TOKEN
        for i in sorted(range(len(out)), key=lambda k: -len(out[k].get("content") or "")):
            text = out[i].get("content") or ""
            if over <= 0:
                break
            new, gone = _cut(text, max(1000, len(text) - int(over) - 400))
            if gone:
                out[i] = {**out[i], "content": new}
                cut += gone
                over -= gone
        total = size(out)
    return Fitted(out, left_out, cut, total)


def _joined(before: str | None, line: str, rest: list[dict]) -> list[dict]:
    """`rest` with `line` first, roles still alternating after a message of role `before`."""
    if rest and rest[0].get("role") != before:
        return [{**rest[0], "content": f"{line}\n\n{rest[0].get('content') or ''}"}, *rest[1:]]
    role = "assistant" if before == "user" else "user"
    return [{"role": role, "content": line}, *rest]


READS = ("16k", "32k", "64k", "128k", "full")


def parse_reads(text: str) -> tuple[str, int | None]:
    """`[guard] local_answer_context`: (as written, tokens, or None for "full": the server's context)."""
    t = text.strip().lower()
    if t in ("full", "default", "max", "server"):
        return "full", None
    m = re.fullmatch(r"(\d+)\s*(k?)", t)
    if not m:
        raise ValueError(f"{text}: give a size such as 32k or 65536, or full")
    n = int(m.group(1)) * (1024 if m.group(2) else 1)
    if not 4096 <= n <= 1_048_576:
        raise ValueError(f"{text}: from 4k to 1024k tokens, or full")
    return (f"{n // 1024}k" if n % 1024 == 0 else str(n)), n


def budget(setting: int | None, context: int | None, writes: int) -> int:
    """What a turn may take: the setting (None: the server's context), never more than the
    server's context less what the model writes and a margin."""
    room = context - writes - 512 if context else None
    if setting is None:
        return room or DEFAULT
    return min(setting, room) if room else setting


def over(data: dict, budget_tokens: int) -> bool:
    """Whether a request, whatever its API, is likely over `budget_tokens` (its JSON, estimated)."""
    return len(json.dumps(data, ensure_ascii=False)) / CHARS_PER_TOKEN > budget_tokens


_LIMIT = re.compile(r"(?:context(?: length| window| size)?|n_ctx|maximum)[^0-9]{0,40}?(\d{4,7})", re.I)


def limit_in(error: str) -> int | None:
    """The context a server's too-long error names, if it names one ("exceeds the context length of 65536")."""
    m = _LIMIT.search(error or "")
    return int(m.group(1)) if m else None


_known: dict[tuple[str, str], tuple[int | None, float]] = {}


async def context_of(client, url: str, model: str) -> int | None:
    """The context a local server runs `model` at, as it says: /v1/models (`context_length`,
    `max_model_len`: Splash, vLLM, LM Studio), llama.cpp's /props, Ollama's /api/show (`num_ctx`).
    None if it does not say. Kept CONTEXT_TTL seconds."""
    key = (url, model)
    got = _known.get(key)
    if got and time.time() - got[1] < CONTEXT_TTL:
        return got[0]
    base, n = url.rstrip("/"), None
    try:
        r = await client.get(base + "/v1/models", timeout=3)
        if r.status_code == 200:
            for m in r.json().get("data") or []:
                if isinstance(m, dict) and m.get("id") == model:
                    n = next((m[k] for k in ("context_length", "max_model_len", "max_context_length", "loaded_context_length")
                              if isinstance(m.get(k), int)), None)
        if n is None:
            r = await client.get(base + "/props", timeout=3)
            if r.status_code == 200:
                n = ((r.json().get("default_generation_settings") or {}).get("n_ctx"))
        if n is None:
            r = await client.post(base + "/api/show", json={"model": model}, timeout=3)
            if r.status_code == 200:
                m = re.search(r"(?m)^num_ctx\s+(\d+)", r.json().get("parameters") or "")
                n = int(m.group(1)) if m else None
    except Exception:  # a server that says nothing of it: fitted to the setting alone
        n = None
    n = n if isinstance(n, int) and n > 0 else None
    _known[key] = (n, time.time())
    return n
