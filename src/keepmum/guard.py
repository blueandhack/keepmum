"""The prompt-injection guard: an optional check of tool results for planted instructions.

Off by default, and its model is never bundled. Shield Small (ZeroLeaks, release s15e) is the only
local model that worked in the benchmark (bench/results/GUARD.md), and its weights are licensed
CC BY-NC 4.0: non-commercial use only. `keepmum guard setup injection` shows those terms, asks the user to
confirm their use is non-commercial, then downloads the weights at a pinned commit and checks every
file's SHA-256. keepmum's own code stays Apache-2.0; the licence is between the user and ZeroLeaks.

What it scans: the text of tool results (Messages tool_result, Responses function_call_output,
Gemini functionResponse) that a request brings for the first time. Clients resend the whole
conversation, so text seen before is not scored again; a result once flagged stays flagged.

    warn   the request goes out at once, and the check runs after it (no added delay); a hit is
           logged, counted in the stats feed and audited, once
    block  the check runs first; the request is refused, and so is every later one that still
           carries the flagged result

The model loads on the first check and unloads after IDLE_S unused (`Idle`), so it takes memory
only while agents are working. `Guard` is shared with the harm check (harm.py).

Scores come from 256-token windows (stride 192, at most 32 windows: 28 from the start and 4 from
the end, as the model card describes); a text gets its highest window. The default threshold, 0.92,
is the one the benchmark set on dev data (at most 2% of ordinary documents flagged).

The runtime needs onnxruntime, tokenizers and numpy: the `guard` extra (`keepmum[guard]`).
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

from .detect.pfserve import data_dir

REPO = "ZeroLeaks/shield-small"
RELEASE = "s15e"
REVISION = "f6d60339a424e17b60272f2d17c4e68995e933cf"  # the s15e tag
LICENCE = "CC-BY-NC-4.0"
ATTRIBUTION = "Shield Small s15e by ZeroLeaks (https://zeroleaks.ai/shield), licensed CC BY-NC 4.0"
# Path in the repo: (SHA-256, bytes), from the release's manifest.json and checked on download.
FILES = {
    "onnx/model_quantized.onnx": ("40d6daf45ceb2d325e631270baf4de201be4a98313343d28c259bbef8de862e7", 118_445_560),
    "tokenizer.json": ("b8ab08d043462fecde577bbb4e0e331953586530aada7874dc1acb79f562cb12", 17_082_898),
    "tokenizer_config.json": ("de5d1da7b3b58ae019aa8913ae1ac190681623cbd70d902b37568b7a4ce0e905", 1_311),
    "config.json": ("8c93b9d7eea656188bf7faa2c0c356834e0cdac205829492ef8db3b554099006", 760),
    "LICENSE": ("41003d4a74749c0220e33dd415042164b5a1093ed401f36277234f772d22d3d0", 19_347),
    "NOTICE": ("6b2a254eb3d02d38aacf9c2ecbc822608be87bd430a7d24c685f1e5d0dc8d32e", 1_191),
}
MODES = ("off", "warn", "block")
THRESHOLD = 0.92
WINDOW, STRIDE, MAX_WINDOWS = 256, 192, 32
MIN_CHARS = 40  # shorter tool results ("ok", an exit code) carry no instructions worth scoring
TERMS = f"""The prompt-injection guard uses Shield Small, a model by ZeroLeaks. keepmum does not ship it.

Its weights are licensed {LICENCE} (Creative Commons Attribution-NonCommercial 4.0):
  https://huggingface.co/{REPO}/blob/{REVISION}/LICENSE
