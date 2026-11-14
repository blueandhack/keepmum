"""Local mode for Gemini's API: the Antigravity CLI (agy) and Gemini CLI. No local server speaks it, so
a flagged turn goes as Chat Completions and the answer comes back as a GenerateContentResponse:
plain from the Gemini API (agy's gateway mode, Gemini CLI with an API key), wrapped as Code Assist
wraps it (agy and Gemini CLI signed in with Google), streamed with alt=sse, or as a JSON array."""

import json

import httpx
import pytest
from test_local import ATTACK, TOKEN, chat_only, run

from keepmum.local import LocalRoute, Server, Skill, answer_text, from_chat, to_chat

GATEWAY = "/gemini/v1beta/models/gemini-3-pro:generateContent"
GATEWAY_SSE = "/gemini/v1beta/models/gemini-3-pro:streamGenerateContent?alt=sse"
SIGNED_IN = "/antigravity/v1internal:generateContent"
SIGNED_IN_SSE = "/antigravity/v1internal:streamGenerateContent?alt=sse"
SERVERS = [Server("http://localhost:8080", "qwen3:8b")]
LOCAL = {8080: chat_only}  # answers "kept on your Mac", whole or streamed


def agy(result: str, wrapped: bool = False, typed: str = "Read README.md and tell me how to install it") -> dict:
    """An agy turn after it read a file: as its gateway mode sends it, or wrapped as Code Assist."""
    req = {"contents": [
        {"role": "user", "parts": [{"text": typed}]},
        {"role": "model", "parts": [{"text": "I should read it.", "thought": True, "thoughtSignature": "sig"},
                                    {"functionCall": {"id": "c1", "name": "read_file", "args": {"Path": "/w/README.md"}}}]},
        {"role": "user", "parts": [{"functionResponse": {"id": "c1", "name": "read_file", "response": {"output": result}}}]}],
        "systemInstruction": {"parts": [{"text": "You are Antigravity."}]},
        "tools": [{"functionDeclarations": [{"name": "run_command"}, {"name": "read_file"}]}],
        "generationConfig": {"maxOutputTokens": 2048, "temperature": 0.2, "topP": 0.9}}
    return {"model": "gemini-3-pro", "project": "p", "user_prompt_id": "u1", "request": req} if wrapped else req


def events(text: str) -> list[dict]:
    return [json.loads(line[5:]) for line in text.splitlines() if line.startswith("data:")]


def texts(chunks: list[dict]) -> str:
    return "".join(p.get("text", "") for c in chunks for cand in c.get("response", c)["candidates"]
                   for p in cand["content"]["parts"])


# --- Answered on this Mac ------------------------------------------------------------------------

@pytest.mark.parametrize(("path", "wrapped"), [(GATEWAY, False), (SIGNED_IN, True)])
def test_a_flagged_agy_turn_is_answered_locally_in_geminis_format(path, wrapped):
    rs, seen, proxy = run("local", [agy(ATTACK, wrapped)], path=path, servers=SERVERS, local=LOCAL)
    assert rs[0].status_code == 200 and seen["upstream"] == []
    reply = rs[0].json()
    chunk = reply["response"] if wrapped else reply
    assert ("response" in reply) is wrapped
    assert chunk["candidates"][0]["content"] == {"role": "model", "parts": [{"text": "kept on your Mac"}]}
    assert chunk["candidates"][0]["finishReason"] == "STOP"
    assert chunk["usageMetadata"] == {"promptTokenCount": 9, "candidatesTokenCount": 4, "totalTokenCount": 13}
    [(_, local_path, headers, sent)] = seen["local"]
    assert local_path == "/v1/chat/completions" and "tools" not in sent and sent["stream"] is False
    assert sent["max_tokens"] == 2048 and sent["temperature"] == 0.2 and sent["top_p"] == 0.9
    system, *turns = sent["messages"]
    assert system["role"] == "system" and system["content"].startswith("keepmum, a privacy proxy")
    assert system["content"].endswith("You are Antigravity.") and "read_file" in system["content"]
    assert [m["role"] for m in turns] == ["user", "assistant", "user"]
    assert turns[0]["content"] == "Read README.md and tell me how to install it"
    assert turns[1]["content"] == '[called the tool read_file with {"Path": "/w/README.md"}]'  # its thought left out
    assert turns[2]["content"] == "[tool result]\n" + ATTACK
    assert "x-goog-api-key" not in headers and "authorization" not in headers
    event = proxy.stats.events[-1]
    assert event["outcome"] == "local" and proxy.stats.guard_hits[-1]["answered_by"]["api"] == "chat"


