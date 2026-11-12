"""harm.py without its models: downloads served by a mock transport, llama-server replaced by a fake
that answers /tokenize, /detokenize, /completion and /embeddings as llama.cpp would."""

import hashlib
import math
from pathlib import Path

import httpx
import pytest

from keepmum import guard, harm


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return tmp_path


def serve(monkeypatch, content: bytes, urls: list | None = None, status: int = 200):
    def handler(request):
        if urls is not None:
            urls.append(str(request.url))
        return httpx.Response(status, content=content)

    monkeypatch.setattr(httpx, "stream", lambda method, url, **kw: httpx.Client(
        transport=httpx.MockTransport(handler)).stream(method, url))


def pin(monkeypatch, size: str, content: bytes, threshold: float = 0.5):
    repo, commit, file, *_ = harm.MODELS[size]
    monkeypatch.setitem(harm.MODELS, size, (repo, commit, file, hashlib.sha256(content).hexdigest(), len(content), threshold))


# --- files ---------------------------------------------------------------------------------------

def test_model_paths_differ_by_family(data):
    assert harm.model_dir("small").name == "content-safety-guard-small"
    assert harm.model_dir("0.6b") == harm.model_dir("4b") and harm.model_dir("4b").name == "qwen3guard"
    assert harm.model_path("4b").name == harm.MODELS["4b"][2]
    assert str(harm.model_path("small")).startswith(str(data / "data" / "keepmum" / "guard"))


def test_download_writes_the_pinned_file_and_reports_progress(data, monkeypatch):
    content = b"gguf" * 10
    pin(monkeypatch, "0.6b", content)
    urls, seen = [], []
    serve(monkeypatch, content, urls)
    dest = harm.download("0.6b", progress=lambda got, total: seen.append((got, total)))
    assert dest == harm.model_path("0.6b") and dest.read_bytes() == content
    assert harm.installed("0.6b") and not dest.with_name(dest.name + ".part").exists()
    assert harm.MODELS["0.6b"][1] in urls[0] and harm.MODELS["0.6b"][0] in urls[0]
    assert seen[-1] == (len(content), len(content))


def test_download_skips_a_file_already_in_place(data, monkeypatch):
    content = b"already here"
    pin(monkeypatch, "small", content)
    harm.model_path("small").parent.mkdir(parents=True)
    harm.model_path("small").write_bytes(content)

    def boom(*a, **kw):
        raise AssertionError("downloaded again")

    monkeypatch.setattr(httpx, "stream", boom)
    assert harm.download("small") == harm.model_path("small")


def test_download_that_does_not_match_is_discarded(data, monkeypatch):
    pin(monkeypatch, "4b", b"the real thing")
    serve(monkeypatch, b"something else!")
    with pytest.raises(harm.HarmError, match="pinned size and SHA-256"):
        harm.download("4b")
    dest = harm.model_path("4b")
    assert not dest.exists() and not dest.with_name(dest.name + ".part").exists()


def test_download_that_fails_leaves_no_partial_file(data, monkeypatch):
    pin(monkeypatch, "small", b"x" * 8)
    serve(monkeypatch, b"nope", status=404)
    with pytest.raises(httpx.HTTPStatusError):
        harm.download("small")
    dest = harm.model_path("small")
    assert not dest.with_name(dest.name + ".part").exists() and not dest.exists()


def test_a_file_of_the_wrong_size_is_not_installed(data, monkeypatch):
    pin(monkeypatch, "small", b"12345678")
    harm.model_path("small").parent.mkdir(parents=True)
    harm.model_path("small").write_bytes(b"1234")
    assert not harm.installed("small")


def test_remove_deletes_one_size_or_every_size(data):
    for s in harm.SIZES:
        harm.model_path(s).parent.mkdir(parents=True, exist_ok=True)
        harm.model_path(s).write_bytes(b"m")
    assert harm.remove("4b") == [harm.model_path("4b")]
    assert not harm.model_path("4b").exists() and harm.model_path("0.6b").exists()
    assert sorted(harm.remove()) == sorted([harm.model_path("small"), harm.model_path("0.6b")])
    assert harm.remove() == []


