import hashlib
import os
import stat
import sys

import pytest

from keepmum.detect import pfserve
from keepmum.detect.pfserve import PfServe, PfServeError

# A stand-in for pf-serve that speaks its protocol: it reports " Priya Raman" (with the leading
# space a BPE token carries) as a person, "ghp_" tokens as secrets, and "kitten" with a label
# keepmum does not map. MODE selects a failure; "crash-once" crashes on its first request only.
FAKE = r'''#!{python}
import json, os, re, struct, sys, time
mode = "{mode}"
marker = "{marker}"
out = sys.stdout.buffer
if mode == "nohello":
    sys.exit(1)
if mode == "badload":
    out.write(b'{{"error":"the model didn\'t load"}}\n'); out.flush(); sys.exit(1)
out.write(b'{{"device":"Metal: fake","gpu":true,"ms":5}}\n'); out.flush()
inp = sys.stdin.buffer
def u32():
    b = inp.read(4)
    return struct.unpack("<I", b)[0] if len(b) == 4 else None
while True:
    n = u32()
    if n is None:
        break
    texts = [inp.read(u32()) for _ in range(n)]
    if mode == "error":
        msg = b"classify failed"
        out.write(struct.pack("<II", 1, len(msg)) + msg); out.flush(); continue
    if mode == "crash-once" and not os.path.exists(marker):
        open(marker, "w").close()
        sys.exit(3)
    if mode == "hang":
        time.sleep(30)
    reply = struct.pack("<I", 0)
    for t in texts:
        found = [(m.start(), m.end(), 0.97, b"private_person") for m in re.finditer(rb" Priya Raman", t)]
        found += [(m.start(), m.end(), 0.88, b"secret") for m in re.finditer(rb"ghp_[A-Za-z0-9]+", t)]
        found += [(m.start(), m.end(), 0.5, b"kitten") for m in re.finditer(rb"kitten", t)]
        reply += struct.pack("<I", len(found))
        for a, b, s, lab in found:
            reply += struct.pack("<iif", a, b, s) + bytes([len(lab)]) + lab
    out.write(reply); out.flush()
'''


def fake(tmp_path, mode="ok"):
    p = tmp_path / f"pf-serve-{mode}"
    p.write_text(FAKE.format(python=sys.executable, mode=mode, marker=tmp_path / "crashed"))
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


def test_spans_use_str_offsets_after_multibyte_text(tmp_path):
    pf = PfServe(fake(tmp_path), "model.gguf")
    try:
        token = "ghp_" + "Zq8vX2mN4pL7kR9tY3wB6cF1hJ5dG0sAe7Qw"
        text = f"Café owner — 日本語 — is Priya Raman; token {token} kitten"
        spans = pf.detect(text)
        got = {(text[s.start:s.end], s.type, round(s.confidence, 2), s.source) for s in spans}
        assert got == {("Priya Raman", "PERSON", 0.97, "pf-serve"), (token, "SECRET", 0.88, "pf-serve")}
        assert pf.device == "Metal: fake"
        assert pf.detect("   ") == []  # nothing sent for blank text
    finally:
        pf.close()


def test_many_requests_on_one_process(tmp_path):
    pf = PfServe(fake(tmp_path), "model.gguf")
    try:
        for i in range(50):
            assert len(pf.detect(f"{i} ghp_abc{i}")) == 1
    finally:
        pf.close()


@pytest.mark.parametrize("mode, message", [("nohello", "did not start: pf-serve closed its output"), ("badload", "the model didn't load")])
def test_start_failures_are_reported(tmp_path, mode, message):
    with pytest.raises(PfServeError, match=message):
        PfServe(fake(tmp_path, mode), "model.gguf")


def test_error_reply_raises_and_keeps_the_process(tmp_path):
    pf = PfServe(fake(tmp_path, "error"), "model.gguf")
    try:
        pid = pf._proc.pid
        for _ in range(2):
            with pytest.raises(PfServeError, match="classify failed"):
                pf.detect("hello Priya Raman")
        assert pf._proc.pid == pid and pf.restarts == 0
    finally:
        pf.close()


