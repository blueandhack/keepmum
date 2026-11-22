"""Masking requests of the less common shapes each API allows. Whatever the shape, the secret must not
be in what is sent, and nothing that is not text may change."""

import json

from keepmum.detect.pipeline import Pipeline
from keepmum.detect.rules import RulesDetector
from keepmum.redact import PLACEHOLDER_HINT, Redactor
from keepmum.vault import Vault

TOKEN = "ghp_" + "Zq8vX2mN4pL7kR9tY3wB6cF1hJ5dG0sAe7Qw"
SECRET = f"GITHUB_TOKEN={TOKEN}"


def redactor():
    return Redactor(Pipeline(RulesDetector(), None), Vault(bytes(range(32))))


def test_gemini_requests_of_every_shape_are_masked():
    r = redactor()
    # systemInstruction as a plain string: masked, and the hint goes with it as a part.
    out = r.redact_gemini_request({"systemInstruction": SECRET, "contents": [{"role": "user", "parts": [{"text": "hi"}]}]}, hint=True)
    assert TOKEN not in json.dumps(out) and out["systemInstruction"]["parts"][1]["text"] == PLACEHOLDER_HINT
    # contents as one object, and as plain strings; parts as strings, and of no known kind.
    out = r.redact_gemini_request({"contents": {"parts": [SECRET, 5, {"executable_code": {"code": f"print('{SECRET}')"}},
                                                          {"code_execution_result": {"output": SECRET}}]}})
    assert TOKEN not in json.dumps(out) and out["contents"]["parts"][1] == 5
    out = r.redact_gemini_request({"contents": [SECRET, 7, {"role": "user"}], "content": {"parts": [{"text": SECRET}]}})
    assert TOKEN not in json.dumps(out) and out["contents"][1] == 7 and out["contents"][2] == {"role": "user"}
    # A function's result whose parts are not a list: masked as values.
    out = r.redact_gemini_request({"contents": [{"role": "user", "parts": [
        {"functionResponse": {"name": "read_file", "response": {"output": SECRET}, "parts": {"text": SECRET}}}]}]})
    assert TOKEN not in json.dumps(out)


def test_responses_requests_of_every_shape_are_masked():
    r = redactor()
    out = r.redact_responses_request({"input": [
        5, {"type": "message", "role": "user", "content": SECRET},
        {"type": "message", "role": "user", "content": [7, {"type": "input_text", "text": SECRET}]},
        {"type": "message", "role": "user", "content": None},
        {"type": "function_call", "name": "shell", "call_id": "c", "arguments": f"not json {SECRET}"},
        {"type": "function_call_output", "call_id": "c", "output": SECRET},
    ]})
    sent = json.dumps(out)
    assert TOKEN not in sent and out["input"][1] == 5 and out["input"][3]["content"][0] == 7
    assert out["input"][4]["content"] is None


def test_messages_requests_of_every_shape_are_masked():
    r = redactor()
    out = r.redact_request({"system": SECRET, "messages": [
        {"role": "user", "content": SECRET},
        {"role": "user", "content": [5, {"type": "tool_result", "tool_use_id": "t", "content": [{"type": "text", "text": SECRET}]}]},
        {"role": "user", "content": [{"type": "document", "source": {"type": "content", "content": [{"type": "text", "text": SECRET}]}}]},
        {"role": "assistant", "content": None},
    ]})
    sent = json.dumps(out)
    assert TOKEN not in sent and out["messages"][1]["content"][0] == 5 and out["messages"][3]["content"] is None