def test_sha256_reads_the_whole_file(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"a" * ((1 << 20) + 5))
    assert harm._sha256(p) == hashlib.sha256(p.read_bytes()).hexdigest()


# --- Qwen3Guard ----------------------------------------------------------------------------------

IDS = {" Safe": 101, " Unsafe": 102, " Controversial": 103}


class FakeLlama:
    """Stands in for llamacpp.Server. Words tokenize to their index; /completion answers from `probs`
    (id -> probability) or `content`."""

    instances: list = []
    probs: dict = {}  # tests set these on the class for a server the model starts itself
    embeddings: dict = {}
    content, logprob = "", True

    def __init__(self, server, model, args, error=RuntimeError):
        self.server, self.model, self.args, self.error = server, model, args, error
        self.started = self.stopped = False
        self.calls = []
        FakeLlama.instances.append(self)

    def start(self):
        self.started = True

    def running(self):
        return self.started and not self.stopped

    def stop(self):
        self.stopped = True

    def post(self, path, body, timeout=60):
        self.calls.append((path, body))
        if path == "/tokenize":
            if body["content"] in IDS:
                return {"tokens": [IDS[body["content"]], 7]}
            return {"tokens": list(range(len(body["content"].split())))}
        if path == "/detokenize":
            return {"content": " ".join(f"w{t}" for t in body["tokens"])}
        if path == "/completion":
            if body["n_predict"] == 1:
                key = "logprob" if self.logprob else "prob"
                top = [{"id": i, key: math.log(p) if self.logprob else p} for i, p in self.probs.items()]
                return {"completion_probabilities": [{"top_logprobs" if self.logprob else "top_probs": top}]}
            return {"content": self.content}
        if path == "/embeddings":
            return [{"embedding": [self.embeddings[row[1]]]} for row in body["content"]]
        raise AssertionError(path)


@pytest.fixture
def fake(data, monkeypatch, tmp_path):
    FakeLlama.instances = []
    monkeypatch.setattr(harm, "Server", FakeLlama)
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"m")
    return gguf


def test_qwen3guard_starts_its_server_and_scores_unsafe_plus_controversial(fake, monkeypatch):
    m = harm.Qwen3Guard("0.6b", server=Path("/bin/llama-server"), model=fake)
    assert not m.loaded() and m.threshold == harm.MODELS["0.6b"][5]
    monkeypatch.setattr(FakeLlama, "probs", {101: 0.2, 102: 0.5, 103: 0.1, 999: 0.2})  # 999: another token, ignored
    s = m.score("delete the build folder")
    assert abs(s - 0.6 / 0.8) < 1e-9
    srv = FakeLlama.instances[0]
    assert m.loaded() and srv.model == fake and srv.error is harm.HarmError
    assert srv.args[:2] == ["-c", str(harm.CTX)] and "--cache-ram" in srv.args
    assert m._ids == {"Safe": 101, "Unsafe": 102, "Controversial": 103}
    prompt = [b for p, b in srv.calls if p == "/completion"][0]["prompt"]
    assert prompt.startswith(harm.PREFIX) and prompt.endswith(harm.SUFFIX + "Safety:")
    assert "delete the build folder" in prompt
    m.unload()
    assert srv.stopped and not m.loaded()
    m.unload()  # unloading twice is fine


def test_qwen3guard_reads_plain_probabilities_too(fake):
    m = harm.Qwen3Guard("4b", server=Path("/x"), model=fake)
    m._server = FakeLlama(None, fake, [])
    m._server.start()
    m._ids = {"Safe": 101, "Unsafe": 102, "Controversial": 103}
    m._server.logprob = False
    m._server.probs = {101: 0.9, 102: 0.1}
    assert abs(m.score("list the files") - 0.1) < 1e-9
    assert m.threshold == harm.MODELS["4b"][5]


