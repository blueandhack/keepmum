"""Each case on the guard-flow page, run through the proxy: how many reviews, Logs rows, and where
the turn ends. Uses the harnesses in test_review (a Flags model flags text holding ATTACK)."""
import json

import pytest
from test_local import ATTACK
from test_review import ASKED, KEPT, ONE, TITLE, WRITTEN, run, run_both

TOOL = [{"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "WebFetch", "input": {"url": "https://x.example"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "README. Clean."}]}]


def rows(proxy):
    return sorted((h["check"], h["action"]) for h in proxy.stats.guard_hits)


def test_only_what_was_typed_flagged_and_sent():
    r, seen, proxy = run({"action": "send"}, harm=True)
    assert len(seen["reviews"]) == 1 and rows(proxy) == [("harm", "local")] and r.json()["id"] == "up"
    assert KEPT in json.dumps(seen["sent"]) and ATTACK not in json.dumps(seen["sent"])


def test_only_a_tool_result_flagged_and_sent():
    r, seen, proxy = run({"action": "send"})
    assert len(seen["reviews"]) == 1 and rows(proxy) == [("injection", "local")] and r.json()["id"] == "up"
    assert WRITTEN in json.dumps(seen["sent"])


def test_both_flagged_both_sent():
    r, seen, proxy = run_both([{"action": "send"}, {"action": "send"}], reviewer=True)
    assert [rv["check"] for rv in seen["reviews"]] == ["harm", "injection"]
    assert rows(proxy) == [("harm", "local"), ("injection", "local")] and r.json()["id"] == "up"


def test_both_flagged_what_was_typed_kept_local():
    r, seen, proxy = run_both([{"action": "local"}], reviewer=True)
    assert [rv["check"] for rv in seen["reviews"]] == ["harm"] and len(rows(proxy)) == 2
    assert seen["upstream"] == [] and seen["local"] == 1


def test_both_flagged_the_harm_rewrite_fails_and_nobody_reviews():
    r, seen, proxy = run_both(harm_rewrite=ONE, typed=ONE)
    assert seen["reviews"] == [] and len(rows(proxy)) == 2 and seen["upstream"] == [] and seen["local"] == 1


def test_both_flagged_with_nobody_to_review_both_rewrites_go():
    r, seen, proxy = run_both()
    assert seen["reviews"] == [] and len(rows(proxy)) == 2 and r.json()["id"] == "up"


@pytest.mark.parametrize("modes", [("block", "local"), ("local", "block"), ("block", "block")])
def test_one_check_in_block_mode_refuses_with_both_rows(modes):
    r, seen, proxy = run_both(modes=modes)
    assert r.status_code == 403 and rows(proxy) == [("harm", "blocked"), ("injection", "blocked")]
    assert seen["upstream"] == [] and seen["local"] == 0
    assert "harm check" in r.json()["error"]["message"] and "injection check" in r.json()["error"]["message"]


def test_codex_sends_the_turn_and_its_title_request():
    r, seen, proxy = run({"action": "send"}, harm=True, also=[TITLE + ASKED + ATTACK])
    assert len(seen["reviews"]) == 1 and rows(proxy) == [("harm", "local")] and len(seen["upstream"]) == 2


def test_the_same_typed_text_twice_in_one_request():
    messages = [{"role": "user", "content": ASKED + ATTACK}, {"role": "assistant", "content": "On it."},
                {"role": "user", "content": ASKED + ATTACK}]
    r, seen, proxy = run_both([{"action": "send"}], reviewer=True, messages=messages)
    assert len(seen["reviews"]) == 1 and rows(proxy) == [("harm", "local")] and r.json()["id"] == "up"
    assert ATTACK not in json.dumps(seen["sent"])


def test_an_injection_typed_by_the_user_is_the_harm_checks():
    r, seen, proxy = run_both([{"action": "send"}], reviewer=True, messages=[{"role": "user", "content": ASKED + ATTACK}, *TOOL])
    assert [rv["check"] for rv in seen["reviews"]] == ["harm"] and rows(proxy) == [("harm", "local")]
