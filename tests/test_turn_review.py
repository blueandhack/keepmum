"""A turn reviewed whole, in one window ([guard] local_handback_review = "turn"): every flagged piece of
it, each answered on its own, then the turn sent on or kept here (review.TurnReview, Proxy._review_turn)."""
import json
import secrets

import anyio
import httpx
import pytest
from test_local import ATTACK, Flags, answer
from test_review import ASKED, AUTH, KEPT, ONE, WRITTEN

from keepmum import guard, review
from keepmum.detect.pipeline import Pipeline
from keepmum.detect.rules import RulesDetector
from keepmum.local import Server, servers_for, skills_for
from keepmum.proxy import Proxy
from keepmum.redact import Redactor
from keepmum.vault import Vault

TOOL = "README. " + ATTACK


def body(typed=ASKED + ATTACK):
    return {"model": "claude-x", "max_tokens": 50, "messages": [
        {"role": "user", "content": typed},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "WebFetch", "input": {"url": "https://x.example"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": TOOL}]}]}


def run_turn(decide=None, harm_rewrite=KEPT, strategy="rewrite", harm_strategy="rewrite", timeout="stay", wait=30,
             reviewer=True, then=None, again=False, typed=ASKED + ATTACK):
    """One turn whose typed text (harm) and WebFetch result (injection) are flagged, both checks local,
    review "turn". `decide(agent, turn)` answers the turn once its drafts are written: it returns the
    POST body, or None for no answer. `again`: the same request once more after. What happened is in seen."""
    seen = {"upstream": [], "sent": [], "turns": [], "reviews": [], "chat": [], "local": 0, "results": []}

    def handler(request):
        if request.url.host == "localhost":
            data = json.loads(request.content)
            if request.url.path == "/v1/chat/completions":
                seen["chat"].append(data)
                if data["messages"][0]["content"].startswith("You help a user edit"):
                    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "EDITED"}}]})
                harm = data["messages"][-1]["content"].startswith("The user's message:")
                return httpx.Response(200, json={"choices": [{"message": {"role": "assistant",
                                                                          "content": harm_rewrite if harm else WRITTEN}}]})
            seen["local"] += 1
            return answer(data, data["model"])
        seen["upstream"].append(request.url.path)
        seen["sent"].append(json.loads(request.content))
        return httpx.Response(200, json={"id": "up", "type": "message", "role": "assistant", "content": [{"type": "text", "text": "ok"}]})

    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        checks = [guard.Guard("local", Flags(), name="injection"), guard.Guard("local", Flags(), name="harm")]
        proxy = Proxy(Redactor(Pipeline(RulesDetector(), None), Vault(secrets.token_bytes(32))), upstream="http://up",
                      client=client, stats_token="t", guards=checks, gemini_upstream="http://up",
                      local=lambda check: servers_for([Server("http://localhost:11434", "m")], check),
                      local_skills=lambda check: skills_for([], check), handback=lambda: (strategy, ""),
                      review=lambda: ("turn", wait, timeout), handback_harm=lambda: harm_strategy)
        agent = httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1:7788")
        if reviewer:
            await agent.get("/_keepmum/reviews", headers=AUTH)
        headers = {"x-api-key": "sk-ant-x", "anthropic-version": "2023-06-01"}

        async def send():
            seen["results"].append(await agent.post("/v1/messages", json=body(typed), headers=headers))
            tg.cancel_scope.cancel()

        async def review_it():
            for _ in range(500):
                got = (await agent.get("/_keepmum/reviews", headers=AUTH)).json()
                seen["reviews"] += got["reviews"]
                turns = got["turns"]
                if turns and not any(p["drafting"] for p in turns[0]["pieces"]):
                    seen["turns"].append(turns[0])
                    if decide:
                        sent = await decide(agent, turns[0])
                        if sent is not None:
                            r = await agent.post(f"/_keepmum/reviews/{turns[0]['id']}", json=sent, headers=AUTH)
                            seen["answer"] = (r.status_code, r.json())
                    return
                await anyio.sleep(0.01)

        async with anyio.create_task_group() as tg:
            tg.start_soon(send)
            if reviewer:
                tg.start_soon(review_it)
        if again:
            seen["results"].append(await agent.post("/v1/messages", json=body(typed), headers=headers))
        await proxy.settle()
        if then:
            await then(agent, proxy)
        return seen, proxy

    return anyio.run(go)


def send(pieces=None):
    async def decide(agent, turn):
        return {"action": "send", **({"pieces": pieces} if pieces else {})}
    return decide


def sent_text(seen, i=0):
    return json.dumps(seen["sent"][i])