You may use them only for non-commercial purposes: not "primarily intended for or directed towards
commercial advantage or monetary compensation". Using keepmum on work for an employer or a client
is commercial use; for that you need a licence from ZeroLeaks (https://zeroleaks.ai/contact).

The download is {sum(size for _, size in FILES.values()) / 1e6:.0f} MB from Hugging Face, checked against pinned SHA-256s."""


class GuardError(RuntimeError):
    pass


def model_dir() -> Path:
    return data_dir() / "guard" / f"shield-small-{RELEASE}"


def acceptance_file() -> Path:
    return model_dir() / "ACCEPTED.json"


def runtime_missing() -> str | None:
    """The first runtime package that is not installed, or None."""
    import importlib.util

    for name in ("onnxruntime", "tokenizers", "numpy"):
        if importlib.util.find_spec(name) is None:
            return name
    return None


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


def accept(by: str) -> None:
    model_dir().mkdir(parents=True, exist_ok=True)
    acceptance_file().write_text(json.dumps({"licence": LICENCE, "model": f"{REPO}@{RELEASE}", "ts": round(time.time()),
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
    """Shield Small on ONNX Runtime (CPU). Loaded on first use, unloaded when idle; calls are serialized."""

    def __init__(self, path: Path | None = None):
        self.path = path or model_dir()
        self._lock = threading.Lock()
        self._session = None

    def loaded(self) -> bool:
        return self._session is not None

    def unload(self) -> None:
        self._session = self._tok = None

    def _load(self) -> None:
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        if json.loads((self.path / "config.json").read_text()).get("id2label") != {"0": "BENIGN", "1": "INJECTION"}:
            raise GuardError("the guard model is not the Shield Small binary classifier")
        tok = Tokenizer.from_file(str(self.path / "tokenizer.json"))
        tok.no_padding()
        tok.no_truncation()
        plain = tok.encode("a", add_special_tokens=False).ids
        wrapped = tok.encode("a").ids
        at = next(i for i in range(len(wrapped)) if wrapped[i:i + len(plain)] == plain)
        self._head, self._tail = wrapped[:at], wrapped[at + len(plain):]
        self._body = WINDOW - len(self._head) - len(self._tail)
        pad = json.loads((self.path / "tokenizer_config.json").read_text())["pad_token"]
        self._pad = tok.token_to_id(pad["content"] if isinstance(pad, dict) else pad)
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        opts.inter_op_num_threads = 1
        self._np, self._tok = np, tok
        self._session = ort.InferenceSession(str(self.path / "onnx" / "model_quantized.onnx"), opts,
                                             providers=["CPUExecutionProvider"])
        self._inputs = {i.name for i in self._session.get_inputs()}

    def windows(self, ids: list[int]) -> list[list[int]]:
        body = self._body
        if len(ids) <= body:
            return [ids]
        if len(ids) <= STRIDE * (MAX_WINDOWS - 1) + body:
            return [ids[s:s + body] for s in range(0, len(ids) - body + STRIDE, STRIDE)]
        out = [ids[s:s + body] for s in range(0, STRIDE * (MAX_WINDOWS - 4), STRIDE)]
        return out + [ids[len(ids) - k * STRIDE - body:len(ids) - k * STRIDE] for k in (3, 2, 1, 0)]

    def score(self, text: str) -> float:
        with self._lock:
            if self._session is None:
                self._load()
            self._touch()
            np = self._np
            best = 0.0
            bodies = self.windows(self._tok.encode(text, add_special_tokens=False).ids)
            for i in range(0, len(bodies), 8):
                rows = [self._head + b + self._tail for b in bodies[i:i + 8]]
                width = max(map(len, rows))
                feed = {"input_ids": np.array([r + [self._pad] * (width - len(r)) for r in rows], dtype=np.int64),
                        "attention_mask": np.array([[1] * len(r) + [0] * (width - len(r)) for r in rows], dtype=np.int64)}
                if "token_type_ids" in self._inputs:
                    feed["token_type_ids"] = np.zeros_like(feed["input_ids"])
                logits = self._session.run(None, {k: v for k, v in feed.items() if k in self._inputs})[0].astype(np.float64)
                e = np.exp(logits - logits.max(axis=1, keepdims=True))
                best = max(best, float((e[:, 1] / e.sum(axis=1)).max()))
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