def test_a_crash_fails_that_request_and_the_next_one_restarts(tmp_path):
    pf = PfServe(fake(tmp_path, "crash-once"), "model.gguf")
    try:
        with pytest.raises(PfServeError, match="closed its output"):
            pf.detect("hello Priya Raman")
        assert [s.type for s in pf.detect("hello Priya Raman")] == ["PERSON"]
        assert pf.restarts == 1
    finally:
        pf.close()


def test_a_hung_process_times_out(tmp_path, monkeypatch):
    monkeypatch.setattr(pfserve, "request_timeout", lambda n: 0.5)
    pf = PfServe(fake(tmp_path, "hang"), "model.gguf")
    with pytest.raises(PfServeError, match="timed out"):
        pf.detect("hello Priya Raman")
    assert pf._proc is None  # killed; the next request starts a new one


def test_finds_the_binary_and_model(tmp_path, monkeypatch):
    for k in ("KEEPMUM_PF_SERVE", "KEEPMUM_PF_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    monkeypatch.setattr(pfserve, "MODEL_SIZE", 5)
    assert pfserve.find_binary() is None and pfserve.find_model() is None
    binary = tmp_path / "data" / "keepmum" / "bin" / "pf-serve"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    hf = pfserve.model_candidates()[1]
    hf.parent.mkdir(parents=True)
    hf.write_bytes(b"12345")
    assert pfserve.find_binary() == binary and pfserve.find_model() == hf
    hf.write_bytes(b"1234")  # wrong size: an interrupted download
    assert pfserve.find_model() is None
    monkeypatch.setenv("KEEPMUM_PF_MODEL", str(hf))
    assert pfserve.find_model() == hf


class _Stream:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_bytes(self, n):
        yield from (self.body[i:i + n] for i in range(0, len(self.body), n))


@pytest.mark.parametrize("served, ok", [(b"weights!", True), (b"weightz!", False)])
def test_download_checks_size_and_hash(tmp_path, monkeypatch, served, ok):
    import httpx

    monkeypatch.delenv("KEEPMUM_PF_MODEL", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    monkeypatch.setattr(pfserve, "MODEL_SIZE", 8)
    monkeypatch.setattr(pfserve, "MODEL_SHA256", hashlib.sha256(b"weights!").hexdigest())
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _Stream(served))
    if ok:
        path = pfserve.download_model()
        assert path.read_bytes() == b"weights!" and path == pfserve.find_model()
    else:
        with pytest.raises(PfServeError, match="does not match"):
            pfserve.download_model()
        assert not any(p.exists() for p in pfserve.model_candidates())
        assert not os.listdir(pfserve.model_candidates()[0].parent)  # no partial file left


def test_an_idle_model_is_unloaded_and_loads_again_with_the_next_request(tmp_path, monkeypatch):
    """It holds 1.7 GB while loaded: after IDLE_S without a request it goes, as the checks' do."""
    import time

    monkeypatch.setattr(PfServe, "idle_s", 0.2)
    pf = PfServe(fake(tmp_path), "model.gguf")
    try:
        assert pf.loaded() and [s.type for s in pf.detect("hello Priya Raman")] == ["PERSON"]
        deadline = time.monotonic() + 5
        while (pf.loaded() or pf.unloads == 0) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not pf.loaded() and pf.unloads == 1
        assert [s.type for s in pf.detect("and ghp_abc123 too")] == ["SECRET"]
        assert pf.loaded() and pf.restarts == 0  # loaded again: not a crash
    finally:
        pf.close()


def test_a_long_reply_is_read_whole(tmp_path):
    """Replies are read in large pieces through one buffer: many spans, split across reads."""
    pf = PfServe(fake(tmp_path), "model.gguf")
    try:
        text = " ".join(f"ghp_{i:06d}" for i in range(3000))
        spans = pf.detect(text)
        assert len(spans) == 3000 and text[spans[-1].start:spans[-1].end] == "ghp_002999"
    finally:
        pf.close()
