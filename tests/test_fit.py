"""A long conversation fitted to what the local model reads, for a turn it answers (fit.py)."""

import json
import secrets

import anyio
import httpx
import pytest
from test_local import ATTACK, Flags, conversation

from keepmum import fit, guard
from keepmum.config import ConfigError, load
from keepmum.detect.pipeline import Pipeline
from keepmum.detect.rules import RulesDetector
from keepmum.fit import Fitted, budget, limit_in, parse_reads, size
from keepmum.local import Server, servers_for, skills_for
from keepmum.proxy import Proxy
from keepmum.redact import Redactor
from keepmum.vault import Vault


def chat(n: int, each: int = 3000) -> list[dict]:
    """A system message, the task, then n turns that alternate, each about `each` characters."""
    out = [{"role": "system", "content": "keepmum's note. You are Claude Code."}, {"role": "user", "content": "TASK: fix the parser"}]
    for i in range(n):
        out.append({"role": "assistant" if i % 2 == 0 else "user", "content": f"turn {i} " + "x" * each})
    out.append({"role": "user" if out[-1]["role"] == "assistant" else "assistant", "content": "NEWEST: and now?"})
    if out[-1]["role"] != "user":
        out.append({"role": "user", "content": "NEWEST: and now?"})
    return out


def test_a_conversation_that_fits_is_unchanged():
    msgs = chat(4)
    f = fit.fit(msgs, 100_000)
    assert f.messages is msgs and f.note() == ""


def test_old_turns_go_and_the_task_and_the_newest_stay():
    msgs = chat(200)
    f = fit.fit(msgs, 8000)
    assert f.tokens <= 8000 and f.left_out > 100 and size(f.messages) == f.tokens
    texts = [m["content"] for m in f.messages]
    assert texts[0].startswith("keepmum's note") and texts[1] == "TASK: fix the parser" and texts[-1].endswith("NEWEST: and now?")
    assert "earlier messages of this conversation are left out here" in texts[2]
    roles = [m["role"] for m in f.messages[1:]]
    assert all(a != b for a, b in zip(roles, roles[1:]))  # still alternating


def test_a_huge_message_keeps_its_start_and_end():
    msgs = [{"role": "system", "content": "note"}, {"role": "user", "content": "START " + "y" * 500_000 + " END"}]
    f = fit.fit(msgs, 16_000)
    text = f.messages[1]["content"]
    assert text.startswith("START") and text.endswith("END") and "characters of this message are left out here" in text
    assert f.cut > 400_000 and f.tokens <= 16_000


def test_the_start_moves_in_steps_so_a_prompt_cache_holds():
    """Turn by turn, the conversation the local model reads starts at the same message for a while."""
    starts = []
    msgs = chat(60)
    for extra in range(12):
        grown = msgs[:-1] + [{"role": "assistant" if msgs[-2]["role"] == "user" else "user", "content": "z" * 3000}] * 0 + msgs[-1:]
        msgs = msgs[:-1] + [{"role": "assistant", "content": f"more {extra} " + "w" * 3000},
                            {"role": "user", "content": f"again {extra} " + "v" * 3000}] + msgs[-1:]
        f = fit.fit(msgs, 30_000)
        starts.append(f.messages[2]["content"][:80])
    assert len(set(starts)) < len(starts) / 2  # it stays put for several turns at a time


def test_budget_and_limits():
    assert budget(32768, 262144, 4096) == 32768
    assert budget(32768, 16384, 4096) == 16384 - 4096 - 512  # never more than the server holds
    assert budget(None, 65536, 4096) == 65536 - 4096 - 512  # "full": the server's context
    assert budget(None, None, 4096) == fit.DEFAULT
    assert limit_in("the request (70000 tokens) exceeds the context length of 65536") == 65536 or \
        limit_in("exceeds the context length of 65536") == 65536
    assert limit_in("prompt exceeds the context window") is None
    assert parse_reads("32k") == ("32k", 32768) and parse_reads("full") == ("full", None) and parse_reads("65536") == ("64k", 65536)
    for bad in ("1k", "lots", "2000k"):
        with pytest.raises(ValueError):
            parse_reads(bad)


