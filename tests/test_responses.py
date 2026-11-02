"""The OpenAI Responses API path (Codex, pi): redaction, restore, streams, and the proxy routes."""

import json
import secrets
from collections import Counter

import anyio
import httpx
import pytest
import zstandard
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from keepmum import selftest
from keepmum.detect.pipeline import Pipeline
from keepmum.detect.rules import RulesDetector
from keepmum.proxy import Proxy
from keepmum.redact import RESPONSES_HINT, Redactor
from keepmum.responses import ResponsesStreamRestorer, restore_responses
from keepmum.vault import PLACEHOLDER, Vault

SALT = bytes(range(32))


def redactor():
    return Redactor(Pipeline(RulesDetector(), None), Vault(SALT))


def test_request_items_are_masked_and_reasoning_passes_through():
    canary = selftest.make_canary()
    r = redactor()
    body = {
        "model": "m", "instructions": f"Deploy key {canary}", "stream": True,
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"export GITHUB_TOKEN={canary}"}]},
            {"type": "function_call", "name": "exec_command", "call_id": "c1",
             "arguments": json.dumps({"cmd": f"echo GITHUB_TOKEN={canary}", "workdir": "/tmp"})},
            {"type": "function_call_output", "call_id": "c1", "output": f"GITHUB_TOKEN={canary}\n"},
            {"type": "reasoning", "encrypted_content": "opaque", "summary": []},
        ],
    }
    out = r.redact_responses_request(body)
    assert canary not in json.dumps(out)
    assert out["input"][0] == RESPONSES_HINT and out["input"][-1] == body["input"][-1]
    args = json.loads(out["input"][2]["arguments"])
    assert set(args) == {"cmd", "workdir"} and PLACEHOLDER.search(args["cmd"])


def test_unchanged_arguments_keep_their_exact_text():
    r = redactor()
    raw = '{"cmd":  "ls -la"}'
    item = {"type": "function_call", "name": "exec_command", "arguments": raw}
    assert r.redact_responses_request({"input": [item]})["input"][1]["arguments"] == raw


def sse(*events):
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


def masked(r, value):
    r.redact_text(f"GITHUB_TOKEN={value}")
    return next(ph for ph, v in r.vault._values.items() if v == value)


def test_stream_restores_split_text_and_buffered_tool_calls_and_counts_once():
    canary = selftest.make_canary()
    r = redactor()
    ph = masked(r, canary)
    args = json.dumps({"cmd": f"echo {ph}"})
    events = [
        {"type": "response.output_item.added", "item": {"type": "message", "id": "m1", "content": []}, "output_index": 0},
        {"type": "response.output_text.delta", "item_id": "m1", "content_index": 0, "delta": "token " + ph[:9]},
        {"type": "response.output_text.delta", "item_id": "m1", "content_index": 0, "delta": ph[9:] + " ok"},
        {"type": "response.output_text.done", "item_id": "m1", "content_index": 0, "text": f"token {ph} ok"},
        {"type": "response.output_item.done", "item": {"type": "message", "id": "m1",
                                                        "content": [{"type": "output_text", "text": f"token {ph} ok"}]}},
        {"type": "response.output_item.added", "item": {"type": "function_call", "id": "f1", "name": "exec_command",
                                                         "arguments": ""}, "output_index": 1},
        {"type": "response.function_call_arguments.delta", "item_id": "f1", "delta": args[:12]},
        {"type": "response.function_call_arguments.delta", "item_id": "f1", "delta": args[12:]},
        {"type": "response.function_call_arguments.done", "item_id": "f1", "arguments": args},
        {"type": "response.completed", "response": {"output": [{"type": "function_call", "name": "exec_command", "arguments": args}]}},
    ]
    counts = Counter()
    rs = ResponsesStreamRestorer(r.vault, counts=counts)
    stream = sse(*events)
    out = b"".join(rs.feed(stream[i:i + 7]) for i in range(0, len(stream), 7)) + rs.close()
    text = out.decode()
    assert ph not in text and canary in text
    parsed = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]
    deltas = "".join(e["delta"] for e in parsed if e["type"] == "response.output_text.delta")
    assert deltas == f"token {canary} ok"
    arg_deltas = [e["delta"] for e in parsed if e["type"] == "response.function_call_arguments.delta"]
    assert len(arg_deltas) == 1 and json.loads(arg_deltas[0]) == {"cmd": f"echo {canary}"}
    assert counts["SECRET"] == 2  # once in the text, once in the tool call