def test_one_window_shows_every_flagged_piece_with_its_draft_and_the_defaults_go():
    seen, proxy = run_turn(send())
    [turn] = seen["turns"]
    harm, tool = turn["pieces"]
    assert (harm["check"], harm["default"], harm["draft"], harm["failed"]) == ("harm", "rewrite", KEPT, None)
    assert (tool["check"], tool["source"], tool["default"], tool["draft"], tool["failed"]) == ("injection", "WebFetch", "rewrite", WRITTEN, None)
    assert tool["actions"] == ["summary", "rewrite", "original", "remove"] and harm["actions"] == ["rewrite", "original", "remove"]
    assert turn["default"] == "send" and seen["reviews"] == []  # no panel of its own for either piece
    assert seen["results"][0].json()["id"] == "up" and seen["local"] == 0
    out = sent_text(seen)
    assert "Sort the list in utils.py by date." in out and WRITTEN in out and json.dumps(ATTACK)[1:-1] not in out
    notes = [h["handed_back"] for h in proxy.stats.guard_hits]
    assert all("reviewed with the turn" in n["review"] for n in notes)


def test_each_piece_takes_its_own_answer():
    seen, proxy = run_turn(send({"1": {"action": "original"}, "2": {"action": "remove"}}))
    out = sent_text(seen)
    assert seen["results"][0].json()["id"] == "up"
    assert json.dumps(ASKED + ATTACK)[1:-1] in out  # what was typed, as typed
    assert WRITTEN not in out and "keepmum withheld this result of WebFetch" in out and "README." not in out
    reviews = " ".join(h["handed_back"]["review"] for h in proxy.stats.guard_hits)
    assert "the original, as you chose" in reviews and "removed" in reviews


def test_the_users_own_text_goes_for_a_piece_and_is_checked():
    seen, proxy = run_turn(send({"1": {"action": "rewrite", "text": "Sort it by date, please."},
                                 "2": {"action": "summary", "text": "A README with install steps."}}))
    out = sent_text(seen)
    assert "Sort it by date, please." in out and "A README with install steps." in out and WRITTEN not in out
    assert "edited it as the user asked" in out
    assert "rewrite (yours)" in " ".join(h["handed_back"]["review"] for h in proxy.stats.guard_hits)


def test_kept_local_nothing_reaches_the_cloud_and_later_turns_stay():
    async def decide(agent, turn):
        return {"action": "local"}

    seen, proxy = run_turn(decide, again=True)
    assert seen["upstream"] == [] and seen["local"] == 2 and len(seen["turns"]) == 1  # not asked again
    assert all(h["handed_back"]["kept"] == "you kept it on this Mac" for h in list(proxy.stats.guard_hits)[:2])


def test_sent_on_later_turns_carry_the_answers_without_asking_again():
    seen, proxy = run_turn(send(), again=True)
    assert seen["upstream"] == ["/v1/messages", "/v1/messages"] and len(seen["turns"]) == 1
    assert WRITTEN in sent_text(seen, 1) and json.dumps(ATTACK)[1:-1] not in sent_text(seen, 1)


def test_with_the_settings_on_stay_the_window_defaults_to_local_but_the_user_can_send():
    seen, proxy = run_turn(send(), strategy="stay", harm_strategy="stay", again=True)
    assert seen["turns"][0]["default"] == "local"
    assert seen["upstream"] == ["/v1/messages", "/v1/messages"]  # and later turns go as answered
    assert WRITTEN in sent_text(seen, 1)


@pytest.mark.parametrize("timeout, harm_rewrite, typed, upstream", [
    ("send", KEPT, ASKED + ATTACK, ["/v1/messages"]),  # every default ready and passed: sent
    ("send", "Sort the list by date please.", ONE, []),  # the rewrite changed words, and one sentence: kept local
    ("stay", KEPT, ASKED + ATTACK, []),
])
def test_no_answer_sends_only_defaults_that_passed(monkeypatch, timeout, harm_rewrite, typed, upstream):
    monkeypatch.setattr(review, "MIN_WAIT", 0)
    monkeypatch.setattr(review, "AFTER_ASSIST_S", 0.05)

    async def decide(agent, turn):
        return None

    seen, proxy = run_turn(decide, harm_rewrite=harm_rewrite, timeout=timeout, wait=0.1, typed=typed)
    assert seen["upstream"] == upstream
    assert all(h["handed_back"]["review"].startswith("no answer in") for h in list(proxy.stats.guard_hits)[:2])


