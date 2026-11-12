"""Models for local mode: keepmum's tested picks, and getting one onto Ollama.

The picks come from keepmum's end-to-end tests of local mode (the injection check's summary and
rewrite hand-backs on Claude Code, Codex and the Antigravity CLI), measured one model at a time on
an Apple silicon Mac. `memory_gb` is what Ollama holds for the model with a 32k context; Ollama's
default context is the model's maximum, which can take several GB more (`OLLAMA_CONTEXT_LENGTH`
sets it). The first pick is the default.

Any other model can be named as Ollama names it: one from its library (`qwen3:8b`), a GGUF repo on
Hugging Face (`hf.co/owner/repo:Q4_K_M`, or the repo's link, with the quant given apart), or another
registry Ollama pulls from. Only Ollama downloads for keepmum; LM Studio and the others download in
their own app, and keepmum then picks the model from what the server lists.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Pick:
    """One tested model, as Ollama names it, with what it costs on a Mac."""
    id: str
    name: str
    model: str
    download_gb: float  # what Ollama downloads
    memory_gb: float  # what Ollama holds with a 32k context
    mac_gb: int  # the smallest Mac memory it suits, beside the apps you run
    note: str


PICKS = (
    Pick("recommended", "Qwen3.6 35B-A3B Uncensored (HauhauCS), IQ3_M",
         "hf.co/HauhauCS/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:IQ3_M", 16.3, 16.1, 32,
         "The best balance in keepmum's tests: passed every run, fast (about 85 tokens a second on an M5 Pro), "
         "and about 2 CPU cores while it answers."),
    Pick("larger", "Qwen3.6 35B-A3B Uncensored (HauhauCS), Q4_K_M",
         "hf.co/HauhauCS/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:Q4_K_M", 22.1, 21.7, 64,
         "The same model with more bits per weight: as fast, and no better in keepmum's tests."),
    Pick("light", "Qwen3.5 9B Defiant Fable (DavidAU), Q8_0",
         "hf.co/DavidAU/Qwen3.5-9B-The-Defiant-Fable-Uncensored-Heretic-NEO-IMATRIX-MAX-MTP-GGUF:Q8_0", 11.4, 9.1, 24,
         "Under half a CPU core, but slower (about 28 tokens a second): a long answer can take over a minute."),
    Pick("small", "Qwen3.5 9B Defiant Fable (DavidAU), Q4_K_M",
         "hf.co/DavidAU/Qwen3.5-9B-The-Defiant-Fable-Uncensored-Heretic-NEO-IMATRIX-MAX-MTP-GGUF:Q4_K_M", 7.7, 5.9, 16,
         "For a 16 GB Mac. It thinks at length, so more flagged turns stay local instead of going back to the cloud."),
)
DEFAULT_PICK = PICKS[0].id
# Ollama's quant names, as GGUF files on Hugging Face carry them.
_QUANT = re.compile(r"(?i)(?<![A-Z0-9])((?:I?Q\d(?:_[0-9KSMLXN]+)*)|F16|BF16|F32|MXFP4)(?=[.-]|$)")
_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")


class PullError(Exception):
    pass


def picks_json() -> list[dict]:
    return [asdict(p) for p in PICKS]


def pick(name: str) -> Pick | None:
    return next((p for p in PICKS if p.id == name or p.model == name), None)


def quant_of(filename: str) -> str | None:
    """The quant in a GGUF file's name (`Model.Q4_K_M.gguf` -> Q4_K_M), or None."""
    stem = filename.rsplit("/", 1)[-1].removesuffix(".gguf")
    found = _QUANT.findall(stem)
    return found[-1].upper() if found else None


def resolve(ref: str, quant: str | None = None) -> str:
    """The name Ollama pulls for `ref`: a pick's id, a Hugging Face link or repo
    (`hf.co/owner/repo`, `huggingface.co/owner/repo`, the repo's page or one of its .gguf files),
    or a name Ollama already knows (`qwen3:8b`), given as it is. `quant` picks the GGUF file in a
    Hugging Face repo (Q4_K_M, IQ3_M...); without one Ollama takes the repo's default."""
    ref = ref.strip()
    if not ref:
        raise PullError("name a model: a pick (" + ", ".join(p.id for p in PICKS) + "), a Hugging Face link, or an Ollama name")
    if p := pick(ref):
        return p.model
    quant = quant.strip() if quant else None
    if quant and not re.fullmatch(r"[\w.-]+", quant):
        raise PullError(f"--quant {quant}: give a quant such as Q4_K_M or IQ3_M")
    host_path = ref.split("://", 1)[-1] if "://" in ref else ref
    host, _, path = host_path.partition("/")
    if host.lower() in ("huggingface.co", "www.huggingface.co", "hf.co"):
        if "://" in ref and urlsplit(ref).scheme not in ("http", "https"):
            raise PullError(f"{ref}: not a Hugging Face link")
        path = path.split("?", 1)[0].split("#", 1)[0].strip("/")
        parts = path.split("/")
        if len(parts) < 2:
            raise PullError(f"{ref}: give the repo, as huggingface.co/owner/repo")
        repo, tag = "/".join(parts[:2]), None
        if ":" in repo:
            repo, tag = repo.split(":", 1)
        rest = parts[2:]
        if rest and rest[-1].endswith(".gguf"):  # a file's link: its quant
            tag = tag or quant_of(rest[-1])
        if not _REPO.match(repo):
            raise PullError(f"{ref}: give the repo, as huggingface.co/owner/repo")
        tag = quant or tag
        return f"hf.co/{repo}" + (f":{tag}" if tag else "")
    if quant:
        return ref.split(":", 1)[0] + f":{quant}"
    return ref


def pull(url: str, model: str, progress: Callable[[int, int], None] | None = None, timeout: float = 30.0) -> None:
    """Have the Ollama at `url` download `model`, reporting bytes done of the total as they come.
    Ollama downloads each layer in turn; the total is the sum of those it has announced."""
    import httpx

    sizes: dict[str, int] = {}
    done: dict[str, int] = {}
    try:
        with httpx.stream("POST", url.rstrip("/") + "/api/pull", json={"model": model, "stream": True},
                          timeout=httpx.Timeout(timeout, read=None)) as r:
            if r.status_code != 200:
                r.read()
                raise PullError(_ollama_error(r.text) or f"Ollama answered {r.status_code}")
            for line in r.iter_lines():
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if err := event.get("error"):
                    raise PullError(str(err))
                digest, total = event.get("digest"), event.get("total")
                if digest and isinstance(total, int) and total > 0:
                    sizes[digest] = total
                    done[digest] = int(event.get("completed") or 0)
                    if progress:
                        progress(sum(done.values()), sum(sizes.values()))
                if event.get("status") == "success":
                    return
    except httpx.ConnectError:
        raise PullError(f"nothing answers at {url}: open the Ollama app, or `ollama serve`") from None
    except httpx.HTTPError as e:
        raise PullError(f"Ollama at {url}: {e}") from None
    raise PullError("Ollama stopped before the download finished")


def _ollama_error(text: str) -> str | None:
    try:
        return str(json.loads(text).get("error") or "") or None
    except (ValueError, AttributeError):
        return text.strip()[:200] or None
