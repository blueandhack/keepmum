"""Restoring in the cases the round-trip tests do not reach: a placeholder this conversation never made,
a value inside a JSON string, a reply or a stream event of a shape keepmum does not expect. In each,
nothing real is put where it should not be, and nothing fails."""

import json
from collections import Counter

from keepmum.detect.pipeline import Pipeline
from keepmum.detect.rules import RulesDetector
from keepmum.redact import Redactor
from keepmum.restore import DEFAULT_POLICY, UNRESOLVED, StreamRestorer, restore_reply, restore_response, restore_text, restore_tool
from keepmum.vault import Vault

TOKEN = "ghp_" + "Zq8vX2mN4pL7kR9tY3wB6cF1hJ5dG0sAe7Qw"
STRANGER = "KEEPMUM_SECRET_0badc0de"  # the shape of a placeholder, made by no one here


def setup():
    r = Redactor(Pipeline(RulesDetector(), None), Vault(bytes(range(32))))
    r.redact_text(f"GITHUB_TOKEN={TOKEN}")
    return r.vault, next(iter(r.vault._values))


def test_a_placeholder_from_elsewhere_stays_and_is_counted_everywhere():
    vault, ph = setup()
    counts = Counter()
    assert restore_text(f"{STRANGER} and {ph}", vault, counts) == f"{STRANGER} and {TOKEN}"
    assert counts[UNRESOLVED] == 1 and counts["SECRET"] == 1
    # Only some placeholders allowed (`only`): the others stay, uncounted.
    assert restore_text(f"{ph}", vault, Counter(), only=set()) == ph
    # In a shell command, and in a Codex script's literal.
    counts = Counter()
    out = restore_tool("Bash", {"command": f"echo {STRANGER} {ph}"}, vault, DEFAULT_POLICY, counts)
    assert out["command"] == f"echo {STRANGER} {TOKEN}" and counts[UNRESOLVED] == 1
    counts = Counter()
    script = f'text(await tools.exec_command({{cmd: "echo {STRANGER} {ph}"}}));'
    out = restore_tool("exec", script, vault, DEFAULT_POLICY, counts)
    assert TOKEN in out and STRANGER in out and counts[UNRESOLVED] >= 1


def test_a_value_inside_a_json_string_in_the_reply_is_escaped_for_it():
    """A reply that is one JSON document gets a value in a string escaped as JSON (it was masked
    unescaped); in prose, even JSON quoted in prose, it goes as it is."""
    vault, _ = setup()
    value = 'pa"ss\\word9Xq2'
    ph = vault.placeholder("SECRET", value)
    out = restore_reply(f'{{"token": "{ph}", "note": "x"}}', vault)
    assert json.loads(out) == {"token": value, "note": "x"}
    assert restore_reply(f'the token is {ph}', vault) == f"the token is {value}"


def test_a_reply_of_an_unexpected_shape_passes_as_it_came():
    vault, ph = setup()
    assert restore_response({"type": "error"}, vault) == {"type": "error"}
    out = restore_response({"content": ["odd", {"type": "text", "text": ph}, {"type": "thinking", "thinking": ph}]}, vault)
    assert out["content"][0] == "odd" and out["content"][1]["text"] == TOKEN and out["content"][2]["thinking"] == ph


def test_a_stream_event_keepmum_cannot_read_passes_as_it_came():
    vault, ph = setup()
    for raw in (b"event: ping\n\n", b"data: {not json\n\n", b"data: \xff\xfe\n\n"):
        s = StreamRestorer(vault)
        assert s.feed(raw) + s.close() == raw
    # A tool call's input with a placeholder from elsewhere: it stays, and is counted.
    counts = Counter()
    s = StreamRestorer(vault, counts=counts)
    events = (
        'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","name":"Read"}}\n\n'
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta",'
        f'"partial_json":"{{\\"file_path\\": \\"/tmp/{STRANGER}\\"}}"}}}}\n\n'
        'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
    ).encode()
    out = (s.feed(events) + s.close()).decode()
    assert STRANGER in out and counts[UNRESOLVED] == 1


def test_openai_stream_events_keepmum_does_not_expect_pass_as_they_came():
    from keepmum.responses import ResponsesStreamRestorer, restore_item

    vault, ph = setup()
    for raw in (b"event: ping\n\n", b"data: {not json\n\n", b"data: [1]\n\n", b"data: \xff\xfe\n\n",
                b'data: {"type": "response.content_part.added", "part": {"type": "refusal"}}\n\n'):
        s = ResponsesStreamRestorer(vault)
        assert s.feed(raw) + s.close() == raw
    # A content part's text is restored when it starts and when it ends.
    s = ResponsesStreamRestorer(vault)
    part = {"type": "response.content_part.done", "item_id": "m", "part": {"type": "output_text", "text": f"use {ph}"}}
    assert TOKEN in (s.feed(f"data: {json.dumps(part)}\n\n".encode()) + s.close()).decode()
    assert restore_item("not an item", vault) == "not an item"
