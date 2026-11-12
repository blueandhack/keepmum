"""Reviewing a hand-back before it goes to the cloud: `[guard] local_handback_review`.

When the injection check keeps a tool result on this Mac and the local model writes a summary or
rewrite to hand back (handback.py), keepmum can hold the request until the user looks at it: send
it as written, send their own edit, withhold the result, or keep the turn on this Mac. The agent
waits meanwhile, so a review that gets no answer within `wait` seconds does what `timeout` says:
"stay" (the turn stays local, as when the local model fails: nothing reaches the cloud unasked) or
"send" (as written).

With "answer", the local model's answer goes back to the agent at once; the review is of the hand-back
that follows: the next request, carrying the flagged result, waits for it. Sending puts keepmum's
notice in the result's place, an edit puts the user's text there, and keeping it local keeps every
turn that carries the result on this Mac.

Someone has to be there to answer: the Mac app (or `keepmum review --watch`) asks for pending
reviews every second or so, and keepmum holds a request only while someone asked within PRESENT_S.
With nobody there, a hand-back goes as it would without review.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

import anyio

MODES = ("off", "rewrite", "all")  # all: answers, summaries and rewrites
TIMEOUTS = ("stay", "send")
DEFAULT_WAIT = 60
MIN_WAIT, MAX_WAIT = 10, 600
ACTIONS = ("send", "edit", "withhold", "local")
PRESENT_S = 15.0  # someone asked for reviews this recently: they are there to answer


def parse(mode, wait, timeout, where: str = "guard") -> tuple[str, int, str]:
    if mode not in MODES:
        raise ValueError(f"{where}.local_handback_review must be one of {', '.join(MODES)}")
    if not isinstance(wait, int) or isinstance(wait, bool) or not MIN_WAIT <= wait <= MAX_WAIT:
        raise ValueError(f"{where}.local_handback_review_wait must be seconds from {MIN_WAIT} to {MAX_WAIT}")
    if timeout not in TIMEOUTS:
        raise ValueError(f"{where}.local_handback_review_timeout must be one of {', '.join(TIMEOUTS)}")
    return mode, wait, timeout


def applies(mode: str, strategy: str) -> bool:
    return mode == "all" and strategy in ("answer", "summary", "rewrite") or mode == "rewrite" and strategy == "rewrite"


@dataclass
class Decision:
    action: str  # send, edit, withhold, local; "timeout" is never asked for
    text: str | None = None  # the edit
    timed_out: bool = False


@dataclass
class Review:
    strategy: str
    source: str | None
    path: str
    flagged: str  # the tool result the check flagged
    proposed: str  # what would take the result's place: the local model's text, or for an answer, keepmum's notice
    model: str
    wait: int
    answer: str | None = None  # "answer": the local model's answer, which the cloud model sees as that turn's reply
    id: str = field(default_factory=lambda: secrets.token_hex(6))
    created: float = field(default_factory=time.time)
    decision: Decision | None = None
    done: anyio.Event = field(default_factory=anyio.Event)

    def public(self) -> dict:
        return {"id": self.id, "strategy": self.strategy, "source": self.source, "path": self.path,
                "flagged": self.flagged, "proposed": self.proposed, "answer": self.answer, "model": self.model,
                "created": self.created,
                "deadline": self.created + self.wait, "seconds_left": max(0, round(self.created + self.wait - time.time()))}


class Reviews:
    """The reviews waiting for an answer, and whether anyone is there to give one."""

    def __init__(self) -> None:
        self._pending: dict[str, Review] = {}
        self._asked = 0.0

    def seen(self) -> None:
        """Someone asked for the pending reviews: they are there to answer."""
        self._asked = time.monotonic()

    def present(self) -> bool:
        return time.monotonic() - self._asked <= PRESENT_S

    def pending(self) -> list[dict]:
        return [r.public() for r in sorted(self._pending.values(), key=lambda r: r.created)]

    async def ask(self, review: Review, timeout: str) -> Decision:
        """Wait for the user's answer, or `review.wait` seconds; on no answer, what `timeout` says."""
        self._pending[review.id] = review
        try:
            with anyio.move_on_after(review.wait):
                await review.done.wait()
        finally:
            self._pending.pop(review.id, None)
        if review.decision is not None:
            return review.decision
        return Decision("send" if timeout == "send" else "local", timed_out=True)

    def decide(self, rid: str, action: str, text: str | None = None) -> str | None:
        """Answer review `rid`; None when it worked, else why not."""
        review = self._pending.get(rid)
        if review is None:
            return "no such review: it was answered, or ran out of time"
        if action not in ACTIONS:
            return f"action must be one of {', '.join(ACTIONS)}"
        if action == "edit" and not (text or "").strip():
            return "an edit needs its text"
        review.decision = Decision(action, text.strip() if action == "edit" and text else None)
        review.done.set()
        return None
