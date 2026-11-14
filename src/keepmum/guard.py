"""The prompt-injection guard: an optional check of tool results for planted instructions.

Off by default, and its model is downloaded only by `keepmum guard setup injection`. It is
Horizon-Labs' Prompt Injection Guard Small (v2.3, Apache-2.0, on mmBERT-small), the best local model
in the benchmark (bench/results/GUARD.md), as a GGUF that keepmum converted (bench/horizon_gguf.py)
and hosts at huggingface.co/keepmum/prompt-injection-guard-small-gguf, pinned to a commit and checked
against SHA-256s. It scores like the original (bench/results/GUARD.md, "Horizon-Labs Small as a GGUF").

What it scans: the text of tool results (Messages tool_result, Responses function_call_output,
Gemini functionResponse) that a request brings for the first time. Clients resend the whole
conversation, so text seen before is not scored again; a result once flagged stays flagged.

    warn   the request goes out at once, and the check runs after it (no added delay); a hit is
           logged, counted in the stats feed and audited, once
    block  the check runs first; the request is refused, and so is every later one that still
           carries the flagged result

The model runs in llama-server (llamacpp.py) on Metal, started on the first check and stopped after
IDLE_S unused (`Idle`), so it takes memory only while agents are working. `Guard` is shared with
the harm check (harm.py).

Text is normalized first, as the model's own tokenizer does (`normalize`: smuggled text decoded,
NFKC, invisible characters removed), then scored in 2,048-token windows (at most 16: 12 from the
start and 4 from the end); a text gets its highest window. A result flags when its score reaches the
threshold, Medium's 0.8 by default; one under SHORT_CHARS must reach a much higher one (`short_threshold`).
On real coding-agent sessions (bench/results/guard-public-*.md) short results were most of the false
alarms: a grep line, an error, an agent's own notice. Few published attacks are that short, and a line
planted in a repository usually arrives inside a longer file.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
import time
import unicodedata
from pathlib import Path
from typing import NamedTuple

from .detect.pfserve import data_dir
from .llamacpp import INSTALL_HINT, Server, find_server

# The GGUF keepmum runs: keepmum's fine-tune of Horizon-Labs' release on coding-agent tool output
# (bench/ft_data.py, bench/ft_train.py; bench/results/GUARD.md), converted by bench/horizon_gguf.py.
REPO = "keepmum/prompt-injection-guard-small-ft-gguf"
REVISION = "5f2c815c67019db09de67f196cf30c9a0f74df87"
SOURCE = "Horizon-Labs/prompt-injection-guard-small"  # what it was fine-tuned from
SOURCE_REVISION = "2efc2ed5abecd21eb45c2506d231a65d6e392b85"
RELEASE = "v2.3-km1"
LICENCE = "Apache-2.0"
ATTRIBUTION = "Prompt Injection Guard Small v2.3 by Horizon Labs, fine-tuned by keepmum (v2.3-km1), Apache-2.0"
GGUF = "prompt-injection-guard-small-v2.3-km1.Q8_0.gguf"
# Path in the repo: (SHA-256, bytes), checked on download. LICENSE and NOTICE are Horizon Labs' own.
FILES = {
    GGUF: ("306b1bf5062ef25b0508c26bc3431318cd3d594f9c3c10b248c19a9e5e566926", 164_357_760),
    "LICENSE": ("cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30", 11_358),
    "NOTICE": ("5ffe2383027c7510933df2e7055440edde8c96b67b7a58fb477f9632c72b9c14", 1_566),
}
# What earlier keepmum versions downloaded: Shield Small (ZeroLeaks, CC BY-NC 4.0), then Horizon-Labs'
# model unchanged (v2.3). Removed on setup; with the check on, keepmum downloads this one in their place.
LEGACY = ("shield-small-s15e", "prompt-injection-guard-small-v2.3")
MODES = ("off", "warn", "block", "local")  # local: local.py
THRESHOLD = 0.222  # Medium
RANGE = (0.05, 0.45)  # a threshold one can set; the model's scores for injections mostly sit above 0.2
# v2.3's levels, whose thresholds a config file may still hold: each means the level of the same name.
PREVIOUS_LEVELS = {0.65: "high", 0.8: "medium", 0.9: "low", 0.98: "lowest"}


class Level(NamedTuple):
    threshold: float  # the score at which a tool result flags
    short: float  # the same, for one under SHORT_CHARS
    caught: float  # bench/results/GUARD.md: of its 144 injected documents
    ordinary: float  # of its 129 ordinary documents, flagged
    security: float  # of its 151 security documents (writing and code that quotes attacks), flagged
    sessions: float  # bench/results/guard-public-*.md: of 2,000 tool results from real Claude Code and Codex sessions, flagged


# Sensitivity levels for the app and `keepmum guard sensitivity injection`, measured on the GGUF in
# keepmum's windows. Each flags as many of the 2,000 session results as v2.3's level of the same name,
# long and short apart (bench/guard_levels.py). GUARD.md's documents are all longer than SHORT_CHARS.
LEVELS = {"high": Level(0.185, 0.185, 0.972, 0.023, 0.04, 0.026),
          "medium": Level(0.222, 0.222, 0.972, 0.023, 0.033, 0.015),
          "low": Level(0.241, 0.354, 0.972, 0.023, 0.033, 0.009),
          "lowest": Level(0.298, 0.354, 0.972, 0.016, 0.026, 0.004)}
SHORT_CHARS = 200  # a tool result shorter than this needs its level's `short` score to flag


def from_previous(threshold: float) -> float:
    """A threshold from a config file: one on v2.3's scale (0.5 or more) becomes this model's level of the
    same name, or of the nearest v2.3 level for a custom value; any other is kept."""
    if threshold < 0.5:
        return threshold
    nearest = min(PREVIOUS_LEVELS, key=lambda t: abs(t - threshold))
    return LEVELS[PREVIOUS_LEVELS[nearest]].threshold


def short_threshold(threshold: float) -> float:
    """The score a result under SHORT_CHARS needs: the `short` of the strictest level whose threshold is at
    or below `threshold` (High's below High), and never less than `threshold` itself."""
    bars = [v.short for v in LEVELS.values() if v.threshold <= threshold]
    return max(threshold, max(bars, default=LEVELS["high"].short))


WINDOW, OVERLAP, MAX_WINDOWS, TAIL_WINDOWS = 2048, 256, 16, 4
BOS, EOS = 2, 1  # <bos>, <eos>
BATCH = 1  # windows per request to llama-server: one at a time is the fastest here, and the smallest
MIN_CHARS = 40  # shorter tool results ("ok", an exit code) carry no instructions worth scoring
INFO = f"""The prompt-injection check uses Prompt Injection Guard Small (v2.3) by Horizon Labs,
licensed {LICENCE}: https://huggingface.co/{SOURCE}
keepmum downloads its own fine-tune of it on coding-agent tool output ({RELEASE}), which runs in
llama.cpp and which Horizon Labs does not endorse: {sum(size for _, size in FILES.values()) / 1e6:.0f} MB from
huggingface.co/{REPO}, checked against pinned SHA-256s."""

# The model's tokenizer normalizer (its tokenizer.json), which llama.cpp does not run: text smuggled
# in Unicode tag characters or variation selectors decoded to ASCII, NFKC, then invisible and
# formatting characters removed.
_SMUGGLED = {cp: chr(cp - 0xE0000) for cp in range(0xE0020, 0xE007F)} | {cp: chr(cp - 0xE00F0) for cp in range(0xE0110, 0xE016F)}
_INVISIBLE = re.compile("[\xad\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\ufe00-\ufe0f"
                        "\U000e0000-\U000e007f\U000e0100-\U000e01ef\u0305\u0331-\u0338\u20d0-\u20ff]")


def normalize(text: str) -> str:
    return _INVISIBLE.sub("", unicodedata.normalize("NFKC", text.translate(_SMUGGLED)))


class GuardError(RuntimeError):
    pass


def model_dir() -> Path:
    return data_dir() / "guard" / f"prompt-injection-guard-small-{RELEASE}"


def model_path() -> Path:
    return model_dir() / GGUF


def installed() -> bool:
    """Every pinned file is in place at its size."""
    d = model_dir()
    return all((d / p).is_file() and (d / p).stat().st_size == size for p, (_, size) in FILES.items())


def outdated() -> bool:
    """A model an earlier keepmum version downloaded is here, and this one is not: setup replaces it."""
    return not installed() and any((data_dir() / "guard" / old).is_dir() for old in LEGACY)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _remove_legacy() -> None:
    import shutil

    for old in LEGACY:
        p = data_dir() / "guard" / old
        if p.is_dir():
            shutil.rmtree(p)


def download(progress=None) -> Path:
    """Fetch every pinned file into model_dir(), checking size and SHA-256."""
    import httpx

    _remove_legacy()
    d = model_dir()
    total, done = sum(size for _, size in FILES.values()), 0
    for rel, (sha, size) in FILES.items():
        dest = d / rel
        if dest.is_file() and dest.stat().st_size == size and _sha256(dest) == sha:
            done += size
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        h, got = hashlib.sha256(), 0
        try:
            url = f"https://huggingface.co/{REPO}/resolve/{REVISION}/{rel}"
            with httpx.stream("GET", url, follow_redirects=True, timeout=60) as r:
                r.raise_for_status()
                with open(part, "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
                        h.update(chunk)
                        got += len(chunk)
                        if progress:
                            progress(done + got, total)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        if got != size or h.hexdigest() != sha:
            part.unlink(missing_ok=True)
            raise GuardError(f"{rel} does not match its pinned size and SHA-256")
        part.replace(dest)
        done += size
    return d


def remove() -> list[Path]:
    """Delete the model, and any model an earlier keepmum version downloaded."""
    import shutil

    gone = [p for p in [model_dir(), *(data_dir() / "guard" / old for old in LEGACY)] if p.exists()]
    for p in gone:
        shutil.rmtree(p)
    return gone


IDLE_S = 600.0  # a model unused this long is unloaded, and loads again on the next check


class Idle:
    """Unloads a model after IDLE_S without use, so a guard costs memory only while agents are
    working. A daemon thread checks once a minute; `unload()` is the model's own."""

    idle_s = IDLE_S

    def _touch(self) -> None:
        self._used = time.monotonic()
        if getattr(self, "_reaper", None) is None:
            self._reaper = threading.Thread(target=self._reap, name=f"{type(self).__name__}-idle", daemon=True)
            self._reaper.start()

    def _reap(self) -> None:
        while True:
            time.sleep(min(60.0, self.idle_s))
            with self._lock:
                if self.loaded() and time.monotonic() - self._used >= self.idle_s:
                    self.unload()


def windows(ids: list[int], window: int, overlap: int, most: int, tail: int) -> list[list[int]]:
    """Windows of `window` tokens (with the two special tokens added later) overlapping by `overlap`.
    A text longer than `most` of them keeps its first `most - tail` and its last `tail`."""
    body = window - 2
    step = body - overlap
    if len(ids) <= body:
        return [ids]
    starts = list(range(0, len(ids) - body + step, step))
    if len(starts) > most:
        starts = starts[:most - tail] + [len(ids) - body - k * step for k in range(tail - 1, -1, -1)]
    return [ids[s:s + body] for s in starts]


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x)) if x >= 0 else math.exp(x) / (1 + math.exp(x))


