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


def _only_in_export(t):
    """The detector recognizes the token only in an `export` line, as a rule finds an assignment."""
    if "export" not in t or FAKE_TOKEN not in t:
        return []
    a = t.index(FAKE_TOKEN)
    return [Span(a, a + len(FAKE_TOKEN), "SECRET")]


def test_a_value_found_later_in_a_request_is_masked_earlier_in_it_too():
    """A resumed session's first request: the token is plain in message 0 and found in message 2.
    It went out in message 0, and the next request masked it there, so the two differed and the
    provider's prompt cache missed from message 0 (Nov 22)."""
    r = Redactor(Fixed(_only_in_export), Vault(SALT))
    body = {"model": "m", "max_tokens": 8, "messages": [
        {"role": "user", "content": f"why does {FAKE_TOKEN} fail?"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "cat .env"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": f"export TOKEN={FAKE_TOKEN}"}]},
    ]}
    first = r.redact_request(body)
    assert FAKE_TOKEN not in json.dumps(first)
    assert r.counts["SECRET"] == 2  # what went: both places, counted once each
    assert len(r.findings) == 1 and r.fresh == 1  # what the request brought: the one finding, once
    assert [(tool, new) for tool, _, new in r.tool_results] == [("Bash", True)]  # read once, new
    assert [t for _, t, _ in r.user_texts] == [f"why does {FAKE_TOKEN} fail?"]
    r.counts.clear(); r.findings = []; r.fresh = 0; r.tool_results = []; r.user_texts = []
    assert r.redact_request(body) == first  # the same request again goes the same
    assert r.counts["SECRET"] == 2 and r.findings == [] and r.fresh == 0


def test_a_request_with_nothing_new_is_walked_once():
    det = Fixed(_only_in_export)
    r = Redactor(det, Vault(SALT))
    body = {"messages": [{"role": "user", "content": "nothing here"}, {"role": "user", "content": "nor here"}]}
    walks = []
    real = r._redact_request
    r._redact_request = lambda b: walks.append(1) or real(b)
    r.redact_request(body)
    assert len(walks) == 1


def test_responses_and_gemini_requests_are_masked_as_a_whole():
    r = Redactor(Fixed(_only_in_export), Vault(SALT))
    out = r.redact_responses_request({"input": [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"why does {FAKE_TOKEN} fail?"}]},
        {"type": "function_call", "call_id": "c1", "name": "shell", "arguments": json.dumps({"command": ["cat", ".env"]})},
        {"type": "function_call_output", "call_id": "c1", "output": f"export TOKEN={FAKE_TOKEN}"},
    ]})
    assert FAKE_TOKEN not in json.dumps(out)
    r = Redactor(Fixed(_only_in_export), Vault(SALT))
    out = r.redact_gemini_request({"request": {"contents": [
        {"role": "user", "parts": [{"text": f"why does {FAKE_TOKEN} fail?"}]},
        {"role": "user", "parts": [{"functionResponse": {"name": "run", "response": {"output": f"export TOKEN={FAKE_TOKEN}"}}}]},
    ]}}, hint=True)
    assert FAKE_TOKEN not in json.dumps(out)


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


@pytest.mark.parametrize("line", ["GITHUB_TOKEN={tok}$\nLOG_LEVEL=info$\n", "grep '^GITHUB_TOKEN={tok}$' .env"])
def test_a_lines_dollar_is_not_masked_with_the_token_before_it(line):
    """`sed -n l` and `cat -A` end each line with `$`, as a grep anchor does: the generic KEY=value
    match ran one `$` past the token, and the model got a placeholder for both (live, DeepSeek)."""
    v = Vault(bytes(range(32)))
    r = Redactor(Pipeline(RulesDetector()), v)
    out = r.redact_text(line.format(tok=FAKE_TOKEN))
    ph = r.redact_text(f"GITHUB_TOKEN={FAKE_TOKEN}\n").split("=")[1].strip()
    assert f"{ph}$" in out and FAKE_TOKEN not in out


