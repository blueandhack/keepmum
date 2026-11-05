"""The prompt-injection guard: an optional check of tool results for planted instructions.

Off by default, and its model is never bundled. Shield Small (ZeroLeaks, release s15e) is the only
local model that worked in the benchmark (bench/results/GUARD.md), and its weights are licensed
CC BY-NC 4.0: non-commercial use only. keepmum runs an unofficial GGUF conversion of it in llama.cpp
(bench/shield_gguf.py; hosted at huggingface.co/keepmum/shield-small-gguf, same licence), which
scores like ZeroLeaks' int8 ONNX (bench/results/GUARD.md, "as a GGUF"). `keepmum guard setup
injection` shows the terms, asks the user to confirm their use is non-commercial, then downloads the
files at a pinned commit and checks each one's SHA-256. keepmum's own code stays Apache-2.0; the
licence is between the user and ZeroLeaks.

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

Scores come from 256-token windows (stride 192, at most 32 windows: 28 from the start and 4 from
the end, as the model card describes); a text gets its highest window. The threshold, 0.945, is
the one the benchmark set on dev data for the GGUF (at most 2% of ordinary documents flagged).
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from pathlib import Path

from .detect.pfserve import data_dir
from .llamacpp import INSTALL_HINT, Server, find_server

# The GGUF keepmum runs, converted from ZeroLeaks' release by bench/shield_gguf.py.
REPO = "keepmum/shield-small-gguf"
REVISION = "6c0fbb6604a8f4071f4e3c07a5aa194714057a8a"
SOURCE = "ZeroLeaks/shield-small"  # what it was converted from
SOURCE_REVISION = "f6d60339a424e17b60272f2d17c4e68995e933cf"  # the s15e tag
RELEASE = "s15e"
LICENCE = "CC-BY-NC-4.0"
ATTRIBUTION = "Shield Small s15e by ZeroLeaks (https://zeroleaks.ai/shield), licensed CC BY-NC 4.0"
GGUF = "shield-small-s15e.Q8_0.gguf"
# Path in the repo: (SHA-256, bytes), checked on download. LICENSE and NOTICE are ZeroLeaks' own.
FILES = {
    GGUF: ("8429b324eff78354df7486ad64c9ac72a4270732076b0dc1650b6eb8836728ea", 132_601_088),
    "LICENSE": ("41003d4a74749c0220e33dd415042164b5a1093ed401f36277234f772d22d3d0", 19_347),
    "NOTICE": ("6b2a254eb3d02d38aacf9c2ecbc822608be87bd430a7d24c685f1e5d0dc8d32e", 1_191),
}
# What earlier keepmum versions downloaded (ZeroLeaks' int8 ONNX, for ONNX Runtime): removed on setup.
LEGACY = ("onnx", "tokenizer.json", "tokenizer_config.json", "config.json")
MODES = ("off", "warn", "block")
THRESHOLD = 0.945
WINDOW, STRIDE, MAX_WINDOWS = 256, 192, 32
BOS, EOS, SPACE = 0, 2, 6  # <s>, </s>, and the ▁ that Shield's tokenizer ends text ending in whitespace with
BATCH = 8  # windows per request to llama-server
MIN_CHARS = 40  # shorter tool results ("ok", an exit code) carry no instructions worth scoring
TERMS = f"""The prompt-injection guard uses Shield Small, a model by ZeroLeaks. keepmum does not ship it.
It downloads an unofficial conversion that runs in llama.cpp, which keepmum hosts; ZeroLeaks does
not endorse it.

Its weights are licensed {LICENCE} (Creative Commons Attribution-NonCommercial 4.0):
  https://huggingface.co/{SOURCE}/blob/{SOURCE_REVISION}/LICENSE