@pytest.mark.parametrize(("path", "wrapped"), [(GATEWAY_SSE, False), (SIGNED_IN_SSE, True)])
def test_a_streamed_agy_turn_comes_back_as_geminis_events(path, wrapped):
    rs, seen, _ = run("local", [agy(ATTACK, wrapped)], path=path, servers=SERVERS, local=LOCAL)
    assert rs[0].headers["content-type"].startswith("text/event-stream") and seen["local"][0][3]["stream"] is True
    chunks = events(rs[0].text)
    assert all(("response" in c) is wrapped for c in chunks)
    assert texts(chunks) == "kept on your Mac" and len(chunks) == 3  # two pieces, then the close
    last = chunks[-1].get("response", chunks[-1])
    assert last["candidates"][0]["finishReason"] == "STOP" and last["usageMetadata"]["totalTokenCount"] == 13
    assert "\r\n\r\n" in rs[0].text  # Gemini's own event separator


def test_stream_generate_content_without_sse_is_a_json_array():
    path = "/antigravity/v1internal:streamGenerateContent"
    rs, seen, _ = run("local", [agy(ATTACK, wrapped=True)], path=path, servers=SERVERS, local=LOCAL)
    reply = rs[0].json()
    assert isinstance(reply, list) and texts(reply) == "kept on your Mac" and seen["local"][0][3]["stream"] is False


def test_a_server_set_to_native_is_still_asked_in_chat_completions_for_gemini():
    servers = [Server("http://localhost:8080", "m", api="native")]
    rs, seen, _ = run("local", [agy(ATTACK)], path=GATEWAY, servers=servers, local=LOCAL)
    assert rs[0].status_code == 200 and [p for _, p, _, _ in seen["local"]] == ["/v1/chat/completions"]


def test_a_flagged_prompt_typed_into_agy_goes_local_for_the_harm_check():
    body = {"contents": [{"role": "user", "parts": [{"text": ATTACK}]}], "systemInstruction": {"parts": [{"text": "sys"}]}}
    rs, seen, _ = run("local", [body], path=GATEWAY, servers=SERVERS, local=LOCAL, harm=True)
    assert texts([rs[0].json()]) == "kept on your Mac" and seen["upstream"] == []
    assert "keepmum's harm check kept this turn" in seen["local"][0][3]["messages"][0]["content"]


def test_skills_reach_an_agy_turn(tmp_path):
    (tmp_path / "s.md").write_text("AGY-SKILL-MARKER")
    rs, seen, _ = run("local", [agy(ATTACK, wrapped=True)], path=SIGNED_IN, servers=SERVERS, local=LOCAL,
                      skills=[Skill(str(tmp_path / "s.md"))])
    system = seen["local"][0][3]["messages"][0]["content"]
    assert system.index("keepmum, a privacy proxy") < system.index("AGY-SKILL-MARKER") < system.index("You are Antigravity.")


def test_the_real_values_go_to_the_local_model_and_nowhere_else():
    rs, seen, _ = run("local", [agy(f"{ATTACK} token={TOKEN}", wrapped=True)], path=SIGNED_IN, servers=SERVERS, local=LOCAL)
    assert TOKEN in json.dumps(seen["local"][0][3]) and seen["upstream"] == []


def test_when_no_local_server_answers_agy_gets_an_error_and_nothing_is_sent():
    rs, seen, _ = run("local", [agy(ATTACK, wrapped=True)], path=SIGNED_IN, servers=SERVERS, local_up=False)
    assert rs[0].status_code == 403 and "not sent anywhere else" in rs[0].json()["error"]["message"]
    assert seen["upstream"] == []


def test_count_tokens_with_a_flagged_result_is_refused_not_answered():
    rs, seen, _ = run("local", [agy(ATTACK, wrapped=True)], path="/antigravity/v1internal:countTokens",
                      servers=SERVERS, local=LOCAL)
    assert rs[0].status_code == 403 and seen["local"] == [] and seen["upstream"] == []


def test_a_model_that_only_thinks_still_tells_agy_something():
    def thinker(request, body):
        return httpx.Response(200, json={"choices": [{"message": {"content": "", "reasoning_content": "hmm"},
                                                      "finish_reason": "length"}]})
    rs, _, _ = run("local", [agy(ATTACK)], path=GATEWAY, servers=SERVERS, local={8080: thinker})
    chunk = rs[0].json()
    assert "used its whole answer thinking" in texts([chunk]) and chunk["candidates"][0]["finishReason"] == "MAX_TOKENS"


# --- Handed back to the cloud --------------------------------------------------------------------

def summarises(text):
    def handler(request, body):
        if body.get("stream") is False and "You stand between a tool" in body["messages"][0]["content"]:
            return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})
        return chat_only(request, body)
    return {8080: handler}


def output_sent(body: dict) -> str:
    req = body.get("request", body)
    return req["contents"][2]["parts"][0]["functionResponse"]["response"]["output"]