def test_the_setting_is_read_and_checked(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[guard]\nlocal_answer_context = "full"\n')
    assert load(path).guard_local_answer_context == "full"
    assert load(tmp_path / "missing.toml").guard_local_answer_context == "32k"
    path.write_text('[guard]\nlocal_answer_context = "a lot"\n')
    with pytest.raises(ConfigError):
        load(path)


def answer_long(reads: str, context: int | None, refuse_over: int | None = None, api="auto"):
    """A long flagged conversation answered by a mocked local server that says it holds `context`
    tokens (/v1/models) and, with `refuse_over`, refuses a request over that many characters as too long."""
    fit._known.clear()
    seen = {"local": []}
    body = conversation(ATTACK)
    for i in range(80):  # about 240,000 characters of earlier turns
        body["messages"] += [{"role": "assistant", "content": f"step {i} " + "a" * 3000},
                             {"role": "user", "content": f"ok {i} " + "b" * 3000}]
    body["messages"].append({"role": "assistant", "content": [{"type": "tool_use", "id": "t9", "name": "WebFetch", "input": {}}]})
    body["messages"].append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t9", "content": ATTACK}]})

    def handler(request):
        if request.url.path == "/v1/models":
            data = [{"id": "m", "context_length": context}] if context else []
            return httpx.Response(200, json={"data": data})
        if request.method == "GET" or request.url.path in ("/props", "/api/show"):
            return httpx.Response(404)
        data = json.loads(request.content)
        seen["local"].append((request.url.path, data))
        if refuse_over and len(request.content) > refuse_over:
            return httpx.Response(400, json={"error": {"message": "prompt exceeds the context window", "code": "context_length_exceeded"}})
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "kept on your Mac"},
                                                          "finish_reason": "stop"}], "usage": {}})
        return httpx.Response(200, json={"id": "local", "type": "message", "role": "assistant", "model": "m",
                                         "content": [{"type": "text", "text": "kept on your Mac"}], "stop_reason": "end_turn",
                                         "usage": {"input_tokens": 1, "output_tokens": 1}})

    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        proxy = Proxy(Redactor(Pipeline(RulesDetector(), None), Vault(secrets.token_bytes(32))), upstream="http://up",
                      client=client, stats_token="t", guards=[guard.Guard("local", Flags())], gemini_upstream="http://up",
                      local=lambda check: servers_for([Server("http://localhost:8000", "m", api=api)], check),
                      local_skills=lambda check: skills_for([], check), speed=lambda: ("5m", True, reads))
        agent = httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1:7788")
        r = await agent.post("/v1/messages", json=body, headers={"x-api-key": "sk-ant-x", "anthropic-version": "2023-06-01"})
        await proxy.settle()
        return r, seen, proxy

    return anyio.run(go)


def test_a_long_flagged_conversation_is_answered_from_its_newest_part():
    r, seen, proxy = answer_long("16k", 262144)
    assert r.status_code == 200 and r.json()["content"][0]["text"] == "kept on your Mac"
    [(path, sent)] = seen["local"]
    assert path == "/v1/chat/completions"  # fitted as Chat Completions, not sent whole in the agent's API
    assert fit.size(sent["messages"]) <= 16384
    text = json.dumps(sent)
    assert "Read the README" in text and ATTACK in text and "earlier messages of this conversation are left out here" in text
    assert "left out" in proxy.stats.guard_hits[-1]["answered_by"]["fitted"]


def test_a_conversation_within_the_budget_goes_in_the_agents_own_api():
    r, seen, _ = answer_long("full", 262144)
    [(path, _)] = seen["local"]
    assert path == "/v1/messages" and r.status_code == 200


def test_a_server_smaller_than_it_said_gets_the_conversation_fitted_again():
    r, seen, _ = answer_long("full", 262144, refuse_over=60_000)
    assert r.status_code == 200
    paths = [p for p, _ in seen["local"]]
    assert paths[0] == "/v1/messages" or paths[0] == "/v1/chat/completions"
    assert len(json.dumps(seen["local"][-1][1])) <= 60_000 and paths[-1] == "/v1/chat/completions" and len(paths) <= 4


def test_the_servers_own_context_bounds_full():
    r, seen, _ = answer_long("full", 20000)
    [(path, sent)] = seen["local"]
    assert path == "/v1/chat/completions" and fit.size(sent["messages"]) <= 20000 - 50 - 512  # it writes up to 50


def test_fitted_says_what_it_did():
    assert Fitted([], 3, 1200).note() == "3 earlier messages left out; 1,200 characters cut from long messages"
    assert Fitted([], 1, 0).note() == "1 earlier message left out"


def test_instructions_and_the_newest_message_over_the_budget_are_cut_too():
    msgs = [{"role": "system", "content": "NOTE " + "s" * 60_000}, {"role": "user", "content": "TASK"},
            {"role": "assistant", "content": "a" * 30_000}, {"role": "user", "content": "NEWEST " + "n" * 60_000 + " END"}]
    f = fit.fit(msgs, 8000)
    assert f.tokens <= 8000 and f.messages[0]["content"].startswith("NOTE") and f.messages[-1]["content"].endswith("END")
