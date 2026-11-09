import json
import random
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from keepmum.cli import app
from keepmum.detect import Span
from keepmum.detect.pipeline import Pipeline
from keepmum.detect.rules import RulesDetector
from keepmum.redact import Redactor
from keepmum.restore import restore_text
from keepmum.vault import PLACEHOLDER, Vault, install_salt, salt_file

ROOT = Path(__file__).resolve().parents[1]
SALT = bytes(range(32))  # fixed test salt; real installs use ~/.config/keepmum/salt
# Synthetic only: a random string in GitHub's token format.
FAKE_TOKEN = "ghp_" + "Zq8vX2mN4pL7kR9tY3wB6cF1hJ5dG0sAe7Qw"


class Fixed:
    """A detector that returns given (start, end, type) spans for any text containing a marker."""

    def __init__(self, fn):
        self.fn = fn
        self.calls = 0

    def detect(self, text):
        self.calls += 1
        return self.fn(text)


@pytest.fixture(scope="session")
def dataset(tmp_path_factory) -> list[dict]:
    out = tmp_path_factory.mktemp("data")
    subprocess.run([sys.executable, str(ROOT / "bench" / "gen.py"), "--out", str(out)], check=True, capture_output=True)
    return [json.loads(line) for f in sorted(out.glob("*.jsonl")) for line in f.read_text().splitlines()]


def round_trip(redactor, text):
    masked = redactor.redact_text(text)
    assert restore_text(masked, redactor.vault) == text
    return masked


def test_every_dataset_record_round_trips_with_rules(dataset):
    r = Redactor(Pipeline(RulesDetector(), None, pii=True), Vault(SALT))
    for rec in dataset:
        masked = round_trip(r, rec["text"])
        for ph in PLACEHOLDER.finditer(masked):  # no masked value survives in the output
            assert r.vault.value(ph.group()) not in masked


def test_every_dataset_record_round_trips_with_the_model(dataset):
    from keepmum.detect.pipeline import ModelUnavailable, default_pipeline

    try:
        pipeline = default_pipeline(pii=True)
    except ModelUnavailable:
        pytest.skip("Privacy Filter model not downloaded")
    r = Redactor(pipeline, Vault(SALT))
    for rec in dataset:
        round_trip(r, rec["text"])


def test_round_trip_property():
    rng = random.Random(0)
    alphabet = "abcdef0123456789 _-=:\"'\n\t/@.ÄéЖ漢🙂KEEPMUM_SECRET_"
    for _ in range(2000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 80)))

        def spans(t, rng=rng):
            out = []
            for _ in range(rng.randint(0, 4)):
                if not t:
                    break
                a = rng.randrange(len(t))
                b = rng.randint(a + 1, min(len(t), a + 12))
                out.append(Span(a, b, rng.choice(["SECRET", "PERSON", "EMAIL"])))
            return out

        round_trip(Redactor(Fixed(spans), Vault(SALT)), text)


def test_placeholder_followed_by_hex_text_restores():
    text = "token=Xk29fLq8vPzabcd1234"
    r = Redactor(Fixed(lambda t: [Span(6, 17, "SECRET")] if t else []), Vault(SALT))
    masked = round_trip(r, text)
    assert masked.endswith("abcd1234")


def test_placeholders_are_deterministic_and_typed():
    a, b = Vault(SALT), Vault(SALT)
    assert a.placeholder("SECRET", "v1") == b.placeholder("SECRET", "v1")
    assert a.placeholder("SECRET", "v1") != a.placeholder("PERSON", "v1")
    assert Vault(bytes(32)).placeholder("SECRET", "v1") != a.placeholder("SECRET", "v1")


def test_collision_extends_the_id():
    import hashlib
    import hmac

    v = Vault(SALT)
    # Pretend another value already owns the 8-digit id of "second".
    short = "KEEPMUM_SECRET_" + hmac.new(SALT, b"SECRET\0second", hashlib.sha256).hexdigest()[:8]
    v._values[short] = "someone else"
    ph = v.placeholder("SECRET", "second")
    assert len(ph.rsplit("_", 1)[1]) == 12 and v.value(ph) == "second"


def test_vault_never_shows_values():
    v = Vault(SALT)
    v.placeholder("SECRET", FAKE_TOKEN)
    assert FAKE_TOKEN not in repr(v) and FAKE_TOKEN not in str(v)


