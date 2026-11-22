"""The Privacy Filter's results on disk (detect/spancache.py)."""

import hashlib
import json
import stat

from keepmum.detect import Span
from keepmum.detect.pipeline import Pipeline
from keepmum.detect.spancache import FORMAT, SpanStore, namespace

SALT = bytes(range(32))
TEXT = "the staging key is 3f9a1c7e5b2d4f6a8c0e1b3d5f7a9c2e, ask Margaret Thornbury"


class FakePf:
    """A model that finds the key and the name, and counts what it reads."""

    name = "pf-serve"

    def __init__(self):
        self.read = []

    def detect(self, text):
        self.read.append(text)
        out = []
        for value, kind in (("3f9a1c7e5b2d4f6a8c0e1b3d5f7a9c2e", "SECRET"), ("Margaret Thornbury", "PERSON")):
            if value in text:
                a = text.index(value)
                out.append(Span(a, a + len(value), kind, 0.97, self.name))
        return out


def _store(tmp_path, salt=SALT, space="ns", **kw):
    return SpanStore(salt, space, tmp_path / "keepmum" / "model-spans.json", save_after=60, **kw)


def _pipeline(tmp_path, pf, **kw):
    p = Pipeline(None, pf, pii=True)
    p.keep_on_disk(lambda model: _store(tmp_path, **kw))
    return p


def test_a_restart_reads_the_model_s_results_from_disk(tmp_path):
    pf = FakePf()
    first = _pipeline(tmp_path, pf)
    spans = first.model_spans(TEXT)
    assert [s.type for s in spans] == ["SECRET", "PERSON"] and len(pf.read) == 1
    first.store.flush()
    again = FakePf()
    restarted = _pipeline(tmp_path, again)
    assert restarted.model_spans(TEXT) == spans  # the same spans, the same source
    assert again.read == []  # and the model read nothing


def test_the_file_holds_no_text_and_only_keyed_hashes(tmp_path):
    p = _pipeline(tmp_path, FakePf())
    p.model_spans(TEXT)
    p.store.flush()
    path = tmp_path / "keepmum" / "model-spans.json"
    raw = path.read_text()
    assert "3f9a" not in raw and "Margaret" not in raw and "staging" not in raw
    assert hashlib.sha256(TEXT.encode()).hexdigest() not in raw  # a guessed text cannot be checked
    data = json.loads(raw)
    assert data["format"] == FORMAT and data["entries"][0][1] == [[19, 51, "SECRET", 0.97], [57, 75, "PERSON", 0.97]]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    # Another install's salt finds nothing it can use.
    other = _store(tmp_path, salt=bytes(32))
    assert other.key_of(TEXT) not in {k for k, _ in other.load()}


def test_another_model_format_or_a_damaged_file_starts_over(tmp_path):
    store = _store(tmp_path)
    store.changed(lambda: [(store.key_of(TEXT), (Span(0, 3, "SECRET", 0.9),))])
    store.flush()
    assert len(store.load()) == 1
    assert _store(tmp_path, space="another model").load() == []
    path = store.path
    data = json.loads(path.read_text())
    for damaged in ({**data, "format": FORMAT + 1}, {**data, "entries": [["zz", []]]},
                    {**data, "entries": [[data["entries"][0][0], [[5, 2, "SECRET", 0.9]]]]},
                    {**data, "entries": [[data["entries"][0][0], [[0, 3, "PASSWORD", 0.9]]]]}):
        path.write_text(json.dumps(damaged))
        assert store.load() == []
    path.write_text("{not json")
    assert store.load() == []


def test_two_keepmum_processes_keep_each_other_s_results(tmp_path):
    a, b = _store(tmp_path), _store(tmp_path)
    a.changed(lambda: [(a.key_of("one"), ())])
    a.flush()
    b.changed(lambda: [(b.key_of("two"), (Span(0, 3, "SECRET", 0.5),))])
    b.flush()
    assert {k for k, _ in a.load()} == {a.key_of("one"), a.key_of("two")}


def test_it_keeps_the_newest_texts(tmp_path):
    store = _store(tmp_path, keep=3)
    store.changed(lambda: [(store.key_of(str(i)), ()) for i in range(5)])
    store.flush()
    assert [k for k, _ in store.load()] == [store.key_of(str(i)) for i in (2, 3, 4)]


def test_nothing_is_written_until_a_result_is_new(tmp_path):
    p = _pipeline(tmp_path, FakePf())
    p.store.flush()
    assert not p.store.path.exists() and p.store.saves == 0
    p.model_spans(TEXT)
    p.store.flush()
    p.model_spans(TEXT)  # seen: no write owed
    p.store.flush()
    assert p.store.saves == 1


def test_the_model_off_and_on_again_reads_the_file_again(tmp_path):
    p = _pipeline(tmp_path, FakePf())
    p.model_spans(TEXT)
    p.set_model(None)  # writes what it had
    assert p.store is None and p.model_spans(TEXT) == []
    pf = FakePf()
    p.set_model(pf)
    assert p.model_spans(TEXT) and pf.read == []


def test_the_namespace_follows_pf_serve_the_model_and_the_threshold(tmp_path):
    binary, model = tmp_path / "pf-serve", tmp_path / "model.gguf"
    binary.write_bytes(b"build 1")
    model.write_bytes(b"weights")
    first = namespace([str(binary), str(model), "0.0"])
    assert namespace([str(binary), str(model), "0.0"]) == first
    assert namespace([str(binary), str(model), "0.5"]) != first
    binary.write_bytes(b"build 2")
    assert namespace([str(binary), str(model), "0.0"]) != first
