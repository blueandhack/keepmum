"""Counters for the stats feed the Mac app reads. Counts, types and timestamps only.

Nothing here may ever hold a value or a placeholder: the feed is read by another process.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from collections import Counter, deque
from pathlib import Path
from urllib.parse import urlsplit

HISTORY_KEEP = 20_000  # events kept when the file is compacted
FLIGHT_STALE = 30 * 60  # seconds before a request still out at a provider is assumed lost


def token_file() -> Path:
    return Path.home() / ".config" / "keepmum" / "stats-token"


def history_file() -> Path:
    """Every request's event (counts only), kept across restarts for the app's Activity page."""
    return Path.home() / ".config" / "keepmum" / "history.jsonl"


def stats_token(path: Path | None = None) -> str:
    """A bearer token for the stats endpoint, in a file only this user can read."""
    path = path or token_file()
    if path.exists():
        return path.read_text().strip()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = secrets.token_urlsafe(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    with os.fdopen(fd, "w") as f:
        f.write(token)
    return token


class History:
    """An append-only JSONL file of events, shared by every keepmum process on this Mac (0600).
    Lines are small, so appends from several processes do not interleave."""

    def __init__(self, path: Path | None = None, keep: int = HISTORY_KEEP):
        self.path, self.keep = path or history_file(), keep
        self._appended = 0

    def append(self, event: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(json.dumps(event, separators=(",", ":")) + "\n")
            self._appended += 1
            if self._appended % 1000 == 0:
                self._compact()
        except OSError:
            pass  # history is a convenience; never fail a request over it

    def _compact(self) -> None:
        lines = self.path.read_text().splitlines()
        if len(lines) > self.keep * 1.5:
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write("\n".join(lines[-self.keep:]) + "\n")
            tmp.replace(self.path)

    def events(self) -> list[dict]:
        try:
            lines = self.path.read_text().splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def page(self, before: float | None = None, limit: int = 100, outcome: str | None = None) -> list[dict]:
        """Events older than `before`, newest first. `outcome`: masked, clean, blocked, observed or flagged."""
        try:
            lines = self.path.read_text().splitlines()
        except OSError:
            return []
        out = []
        for line in reversed(lines):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if before is not None and e.get("ts", 0) >= before:
                continue
            # "flagged" pages every event a check flagged: warned after sending, or blocked.
            if outcome == "flagged" and e.get("guard"):
                pass
            elif outcome and event_kind(e) != outcome:
                continue
            out.append(e)
            if len(out) >= limit:
                break
        return out


# Coding agents keepmum can tell apart by their User-Agent: (prefix, id, name).
CLIENTS = (
    ("claude-cli", "claude-code", "Claude Code"),
    ("claude-code", "claude-code", "Claude Code"),
    ("codex", "codex", "Codex"),  # codex_exec/…, codex_cli_rs/…
    ("pi-coding-agent", "pi", "pi"),
    ("pi/", "pi", "pi"),
    ("opencode", "opencode", "opencode"),
    ("geminicli", "gemini", "Gemini CLI"),  # GeminiCLI/0.66.0/…, GeminiCLI-acp-…
    ("antigravity", "antigravity", "Antigravity"),  # antigravity/cli/1.5.3 (…)
)
PROVIDERS = {"anthropic": "Anthropic", "openai": "OpenAI", "google": "Google"}
# Events from before keepmum recorded clients came from Claude Code, the only client it served.
DEFAULT_CLIENT = "claude-code"


def client_of(user_agent: str) -> str:
    ua = user_agent.strip().lower()
    for prefix, cid, _ in CLIENTS:
        if ua.startswith(prefix):
            return cid
    first = re.split(r"[/\s(]", ua, maxsplit=1)[0]
    return re.sub(r"[^a-z0-9._-]", "", first)[:32] or "unknown"


def client_name(cid: str) -> str:
    return next((name for _, i, name in CLIENTS if i == cid), "Other" if cid == "unknown" else cid)


def provider_of(upstream: str) -> str:
    host = (urlsplit(upstream).hostname or "").lower()
    if host.endswith("anthropic.com"):
        return "anthropic"
    if host.endswith("openai.com") or host.endswith("chatgpt.com"):
        return "openai"
    if host.endswith("googleapis.com"):
        return "google"
    # Another gateway: its address, with the port when it runs on this Mac (127.0.0.1:3425).
    try:
        port = urlsplit(upstream).port
    except ValueError:
        port = None
    return f"{host}:{port}" if port and host in ("127.0.0.1", "localhost", "::1") else host or "unknown"


def flows(events, since: float) -> dict:
    """Totals for the app's flow diagram over events newer than `since`: requests from each
    client through keepmum's result to each provider, and replies back through restore.

    The optional checks have three results of their own: "refused" (block mode: never sent),
    "local" (local mode: answered by the local model, never sent) and "flagged" (warn mode: sent,
    then a check flagged it). A warning is an event of its own, after
    the request's: it moves its request out of masked, clean or observed into flagged, once per
    request, and only when that request is counted (a check may finish before a streamed reply).
    `checks` counts each result's requests by check (injection, harm)."""
    out, back = Counter(), Counter()
    values, unresolved = Counter(), 0
    checks = {"flagged": Counter(), "refused": Counter(), "local": Counter()}
    warned: dict[str, dict] = {}  # request id (or the event's own place) -> its warnings
    for i, e in enumerate(events):
        if e.get("ts", 0) < since:
            continue
        outcome = e.get("outcome")
        if outcome in ("forwarded", "observed") and e.get("scan_ms") is None:
            continue  # bodyless checks such as /api/hello: no content went anywhere
        client = e.get("client") or DEFAULT_CLIENT
        provider = e.get("provider") or "anthropic"
        if outcome == "flagged":  # a check's note on a request counted on its own
            w = warned.setdefault(e.get("request") or f"#{i}", {"client": client, "provider": provider,
                                                              "kind": e.get("kind"), "checks": set()})
            w["checks"].update(g.get("check", "injection") for g in e.get("guard") or [])
            continue
        if outcome == "upstream_error":
            out[(client, None, "failed")] += 1
            continue
        kind = event_kind(e)
        if kind == "local":  # answered by the local model (local.py): it never left this Mac
            out[(client, None, "local")] += 1
            checks["local"].update({g.get("check", "injection") for g in e.get("guard") or []})
            continue
        if kind == "blocked":
            if e.get("guard"):
                out[(client, None, "refused")] += 1
                checks["refused"].update({g.get("check", "injection") for g in e["guard"]})
            else:
                out[(client, None, "blocked")] += 1
            continue
        out[(client, provider, kind)] += 1
        n = sum((e.get("restored") or {}).values())
        back[(provider, client, n > 0)] += 1
        values[(provider, client)] += n
        unresolved += e.get("unresolved") or 0
    for w in warned.values():
        key = (w["client"], w["provider"], w["kind"])
        if out[key] > 0:
            out[key] -= 1
            out[(w["client"], w["provider"], "flagged")] += 1
            checks["flagged"].update(w["checks"])
    out = +out  # without the counts a warning moved to zero
    clients = Counter()
    for (c, _, _), n in out.items():
        clients[c] += n
    providers = Counter()
    for (p, _, _), n in back.items():
        providers[p] += n
    return {
        "clients": [{"id": c, "name": client_name(c), "sent": n} for c, n in clients.most_common()],
        "providers": [{"id": p, "name": PROVIDERS.get(p, p), "replies": n} for p, n in providers.most_common()],
        "out": [{"client": c, "provider": p, "kind": k, "n": n} for (c, p, k), n in sorted(out.items(), key=str)],
        "back": [{"provider": p, "client": c, "restored": r, "n": n, "values": values[(p, c)] if r else 0}
                 for (p, c, r), n in sorted(back.items(), key=str)],
        "unresolved": unresolved,
        "checks": {k: dict(v) for k, v in checks.items() if v},
    }


