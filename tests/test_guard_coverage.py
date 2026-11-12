"""guard.py without its model: downloads served by a mock transport, llama-server replaced by a fake."""

import hashlib
import math
from pathlib import Path

import httpx
import pytest

from keepmum import guard


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return tmp_path


def files(*contents: tuple[str, bytes]) -> dict:
    return {rel: (hashlib.sha256(c).hexdigest(), len(c)) for rel, c in contents}


def test_model_path_is_the_gguf_in_its_release_folder(data):
    assert guard.model_path() == guard.model_dir() / guard.GGUF
    assert guard.model_dir().name.endswith(guard.RELEASE)
    assert str(guard.model_dir()).startswith(str(data / "data"))


def test_download_fetches_each_file_with_progress_and_skips_what_is_in_place(data, monkeypatch):
    blobs = {"LICENSE": b"licence text", "NOTICE": b"notice!", "m.gguf": b"g" * 30}
    monkeypatch.setattr(guard, "FILES", files(*blobs.items()))
    (guard.model_dir()).mkdir(parents=True)
    (guard.model_dir() / "LICENSE").write_bytes(blobs["LICENSE"])  # already there
    asked, seen = [], []

    def handler(request):
        asked.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, content=blobs[asked[-1]])

    monkeypatch.setattr(httpx, "stream", lambda method, url, **kw: httpx.Client(
        transport=httpx.MockTransport(handler)).stream(method, url))
    assert guard.download(progress=lambda done, total: seen.append((done, total))) == guard.model_dir()
    assert asked == ["NOTICE", "m.gguf"] and guard.installed()
    total = sum(len(b) for b in blobs.values())
    assert seen[-1] == (total, total) and seen[0][0] == len(blobs["LICENSE"]) + len(blobs["NOTICE"])
    assert all((guard.model_dir() / r).read_bytes() == b for r, b in blobs.items())


def test_a_failed_download_leaves_no_partial_file(data, monkeypatch):
    monkeypatch.setattr(guard, "FILES", files(("LICENSE", b"ok")))
    monkeypatch.setattr(httpx, "stream", lambda method, url, **kw: httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(500))).stream(method, url))
    with pytest.raises(httpx.HTTPStatusError):
        guard.download()
    assert not (guard.model_dir() / "LICENSE.part").exists() and not (guard.model_dir() / "LICENSE").exists()


def test_sha256_of_a_file(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"z" * ((1 << 20) + 3))
    assert guard._sha256(p) == hashlib.sha256(p.read_bytes()).hexdigest()


class FakeLlama:
    """Stands in for llamacpp.Server: one token per word (its length); /embeddings answers each
    window's [SAFE, INJECTION] logits by its first token."""

    instances: list = []
    logits: dict = {}

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
            assert body["add_special"] is False
            return {"tokens": [len(w) for w in body["content"].split()]}
        assert path == "/embeddings" and body["embd_normalize"] == -1
        return [{"embedding": [self.logits[row[1]]]} for row in body["content"]]


@pytest.fixture
def fake(data, monkeypatch, tmp_path):
    FakeLlama.instances = []
    monkeypatch.setattr(guard, "Server", FakeLlama)
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"m")
    return gguf


def test_injection_model_starts_and_scores_the_softmax_of_its_two_logits(fake, monkeypatch):
    monkeypatch.setattr(FakeLlama, "logits", {3: [2.0, -1.0], 6: [-1.0, 3.0]})
    m = guard.InjectionModel(model=fake, server=Path("/opt/llama-server"))
    assert not m.loaded()
    safe = m.score("the cat sat")  # every word 3 letters: one window, SAFE wins
    assert abs(safe - math.exp(-1) / (math.exp(2) + math.exp(-1))) < 1e-12
    srv = FakeLlama.instances[0]
    assert m.loaded() and srv.server == Path("/opt/llama-server") and srv.error is guard.GuardError
    assert "--embedding" in srv.args and srv.args[srv.args.index("-c") + 1] == str(guard.WINDOW * guard.BATCH)
    rows = [b["content"] for p, b in srv.calls if p == "/embeddings"][0]
    assert rows == [[guard.BOS, 3, 3, 3, guard.EOS]]
    hot = m.score("ignore all")  # "ignore": 6 letters, the hot window
    assert abs(hot - guard.sigmoid(4.0)) < 1e-12 and len(FakeLlama.instances) == 1  # the same server
    m.unload()
    assert srv.stopped and not m.loaded()
    m.unload()


