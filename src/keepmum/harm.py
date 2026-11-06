"""The harm check: an optional look at what the user typed, for requests a policy may not allow.

Off by default; its model is downloaded only by `keepmum guard setup harm`. Three sizes, all
Apache-2.0, each a GGUF at a pinned commit, checked against its SHA-256 (bench/results/HARM.md):

    small  Content Safety Guard Small (Horizon Labs, 141M), an encoder keepmum converted
           (bench/horizon_gguf.py): the default, 164 MB, 8 ms, 66% of harmful prompts caught
    0.6b   Qwen3Guard-Gen 0.6B (Qwen team): 805 MB, 76%
    4b     Qwen3Guard-Gen 4B: 4.7 GB, 82%

Each runs in llama-server (llama.cpp, on Metal) in a process of its own, started on the first check
and stopped after IDLE_S unused.

What it scans: the text of user messages that a request brings for the first time, without what
agents add to a user turn (Claude Code's <system-reminder> blocks, Codex's environment context and
AGENTS.md). Never tool results, system prompts or the resent history.

The small model's score is the sigmoid of its `unsafe` logit, over windows of WINDOW tokens (the
highest); its 15 category logits, each at the threshold Horizon Labs set, name the categories of a
hit. Qwen3Guard's score is P(Unsafe) + P(Controversial) for the token after "Safety:", from one
forward pass; on a hit, a short completion names its categories (Violent, Jailbreak, ...). The
threshold for each size is the one the benchmark set on dev data (at most 5% of safe prompts
flagged): the models' own verdict (0.5) flags many ordinary coding prompts.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
from pathlib import Path

from .detect.pfserve import data_dir
from .guard import Idle, sigmoid, windows
from .llamacpp import INSTALL_HINT, Server, find_server  # noqa: F401 (find_server: harm.find_server)
from .qwen3guard_prompt import PREFIX, SUFFIX

# size: (repo, commit, file, SHA-256, bytes, threshold). The thresholds are the Q8 GGUFs' own,
# set on dev data in the benchmark (bench/results/harm-20261104-004512.md; small:
# bench/results/safety-gguf-*.md).
MODELS = {
    "small": ("keepmum/content-safety-guard-small-gguf", "30d413f43092b4d82e959945dae01effe4a71943",
              "content-safety-guard-small-v1.2.Q8_0.gguf", "37a3cec4ca7c600d410867360bc4d21bbf7b24477f9149a18df4360e9d2293d8",
              164_365_728, 0.82),
    "0.6b": ("mradermacher/Qwen3Guard-Gen-0.6B-GGUF", "2a59799e089fba3807d534a89c6b95bdf38ee76a",
             "Qwen3Guard-Gen-0.6B.Q8_0.gguf", "127e8833aa16c6839613cd08df5409440302e8c61e90617914b3100f2141411a",
             804_753_920, 0.970),
    "4b": ("mradermacher/Qwen3Guard-Gen-4B-GGUF", "b3b010b37c75197c5e887fa7dce9ff907d0be6ff",
           "Qwen3Guard-Gen-4B.Q8_0.gguf", "9baa737b535c56b8e04965833d38a0e595fac0fd003306e5f0b039788044e3a8",
           4_693_671_488, 0.981),
}
SIZES = tuple(MODELS)
DEFAULT = "small"
LICENCE = "Apache-2.0"
NAMES = {"small": "Content Safety Guard Small by Horizon Labs", "0.6b": "Qwen3Guard-Gen 0.6B", "4b": "Qwen3Guard-Gen 4B"}
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


def model_dir(size: str = "0.6b") -> Path:
    return data_dir() / "guard" / ("content-safety-guard-small" if size == "small" else "qwen3guard")


def model_path(size: str) -> Path:
    return model_dir(size) / MODELS[size][2]


def installed(size: str) -> bool:
    p = model_path(size)
    return p.is_file() and p.stat().st_size == MODELS[size][4]


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
            raise HarmError(f"llama-server is not installed ({INSTALL_HINT})")
        model = self.model or model_path(self.size)
        if not model.is_file():
            raise HarmError(f"the harm model is not downloaded: run `keepmum guard setup harm --model {self.size}`")
        self._server = Server(server, model, ["-c", str(CTX), "--parallel", "1",
                                              "--cache-ram", "0"],  # its default keeps 8 GB of past prompts; each check is new
                              HarmError)
        self._server.start()
        self._ids = {k: self._post("/tokenize", {"content": " " + k})["tokens"][0] for k in ("Safe", "Unsafe", "Controversial")}

    def _post(self, path: str, body: dict) -> dict:
        return self._server.post(path, body)

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


# The small model: its logits are `unsafe`, then these categories, each with the threshold Horizon
# Labs chose on its validation data (the model's thresholds.json), applied only to a flagged text.
SAFETY_CATEGORIES = (("hate", "Hate", 0.2), ("harassment", "Harassment", 0.25), ("violence", "Violence", 0.3),
                     ("weapons", "Weapons", 0.2), ("sexual", "Sexual", 0.15), ("sexual_minors", "Sexual (minors)", 0.25),
                     ("self_harm", "Self-harm", 0.2), ("criminal_planning", "Criminal planning", 0.2), ("drugs", "Drugs", 0.35),
                     ("privacy", "Privacy", 0.2), ("profanity", "Profanity", 0.25),
                     ("fraud_manipulation", "Fraud or manipulation", 0.15), ("misinformation", "Misinformation", 0.15),
                     ("unauthorized_advice", "Unauthorized advice", 0.2), ("other", "Other", 0.1))
WINDOW, OVERLAP, MAX_WINDOWS, TAIL_WINDOWS = 1024, 128, 8, 2  # it was trained on up to 1,024 tokens
BOS, EOS = 2, 1


class SafetyGuard(Idle):
    """Content Safety Guard Small in llama-server, started on first use and stopped when idle. Calls
    are serialized. A score keeps the categories of its text, for `categories()` right after."""

    size = "small"

    def __init__(self, server: Path | None = None, model: Path | None = None):
        self.server, self.model = server, model
        self.threshold = MODELS["small"][5]
        self._lock = threading.Lock()
        self._server: Server | None = None
        self._last: tuple[str, list[str]] | None = None

    def loaded(self) -> bool:
        return self._server is not None and self._server.running()

    def unload(self) -> None:
        if self._server is not None:
            self._server.stop()
        self._server = None

    def _start(self) -> None:
        server = self.server or find_server()
        if server is None:
            raise HarmError(f"llama-server is not installed ({INSTALL_HINT})")
        model = self.model or model_path("small")
        if not model.is_file():
            raise HarmError("the harm model is not downloaded: run `keepmum guard setup harm --model small`")
        self._server = Server(server, model, ["--embedding", "-c", str(WINDOW), "--parallel", "1", "-b", str(WINDOW),
                                              "-ub", str(WINDOW), "--cache-ram", "0"], HarmError)
        self._server.start()

    def tokens(self, text: str) -> list[int]:
        return self._server.post("/tokenize", {"content": text, "add_special": False})["tokens"]

    @staticmethod
    def windows(ids: list[int]) -> list[list[int]]:
        return windows(ids, WINDOW, OVERLAP, MAX_WINDOWS, TAIL_WINDOWS)

    def score(self, text: str) -> float:
        """P(unsafe) of the text's most likely window; the categories come from that window."""
        with self._lock:
            if not self.loaded():
                self._start()
            self._touch()
            best, logits = -1.0, None
            for body in self.windows(self.tokens(text)):
                out = self._server.post("/embeddings", {"content": [[BOS, *body, EOS]], "embd_normalize": -1})
                row = out[0]["embedding"][0]
                if sigmoid(row[0]) > best:
                    best, logits = sigmoid(row[0]), row
            self._last = (text, [name for (_, name, t), x in zip(SAFETY_CATEGORIES, logits[1:]) if sigmoid(x) >= t])
            return best

    def categories(self, text: str) -> list[str]:
        if self._last is None or self._last[0] != text:
            self.score(text)
        return self._last[1]


def _model(size: str):
    return SafetyGuard() if size == "small" else Qwen3Guard(size)


class HarmModel:
    """The harm check's model at the configured size: `want()` switches when the setting changes,
    stopping the old server and moving its guard to the new size's threshold."""

    def __init__(self, size: str = DEFAULT):
        self._model = _model(size)
        self.guard = None  # the guard.Guard using this model, whose threshold follows the size

    @property
    def size(self) -> str:
        return self._model.size

    @property
    def threshold(self) -> float:
        return self._model.threshold

    @property
    def label(self) -> str:
        return NAMES[self._model.size]

    def want(self, size: str) -> None:
        if size == self._model.size or size not in MODELS:
            return
        old, self._model = self._model, _model(size)
        if self.guard is not None:
            self.guard.threshold = self._model.threshold
        with old._lock:
            old.unload()

    def score(self, text: str) -> float:
        return self._model.score(text)

    def categories(self, text: str) -> list[str]:
        return self._model.categories(text)