def test_a_piece_is_drafted_again_or_changed_as_the_user_asks():
    async def decide(agent, turn):
        tid = turn["id"]
        again = await agent.post(f"/_keepmum/reviews/{tid}/draft", json={"piece": "2", "kind": "summary"}, headers=AUTH)
        assert again.status_code == 200 and again.json()["kind"] == "summary" and again.json()["draft"] == WRITTEN
        changed = await agent.post(f"/_keepmum/reviews/{tid}/draft", headers=AUTH,
                                   json={"piece": "1", "kind": "rewrite", "text": KEPT, "request": "Shorter."})
        piece = changed.json()
        # The local model's change is checked as a rewrite is: this one changed words.
        assert piece["draft"] == "EDITED" and piece["failed"].startswith("The local model changed words")
        bad = await agent.post(f"/_keepmum/reviews/{tid}/draft", json={"piece": "1", "kind": "summary"}, headers=AUTH)
        assert bad.status_code == 400
        gone = await agent.post(f"/_keepmum/reviews/{tid}/draft", json={"piece": "9"}, headers=AUTH)
        assert gone.status_code == 404
        return {"action": "send", "pieces": {"2": {"action": "summary"}}}  # the typed piece: its (failed) draft

    seen, proxy = run_turn(decide)
    ask = seen["chat"][-1]["messages"]
    assert seen["answer"][0] == 200 and "EDITED" in sent_text(seen) and WRITTEN in sent_text(seen)
    assert any(m["content"].startswith("You help a user edit") for c in seen["chat"] for m in c["messages"][:1])
    assert ask


def test_a_bad_answer_is_refused_and_the_turn_waits():
    async def decide(agent, turn):
        r = await agent.post(f"/_keepmum/reviews/{turn['id']}", json={"action": "send", "pieces": {"1": {"action": "summary"}}},
                             headers=AUTH)
        assert r.status_code == 400 and "piece 1" in r.json()["error"]["message"]
        r = await agent.post(f"/_keepmum/reviews/{turn['id']}", json={"action": "withhold"}, headers=AUTH)
        assert r.status_code == 400
        return {"action": "local"}

    seen, proxy = run_turn(decide)
    assert seen["answer"][0] == 200 and seen["upstream"] == []


def test_with_nobody_there_a_turn_goes_as_the_settings_say():
    seen, proxy = run_turn(None, reviewer=False)
    assert seen["turns"] == [] and seen["upstream"] == ["/v1/messages"] and WRITTEN in sent_text(seen)


def test_a_missing_draft_cannot_go_by_default():
    t = review.TurnReview("/p", [review.Piece("1", "harm", "x", whole="x")], 10)
    pieces, why = t.resolve()
    assert pieces is None and "no rewrite to send yet" in why
    t.pieces[0].draft, t.pieces[0].kind, t.pieces[0].failed = "y", "rewrite", "The harm check flagged the rewrite too."
    assert t.resolve()[0] == {"1": {"action": "rewrite", "text": "y", "edited": False}}  # the user saw why, and sent it
    assert t.resolve(defaults_only=True)[0] is None  # but not on no answer


def test_a_draft_that_kept_the_flagged_sentence_is_redone_and_write_again_samples():
    async def decide(agent, turn):
        harm = turn["pieces"][0]
        assert harm["draft"] == KEPT and harm["failed"] is None and harm["model"].endswith("then keepmum sentence by sentence")
        r = await agent.post(f"/_keepmum/reviews/{turn['id']}/draft", json={"piece": "2", "kind": "rewrite"}, headers=AUTH)
        assert r.status_code == 200
        return {"action": "send"}

    seen, proxy = run_turn(decide, harm_rewrite="[removed by keepmum] " + ATTACK)
    temps = [c.get("temperature") for c in seen["chat"]]
    harm_temps = [c.get("temperature") for c in seen["chat"] if c["messages"][-1]["content"].startswith("The user's message:")]
    assert harm_temps == [0] and None in temps  # the first harm draft at 0; the tool rewrite asked again is sampled below
    assert seen["results"][0].json()["id"] == "up" and "Note to AI agents" not in sent_text(seen)


def test_write_again_of_what_was_typed_is_sampled():
    async def decide(agent, turn):
        r = await agent.post(f"/_keepmum/reviews/{turn['id']}/draft", json={"piece": "1", "kind": "rewrite"}, headers=AUTH)
        assert r.status_code == 200
        return {"action": "local"}

    seen, proxy = run_turn(decide)
    harm_temps = [c.get("temperature") for c in seen["chat"] if c["messages"][-1]["content"].startswith("The user's message:")]
    assert harm_temps == [0, 0.7]