def test_salt_file_is_private(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    salt = install_salt()
    assert len(salt) == 32 and install_salt() == salt
    assert salt_file().stat().st_mode & 0o777 == 0o600


def test_known_values_mask_later_occurrences():
    # The detector only recognizes the token in an `export` line; the vault catches it elsewhere.
    def only_in_export(t):
        if "export" not in t:
            return []
        a = t.index(FAKE_TOKEN)
        return [Span(a, a + len(FAKE_TOKEN), "SECRET")]

    r = Redactor(Fixed(only_in_export), Vault(SALT))
    r.redact_text(f"export TOKEN={FAKE_TOKEN}")
    assert FAKE_TOKEN not in r.redact_text(f"it failed with {FAKE_TOKEN} again")


def test_known_values_respect_word_boundaries():
    v = Vault(SALT)
    r = Redactor(Fixed(lambda t: [Span(0, 4, "PERSON")] if t.startswith("Anna ") else []), v)
    r.redact_text("Anna called")
    assert r.redact_text("flights to Annapolis") == "flights to Annapolis"


def test_detection_is_cached_per_text():
    det = Fixed(lambda t: [])
    r = Redactor(det, Vault(SALT))
    for _ in range(3):
        r.redact_text("the same block, resent every turn")
    assert det.calls == 1


def test_request_walk_masks_content_and_keeps_protocol_fields():
    det = Fixed(lambda t: [Span(t.index(FAKE_TOKEN), t.index(FAKE_TOKEN) + len(FAKE_TOKEN), "SECRET")] if FAKE_TOKEN in t else [])
    r = Redactor(det, Vault(SALT))
    body = {
        "model": "claude-opus-5-5",
        "system": [{"type": "text", "text": f"env has {FAKE_TOKEN}", "cache_control": {"type": "ephemeral"}}],
        "tools": [{"name": "Bash", "description": f"never scanned {FAKE_TOKEN}"}],
        "messages": [
            {"role": "user", "content": f"why does {FAKE_TOKEN} fail?"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": f"signed {FAKE_TOKEN}", "signature": "sig"},
                {"type": "tool_use", "id": "toolu_01", "name": "Bash", "input": {"command": f"echo {FAKE_TOKEN}", FAKE_TOKEN: 1}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_01", "content": [{"type": "text", "text": FAKE_TOKEN}]},
                {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": f"doc {FAKE_TOKEN}"}},
                {"type": "future_block", "id": "x1", "text": f"new {FAKE_TOKEN}"},
            ]},
        ],
    }
    out = r.redact_request(body)
    dumped = json.dumps(out)
    msgs = out["messages"]
    assert FAKE_TOKEN not in json.dumps(out["system"]) and FAKE_TOKEN not in json.dumps(msgs[0])
    assert out["tools"] == body["tools"]  # skipped
    assert msgs[1]["content"][0] == body["messages"][1]["content"][0]  # signed thinking untouched
    tool = msgs[1]["content"][1]
    assert tool["id"] == "toolu_01" and tool["name"] == "Bash" and FAKE_TOKEN in tool["input"]  # key kept
    assert FAKE_TOKEN not in tool["input"]["command"]
    assert msgs[2]["content"][0]["tool_use_id"] == "toolu_01"
    assert FAKE_TOKEN not in json.dumps(msgs[2])
    assert msgs[2]["content"][2]["id"] == "x1"
    assert body["messages"][0]["content"].endswith("fail?") and FAKE_TOKEN in body["messages"][0]["content"]  # input not mutated
    assert dumped.count("KEEPMUM_SECRET_") >= 6


def test_scan_reports_without_printing_the_value(tmp_path):
    f = tmp_path / "config.py"
    f.write_text(f'GITHUB_TOKEN = "{FAKE_TOKEN}"\n')
    result = CliRunner().invoke(app, ["scan", "--rules-only", str(f)])
    assert result.exit_code == 1
    assert f"{f}:1:17  SECRET" in result.output
    assert FAKE_TOKEN not in result.output and FAKE_TOKEN[:8] not in result.output


def test_scan_clean_file_exits_zero(tmp_path):
    f = tmp_path / "ok.py"
    f.write_text('api_key = os.environ["API_KEY"]\n')
    assert CliRunner().invoke(app, ["scan", "--rules-only", str(f)]).exit_code == 0


@pytest.mark.parametrize("system", [None, "You are helpful.", [{"type": "text", "text": "a", "cache_control": {"type": "ephemeral"}}]])
def test_placeholder_hint_is_appended_last_and_stable(system):
    from keepmum.redact import PLACEHOLDER_HINT

    r = Redactor(Fixed(lambda t: []), Vault(SALT))
    body = {"model": "m", "messages": []}
    if system is not None:
        body["system"] = system
    first, second = r.redact_request(body), r.redact_request(body)
    assert first["system"][-1] == {"type": "text", "text": PLACEHOLDER_HINT}
    assert first == second  # identical every turn, so the prompt cache stays valid
    if isinstance(system, list):
        assert first["system"][:-1] == system  # cache breakpoint untouched
    assert not PLACEHOLDER.search(PLACEHOLDER_HINT)  # its examples can never collide with a real placeholder


def test_document_metadata_search_result_and_dict_key_restore():
    from collections import Counter

    from keepmum.restore import UNRESOLVED, StreamRestorer, restore_response

    r = Redactor(Pipeline(RulesDetector(), None), Vault(SALT))
    body = {
        "model": "m",
        "messages": [{"role": "user", "content": [
            {"type": "document", "title": f"GITHUB_TOKEN={FAKE_TOKEN}", "context": f"ctx GITHUB_TOKEN={FAKE_TOKEN}",
             "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0xLjQK"}},
            {"type": "search_result", "title": f"GITHUB_TOKEN={FAKE_TOKEN}", "source": f"https://x/?k={FAKE_TOKEN}",
             "content": [{"type": "text", "text": f"GITHUB_TOKEN={FAKE_TOKEN}"}]},
        ]}],
    }
    out = r.redact_request(body)
    assert FAKE_TOKEN not in json.dumps(out)
    ph = next(iter(r.vault._values))
    reply = {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {ph: f"val {ph}"}}]}
    restored = restore_response(reply, r.vault)
    assert restored["content"][0]["input"] == {FAKE_TOKEN: f"val {FAKE_TOKEN}"}

    # Unparseable tool JSON fallback handles 8-digit + hex suffix and counts unresolved placeholders.
    counts = Counter()
    sr = StreamRestorer(r.vault, counts=counts)
    events = (
        f'event: content_block_start\ndata: {{"type":"content_block_start","index":0,"content_block":{{"type":"tool_use","name":"Bash"}}}}\n\n'
        f'event: content_block_delta\ndata: {{"type":"content_block_delta","index":0,"delta":{{"type":"input_json_delta","partial_json":"{{{ph}abcd KEEPMUM_SECRET_deadbeef"}}}}\n\n'
        f'event: content_block_stop\ndata: {{"type":"content_block_stop","index":0}}\n\n'
    ).encode()
    text = (sr.feed(events) + sr.close()).decode()
    assert f"{FAKE_TOKEN}abcd" in text and counts["SECRET"] == 1 and counts[UNRESOLVED] == 1