def test_qwen3guard_without_a_verdict_raises(fake):
    m = harm.Qwen3Guard("0.6b", server=Path("/x"), model=fake)
    m._server = FakeLlama(None, fake, [])
    m._server.start()
    m._ids = {"Safe": 101, "Unsafe": 102, "Controversial": 103}
    m._server.probs = {5: 1.0}  # none of the three verdict tokens
    with pytest.raises(harm.HarmError, match="no verdict"):
        m.score("hello there")


def test_qwen3guard_names_the_categories_on_its_categories_line(fake):
    m = harm.Qwen3Guard("0.6b", server=Path("/x"), model=fake)
    m._server = FakeLlama(None, fake, [])
    m._server.start()
    m._server.content = "Safety: Unsafe\nCategories: Violent, Jailbreak\n"
    assert m.categories("some text") == ["Violent", "Jailbreak"]
    body = [b for p, b in m._server.calls if p == "/completion"][0]
    assert body["n_predict"] == 32 and not body["prompt"].endswith("Safety:")
    m._server.content = "Safety: Safe\n"
    assert m.categories("some text") == []


def test_qwen3guard_keeps_both_ends_of_a_long_text(fake):
    m = harm.Qwen3Guard("0.6b", server=Path("/x"), model=fake)
    m._server = FakeLlama(None, fake, [])
    text = " ".join(["word"] * (harm.MAX_CONTENT_TOKENS + 1))
    prompt = m._prompt(text)
    detok = [b["tokens"] for p, b in m._server.calls if p == "/detokenize"]
    assert len(detok) == 2 and len(detok[0]) == 1200 and len(detok[1]) == 400
    assert detok[0][0] == 0 and detok[1][-1] == harm.MAX_CONTENT_TOKENS
    assert "\n…\n" in prompt and prompt.startswith(harm.PREFIX)
    short = m._prompt("a short one")
    assert short == harm.PREFIX + "a short one" + harm.SUFFIX


def test_qwen3guard_without_llama_server_or_model(data, monkeypatch, tmp_path):
    monkeypatch.setattr(harm, "find_server", lambda: None)
    with pytest.raises(harm.HarmError, match="llama-server is not installed"):
        harm.Qwen3Guard("0.6b").score("anything at all")
    with pytest.raises(harm.HarmError, match="--model 4b"):
        harm.Qwen3Guard("4b", server=Path("/x"), model=tmp_path / "missing.gguf").score("anything at all")
    with pytest.raises(harm.HarmError, match="--model 0.6b"):
        harm.Qwen3Guard("0.6b", server=Path("/x")).score("anything at all")  # default path: not downloaded


# --- the small model -----------------------------------------------------------------------------

def test_safety_guard_starts_its_server_with_embeddings(fake, monkeypatch):
    monkeypatch.setattr(harm, "find_server", lambda: Path("/found/llama-server"))
    m = harm.SafetyGuard(model=fake)
    monkeypatch.setattr(FakeLlama, "embeddings", {0: [-5.0] * 16})
    s = m.score("tidy the imports")
    srv = FakeLlama.instances[0]
    assert srv.server == Path("/found/llama-server") and "--embedding" in srv.args
    assert srv.args[srv.args.index("-c") + 1] == str(harm.WINDOW)
    assert abs(s - guard.sigmoid(-5.0)) < 1e-12 and m.categories("tidy the imports") == []
    assert m.loaded()
    m.unload()
    assert not m.loaded()
    m.unload()