def test_a_url_and_the_name_in_it_found_together_mask_only_the_name():
    """The pipeline merges overlaps: a URL and the name inside it, both found, were one placeholder."""
    name = "Margaret Thornbury"
    url = f"https://html.duckduckgo.com/html/?q={name}"
    text = f'curl -sL "{url}"'

    class Both:
        def detect(self, t, *a, **k):
            return [Span(t.index(url), t.index(url) + len(url), "URL", 0.9, "pf-serve"),
                    Span(t.index(name), t.index(name) + len(name), "PERSON", 0.9, "pf-serve")]

    out = Redactor(Pipeline(RulesDetector(), Both(), pii=True), Vault(bytes(range(32)))).redact_text(text)
    assert out.startswith('curl -sL "https://html.duckduckgo.com/html/?q=KEEPMUM_PERSON_') and name not in out


class _Guesses:
    """A model that takes every bare base64 run of 40 characters or more for a secret, as pf-serve did."""

    def __init__(self, rules):
        self.rules = rules

    def detect(self, text):
        import re as _re
        return [Span(m.start(), m.end(), "SECRET", 0.97, "pf-serve") for m in _re.finditer(r"(?<![\w:-])[A-Za-z0-9+/]{40,}", text)]


GOSUM = "golang.org/x/crypto v0.31.0 h1:68RbgsSwMRnuFwGRYM5pbHUGTV0kGOXOutW9t/kK3sk=\n"
NPM = '"integrity": "sha512-6xtPQCUSpt8+nbktNn34Ey7lOwCEK+Q1S53JHtAG9g3kpuO8jXYUWIKQ/zqxGFK7UF/qL+1Lx5kPZRuA+SGJkw=="\n'


def _checking(command: str, result: str, rules=None) -> dict:
    return {"model": "m", "max_tokens": 10, "messages": [
        {"role": "user", "content": "Which checksums are locked?"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "Read", "input": {"file_path": "go.sum"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": result}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": command}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "b", "content": "1"}]}]}


@pytest.mark.parametrize("command", [
    "for s in '68RbgsSwMRnuFwGRYM5pbHUGTV0kGOXOutW9t/kK3sk=' ; do grep -cF \"$s\" go.sum; done",
    "printf '%s\\n' '6xtPQCUSpt8+nbktNn34Ey7lOwCEK+Q1S53JHtAG9g3kpuO8jXYUWIKQ/zqxGFK7UF/qL+1Lx5kPZRuA+SGJkw==' | cut -c1-16",
])
def test_a_checksum_the_request_shows_is_no_secret_where_it_is_quoted_bare(command):
    """An agent checking the table it wrote quotes lockfile hashes without their label; a guess took them for
    keys, and the go.sum read before was masked too (lock-01, code-02 on Claude Code, Nov 22)."""
    rules = RulesDetector()
    r = Redactor(Pipeline(rules, _Guesses(rules), pii=False), Vault(bytes(32)))
    out = json.dumps(r.redact_request(_checking(command, GOSUM + NPM)))
    assert not r.counts and "68RbgsSw" in out and "6xtPQCUS" in out


def test_a_token_a_rule_knows_is_masked_even_after_a_checksum_label():
    token = "ghp_" + "a1B2c3D4" * 4 + "Zz9q"
    rules = RulesDetector()
    r = Redactor(Pipeline(rules, _Guesses(rules), pii=False), Vault(bytes(32)))
    out = json.dumps(r.redact_request(_checking(f"echo {token} | gh auth login --with-token", f"sha256:{token}\n")))
    assert token not in out and r.counts["SECRET"] >= 1