def test_secret_assignment_rule_has_no_quadratic_backtracking():
    import time

    det = RulesDetector()
    t0 = time.monotonic()
    assert det.detect("prefix_" + "token_" * 8000) == []
    assert time.monotonic() - t0 < 0.25


def test_lone_surrogates_do_not_crash_or_shift_offsets(tmp_path):
    from keepmum.audit import AuditLog
    from keepmum.config import Config
    from keepmum.detect.pfserve import PfServe

    r = Redactor(Pipeline(RulesDetector(), None), Vault(SALT))
    text = f"prefix \ud800 export GITHUB_TOKEN={FAKE_TOKEN}"
    masked = r.redact_text(text)
    assert FAKE_TOKEN not in masked and restore_text(masked, r.vault) == text

    log = AuditLog(tmp_path / "audit.jsonl")
    entry = log.note_config(Config(allow_strings=["bad\ud800secret"]), "test")
    assert entry is not None

    # PfServe.detect replaces lone surrogates 1-for-1 so later character spans stay aligned.
    import struct
    import threading

    pf = PfServe.__new__(PfServe)
    pf.name = "pf-serve"
    pf._lock = threading.Lock()
    pf._proc = type("P", (), {"poll": lambda self: None})()
    sent: list[bytes] = []

    def fake_write(data: bytes, deadline: float):
        sent.append(data)

    def fake_read(n: int, deadline: float) -> bytes:
        if fake_read.buf is None:
            raw = sent[0][8:]
            b_start = raw.index(FAKE_TOKEN.encode())
            b_end = b_start + len(FAKE_TOKEN)
            label = b"secret"
            fake_read.buf = struct.pack("<IIiifB", 0, 1, b_start, b_end, 0.99, len(label)) + label
        out, fake_read.off = fake_read.buf[fake_read.off:fake_read.off + n], fake_read.off + n
        return out

    fake_read.buf = None
    fake_read.off = 0
    pf._write = fake_write
    pf._read = fake_read
    spans = pf.detect(text)
    assert len(spans) == 1 and text[spans[0].start:spans[0].end] == FAKE_TOKEN


def test_a_url_flagged_for_the_name_inside_it_masks_only_the_name():
    """The model tags a search URL with a name in it as one URL (a live pi run, web-01): the name is
    masked, and the command around it stays readable."""
    name = "Margaret Thornbury"
    cmd = f'curl -sL "https://html.duckduckgo.com/html/?q={name}" | head -c 600'

    def spans(text):
        out = []
        if name in text and "https://" in text:
            a = text.index("https://")
            out.append(Span(a, text.index(name) + len(name), "URL", 0.9, "pf-serve"))
        if name in text:
            out.append(Span(text.index(name), text.index(name) + len(name), "PERSON", 0.9, "pf-serve"))
        return out

    r = Redactor(Fixed(spans), Vault(bytes(range(32))))
    out = r.redact_text(cmd)
    assert out.startswith('curl -sL "https://html.duckduckgo.com/html/?q=KEEPMUM_PERSON_') and name not in out
    # Seen before (known), with only the URL flagged now: the same.
    r2 = Redactor(Fixed(lambda t: spans(t)[:1]), r.vault)
    r2.redact_text(f"Client: {name}")
    assert r2.redact_text(cmd) == out
    # A URL with nothing narrower inside is masked whole, as before.
    url = "see https://intranet.example/private/x"
    r3 = Redactor(Fixed(lambda t: [Span(4, len(t), "URL", 0.9, "pf-serve")]), Vault(bytes(range(32))))
    assert r3.redact_text(url).startswith("see KEEPMUM_URL_")