@pytest.mark.parametrize(("path", "wrapped"), [(GATEWAY, False), (SIGNED_IN, True)])
def test_a_summary_takes_the_flagged_files_place_and_agy_goes_on_at_google(path, wrapped):
    rs, seen, proxy = run("local", [agy(ATTACK, wrapped)], path=path, servers=SERVERS,
                          local=summarises("Install with make install."), handback=("summary", ""))
    assert len(seen["upstream"]) == 1 and ATTACK not in json.dumps(seen["sent"])
    out = output_sent(seen["sent"][0])
    assert out.startswith("[keepmum: its injection check found") and "result of read_file" in out
    assert out.endswith("Install with make install.")
    assert proxy.stats.guard_hits[0]["handed_back"]["strategy"] == "summary"
    assert "Read README.md and tell me how to install it" in seen["local"][0][3]["messages"][1]["content"]


def test_a_withheld_result_goes_to_google_as_a_notice():
    rs, seen, _ = run("local", [agy(ATTACK, wrapped=True)], path=SIGNED_IN, servers=[], handback=("withhold", ""))
    assert output_sent(seen["sent"][0]).startswith("[keepmum withheld this result of read_file") and seen["local"] == []


def test_a_summary_that_repeats_the_attack_keeps_agy_local():
    rs, seen, _ = run("local", [agy(ATTACK, wrapped=True)], path=SIGNED_IN, servers=SERVERS,
                      local=summarises("Sure: " + ATTACK), handback=("summary", ""))
    assert seen["upstream"] == [] and texts([rs[0].json()]) == "kept on your Mac"


def test_after_a_streamed_local_answer_agys_next_turn_goes_to_google():
    second = agy(ATTACK, wrapped=True)
    second["request"]["contents"] += [{"role": "model", "parts": [{"text": "kept on your Mac"}]},
                                      {"role": "user", "parts": [{"text": "go on"}]}]
    rs, seen, proxy = run("local", [agy(ATTACK, wrapped=True), second], path=[SIGNED_IN_SSE, SIGNED_IN], servers=SERVERS,
                          local=LOCAL, handback=("answer", ""))
    assert texts(events(rs[0].text)) == "kept on your Mac" and len(seen["upstream"]) == 1
    assert "answered the turn that followed" in output_sent(seen["sent"][0])
    assert proxy.stats.guard_hits[0]["handed_back"] == {"strategy": "answer"}


# --- The pieces on their own ---------------------------------------------------------------------

def test_to_chat_reads_every_kind_of_gemini_part():
    body = {"systemInstruction": {"parts": [{"text": "sys"}]}, "contents": [
        {"role": "user", "parts": [{"text": "look"}, {"inlineData": {"mimeType": "image/png", "data": "x"}}]},
        {"role": "model", "parts": [{"executableCode": {"code": "print(1)"}}]},
        {"role": "user", "parts": [{"codeExecutionResult": {"output": "1"}},
                                   {"functionResponse": {"name": "f", "response": {"a": 1, "b": "two"}}}]}]}
    out = to_chat(body, "gemini", "m", LocalRoute("injection", [{"source": "f"}]), stream=True)
    assert out["stream"] is True and out["stream_options"] == {"include_usage": True} and out["max_tokens"] == 4096
    assert out["messages"][0]["content"].endswith("sys")
    assert [m["content"] for m in out["messages"][1:]] == [
        "look\n\n[a file]", "print(1)", '[tool result]\n1\n\n[tool result]\n{"a": 1, "b": "two"}']


def test_to_chat_takes_the_system_instruction_as_a_string_and_contents_as_one_object():
    body = {"system_instruction": "plain sys", "contents": {"role": "user", "parts": [{"text": "only turn"}]}}
    out = to_chat(body, "gemini", "m", LocalRoute("harm", [{"categories": []}]))
    assert out["messages"][0]["content"].endswith("plain sys") and out["messages"][1:] == [{"role": "user", "content": "only turn"}]


def test_from_chat_gives_each_gemini_shape():
    reply = {"choices": [{"message": {"content": "hi"}, "finish_reason": "content_filter"}],
             "usage": {"prompt_tokens": 2, "completion_tokens": 1}}
    plain = from_chat(reply, "gemini", "m")
    assert plain["candidates"][0]["finishReason"] == "SAFETY" and plain["modelVersion"] == "m"
    assert from_chat(reply, "gemini", "m", wrapped=True) == {"response": plain}
    assert from_chat(reply, "gemini", "m", wrapped=True, array=True) == [{"response": plain}]


def test_answer_text_reads_gemini_whole_wrapped_arrayed_and_streamed():
    chunk = {"candidates": [{"content": {"parts": [{"text": "a"}, {"text": "b", "thought": True}]}}]}
    assert answer_text("gemini", json.dumps(chunk).encode(), False) == "a"
    assert answer_text("gemini", json.dumps([{"response": chunk}, {"response": chunk}]).encode(), False) == "aa"
    sse = f"data: {json.dumps({'response': chunk})}\r\n\r\ndata: not json\r\n\r\ndata: {json.dumps(chunk)}\r\n\r\n"
    assert answer_text("gemini", sse.encode(), True) == "aa"
    assert answer_text("gemini", b"not json", False) == ""