@pytest.mark.parametrize("guess", [
    "sha512-6xtPQCUSpt8+nbktNn34Ey7lOwCEK+Q1S53JHtAG9g3kpuO8jXYUWIKQ/zqxGFK7UF/qL+1Lx5kPZRuA+SGJkw",  # its label in the span
    "6xtPQCUSpt8+nbktNn34Ey7lOwCEK+Q1S53JHtAG9g3kpuO8jXYUWIKQ",  # a span stopped short of its end
])
def test_a_guess_at_a_checksum_with_its_label_or_cut_short_is_none(guess):
    """lock-01, Claude Code on DeepSeek (Nov 22): a guessed `sha512-…` was learned as a secret, then masked in
    the lockfile read before it, labeled and all."""
    command = f"echo '{guess}' | head -c 16"

    class One:
        def detect(self, text):
            return [Span(text.index(guess), text.index(guess) + len(guess), "SECRET", 0.97, "pf-serve")] if guess in text else []

    r = Redactor(Pipeline(RulesDetector(), One(), pii=False), Vault(bytes(32)))
    out = json.dumps(r.redact_request(_checking(command, NPM)))
    assert not r.counts and guess in out


# --- Guesses: none in the model's own text, none at a value already sent (Redactor._spans) -------------

UUID = "9b2e4c71-5d0a-4f3e-8c6b-2a7d1e9f0c35"


class _Flags:
    """A model that flags these values wherever they are, as pf-serve did in the live runs of Nov 22-23."""

    def __init__(self, *values):
        self.values = values

    def detect(self, text):
        return [Span(i, i + len(v), "SECRET", 0.97, "pf-serve") for v in self.values for i in _all(text, v)]


def _all(text, v):
    i = text.find(v)
    while i >= 0:
        yield i
        i = text.find(v, i + 1)


def _turn(*messages):
    return {"model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": "Why does the client get a 401?"}, *messages]}


def test_the_models_own_words_and_commands_get_no_guesses():
    """debug-01 on Codex: a request id it named. cloud-01, srv-01, git-01: its greps and awk patterns."""
    grep = "grep -nE 'KEEPMUM|LS0t|AKIA|token:|secret' docs/infra.md && grep -n 'deploy_ed25519$' ~/.ssh/config"
    r = Redactor(Pipeline(RulesDetector(), _Flags(UUID, "deploy_ed25519$"), pii=False), Vault(bytes(32)))
    body = _turn({"role": "assistant", "content": [
        {"type": "text", "text": f"The failing request ID is `{UUID}`."},
        {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": grep}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "no match"}]})
    out = json.dumps(r.redact_request(body))
    assert not r.counts and UUID in out and "deploy_ed25519$" in out
    # The same guess in the model's words is none; in what the user typed it still masks.
    key = "Kq7vX2mLp9RtW4zB8nQs"
    r = Redactor(Pipeline(RulesDetector(), _Flags(key), pii=False), Vault(bytes(32)))
    out = json.dumps(r.redact_request(_turn({"role": "assistant", "content": f"Is it {key}?"},
                                            {"role": "user", "content": "go on"})))
    assert key in out and not r.counts
    r = Redactor(Pipeline(RulesDetector(), _Flags(key), pii=False), Vault(bytes(32)))
    out = json.dumps(r.redact_request(_turn({"role": "assistant", "content": "ok"}, {"role": "user", "content": f"it is {key}"})))
    assert key not in out and r.counts["SECRET"] == 1


def test_a_value_the_model_was_given_masked_is_masked_where_it_writes_it():
    pw = "Tr0ub4dor&3xqZ"
    r = Redactor(Pipeline(RulesDetector(), _Flags(pw), pii=False), Vault(bytes(32)))
    body = {"model": "m", "max_tokens": 10, "messages": [
        {"role": "user", "content": f"the staging password is {pw}"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "Bash",
                                           "input": {"command": f"psql -W '{pw}' -c 'select 1'"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "1"}]}]}
    out = json.dumps(r.redact_request(body))
    assert pw not in out and r.counts["SECRET"] == 2  # in what was typed, and again, known, in the command


def test_a_format_rule_still_masks_the_models_own_text():
    token = "ghp_" + "a1B2c3D4" * 4 + "Zz9q"
    r = Redactor(Pipeline(RulesDetector(), None, pii=False), Vault(bytes(32)))
    out = json.dumps(r.redact_request(_turn({"role": "assistant", "content": f"export GH_TOKEN={token}"},
                                            {"role": "user", "content": "go on"})))
    assert token not in out


def test_a_guess_cannot_take_back_what_an_earlier_request_sent():
    """debug-01, rotate-01: a value an earlier request sent unmasked, guessed later elsewhere, was masked in
    the earlier message too: the provider had it already, and that message changed (its prompt cache missed)."""
    ref = "acmeOrdersReplica7"  # nothing flags it on its own: only the guess, later
    log = f"2026-11-18T10:42:07Z ERROR request failed: HTTP 401 trace={ref}"

    class Later(_Flags):  # flags it only in the later text, as the model did beside `Bearer` (no rule there)
        def detect(self, text):
            return super().detect(text) if "x-trace" in text else []

    def run(two_requests: bool):
        r = Redactor(Pipeline(RulesDetector(), Later(ref), pii=False), Vault(bytes(32)))
        first = [{"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "cat app.log"}}]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": log}]}]
        sent = r.redact_request(_turn(*first)) if two_requests else None
        second = first + [{"role": "assistant", "content": [{"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": "curl -v x"}}]},
                          {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "b",
                                                        "content": f"< HTTP 401\n< x-trace: {ref}"}]}]
        return r, sent, r.redact_request(_turn(*second))

    r, sent, again = run(two_requests=True)
    assert ref in json.dumps(sent) and not r.counts
    assert json.dumps(again["messages"][:3]) == json.dumps(sent["messages"][:3]) and ref in json.dumps(again)
    # Not sent before, the same guess masks it, in both places.
    r, _, once = run(two_requests=False)
    assert ref not in json.dumps(once) and r.counts["SECRET"] == 2