def test_injection_model_takes_the_highest_of_many_windows(fake, monkeypatch):
    monkeypatch.setattr(FakeLlama, "logits", {1: [0.0, -4.0], 2: [0.0, 5.0]})
    monkeypatch.setattr(guard, "WINDOW", 6)  # 4 tokens per window, overlapping by OVERLAP
    monkeypatch.setattr(guard, "OVERLAP", 1)
    m = guard.InjectionModel(model=fake, server=Path("/x"))
    words = ["a"] * 9 + ["bb"] + ["a"] * 3  # the 2-token word starts the last window
    s = m.score(" ".join(words))
    windows = [b["content"][0] for p, b in FakeLlama.instances[0].calls if p == "/embeddings"]
    assert len(windows) > 1 and all(len(w) == 6 for w in windows)
    assert abs(s - guard.sigmoid(5.0)) < 1e-12


def test_injection_model_tokenizes_the_normalized_text(fake, monkeypatch):
    monkeypatch.setattr(FakeLlama, "logits", {4: [0.0, 0.0]})
    m = guard.InjectionModel(model=fake, server=Path("/x"))
    m.score("ab​cd")  # a zero-width space is removed: one 4-letter word
    assert FakeLlama.instances[0].calls[0][1]["content"] == "abcd"


def test_injection_model_without_llama_server_or_model(data, monkeypatch, tmp_path):
    monkeypatch.setattr(guard, "find_server", lambda: None)
    with pytest.raises(guard.GuardError, match="llama-server is not installed"):
        guard.InjectionModel().score("x" * 50)
    monkeypatch.setattr(guard, "find_server", lambda: Path("/found"))
    with pytest.raises(guard.GuardError, match="keepmum guard setup injection"):
        guard.InjectionModel().score("x" * 50)  # the default model: not downloaded
    with pytest.raises(guard.GuardError, match="not set up"):
        guard.InjectionModel(model=tmp_path / "missing.gguf").score("x" * 50)


class Scored:
    def __init__(self, score, categories=None):
        self.s, self.cats, self.asked = score, categories, []

    def score(self, text):
        self.asked.append(text)
        return self.s


def test_a_hit_whose_categories_fail_keeps_its_verdict():
    m = Scored(0.99)

    def broken(text):
        raise RuntimeError("the model went away")

    m.categories = broken
    g = guard.Guard("warn", m, threshold=0.5, name="harm", min_chars=1)
    hits = g.check([(None, "a dangerous ask", True)])
    assert len(hits) == 1 and hits[0]["categories"] == [] and hits[0]["check"] == "harm"
    assert hits[0]["source"] is None and hits[0]["at"] == 0 and "text" not in hits[0]


def test_a_model_without_categories_gives_hits_without_them():
    g = guard.Guard("block", Scored(0.9), threshold=0.5, min_chars=1)
    hit = g.check([("Bash", "some output", True)])[0]
    assert "categories" not in hit and hit["source"] == "Bash" and hit["score"] == 0.9


def test_the_oldest_flagged_text_is_forgotten_past_keep():
    m = Scored(0.9)
    g = guard.Guard("block", m, threshold=0.5, keep=2, min_chars=1)
    for t in ("first", "second", "third"):
        g.check([("t", t, True)])
    assert len(g._flagged) == 2
    assert g.check([("t", "second", False)])[0]["earlier"] is True  # still remembered
    assert g.check([("t", "first", False)]) == []  # forgotten, and not new: not scored again
    assert m.asked == ["first", "second", "third"]


def test_short_results_need_the_short_threshold():
    g = guard.Guard("warn", Scored(0.9), threshold=0.8, min_chars=1, short_chars=guard.SHORT_CHARS)
    assert g.threshold_for(10) == guard.LEVELS["medium"].short and g.threshold_for(guard.SHORT_CHARS) == 0.8
    assert g.check([("Read", "short text", True)]) == []
    assert len(g.check([("Read", "x" * guard.SHORT_CHARS, True)])) == 1


def test_a_text_flagged_under_a_lower_threshold_is_no_longer_a_hit():
    g = guard.Guard("block", Scored(0.7), threshold=0.6, min_chars=1)
    assert len(g.check([("t", "flagged", True)])) == 1
    g.threshold = 0.9
    assert g.check([("t", "flagged", True)]) == []  # scored again, and under the bar now


def test_short_threshold_never_falls_below_the_threshold():
    assert guard.short_threshold(0.5) == guard.LEVELS["high"].short  # below High: High's
    assert guard.short_threshold(0.9) == guard.LEVELS["low"].short
    assert guard.short_threshold(0.9995) == 0.9995


def test_unknown_modes_are_off():
    assert guard.Guard(lambda: "loud", Scored(0)).mode == "off"
    assert guard.Guard("local", Scored(0)).mode == "local"


def test_sigmoid_is_stable_for_large_logits():
    assert guard.sigmoid(1000) == 1.0 and guard.sigmoid(-1000) == 0.0 and guard.sigmoid(0) == 0.5