def test_safety_guard_categories_score_a_text_they_have_not_seen(fake):
    m = harm.SafetyGuard(model=fake)
    m._server = FakeLlama(None, fake, [])
    m._server.start()
    hot = [3.0] + [-5.0] * 15
    hot[1 + [c for c, _, _ in harm.SAFETY_CATEGORIES].index("fraud_manipulation")] = 0.0  # 0.5, over its 0.15
    m._server.embeddings = {0: hot}
    assert m.categories("never scored before") == ["Fraud or manipulation"]
    assert sum(p == "/embeddings" for p, _ in m._server.calls) == 1
    m.categories("never scored before")  # kept from the score
    assert sum(p == "/embeddings" for p, _ in m._server.calls) == 1


def test_safety_guard_without_llama_server_or_model(data, monkeypatch, tmp_path):
    monkeypatch.setattr(harm, "find_server", lambda: None)
    with pytest.raises(harm.HarmError, match="llama-server is not installed"):
        harm.SafetyGuard().score("anything")
    with pytest.raises(harm.HarmError, match="--model small"):
        harm.SafetyGuard(server=Path("/x"), model=tmp_path / "missing.gguf").score("anything")


# --- HarmModel -----------------------------------------------------------------------------------

class Stub:
    def __init__(self, size):
        self.size = size
        self.unloaded = False
        import threading

        self._lock = threading.Lock()

    def score(self, text):
        return 0.42 if "bad" in text else 0.01

    def categories(self, text):
        return ["Violent"] if "bad" in text else []

    def unload(self):
        self.unloaded = True


def test_harm_model_passes_score_and_categories_through(monkeypatch):
    monkeypatch.setattr(harm, "_model", Stub)
    hm = harm.HarmModel("0.6b", "low")
    assert hm.label == harm.NAMES["0.6b"] and hm.threshold == harm.LEVELS["0.6b"]["low"][0]
    assert hm.score("a bad thing") == 0.42 and hm.categories("a bad thing") == ["Violent"]
    assert hm.score("fine") == 0.01 and hm.categories("fine") == []


def test_harm_model_level_changes_and_unknown_values_are_ignored(monkeypatch):
    monkeypatch.setattr(harm, "_model", Stub)
    hm = harm.HarmModel("small", "nonsense")
    assert hm.level == harm.DEFAULT_LEVEL
    g = guard.Guard("warn", hm, threshold=hm.threshold, name="harm")
    hm.guard = g
    first = hm._model
    hm.want("small", "high")  # same size: the model stays, the threshold follows the level
    assert hm._model is first and not first.unloaded and g.threshold == harm.LEVELS["small"]["high"][0]
    hm.want("13b", "extreme")  # neither is known: nothing changes
    assert hm._model is first and hm.level == "high" and g.threshold == harm.LEVELS["small"]["high"][0]
    hm.want("4b")  # the level carries over to the new size; the old model is unloaded
    assert hm.size == "4b" and first.unloaded and g.threshold == harm.LEVELS["4b"]["high"][0]
    assert hm.label == harm.NAMES["4b"]


def test_harm_model_without_a_guard_still_switches(monkeypatch):
    monkeypatch.setattr(harm, "_model", Stub)
    hm = harm.HarmModel("small")
    hm.want("0.6b", "lowest")
    assert hm.size == "0.6b" and hm.threshold == harm.LEVELS["0.6b"]["lowest"][0]


def test_every_size_has_every_level_and_medium_is_its_own_threshold():
    for size in harm.SIZES:
        assert set(harm.LEVELS[size]) == set(harm.LEVEL_NAMES)
        assert harm.LEVELS[size]["medium"][0] == harm.MODELS[size][5]
        ts = [harm.LEVELS[size][lv][0] for lv in harm.LEVEL_NAMES]
        assert ts == sorted(ts)  # high flags most, lowest least


def test_typed_keeps_only_what_the_user_wrote():
    text = "<system-reminder>be nice</system-reminder>please fix it<command-name>x</command-name>"
    assert harm.typed(text) == "please fix it"
    assert harm.typed("# AGENTS.md instructions for /repo\n\nlots of rules") == ""
