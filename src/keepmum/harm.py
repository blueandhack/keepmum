"""The harm check: an optional look at what the user typed, for requests a policy may not allow.

Off by default; its model is downloaded only by `keepmum guard setup harm`. It is Qwen3Guard-Gen
(Apache-2.0, Qwen team), the best local model in the benchmark (bench/results/HARM.md), as a
quantized GGUF at a pinned commit, checked against its SHA-256. It runs in llama-server (llama.cpp,
on Metal) in a process of its own, started on the first check and stopped after IDLE_S unused.

What it scans: the text of user messages that a request brings for the first time, without what
agents add to a user turn (Claude Code's <system-reminder> blocks, Codex's environment context and
AGENTS.md). Never tool results, system prompts or the resent history.

The score is P(Unsafe) + P(Controversial) for the token after "Safety:", from one forward pass.
The threshold for each size is the one the benchmark set on dev data (at most 5% of safe prompts
flagged): the model's own verdict (0.5) flags a quarter of ordinary coding prompts. On a hit, a
short completion names Qwen3Guard's categories (Violent, Jailbreak, ...).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from .detect.pfserve import data_dir
from .guard import Idle
from .qwen3guard_prompt import PREFIX, SUFFIX

# size: (repo, commit, file, SHA-256, bytes, threshold). The thresholds are the Q8 GGUFs' own,
# set on dev data in the benchmark (bench/results/harm-20261104-004512.md).
MODELS = {
    "0.6b": ("mradermacher/Qwen3Guard-Gen-0.6B-GGUF", "2a59799e089fba3807d534a89c6b95bdf38ee76a",
             "Qwen3Guard-Gen-0.6B.Q8_0.gguf", "127e8833aa16c6839613cd08df5409440302e8c61e90617914b3100f2141411a",
             804_753_920, 0.970),
    "4b": ("mradermacher/Qwen3Guard-Gen-4B-GGUF", "b3b010b37c75197c5e887fa7dce9ff907d0be6ff",
           "Qwen3Guard-Gen-4B.Q8_0.gguf", "9baa737b535c56b8e04965833d38a0e595fac0fd003306e5f0b039788044e3a8",
           4_693_671_488, 0.981),
}
SIZES = tuple(MODELS)
LICENCE = "Apache-2.0"
CTX = 2048  # tokens: the prompt is about 300, and what a user types fits in the rest
MAX_CONTENT_TOKENS = 1600  # longer text keeps its first 1,200 and last 400 tokens
MIN_CHARS = 8
CATEGORIES = ("Violent", "Non-violent Illegal Acts", "Sexual Content or Sexual Acts", "PII", "Suicide & Self-Harm",
              "Unethical Acts", "Politically Sensitive Topics", "Copyright Violation", "Jailbreak")
# What agents put into a user turn that the user did not type.
INJECTED = re.compile(r"<(system-reminder|environment_context|user_instructions|INSTRUCTIONS|local-command-stdout|"
                      r"command-message|command-name|command-args|user-prompt-submit-hook)\b[^>]*>.*?</\1>", re.S)
AGENTS_MD = re.compile(r"^# AGENTS\.md instructions for .*", re.S)


class HarmError(RuntimeError):
    pass


def typed(text: str) -> str:
    """The part of a user message the user typed: agent-injected blocks removed."""
    return AGENTS_MD.sub("", INJECTED.sub("", text)).strip()


def model_dir() -> Path:
    return data_dir() / "guard" / "qwen3guard"


def model_path(size: str) -> Path:
    return model_dir() / MODELS[size][2]


def installed(size: str) -> bool:
    p = model_path(size)
    return p.is_file() and p.stat().st_size == MODELS[size][4]


def find_server() -> Path | None:
    """$KEEPMUM_LLAMA_SERVER (the Mac app's own build), else keepmum's copy, else Homebrew's llama.cpp."""
    for p in (os.environ.get("KEEPMUM_LLAMA_SERVER"), data_dir() / "bin" / "llama-server", shutil.which("llama-server"),
              "/opt/homebrew/bin/llama-server", "/usr/local/bin/llama-server"):
        if p and os.access(p, os.X_OK):
            return Path(p)
    return None


def download(size: str, progress=None) -> Path:
    import httpx

    repo, commit, file, sha, total, _ = MODELS[size]
    dest = model_path(size)
    if installed(size) and _sha256(dest) == sha:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    h, got = hashlib.sha256(), 0
    try:
        with httpx.stream("GET", f"https://huggingface.co/{repo}/resolve/{commit}/{file}", follow_redirects=True, timeout=60) as r:
            r.raise_for_status()
            with open(part, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    if progress:
                        progress(got, total)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    if got != total or h.hexdigest() != sha:
        part.unlink(missing_ok=True)
        raise HarmError(f"{file} does not match its pinned size and SHA-256")
    part.replace(dest)
    return dest


def remove(size: str | None = None) -> list[Path]:
    gone = []
    for s in [size] if size else SIZES:
        p = model_path(s)
        if p.exists():
            p.unlink()
            gone.append(p)
    return gone


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


class Qwen3Guard(Idle):
    """Qwen3Guard-Gen in llama-server, started on first use and stopped when idle. Calls are serialized."""

    def __init__(self, size: str = "0.6b", server: Path | None = None, model: Path | None = None):
        self.size = size
        self.server, self.model = server, model
        self.threshold = MODELS[size][5]
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._url = ""

    def loaded(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def unload(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

    def _start(self) -> None:
        server = self.server or find_server()
        if server is None:
            raise HarmError("llama-server is not installed (brew install llama.cpp, or set KEEPMUM_LLAMA_SERVER)")
        model = self.model or model_path(self.size)
        if not model.is_file():
            raise HarmError(f"the harm model is not downloaded: run `keepmum guard setup harm --model {self.size}`")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self._url = f"http://127.0.0.1:{port}"
        self._proc = subprocess.Popen([str(server), "-m", str(model), "--host", "127.0.0.1", "--port", str(port),
                                       "-ngl", "99", "-c", str(CTX), "--parallel", "1", "--no-webui",
                                       "--cache-ram", "0"],  # its default keeps 8 GB of past prompts; each check is new
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(self._url + "/health", timeout=1) as r:
                    if r.status == 200:
                        self._ids = {k: self._post("/tokenize", {"content": " " + k})["tokens"][0]
                                     for k in ("Safe", "Unsafe", "Controversial")}
                        return
            except OSError:
                time.sleep(0.1)
        self.unload()
        raise HarmError("llama-server did not start")

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(self._url + path, data=json.dumps(body).encode(), headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())

    def _prompt(self, text: str) -> str:
        tokens = self._post("/tokenize", {"content": text})["tokens"]
        if len(tokens) > MAX_CONTENT_TOKENS:
            head = self._post("/detokenize", {"tokens": tokens[:1200]})["content"]
            tail = self._post("/detokenize", {"tokens": tokens[-400:]})["content"]
            text = head + "\n…\n" + tail
        return PREFIX + text + SUFFIX

    def _ready(self) -> None:
        if not self.loaded():
            self._start()
        self._touch()

    def score(self, text: str) -> float:
        with self._lock:
            self._ready()
            r = self._post("/completion", {"prompt": self._prompt(text) + "Safety:", "n_predict": 1, "n_probs": 20,
                                           "temperature": 0, "samplers": [], "cache_prompt": False})
        top = r["completion_probabilities"][0]
        probs = {t["id"]: (math.exp(t["logprob"]) if "logprob" in t else t["prob"])
                 for t in top.get("top_logprobs") or top.get("top_probs") or []}
        mass = {k: probs.get(i, 0.0) for k, i in self._ids.items()}
        total = sum(mass.values())
        if total <= 0:
            raise HarmError("the harm model gave no verdict")
        return (mass["Unsafe"] + mass["Controversial"]) / total

    def categories(self, text: str) -> list[str]:
        with self._lock:
            self._ready()
            r = self._post("/completion", {"prompt": self._prompt(text), "n_predict": 32, "temperature": 0,
                                           "cache_prompt": False})
        line = next((l for l in r.get("content", "").splitlines() if l.startswith("Categories:")), "")
        return [c for c in CATEGORIES if c in line]


class HarmModel:
    """The harm check's model at the configured size: `want()` switches when the setting changes,
    stopping the old server and moving its guard to the new size's threshold."""

    def __init__(self, size: str = "0.6b"):
        self._model = Qwen3Guard(size)
        self.guard = None  # the guard.Guard using this model, whose threshold follows the size

    @property
    def size(self) -> str:
        return self._model.size

    @property
    def threshold(self) -> float:
        return self._model.threshold

    def want(self, size: str) -> None:
        if size == self._model.size or size not in MODELS:
            return
        old, self._model = self._model, Qwen3Guard(size)
        if self.guard is not None:
            self.guard.threshold = self._model.threshold
        with old._lock:
            old.unload()

    def score(self, text: str) -> float:
        return self._model.score(text)

    def categories(self, text: str) -> list[str]:
        return self._model.categories(text)
