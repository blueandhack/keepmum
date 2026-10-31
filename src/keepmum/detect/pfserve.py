"""L2: OpenAI Privacy Filter through pf-serve, privacy-filter.cpp (ggml) in a process of its own.

pf-serve runs on Metal on a Mac and on Vulkan or the CPU elsewhere, so keepmum needs no Python ML
packages. Protocol (little-endian), after one JSON hello line on stdout:

    request: u32 n, then n times: u32 len, len bytes of UTF-8
    reply:   u32 0, then per text: u32 count, count times: i32 start, i32 end (byte offsets),
             f32 score, u8 len, the label; or u32 1, u32 len, an error message

pf-serve and its protocol come from magpie (MIT): https://github.com/yetone/magpie. Build it with
native/pf-serve/build.sh; `keepmum setup` downloads the model.
"""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import struct
import subprocess
import threading
import time
from pathlib import Path

from . import Span

SOURCE = "pf-serve"

# The q8 GGUF of OpenAI Privacy Filter (Apache-2.0) that LocalAI publishes, at a fixed revision.
MODEL_REPO = "LocalAI-io/privacy-filter-GGUF"
MODEL_REVISION = "935d86882f4b5dc6e0eef05aae48ae5ba0a82a7a"
MODEL_FILE = "privacy-filter-q8.gguf"
MODEL_URL = f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/{MODEL_FILE}"
MODEL_SIZE = 1_637_572_192
MODEL_SHA256 = "80efc1803eda7c095a79741d2008c07e2e0a57b01bac8825fbeb448fd097998c"

LABEL_TYPES = {
    "account_number": "ACCOUNT",
    "private_address": "ADDRESS",
    "private_date": "DATE",
    "private_email": "EMAIL",
    "private_person": "PERSON",
    "private_phone": "PHONE",
    "private_url": "URL",
    "secret": "SECRET",
}

START_TIMEOUT = 120.0  # the first start after a build compiles Metal kernels


def request_timeout(nbytes: int) -> float:
    return 30.0 + nbytes / 4096


class PfServeError(RuntimeError):
    pass


def data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "keepmum"


def find_binary() -> Path | None:
    """$KEEPMUM_PF_SERVE, else where build.sh installs it."""
    for p in (os.environ.get("KEEPMUM_PF_SERVE"), data_dir() / "bin" / "pf-serve"):
        if p and os.access(p, os.X_OK):
            return Path(p)
    return None


