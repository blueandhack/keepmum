"""What a live run cost: the tokens each provider reported in its replies, priced per model.

Read from the replies the tap recorded (the provider's own `usage`), so it is what was billed, not an
estimate: Anthropic's Messages API (input, cache reads and writes, output) and OpenAI's Responses API
(input, of it cached, output). A model with no price here is counted in tokens and costed as unknown.
On a login (a ChatGPT or Claude subscription), the same figure is what the run would cost on an API
key: the subscription is billed differently.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# US$ per million tokens: (input, cached input read, cache write, output). Anthropic: its published
# rates (cache writes 1.25x input, reads 0.1x). OpenAI: third-party price lists, Nov 2026; check
# https://openai.com/api/pricing before relying on them. Matched by prefix, longest first.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5-5": (4.00, 0.40, 5.00, 20.00),
    "claude-opus-5": (5.00, 0.50, 6.25, 25.00),
    "claude-sonnet-5-5": (2.00, 0.20, 2.50, 10.00),
    "claude-sonnet-5": (2.00, 0.20, 2.50, 10.00),
    "claude-haiku-5-5": (0.10, 0.01, 0.125, 0.50),
    "claude-haiku-4-5": (1.00, 0.10, 1.25, 5.00),
    # DeepSeek: third-party lists, Nov 2026 (cache hits assumed at 10% of input, writes at input).
    "deepseek-flash": (0.14, 0.014, 0.14, 0.28),
    "deepseek-v4-pro": (1.74, 0.174, 1.74, 3.48),
    "gpt-5-nano": (0.05, 0.005, 0.0, 0.40),
    "gpt-5-mini": (0.25, 0.025, 0.0, 2.00),
    "gpt-6-luna": (0.10, 0.01, 0.0, 0.50),
    "gpt-6.1-terra": (1.00, 0.05, 0.0, 5.00),
    "gpt-6.1-sol": (2.00, 0.10, 0.0, 10.00),
    "gpt-6-astra": (10.00, 1.00, 0.0, 50.00),
}


def price_of(model: str | None) -> tuple[float, float, float, float] | None:
    if not model:
        return None
    for name in sorted(PRICES, key=len, reverse=True):
        if model == name or model.startswith(name + "-") or model.startswith(name + "@"):
            return PRICES[name]
    return None


@dataclass
class Usage:
    calls: int = 0
    input: int = 0  # not cached (Anthropic: input_tokens; OpenAI: input minus cached)
    cached: int = 0  # read from the provider's cache
    cache_write: int = 0  # Anthropic only
    output: int = 0
    cost: float = 0.0
    unpriced: set[str] = field(default_factory=set)  # models with no price: cost is a lower bound
    models: set[str] = field(default_factory=set)

    def add(self, model: str | None, inp: int, cached: int, write: int, out: int) -> None:
        self.calls += 1
        self.input, self.cached, self.cache_write, self.output = (
            self.input + inp, self.cached + cached, self.cache_write + write, self.output + out)
        if model:
            self.models.add(model)
        p = price_of(model)
        if p is None:
            self.unpriced.add(model or "?")
        else:
            self.cost += (inp * p[0] + cached * p[1] + write * p[2] + out * p[3]) / 1e6

    def merge(self, other: "Usage") -> None:
        for k in ("calls", "input", "cached", "cache_write", "output", "cost"):
            setattr(self, k, getattr(self, k) + getattr(other, k))
        self.unpriced |= other.unpriced
        self.models |= other.models

    def as_dict(self) -> dict:
        return {"calls": self.calls, "input": self.input, "cached": self.cached, "cache_write": self.cache_write,
                "output": self.output, "cost": round(self.cost, 6), "models": sorted(self.models),
                "unpriced": sorted(self.unpriced)}

    @classmethod
    def from_dict(cls, d: dict) -> "Usage":
        return cls(d["calls"], d["input"], d["cached"], d["cache_write"], d["output"], d["cost"],
                   set(d.get("unpriced", ())), set(d.get("models", ())))


def _events(reply: str) -> list[dict]:
    """The JSON documents in a reply: each `data:` line of a stream, or the whole body."""
    out = []
    for line in reply.splitlines():
        if line.startswith("data:"):
            try:
                out.append(json.loads(line[5:]))
            except ValueError:
                pass
    if not out:
        try:
            out.append(json.loads(reply))
        except ValueError:
            pass
    return [e for e in out if isinstance(e, dict)]


def _anthropic(events: list[dict]) -> tuple | None:
    usage, model = {}, None
    for e in events:
        if e.get("type") == "message_start":
            usage.update((e.get("message") or {}).get("usage") or {})
            model = (e.get("message") or {}).get("model") or model
        elif e.get("type") == "message_delta":
            usage.update({k: v for k, v in (e.get("usage") or {}).items() if v is not None})
        elif e.get("type") == "message":  # a whole reply
            usage.update(e.get("usage") or {})
            model = e.get("model") or model
    if not usage:
        return None
    return (model, usage.get("input_tokens") or 0, usage.get("cache_read_input_tokens") or 0,
            usage.get("cache_creation_input_tokens") or 0, usage.get("output_tokens") or 0)


def _responses(events: list[dict]) -> tuple | None:
    for e in events:
        r = e.get("response") if e.get("type") == "response.completed" else e if e.get("object") == "response" else None
        if r and r.get("usage"):
            u = r["usage"]
            cached = (u.get("input_tokens_details") or {}).get("cached_tokens") or 0
            return r.get("model"), (u.get("input_tokens") or 0) - cached, cached, 0, u.get("output_tokens") or 0
    return None


def of(exchanges) -> Usage:
    """The usage every model call in a run reported."""
    total = Usage()
    for ex in exchanges:
        if ex.method != "POST" or ex.status != 200:
            continue
        events = _events(ex.reply.decode("utf-8", "replace"))
        hit = _anthropic(events) if ex.target in ("anthropic", "deepseek") else _responses(events) \
            if ex.target in ("openai", "chatgpt") else None
        if hit:
            total.add(*hit)
    return total


def dollars(u: Usage | dict) -> str:
    u = Usage.from_dict(u) if isinstance(u, dict) else u
    return f"${u.cost:.4f}" + ("+" if u.unpriced else "")
