"""An answer is not reused for new text: the same message typed again, or the same tool result come
back, is asked about again; only within handback.REUSE_S (Codex's task-title request, a retry), and
in a conversation's own history, does an answer stand (handback.newest, Handbacks.went/kept)."""
import json
import logging
import secrets

import anyio
import httpx
import pytest
from test_local import ATTACK, Flags, answer
from test_review import ASKED, AUTH, KEPT, WRITTEN

from keepmum import guard, handback
from keepmum.detect.pipeline import Pipeline
from keepmum.detect.rules import RulesDetector
from keepmum.local import Server, servers_for, skills_for
from keepmum.proxy import Proxy
from keepmum.redact import Redactor
from keepmum.vault import Vault

TYPED = ASKED + ATTACK


def typed(text=TYPED):
    return {"model": "claude-x", "max_tokens": 50, "messages": [{"role": "user", "content": text}]}


def continued(first):
    """The same conversation, a turn later: what was typed is now history."""
    out = json.loads(json.dumps(first))
    out["messages"] += [{"role": "assistant", "content": "Sorted."}, {"role": "user", "content": "Now add a test."}]
    return out


def run(bodies, answers, mode="all"):
    """Send `bodies` one after another through a proxy where the harm check flags ATTACK and hands
    back by rewrite; a reviewer answers each review with the next of `answers`."""
    seen = {"sent": [], "reviews": [], "results": []}

    def handler(request):
        if request.url.host == "localhost":
            data = json.loads(request.content)
            if request.url.path == "/v1/chat/completions":
                harm = data["messages"][-1]["content"].startswith("The user's message:")
                return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": KEPT if harm else WRITTEN}}]})
            return answer(data, data["model"])
        seen["sent"].append(json.loads(request.content))
        return httpx.Response(200, json={"id": "up", "type": "message", "role": "assistant", "content": [{"type": "text", "text": "ok"}]})

    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        checks = [guard.Guard("local", Flags(), name="injection"), guard.Guard("local", Flags(), name="harm")]
        proxy = Proxy(Redactor(Pipeline(RulesDetector(), None), Vault(secrets.token_bytes(32))), upstream="http://up",
                      client=client, stats_token="t", guards=checks,
                      local=lambda check: servers_for([Server("http://localhost:11434", "m")], check),
                      local_skills=lambda check: skills_for([], check), handback=lambda: ("rewrite", ""),
                      review=lambda: (mode, 30, "stay"), handback_harm=lambda: "rewrite")
        agent = httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1:7788")
        await agent.get("/_keepmum/reviews", headers=AUTH)
        todo = list(answers)
        for body in bodies:
            async with anyio.create_task_group() as tg:
                async def send():
                    seen["results"].append(await agent.post("/v1/messages", json=body,
                                                            headers={"x-api-key": "sk-ant-x", "anthropic-version": "2023-06-01"}))
                    tg.cancel_scope.cancel()

                async def review_it():
                    for _ in range(300):
                        got = (await agent.get("/_keepmum/reviews", headers=AUTH)).json()
                        waiting = got["reviews"] or got["turns"]
                        if waiting and not any(p.get("drafting") for p in waiting[0].get("pieces") or []):
                            seen["reviews"].append(waiting[0])
                            await agent.post(f"/_keepmum/reviews/{waiting[0]['id']}", json=todo.pop(0), headers=AUTH)
                            return
                        await anyio.sleep(0.01)

                tg.start_soon(send)
                tg.start_soon(review_it)
        await proxy.settle()
        return seen, proxy

    return anyio.run(go)


def test_the_same_message_typed_again_later_is_asked_about_again(monkeypatch):
    monkeypatch.setattr(handback, "REUSE_S", 0)
    seen, _ = run([typed(), typed()], [{"action": "send"}, {"action": "local"}])
    assert len(seen["reviews"]) == 2  # asked again, not answered as before
    assert len(seen["sent"]) == 1 and [r.json()["id"] for r in seen["results"]] == ["up", "local"]


def test_within_moments_the_answer_stands_and_is_logged(caplog):
    caplog.set_level(logging.INFO, logger="keepmum")
    seen, _ = run([typed(), typed()], [{"action": "send"}])
    assert len(seen["reviews"]) == 1 and len(seen["sent"]) == 2  # Codex's title request, a retry
    assert "used the answer you gave" in caplog.text