def model_candidates() -> list[Path]:
    hub = Path(os.environ.get("HF_HUB_CACHE") or Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface") / "hub")
    return [data_dir() / "models" / f"privacy-filter-q8-{MODEL_SHA256[:12]}.gguf",
            hub / f"models--{MODEL_REPO.replace('/', '--')}" / "snapshots" / MODEL_REVISION / MODEL_FILE]


def find_model() -> Path | None:
    """$KEEPMUM_PF_MODEL, else the pinned file where `keepmum setup` or the Hugging Face CLI put it."""
    if env := os.environ.get("KEEPMUM_PF_MODEL"):
        return Path(env) if Path(env).is_file() else None
    for p in model_candidates():
        if p.is_file() and p.stat().st_size == MODEL_SIZE:
            return p
    return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def download_model(progress=None) -> Path:
    """Fetch the pinned model into data_dir(), checking size and SHA-256, unless a verified copy exists."""
    import httpx

    existing = find_model()
    if existing and sha256_file(existing) == MODEL_SHA256:
        return existing
    dest = model_candidates()[0]
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(".part")
    h, done = hashlib.sha256(), 0
    try:
        with httpx.stream("GET", MODEL_URL, follow_redirects=True, timeout=60) as r:
            r.raise_for_status()
            with open(part, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, MODEL_SIZE)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    if done != MODEL_SIZE or h.hexdigest() != MODEL_SHA256:
        part.unlink(missing_ok=True)
        raise PfServeError("the downloaded model does not match its pinned size and SHA-256")
    part.replace(dest)
    return dest


class PfServe:
    """Keeps one pf-serve process warm. Calls are serialized.

    Fail closed: a request that errors, times out or finds the process dead raises PfServeError,
    and the proxy blocks it. The next request starts a new process.
    """

    # 0: every decoded span comes back; the pipeline's refinement decides what to keep.
    def __init__(self, binary: str | Path, model: str | Path, threshold: float = 0.0, name: str = SOURCE):
        self.name = name
        self.args = [str(binary), str(model), repr(threshold)]
        self.restarts = 0
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._start()

    def _start(self) -> None:
        self._proc = subprocess.Popen(self.args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, bufsize=0)
        deadline = time.monotonic() + START_TIMEOUT
        line = b""
        try:
            while not line.endswith(b"\n"):
                line += self._read(1, deadline)
            hello = json.loads(line)
        except (PfServeError, ValueError) as e:
            hello = {"error": str(e) if isinstance(e, PfServeError) else "unreadable hello line"}
        if hello.get("error") or not hello.get("device"):
            self.close()
            raise PfServeError(f"pf-serve did not start: {hello.get('error', 'no device')}")
        self.device = hello["device"]
        self.load_ms = hello.get("ms")

    def detect(self, text: str) -> list[Span]:
        if not text.strip():
            return []
        raw = text.encode("utf-8", "replace")
        with self._lock:
            found = self._classify(raw)
        spans = []
        for start, end, score, label in found:
            # Byte offsets to str offsets; a model span always ends on a character boundary.
            a = len(raw[:start].decode("utf-8", "replace"))
            b = a + len(raw[start:end].decode("utf-8", "replace"))
            while a < b and text[a].isspace():
                a += 1
            while b > a and text[b - 1].isspace():
                b -= 1
            kind = LABEL_TYPES.get(label)
            if kind and a < b:
                spans.append(Span(a, b, kind, score, self.name))
        return spans

    def _classify(self, raw: bytes) -> list[tuple[int, int, float, str]]:
        if self._proc is None or self._proc.poll() is not None:
            self.close()
            self.restarts += 1
            self._start()
        deadline = time.monotonic() + request_timeout(len(raw))
        try:
            self._write(struct.pack("<II", 1, len(raw)) + raw, deadline)
            if self._u32(deadline) != 0:
                error = self._read(self._u32(deadline), deadline).decode("utf-8", "replace")
            else:
                return self._spans(raw, deadline)
        except (PfServeError, OSError) as e:
            self.close()  # mid-reply: start clean next time
            if isinstance(e, PfServeError):
                raise
            raise PfServeError(f"pf-serve stopped responding ({type(e).__name__})") from e
        # A whole error reply leaves the stream in step, so the process is kept.
        raise PfServeError(f"pf-serve: {error}")

    def _spans(self, raw: bytes, deadline: float) -> list[tuple[int, int, float, str]]:
        out = []
        for _ in range(self._u32(deadline)):
            start, end, score = struct.unpack("<iif", self._read(12, deadline))
            label = self._read(self._read(1, deadline)[0], deadline).decode("ascii", "replace")
            if not 0 <= start < end <= len(raw):
                raise PfServeError(f"pf-serve sent span {start}..{end} for {len(raw)} bytes")
            out.append((start, end, score, label))
        return out

    def _u32(self, deadline: float) -> int:
        return struct.unpack("<I", self._read(4, deadline))[0]

    def _read(self, n: int, deadline: float) -> bytes:
        fd, buf = self._proc.stdout.fileno(), b""
        with selectors.DefaultSelector() as sel:
            sel.register(fd, selectors.EVENT_READ)
            while len(buf) < n:
                wait = deadline - time.monotonic()
                if wait <= 0 or not sel.select(wait):
                    raise PfServeError("pf-serve timed out")
                chunk = os.read(fd, n - len(buf))
                if not chunk:
                    raise PfServeError("pf-serve closed its output")
                buf += chunk
        return buf

    def _write(self, data: bytes, deadline: float) -> None:
        fd, view = self._proc.stdin.fileno(), memoryview(data)
        with selectors.DefaultSelector() as sel:
            sel.register(fd, selectors.EVENT_WRITE)
            while view:
                wait = deadline - time.monotonic()
                if wait <= 0 or not sel.select(wait):
                    raise PfServeError("pf-serve timed out")
                view = view[os.write(fd, view[: 1 << 16]):]

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.poll() is None:
            proc.stdin.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        else:
            proc.stdin.close()
        proc.stdout.close()