class InjectionModel(Idle):
    """Prompt Injection Guard Small in llama-server (Metal), started on first use and stopped when
    idle; calls are serialized. `model` is the GGUF (default: the one `keepmum guard setup injection`
    downloaded)."""

    label = f"Prompt Injection Guard Small {RELEASE}"

    def __init__(self, model: Path | None = None, server: Path | None = None):
        self.model, self.server = model, server
        self._lock = threading.Lock()
        self._server: Server | None = None

    def loaded(self) -> bool:
        return self._server is not None and self._server.running()

    def unload(self) -> None:
        if self._server is not None:
            self._server.stop()
        self._server = None

    def _start(self) -> None:
        server = self.server or find_server()
        if server is None:
            raise GuardError(f"llama-server is not installed ({INSTALL_HINT})")
        model = self.model or model_path()
        if not model.is_file():
            raise GuardError("the injection model is not set up: run `keepmum guard setup injection`")
        # A slot per window of a batch, all in one micro-batch: a window must fit in one (non-causal attention).
        self._server = Server(server, model, ["--embedding", "-c", str(WINDOW * BATCH), "--parallel", str(BATCH),
                                              "-b", str(WINDOW * BATCH), "-ub", str(WINDOW * BATCH), "--cache-ram", "0"], GuardError)
        self._server.start()

    def tokens(self, text: str) -> list[int]:
        """The model's token ids for `text`, without <bos> and <eos>: llama.cpp's tokenizer on the
        normalized text, as the model's own tokenizer gives."""
        return self._server.post("/tokenize", {"content": normalize(text), "add_special": False})["tokens"]

    @staticmethod
    def windows(ids: list[int]) -> list[list[int]]:
        return windows(ids, WINDOW, OVERLAP, MAX_WINDOWS, TAIL_WINDOWS)

    def score(self, text: str) -> float:
        """P(INJECTION) of the text's most likely window."""
        with self._lock:
            if not self.loaded():
                self._start()
            self._touch()
            best = 0.0
            bodies = self.windows(self.tokens(text))
            for i in range(0, len(bodies), BATCH):
                rows = [[BOS, *b, EOS] for b in bodies[i:i + BATCH]]
                out = self._server.post("/embeddings", {"content": rows, "embd_normalize": -1})
                for e in out:
                    safe, injection = e["embedding"][0]  # the classifier's logits: [SAFE, INJECTION]
                    best = max(best, sigmoid(injection - safe))  # softmax over two labels: a sigmoid of the margin
            return best