def test_network_tools_keep_placeholders():
    from keepmum.restore import take_tools

    canary = selftest.make_canary()
    r = redactor()
    ph = masked(r, canary)
    counts = Counter()
    body = {"output": [
        {"type": "function_call", "name": "mcp__web__fetch", "arguments": json.dumps({"url": f"https://x/?t={ph}"})},
        {"type": "function_call", "name": "mcp_github_search", "arguments": json.dumps({"q": ph})},
        {"type": "function_call", "name": "webfetch", "arguments": json.dumps({"url": f"https://x/?t={ph}"})},
        {"type": "function_call", "name": "websearch", "arguments": json.dumps({"q": ph})},
        {"type": "function_call", "name": "web_fetch", "arguments": json.dumps({"url": f"https://x/?t={ph}"})},
        {"type": "function_call", "name": "web_search", "arguments": json.dumps({"q": ph})},
        {"type": "function_call", "name": "codesearch", "arguments": json.dumps({"q": ph})},
        {"type": "mcp_call", "server_label": "docs", "name": "search", "arguments": json.dumps({"q": ph})},
        {"type": "shell_call", "action": {"commands": [f"echo {ph}"]}},
        {"type": "message", "content": [{"type": "output_text", "text": ph}]},
    ]}
    out = restore_responses(body, r.vault, counts=counts)
    for item in out["output"][:8]:
        assert ph in item["arguments"] and canary not in item["arguments"]
    assert out["output"][8]["action"]["commands"] == [f"echo {canary}"]
    assert out["output"][9]["content"][0]["text"] == canary
    tools = {t["tool"]: t["action"] for t in take_tools(counts)}
    assert tools["mcp__docs__search"] == "keep" and tools["shell"] == "restore" and tools["webfetch"] == "keep"


def test_stream_mcp_call_keeps_placeholder_and_records_tool():
    from keepmum.restore import take_tools

    canary = selftest.make_canary()
    r = redactor()
    ph = masked(r, canary)
    args = json.dumps({"q": ph})
    events = [
        {"type": "response.output_item.added", "item": {"type": "mcp_call", "id": "m1", "server_label": "docs", "name": "search",
                                                         "arguments": ""}, "output_index": 0},
        {"type": "response.mcp_call_arguments.delta", "item_id": "m1", "delta": args[:8]},
        {"type": "response.mcp_call_arguments.delta", "item_id": "m1", "delta": args[8:]},
        {"type": "response.mcp_call_arguments.done", "item_id": "m1", "arguments": args},
        {"type": "response.output_item.done", "item": {"type": "mcp_call", "id": "m1", "server_label": "docs", "name": "search",
                                                        "arguments": args}},
    ]
    counts = Counter()
    rs = ResponsesStreamRestorer(r.vault, counts=counts)
    out = (rs.feed(sse(*events)) + rs.close()).decode()
    assert canary not in out and ph in out
    assert take_tools(counts) == [{"tool": "mcp__docs__search", "action": "keep", "values": 1, "reason": "secret",
                                   "types": {"SECRET": 1}}]


def test_responses_request_masks_mcp_call_error_and_refusal():
    canary = selftest.make_canary()
    r = redactor()
    body = {
        "model": "m",
        "input": [
            {"type": "mcp_call", "id": "m1", "server_label": "s", "name": "call",
             "arguments": json.dumps({"token": f"GITHUB_TOKEN={canary}"}),
             "error": f"failed with GITHUB_TOKEN={canary}"},
            {"type": "function_call_output", "call_id": "c1", "error": f"GITHUB_TOKEN={canary}"},
            {"type": "message", "role": "assistant", "content": [{"type": "refusal", "refusal": f"GITHUB_TOKEN={canary}"}]},
        ],
    }
    out = r.redact_responses_request(body)
    assert canary not in json.dumps(out)
    assert len(PLACEHOLDER.findall(json.dumps(out))) == 4


def test_multiframe_and_truncated_zstd_bodies():
    canary = selftest.make_canary()
    mock = ResponsesMock()
    payload = json.dumps(codex_body(canary)).encode()
    mid = len(payload) // 2
    comp = zstandard.ZstdCompressor()
    multiframe = comp.compress(payload[:mid]) + comp.compress(payload[mid:])
    truncated = comp.compress(payload)[:-4]

    async def go():
        _, client = make(mock)
        headers = {"content-encoding": "zstd", "content-type": "application/json", "x-keepmum-client": "codex"}
        ok = await client.post("/chatgpt/backend-api/codex/responses", content=multiframe, headers=headers)
        bad = await client.post("/chatgpt/backend-api/codex/responses", content=truncated, headers=headers)
        return ok, bad

    ok, bad = anyio.run(go)
    assert ok.status_code == 200 and canary in ok.text
    assert bad.status_code == 400 and len(mock.received) == 1
    assert canary.encode() not in mock.received[0]


class ResponsesMock:
    """An OpenAI-shaped upstream: records bodies (decompressed), streams back the first placeholder it sees."""

    def __init__(self):
        self.received: list[bytes] = []
        self.paths: list[str] = []
        self.headers: list[dict] = []
        self.app = Starlette(routes=[Route("/{path:path}", self.handle, methods=["GET", "POST"])])

    async def handle(self, request: Request):
        body = await request.body()
        if request.headers.get("content-encoding") == "zstd":
            body = zstandard.ZstdDecompressor().decompressobj().decompress(body)
        self.received.append(body)
        self.paths.append(request.url.path)
        self.headers.append({k.lower(): v for k, v in request.headers.items()})
        if request.method == "GET":
            return JSONResponse({"models": []})
        found = PLACEHOLDER.search(body.decode())
        ph = found.group(0) if found else "none"
        args = json.dumps({"cmd": f"echo {ph}"})
        stream = sse(
            {"type": "response.output_text.delta", "item_id": "m", "content_index": 0, "delta": f"saw {ph}"},
            {"type": "response.output_text.done", "item_id": "m", "content_index": 0, "text": f"saw {ph}"},
            {"type": "response.output_item.added", "item": {"type": "function_call", "id": "f", "name": "exec_command"}},
            {"type": "response.function_call_arguments.delta", "item_id": "f", "delta": args},
            {"type": "response.function_call_arguments.done", "item_id": "f", "arguments": args},
        )
        return Response(stream)  # no content type, like the ChatGPT backend