def test_a_guess_at_base64_of_keepmums_own_placeholder_is_none():
    """Codex encoded the placeholder it was given for a Kubernetes Secret, and the model took the base64
    for a secret (k8s-01, Nov 23). Base64 of a real value is still guessed at."""
    import base64

    ph = "KEEPMUM_SECRET_a719c6d7"
    encoded_ph = base64.b64encode(ph.encode()).decode()
    real = base64.b64encode(b"sk_" b"live_Cz8wSPedxSdC6KOqnWj0rEyw").decode()
    r = Redactor(Pipeline(RulesDetector(), _Flags(encoded_ph, real), pii=False), Vault(bytes(32)))
    out = json.dumps(r.redact_request(_turn(
        {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "cat k8s/secret.yaml"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a",
                                      "content": f"data:\n  STRIPE_SECRET_KEY: {encoded_ph}\n  OTHER: {real}"}]})))
    assert encoded_ph in out and real not in out and r.counts["SECRET"] == 1
    from keepmum.redact import encodes_nothing

    assert encodes_nothing("LOG_LEVEL:aW5mbw") and encodes_nothing("aW5mbw==")  # `info` (k8s-01 on opencode, Nov 23)
    assert not encodes_nothing(real) and not encodes_nothing(f"STRIPE_SECRET_KEY:{real}") and not encodes_nothing("aHVudGVyMg==")


def _responses(*items):
    return {"model": "m", "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Make the Secret."}]}, *items]}


def _ran(call_id, command, output):
    return [{"type": "function_call", "call_id": call_id, "name": "bash", "arguments": json.dumps({"command": command})},
            {"type": "function_call_output", "call_id": call_id, "output": output}]


def _left(body, value):
    """Any form of `value` in a masked body: as is, or JSON-escaped."""
    text = json.dumps(body)
    return value in text or json.dumps(value)[1:-1] in text