You may use them only for non-commercial purposes: not "primarily intended for or directed towards
commercial advantage or monetary compensation". Using keepmum on work for an employer or a client
is commercial use; for that you need a licence from ZeroLeaks (https://zeroleaks.ai/contact).

The download is {sum(size for _, size in FILES.values()) / 1e6:.0f} MB from huggingface.co/{REPO}, checked against pinned SHA-256s."""


class GuardError(RuntimeError):
    pass


def model_dir() -> Path:
    return data_dir() / "guard" / f"shield-small-{RELEASE}"


def acceptance_file() -> Path:
    return model_dir() / "ACCEPTED.json"


def model_path() -> Path:
    return model_dir() / GGUF


def accepted() -> dict | None:
    try:
        record = json.loads(acceptance_file().read_text())
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get("licence") == LICENCE else None


def installed() -> bool:
    """Every pinned file is in place at its size, and the licence was accepted."""
    d = model_dir()
    return accepted() is not None and all((d / p).is_file() and (d / p).stat().st_size == size
                                          for p, (_, size) in FILES.items())


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def download(progress=None) -> Path:
    """Fetch every pinned file into model_dir(), checking size and SHA-256. Records nothing about the
    licence: `accept()` does that, after the user has confirmed."""
    import httpx

    import shutil

    d = model_dir()
    for old in LEGACY:
        p = d / old
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
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


def accept(by: str) -> None:
    model_dir().mkdir(parents=True, exist_ok=True)
    acceptance_file().write_text(json.dumps({"licence": LICENCE, "model": f"{SOURCE}@{RELEASE}", "ts": round(time.time()),
                                             "by": by, "statement": "the user confirmed non-commercial use"}) + "\n")


def remove() -> bool:
    import shutil

    d = model_dir()
    if not d.exists():
        return False
    shutil.rmtree(d)
    return True


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


class Shield(Idle):
    """Shield Small in llama-server (Metal), started on first use and stopped when idle; calls are
    serialized. `model` is the GGUF (default: the one `keepmum guard setup injection` downloaded)."""

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
        # One slot per window of a batch, all in one micro-batch: a window must fit in one (non-causal
        # attention), and one pass over the whole batch is the fastest on Metal.
        self._server = Server(server, model, ["--embedding", "-c", str(WINDOW * BATCH), "--parallel", str(BATCH),
                                              "-b", str(WINDOW * BATCH), "-ub", str(WINDOW * BATCH), "--cache-ram", "0"], GuardError)
        self._server.start()

    def tokens(self, text: str) -> list[int]:
        """Shield's token ids for `text`, without <s> and </s>. llama.cpp's sentencepiece tokenizer
        matches Shield's except at the end: for text that ends in whitespace, Shield's ends with ▁."""
        ids = self._server.post("/tokenize", {"content": text, "add_special": False})["tokens"]
        if text[-1:].isspace() and ids[-1:] != [SPACE]:
            ids.append(SPACE)
        return ids

    @staticmethod
    def windows(ids: list[int]) -> list[list[int]]:
        body = WINDOW - 2  # with <s> and </s>
        if len(ids) <= body:
            return [ids]
        if len(ids) <= STRIDE * (MAX_WINDOWS - 1) + body:
            return [ids[s:s + body] for s in range(0, len(ids) - body + STRIDE, STRIDE)]
        out = [ids[s:s + body] for s in range(0, STRIDE * (MAX_WINDOWS - 4), STRIDE)]
        return out + [ids[len(ids) - k * STRIDE - body:len(ids) - k * STRIDE] for k in (3, 2, 1, 0)]

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
                    benign, injection = e["embedding"][0]  # the classifier's logits: [BENIGN, INJECTION]
                    d = injection - benign  # softmax over two labels: a sigmoid of the margin
                    best = max(best, 1 / (1 + math.exp(-d)) if d >= 0 else math.exp(d) / (1 + math.exp(d)))
            return best


def _key(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).digest()


class Guard:
    """One check (`name`: "injection" or "harm") over the texts of one request. `mode` is a callable
    (the live config). The model has score(text) -> float, and may have categories(text) -> list,
    asked only for a new hit."""

    def __init__(self, mode, model=None, threshold: float = THRESHOLD, keep: int = 4096, name: str = "injection",
                 min_chars: int = MIN_CHARS):
        self._mode = mode if callable(mode) else (lambda: mode)
        self.model = model if model is not None else Shield()
        self.threshold = threshold
        self.name = name
        self.min_chars = min_chars
        self._flagged: dict[bytes, dict] = {}  # text hash -> the hit, for texts flagged earlier
        self._keep = keep
        self._lock = threading.Lock()

    @property
    def mode(self) -> str:
        m = self._mode()
        return m if m in MODES else "off"

    def check(self, items: list[tuple[str | None, str, bool]], earlier: bool = True) -> list[dict]:
        """Hits among (source, text, new) items: new text is scored. A text flagged before is a hit
        again with `earlier` (block mode refuses every request that still carries it); warn mode
        leaves it out, so one hit is one warning. A hit holds the check, the source (a tool, or None
        for what the user typed), the score, the length and any categories, never the text."""
        hits = []
        for source, text, new in items:
            key = _key(text)
            with self._lock:
                seen = self._flagged.get(key)
            if seen is not None:
                if earlier:
                    hits.append({**seen, "source": source, "earlier": True})
                continue
            if not new or len(text) < self.min_chars:
                continue
            s = self.model.score(text)
            if s < self.threshold:
                continue
            hit = {"check": self.name, "source": source, "score": s, "chars": len(text), "earlier": False}
            categories = getattr(self.model, "categories", None)
            if categories is not None:
                try:
                    hit["categories"] = categories(text)
                except Exception:  # the verdict stands without its categories
                    hit["categories"] = []
            with self._lock:
                if len(self._flagged) >= self._keep:
                    self._flagged.pop(next(iter(self._flagged)))
                self._flagged[key] = hit
            hits.append(hit)
        return hits