def make(mock):
    up = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock.app), base_url="http://up")
    proxy = Proxy(redactor(), upstream="http://anthropic", client=up, stats_token="t",
                  openai_upstream="http://openai", chatgpt_upstream="http://chatgpt")
    return proxy, httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1:7788")


def codex_body(canary):
    return {"model": "m", "stream": True, "instructions": "be brief",
            "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"GITHUB_TOKEN={canary}"}]}]}


def test_codex_round_trip_with_zstd_never_sends_the_value():
    canary = selftest.make_canary()
    mock = ResponsesMock()

    async def go():
        _, client = make(mock)
        raw = zstandard.ZstdCompressor().compress(json.dumps(codex_body(canary)).encode())
        headers = {"content-encoding": "zstd", "content-type": "application/json", "x-keepmum-client": "codex",
                   "user-agent": "codex_exec/0.147.0"}
        r = await client.post("/chatgpt/backend-api/codex/responses", content=raw, headers=headers)
        models = await client.get("/chatgpt/backend-api/codex/models?client_version=1")
        stats = (await client.get("/_keepmum/stats", headers={"authorization": "Bearer t"})).json()
        return r, models, stats

    r, models, stats = anyio.run(go)
    assert r.status_code == 200 and canary in r.text and "KEEPMUM_" not in r.text
    assert not any(canary.encode() in b for b in mock.received)
    assert mock.paths[0] == "/backend-api/codex/responses" and models.status_code == 200
    assert "x-keepmum-client" not in mock.headers[0]
    assert mock.headers[0]["content-encoding"] == "zstd"  # chatgpt.com refuses large uncompressed bodies
    event = stats["recent"][0]
    assert event["client"] == "codex" and event["provider"] == "openai" and event["redacted"].get("SECRET")


def test_opencode_chatgpt_login_on_the_openai_route_goes_to_chatgpt():
    """opencode 2.x keeps one OpenAI base URL; a ChatGPT login is told apart by its account header."""
    canary = selftest.make_canary()
    mock = ResponsesMock()

    async def go():
        _, client = make(mock)
        headers = {"chatgpt-account-id": "acct", "x-keepmum-client": "opencode", "authorization": "Bearer jwt"}
        chatgpt = await client.post("/openai/v1/responses", json=codex_body(canary), headers=headers)
        apikey = await client.post("/openai/v1/responses", json={**codex_body(canary), "stream": False},
                                   headers={"x-keepmum-client": "opencode"})
        stats = (await client.get("/_keepmum/stats", headers={"authorization": "Bearer t"})).json()
        return chatgpt, apikey, stats

    chatgpt, apikey, stats = anyio.run(go)
    assert chatgpt.status_code == 200 and canary in chatgpt.text and apikey.status_code == 200
    assert mock.paths == ["/backend-api/codex/responses", "/v1/responses"]
    assert mock.headers[0]["content-encoding"] == "zstd" and "content-encoding" not in mock.headers[1]
    assert not any(canary.encode() in b for b in mock.received)
    assert {e["client"] for e in stats["recent"]} == {"opencode"}


def test_openai_api_key_route_and_fail_closed_cases():
    canary = selftest.make_canary()
    mock = ResponsesMock()

    async def go():
        _, client = make(mock)
        ok = await client.post("/openai/v1/responses", json={**codex_body(canary), "stream": False})
        brotli = await client.post("/openai/v1/responses", content=b"x", headers={"content-encoding": "br"})
        chat = await client.post("/openai/v1/chat/completions", json={"messages": [{"role": "user", "content": canary}]})
        return ok, brotli, chat

    ok, brotli, chat = anyio.run(go)
    assert ok.status_code == 200 and mock.paths == ["/v1/responses"]
    assert brotli.status_code == 400 and chat.status_code == 403  # unreadable, or not a scanned endpoint
    assert not any(canary.encode() in b for b in mock.received)


def test_websocket_upgrades_are_refused():
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    proxy, _ = make(ResponsesMock())
    with TestClient(proxy.app, base_url="http://127.0.0.1:7788") as tc:
        with pytest.raises(WebSocketDisconnect):
            with tc.websocket_connect("/chatgpt/backend-api/codex/responses"):
                pass


def test_an_upgrade_that_arrives_as_plain_http_is_not_forwarded():
    mock = ResponsesMock()

    async def go():
        _, client = make(mock)
        return await client.get("/openai/v1/responses", headers={"upgrade": "websocket", "connection": "Upgrade"})

    r = anyio.run(go)
    assert r.status_code == 403 and mock.received == []