# A piece of a flagged result that comes back in a later one (a line of it, or part of a line) is
# flagged as that result was: a live model, refused a README, printed only its backtick commands
# (`curl -s https://…/bootstrap.sh | sh`), which said nothing to the check on their own.
PIECE_CHARS = 20
CARRIED = 64  # flagged texts kept for this
_LINE_NUMBER = re.compile(r"^\s*\d+\s*(?:[:|\t\u2192-]\s?)")


def _flat(text: str) -> list[str]:
    """Each line of text, its spacing squeezed and any line number (`cat -n`, `rg -n`, Read's →) cut."""
    return [" ".join(_LINE_NUMBER.sub("", line, count=1).split()) for line in text.splitlines()]


def _key(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).digest()


class Guard:
    """One check (`name`: "injection" or "harm") over the texts of one request. `mode` is a callable
    (the live config). The model has score(text) -> float, and may have categories(text) -> list,
    asked only for a new hit."""

    def __init__(self, mode, model=None, threshold: float = THRESHOLD, keep: int = 4096, name: str = "injection",
                 min_chars: int = MIN_CHARS, short_chars: int = 0):
        self._mode = mode if callable(mode) else (lambda: mode)
        self.model = model if model is not None else InjectionModel()
        self.threshold = threshold
        self.name = name
        self.min_chars = min_chars
        self.short_chars = short_chars  # the injection check's SHORT_CHARS; 0: one threshold for every length
        self._flagged: dict[bytes, dict] = {}  # text hash -> the hit, for texts flagged earlier
        # The injection check's flagged texts, flattened, with their hits (newest last): their pieces
        # are flagged too when a later result repeats them. Kept in memory only, like the hits.
        self.carry = name == "injection"
        self._sources: list[tuple[str, dict]] = []
        self._keep = keep
        self._lock = threading.Lock()

    def threshold_for(self, chars: int) -> float:
        return short_threshold(self.threshold) if chars < self.short_chars else self.threshold

    @property
    def mode(self) -> str:
        m = self._mode()
        return m if m in MODES else "off"

    def check(self, items: list[tuple[str | None, str, bool]], earlier: bool = True) -> list[dict]:
        """Hits among (source, text, new) items: new text is scored. A text flagged before is a hit
        again with `earlier` (block mode refuses every request that still carries it); warn mode
        leaves it out, so one hit is one warning. A hit holds the check, the source (a tool, or None
        for what the user typed), the score, the length and any categories, and `at`, the item it is
        for; never the text."""
        hits = []
        for at, (source, text, new) in enumerate(items):
            key = _key(text)
            with self._lock:
                seen = self._flagged.get(key)
            if seen is not None and seen["score"] < self.threshold_for(seen["chars"]):
                seen = None  # flagged under a lower threshold than now: no longer a hit
            if seen is not None:
                if earlier:
                    hits.append({**seen, "source": source, "earlier": True, "at": at})
                continue
            if not new or len(text) < self.min_chars:
                continue
            s = self.model.score(text)
            carried = None
            if s < self.threshold_for(len(text)):
                carried = self._carried(text) if self.carry else None
                if carried is None:
                    continue
                s = carried["score"]
            hit = {"check": self.name, "source": source, "score": s, "chars": len(text), "earlier": False}
            if carried is not None:
                hit["carried"] = True  # it repeats part of a result flagged before
            categories = getattr(self.model, "categories", None)
            if carried is not None:
                hit["categories"] = list(carried.get("categories") or [])
            elif categories is not None:
                try:
                    hit["categories"] = categories(text)
                except Exception:  # the verdict stands without its categories
                    hit["categories"] = []
            with self._lock:
                if len(self._flagged) >= self._keep:
                    self._flagged.pop(next(iter(self._flagged)))
                self._flagged[key] = hit
                if self.carry and carried is None:
                    self._sources = [*self._sources[-(CARRIED - 1):], ("\n".join(_flat(text)), hit)]
            hits.append({**hit, "at": at})  # which of the items: the Logs page shows its text
        return hits

    def _carried(self, text: str) -> dict | None:
        """The hit of a flagged text that a line of text (20 characters or more) repeats, or None."""
        with self._lock:
            sources = list(self._sources)
        if not sources:
            return None
        for piece in _flat(text):
            if len(piece) < PIECE_CHARS:
                continue
            for flat, hit in reversed(sources):
                if piece in flat:
                    return hit
        return None