def guard_summary(hits: list[dict], action: str) -> list[dict]:
    """A check's hits as an event carries them: never the text."""
    return [{"check": h.get("check", "injection"), "action": action, "source": h.get("source"),
             "score": round(h["score"], 3), "categories": h.get("categories") or None} for h in hits]


def event_kind(e: dict) -> str:
    """How the app labels an event: blocked, observed, flagged (a check's hit after the request was
    sent), masked (new sensitive text) or clean."""
    if e.get("outcome") == "flagged":
        return "flagged"
    if e.get("outcome") == "local":
        return "local"
    if e.get("outcome") in ("blocked", "rejected"):
        return "blocked"
    if e.get("outcome") == "observed":
        return "observed"
    return "masked" if e.get("new") else "clean"


class Stats:
    def __init__(self, recent: int = 200, history: History | None = None, findings: int = 2000):
        self._lock = threading.Lock()
        self.history = history
        self.started = time.time()
        self.requests = Counter()  # by outcome: forwarded, blocked, rejected, upstream_error
        self.redacted = Counter()  # by type
        self.restored = Counter()  # by type
        self.scan_ms: deque[float] = deque(maxlen=1000)
        self.events: deque[dict] = deque(maxlen=recent)
        self.scanned = 0  # requests that went through the detectors
        self.with_findings = 0  # of those, requests where something was masked
        self.with_new = 0  # of those, requests whose new text (not resent history) had something to mask
        self.unresolved = 0  # placeholders in replies that this process could not restore
        self.minutes: deque[dict] = deque(maxlen=180)  # per-minute buckets for the timeline
        # The Logs page: what new text held, one entry per masked value. In memory only, never in
        # the history file: a preview is the first and last character of a value.
        self.findings: deque[dict] = deque(maxlen=findings)
        # The guards (guard.py, harm.py, commands.py): what they flagged. Check, source, score,
        # length, categories; no text, so the stats feed stays small.
        self.guard_hits: deque[dict] = deque(maxlen=recent)
        # With [log] context on: each hit's text and the user's prompt, every masked value shown as
        # its type, by the hit's seq. In memory only, for the Logs page's check details.
        self.check_texts: dict[int, dict] = {}
        self._check_seq = 0
        self.guarded = Counter()  # requests with a hit, by action: warned, blocked
        # Requests on their way to a provider, by flight number, until their reply is done: the
        # dashboard draws them waiting there. Client, provider and kind; never content.
        self.inflight: dict[int, dict] = {}
        self._flight = 0

    def depart(self, origin: tuple[str, str] | None, kind: str) -> int:
        """A scanned request is leaving for its provider: its number, which `record` closes."""
        with self._lock:
            self._flight += 1
            client, provider = origin or (None, None)
            self.inflight[self._flight] = {"id": self._flight, "ts": round(time.time(), 6), "client": client,
                                           "provider": provider, "kind": kind}
            return self._flight

    def live(self, after: float = 0.0) -> dict:
        """The dashboard's fast poll: requests out at a provider now, and events newer than `after`."""
        with self._lock:
            now = time.time()
            # A request that never recorded (a crash between leaving and its reply) stops being drawn.
            for n in [n for n, f in self.inflight.items() if now - f["ts"] > FLIGHT_STALE]:
                del self.inflight[n]
            return {"now": round(now, 6), "inflight": list(self.inflight.values()),
                    "recent": [e for e in self.events if e["ts"] > after]}

    def record(self, path: str, outcome: str, status: int, scan_ms: float | None = None,
               redacted: Counter | None = None, restored: Counter | None = None, new: int = 0,
               origin: tuple[str, str] | None = None, tools: list[dict] | None = None,
               guard: list[dict] | None = None, flight: int | None = None) -> None:
        from .restore import UNRESOLVED

        restored = Counter(restored or {})
        unresolved = restored.pop(UNRESOLVED, 0)
        with self._lock:
            self.unresolved += unresolved
            self.requests[outcome] += 1
            self.redacted.update(redacted or {})
            self.restored.update(restored or {})
            if scan_ms is not None:
                self.scan_ms.append(scan_ms)
                self.scanned += 1
                self.with_findings += bool(redacted)
                self.with_new += new > 0
            minute = int(time.time() // 60) * 60
            if not self.minutes or self.minutes[-1]["t"] != minute:
                self.minutes.append({"t": minute, "requests": 0, "masked": Counter(), "restored": 0, "blocked": 0})
            bucket = self.minutes[-1]
            bucket["requests"] += 1
            bucket["masked"].update(redacted or {})
            bucket["restored"] += sum((restored or {}).values())
            bucket["blocked"] += outcome in ("blocked", "rejected")
            event = {
                "ts": round(time.time(), 6), "path": path, "outcome": outcome, "status": status,
                "scan_ms": None if scan_ms is None else round(scan_ms, 1),
                "redacted": dict(redacted or {}), "restored": dict(restored), "new": new,
                "unresolved": unresolved,
            }
            if origin:
                event["client"], event["provider"] = origin
            if tools:  # tool calls whose input held placeholders: names, types and counts, never values
                event["tools"] = tools
            if guard:  # a check's hits: check, action, source, score, categories, never the text
                event["guard"] = guard
            kept = event
            if flight is not None:  # its reply is done: no longer out at the provider
                self.inflight.pop(flight, None)
                # Flight numbers start over with each run, so the history file does not get them.
                event = {**event, "flight": flight}
            self.events.append(event)
        if self.history:
            self.history.append(kept)

    def record_findings(self, found: list[dict], outcome: str = "masked", origin: tuple[str, str] | None = None) -> None:
        ts = round(time.time(), 6)
        client, provider = origin or (None, None)
        with self._lock:
            for i, f in enumerate(found):
                self.findings.append({**f, "ts": ts, "seq": i, "outcome": outcome, "client": client, "provider": provider})

    def record_flagged(self, path: str, hits: list[dict], origin: tuple[str, str] | None = None,
                       kind: str = "clean", request: str | None = None, action: str = "warned") -> None:
        """A warn-mode check's hits, found after its request was sent: an event of their own. `kind`
        is how that request went out (masked, clean, observed), and `request` an id shared by the
        hits of every check on it, so the flow diagram moves the request to Flagged once. `action`
        is "blocked" for a command in the reply the commands check refused (commands.py)."""
        event = {"ts": round(time.time(), 6), "path": path, "outcome": "flagged", "status": 0, "scan_ms": None,
                 "redacted": {}, "restored": {}, "new": 0, "unresolved": 0, "guard": guard_summary(hits, action),
                 "kind": kind}
        if request:
            event["request"] = request
        if origin:
            event["client"], event["provider"] = origin
        with self._lock:
            self.events.append(event)
        if self.history:
            self.history.append(event)

    def record_guard(self, hits: list[dict], action: str, origin: tuple[str, str] | None = None,
                     about: dict | None = None, shown: list[tuple[str | None, str | None]] | None = None) -> list[dict]:
        """The Logs page's check hits: what was flagged where, the score, the length and the
        categories; and `about` the check (mode, model, threshold, path). `shown`, per hit, is the
        flagged text and the user's prompt with every masked value as its type ([log] context),
        kept apart in check_texts and served only with the findings. Returns the entries, for
        local mode to add which local server answered (`note_guard`)."""
        ts = round(time.time(), 6)
        client, provider = origin or (None, None)
        added = []
        with self._lock:
            self.guarded[action] += 1
            for i, h in enumerate(hits):
                self._check_seq += 1
                added.append({"ts": ts, "seq": self._check_seq, "check": h.get("check", "injection"),
                              "source": h.get("source"), "score": round(h["score"], 3), "chars": h["chars"],
                              "categories": h.get("categories"), "earlier": h.get("earlier", False),
                              "action": action, "client": client, "provider": provider, **(about or {})})
                text, prompt = (shown[i] if shown and i < len(shown) else (None, None))
                if text or prompt:
                    self.check_texts[self._check_seq] = {"text": text, "prompt": prompt}
            self.guard_hits.extend(added)
            # The texts go with their hits: those the list no longer holds are forgotten.
            oldest = self.guard_hits[0]["seq"] if self.guard_hits else self._check_seq + 1
            for seq in [k for k in self.check_texts if k < oldest]:
                del self.check_texts[seq]
        return added

    def check_texts_page(self) -> dict[str, dict]:
        """The flagged texts and prompts still kept, by hit (`seq`), for the Logs page."""
        with self._lock:
            return {str(k): dict(v) for k, v in self.check_texts.items()}

    def note_guard(self, entries: list[dict], **facts) -> None:
        """Add facts learned after a hit was recorded: the local server that answered, or why none did."""
        with self._lock:
            for e in entries:
                e.update(facts)

    def findings_page(self, limit: int = 2000) -> list[dict]:
        with self._lock:
            return sorted(self.findings, key=lambda f: (-f["ts"], f["seq"]))[:limit]  # newest request first, in text order

    def drop_previews(self) -> None:
        """[log] previews turned off: forget the characters already kept."""
        with self._lock:
            for f in self.findings:
                f["preview"] = None

    def drop_context(self) -> None:
        """[log] context turned off: forget the text kept around each value, and the checks' texts."""
        with self._lock:
            for f in self.findings:
                f["before"] = f["after"] = None
            self.check_texts.clear()

    def snapshot(self, **extra) -> dict:
        with self._lock:
            lat = sorted(self.scan_ms)

            def pct(q):
                return round(lat[min(len(lat) - 1, int(q * len(lat)))], 1) if lat else None

            return {
                "started": self.started, "uptime_s": round(time.time() - self.started, 1),
                "requests": dict(self.requests), "redacted": dict(self.redacted), "restored": dict(self.restored),
                # clean: scanned requests whose new text held nothing to mask (resent history may).
                "funnel": {"requests": sum(self.requests.values()), "scanned": self.scanned,
                           "clean": self.scanned - self.with_new,
                           "with_findings": self.with_findings, "with_new": self.with_new,
                           "masked": sum(self.redacted.values()),
                           "restored": sum(self.restored.values()), "unresolved": self.unresolved},
                "timeline": [{**b, "masked": dict(b["masked"])} for b in self.minutes],
                "scan_ms": {"p50": pct(0.5), "p95": pct(0.95), "n": len(lat)},
                "guard": {"requests": dict(self.guarded), "hits": list(self.guard_hits)},
                "recent": list(self.events), **extra,
            }