def test_a_guess_never_hides_an_exact_find_nor_takes_a_name_with_it():
    """k8s-01 on opencode (Nov 23): a script printed `STRIPE_SECRET_KEY:<base64 of the key>`, the model
    flagged the name with the base64, and that guess hid the decoded find. Masked as one value, it
    counted as sent; the file read later showed the base64 alone, the guess there was dropped as sent
    already, and nothing else masked it: the base64 of the Stripe key left. Now the decoded find masks
    the base64 alone, the name and the earlier message stay as they were, and nothing of it leaves."""
    import base64

    key = "sk_" "live_LoMQh2hq5sSFRmXYSk7wAyTF"
    b64 = base64.b64encode(key.encode()).decode()
    printed = f"DATABASE_URL:cG9zdGdyZXM6Ly9vcmRlcnM=\nSTRIPE_SECRET_KEY:{b64}\nLOG_LEVEL:aW5mbw==\n"
    r = Redactor(Pipeline(RulesDetector(), _Flags(f"STRIPE_SECRET_KEY:{b64}", "LOG_LEVEL:aW5mbw"), pii=False), Vault(bytes(32)))
    first = _ran("a", "python3 enc.py", printed)
    sent = r.redact_responses_request(_responses(*first))
    later = r.redact_responses_request(_responses(*first, *_ran("b", "cat k8s/secret.yaml",
                                                                f"data:\n  STRIPE_SECRET_KEY: {b64}\n  LOG_LEVEL: aW5mbw==\n")))
    for body in (sent, later):
        assert not _left(body, b64) and not _left(body, key)
    out = json.dumps(later)
    assert out.count("STRIPE_SECRET_KEY") == 2 and "LOG_LEVEL:aW5mbw==" in out  # the name, and `info` encoded
    assert json.dumps(later["input"][:len(sent["input"])]) == json.dumps(sent["input"])  # the earlier messages as they went


def test_a_value_masked_inside_a_wider_one_was_not_sent():
    """k8s-01 on pi (Nov 23): two lines of base64 masked as one value, the first line alone guessed
    in a later output, the guess dropped as sent: the database URL's base64 left. What went out
    decides, not the text before masking."""
    import base64

    db = base64.b64encode(b"postgres://orders:I705gxxHJzPmZWUKXk@db.internal:5432/orders").decode()
    other = base64.b64encode(b"sk_" b"live_xTKOBF7k9GsLU85sHiQcY1pC").decode()
    r = Redactor(Pipeline(RulesDetector(), _Flags(f"{db}\n{other}", db), pii=False), Vault(bytes(32)))
    first = _ran("a", "python3 enc.py", f"{db}\n{other}\naW5mbw==\n")
    sent = r.redact_responses_request(_responses(*first))
    later = r.redact_responses_request(_responses(*first, *_ran("b", "python3 show.py", f"db '{db}'\nlog 'aW5mbw=='\n")))
    assert not _left(sent, db) and not _left(later, db)


def test_a_guess_at_what_the_model_wrote_is_none_but_a_value_restored_into_it_still_masks():
    """k8s-01 on Claude Code (Nov 23): its script printed a label it wrote, `startswith_KEEPMUM_in_b64_text`,
    the model flagged the label in the output, and it was masked in the command too. The model's text
    holds real values too, restored from placeholders: one restored inside a wider value is no part
    of what the model sent, and a guess at it alone still masks it."""
    label = "startswith_KEEPMUM_in_b64_text"
    cmd = f"python3 - <<'EOF'\nprint(k, 'matches_env', ok, '{label}', b.startswith('KEEPMUM'))\nEOF"
    r = Redactor(Pipeline(RulesDetector(), _Flags(label), pii=False), Vault(bytes(32)))
    out = r.redact_request(_turn({"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": cmd}}]},
                                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a",
                                                               "content": f"LOG_LEVEL matches_env True {label} False"}]}))
    assert json.dumps(out).count(label) == 2 and not r.counts  # in the command and in its output

    pw = "Xk9mPq2vLw7RtZ"
    r = Redactor(Pipeline(RulesDetector(), _Flags(pw), pii=False), Vault(bytes(32)))
    r.vault.placeholder("SECRET", f"ADMIN_PASS:{pw}", ("pf-serve",))
    out = r.redact_request(_turn({"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "Bash",
                                                                     "input": {"command": f"echo ADMIN_PASS:{pw} > creds"}}]},
                                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": f"saved {pw}"}]}))
    assert pw not in json.dumps(out)
