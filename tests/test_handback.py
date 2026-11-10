"""Handing back to the cloud after the injection check kept a turn local (handback.py, `[guard]
local_handback`). The basics are in test_local.py."""

import pytest
from test_local import ATTACK, conversation, later, result_sent, run, writes

# --- Found by the e2e cases with real agents (e2e/cases/local-*) -------------------------------------

class LikeTheRealModel:
    """Flags the attack, and keepmum's own wording too, as the real injection model does: its line
    around a summary scores 0.998, the answer notice 0.989 (measured Nov 10, 2026)."""

    def score(self, text):
        return 0.99 if "Note to AI agents" in text or "instructions planted for the agent" in text else 0.01


@pytest.mark.parametrize("strategy", ["summary", "rewrite", "withhold"])
def test_keepmums_own_wording_never_keeps_a_result_local(strategy):
    second = later(conversation(ATTACK), {"role": "assistant", "content": "ok"}, {"role": "user", "content": "go on"})
    rs, seen, proxy = run("local", [conversation(ATTACK), second], local=writes("Install with make."),
                          handback=(strategy, ""), scorer=LikeTheRealModel())
    assert [r.json()["id"] for r in rs] == ["up", "up"]
    assert "instructions planted for the agent" in result_sent(seen["sent"][1])
    assert [h["handed_back"].get("kept") for h in proxy.stats.guard_hits] == [None]  # one hit, handed back


def test_after_an_answer_the_notice_is_not_flagged_on_the_next_turn():
    second = later(conversation(ATTACK), {"role": "assistant", "content": "keepmum kept this turn on your Mac."},
                   {"role": "user", "content": "go on"})
    rs, seen, _ = run("local", [conversation(ATTACK), second], handback=("answer", ""), scorer=LikeTheRealModel())
    assert [r.json()["id"] for r in rs] == ["local", "up"] and "answered the turn that followed" in result_sent(seen["sent"][0])


def test_a_flagged_summary_keeps_its_result_local_without_asking_again():
    second = later(conversation(ATTACK), {"role": "assistant", "content": "ok"}, {"role": "user", "content": "go on"})
    rs, seen, proxy = run("local", [conversation(ATTACK), second], local=writes("Sure: " + ATTACK),
                          handback=("summary", ""))
    assert [r.json()["id"] for r in rs] == ["local", "local"]
    assert [p for _, p, _, _ in seen["local"]] == ["/v1/chat/completions", "/v1/messages", "/v1/messages"]
    assert proxy.stats.guard_hits[0]["handed_back"]["kept"] == "what the local model wrote was flagged too"