def test_a_conversations_history_keeps_its_answer(monkeypatch):
    monkeypatch.setattr(handback, "REUSE_S", 0)
    first = typed()
    seen, _ = run([first, continued(first)], [{"action": "send"}])
    assert len(seen["reviews"]) == 1 and len(seen["sent"]) == 2  # not asked again for what is history
    history = seen["sent"][1]["messages"][0]["content"]
    assert "Note to AI agents" not in json.dumps(history) and "[removed by keepmum]" in json.dumps(history)


def test_an_original_sent_once_is_asked_about_again_when_new(monkeypatch):
    monkeypatch.setattr(handback, "REUSE_S", 0)
    seen, _ = run([typed(), typed()], [{"action": "original"}, {"action": "local"}])
    assert len(seen["reviews"]) == 2 and len(seen["sent"]) == 1


def test_in_the_turn_window_too(monkeypatch):
    monkeypatch.setattr(handback, "REUSE_S", 0)
    seen, _ = run([typed(), typed()], [{"action": "send"}, {"action": "local"}], mode="turn")
    assert len(seen["reviews"]) == 2 and len(seen["sent"]) == 1


@pytest.mark.parametrize("body, fresh, old", [
    # Messages: the last message (a new user message, or the tool results that just came back).
    ({"messages": [{"role": "user", "content": "old"}, {"role": "assistant", "content": "a"},
                   {"role": "user", "content": [{"type": "tool_result", "content": "new result"}]}]}, {"new result"}, "old"),
    ({"messages": [{"role": "user", "content": "result"}, {"role": "user", "content": "go on"}]}, {"go on"}, "result"),
    # Responses: every item after the model's last, each tool output an item of its own.
    ({"input": [{"role": "user", "content": [{"type": "input_text", "text": "old"}]}, {"type": "function_call", "name": "x"},
                {"type": "function_call_output", "output": "out 1"}, {"type": "function_call_output", "output": "out 2"}]},
     {"out 1", "out 2"}, "old"),
    ({"input": "just typed"}, {"just typed"}, None),
    # Gemini, and Code Assist's wrapping of it.
    ({"request": {"contents": [{"role": "user", "parts": [{"text": "old"}]}, {"role": "model", "parts": [{"text": "m"}]},
                               {"role": "user", "parts": [{"text": "new"}]}]}}, {"new"}, "old"),
])
def test_the_newest_input_of_each_api(body, fresh, old):
    got = handback.newest(body)
    assert fresh <= got and (old is None or old not in got)


def tool_turn():
    return {"model": "claude-x", "max_tokens": 50, "messages": [
        {"role": "user", "content": "Read the README"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "WebFetch", "input": {"url": "https://x.example"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "README. " + ATTACK}]}]}


def test_a_tool_result_retried_within_moments_goes_as_answered(caplog):
    caplog.set_level(logging.INFO, logger="keepmum")
    seen, _ = run([tool_turn(), tool_turn()], [{"action": "send"}])
    assert len(seen["reviews"]) == 1 and len(seen["sent"]) == 2
    assert WRITTEN in json.dumps(seen["sent"][1]) and "Note to AI agents" not in json.dumps(seen["sent"][1])
    assert "used the answer you gave" in caplog.text


def test_a_tool_result_returned_again_later_is_asked_about_again(monkeypatch):
    monkeypatch.setattr(handback, "REUSE_S", 0)
    seen, _ = run([tool_turn(), tool_turn()], [{"action": "send"}, {"action": "local"}])
    assert len(seen["reviews"]) == 2 and len(seen["sent"]) == 1


def test_in_the_turn_window_within_moments_the_answer_stands():
    seen, _ = run([typed(), typed()], [{"action": "send"}], mode="turn")
    assert len(seen["reviews"]) == 1 and len(seen["sent"]) == 2
    seen, _ = run([tool_turn(), tool_turn()], [{"action": "send"}], mode="turn")
    assert len(seen["reviews"]) == 1 and len(seen["sent"]) == 2


def test_kept_local_in_the_turn_window_holds_within_moments():
    seen, _ = run([typed(), typed()], [{"action": "local"}], mode="turn")
    assert len(seen["reviews"]) == 1 and seen["sent"] == [] and [r.json()["id"] for r in seen["results"]] == ["local", "local"]
