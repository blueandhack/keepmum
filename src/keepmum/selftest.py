"""`keepmum selftest`: a canary secret goes through the proxy to a local mock upstream.

Everything runs in this process over ASGI transports: no port is opened and nothing leaves
the machine. The test fails if any form of the canary (raw, JSON-escaped, base64, URL-encoded)
reaches the mock upstream, or if restore, the tool policy or a fail-closed path misbehaves.
"""

from __future__ import annotations

import base64
import json
import secrets
import string
import urllib.parse

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .proxy import Proxy
from .redact import Redactor
from .vault import PLACEHOLDER, Vault


def make_canary() -> str:
    """A fresh random string in GitHub's token format. Synthetic: it opens nothing."""
    return "ghp_" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(36))


class MockUpstream:
    """Records every byte it receives and echoes placeholders back the way a model might."""

    def __init__(self):
        self.received: list[bytes] = []
        self.app = Starlette(routes=[Route("/{path:path}", self.handle, methods=["GET", "POST"])])

    async def handle(self, request: Request) -> Response:
        body = await request.body()
        self.received.append(request.url.path.encode() + b"?" + request.url.query.encode() + b"\n"
                             + b"\n".join(f"{k}: {v}".encode() for k, v in request.headers.items()) + b"\n\n" + body)
        if request.url.path == "/v1/messages/count_tokens":
            return JSONResponse({"input_tokens": 42})
        if request.url.path not in ("/v1/messages", "/v1/responses"):
            return JSONResponse({"ok": True})
        req = json.loads(body)
        found = PLACEHOLDER.findall(body.decode())
        ph = f"KEEPMUM_{found[0][0]}_{found[0][1]}" if found else "none"
        if request.url.path == "/v1/responses":
            if req.get("stream"):
                return Response(self.responses_stream(ph), media_type="text/event-stream")
            return JSONResponse({"id": "resp_selftest", "object": "response", "status": "completed",
                                 "output": self.responses_items(ph)})
        if req.get("stream"):
            return Response(self.stream(ph), media_type="text/event-stream")
        return JSONResponse({
            "id": "msg_selftest", "type": "message", "role": "assistant", "model": req.get("model"),
            "content": [
                {"type": "text", "text": f"The token {ph} is set."},
                {"type": "tool_use", "id": "toolu_w", "name": "Write", "input": {"file_path": "/tmp/x", "content": f"TOKEN={ph}\n"}},
                {"type": "tool_use", "id": "toolu_f", "name": "WebFetch", "input": {"url": f"https://example.com/?t={ph}"}},
            ],
            "stop_reason": "tool_use", "usage": {"input_tokens": 1, "output_tokens": 1},
        })


    @staticmethod
    def stream(ph: str) -> str:
        """A realistic SSE answer with the placeholder split across deltas."""
        def ev(data):
            return f"event: {data['type']}\ndata: {json.dumps(data)}\n\n"

        cut = len(ph) // 2
        tool_json = json.dumps({"file_path": "/tmp/x", "content": f"TOKEN={ph}\n"})
        fetch_json = json.dumps({"url": f"https://example.com/?t={ph}"})
        out = [ev({"type": "message_start", "message": {"id": "msg_s", "type": "message", "role": "assistant", "content": []}}),
               ev({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
               ev({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": f"The token {ph[:cut]}"}}),
               ev({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": f"{ph[cut:]} is set."}}),
               ev({"type": "content_block_stop", "index": 0})]
        for i, name, js in ((1, "Write", tool_json), (2, "WebFetch", fetch_json)):
            out.append(ev({"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": f"toolu_{i}", "name": name, "input": {}}}))
            mid = len(js) // 2
            for part in (js[:mid], js[mid:]):
                out.append(ev({"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": part}}))
            out.append(ev({"type": "content_block_stop", "index": i}))
        out.append(ev({"type": "message_stop"}))
        return "".join(out)

    @staticmethod
    def responses_items(ph: str) -> list[dict]:
        """A Responses API answer: text, a function call, an MCP call and a shell call, each
        holding the placeholder."""
        return [
            {"type": "message", "id": "msg_1", "role": "assistant", "content": [{"type": "output_text", "text": f"The token {ph} is set."}]},
            {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "write_file",
             "arguments": json.dumps({"path": "/tmp/x", "content": f"TOKEN={ph}\n"})},
            {"type": "mcp_call", "id": "mcp_1", "server_label": "web", "name": "fetch",
             "arguments": json.dumps({"url": f"https://example.com/?t={ph}"})},
            {"type": "shell_call", "id": "sh_1", "call_id": "call_2", "action": {"commands": [f"echo {ph}"]}},
        ]

    @classmethod
    def responses_stream(cls, ph: str) -> str:
        """The same answer streamed, the placeholder split across text and argument deltas."""
        def ev(data):
            return f"event: {data['type']}\ndata: {json.dumps(data)}\n\n"

        msg, fn, mcp, shell = cls.responses_items(ph)
        cut = len(ph) // 2
        out = [ev({"type": "response.created", "response": {"id": "resp_s", "output": []}}),
               ev({"type": "response.output_item.added", "output_index": 0, "item": {**msg, "content": []}})]
        for part in (f"The token {ph[:cut]}", f"{ph[cut:]} is set."):
            out.append(ev({"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 0, "content_index": 0, "delta": part}))
        out += [ev({"type": "response.output_text.done", "item_id": "msg_1", "output_index": 0, "content_index": 0,
                    "text": msg["content"][0]["text"]}),
                ev({"type": "response.output_item.done", "output_index": 0, "item": msg})]
        for i, item, kind in ((1, fn, "function_call"), (2, mcp, "mcp_call")):
            args = item["arguments"]
            out.append(ev({"type": "response.output_item.added", "output_index": i, "item": {**item, "arguments": ""}}))
            for part in (args[: len(args) // 2], args[len(args) // 2:]):
                out.append(ev({"type": f"response.{kind}_arguments.delta", "item_id": item["id"], "output_index": i, "delta": part}))
            out += [ev({"type": f"response.{kind}_arguments.done", "item_id": item["id"], "output_index": i, "arguments": args}),
                    ev({"type": "response.output_item.done", "output_index": i, "item": item})]
        out += [ev({"type": "response.output_item.added", "output_index": 3, "item": shell}),
                ev({"type": "response.output_item.done", "output_index": 3, "item": shell}),
                ev({"type": "response.completed", "response": {"id": "resp_s", "status": "completed", "output": [msg, fn, mcp, shell]}})]
        return "".join(out)


def _forms(canary: str) -> list[bytes]:
    return [canary.encode(), json.dumps(canary).strip('"').encode(), base64.b64encode(canary.encode()),
            urllib.parse.quote(canary).encode(), canary[4:].encode()]


async def run(detector, verbose: bool = True) -> bool:
    canary = make_canary()
    mock = MockUpstream()
    upstream = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock.app), base_url="http://upstream.test")
    proxy = Proxy(Redactor(detector, Vault(secrets.token_bytes(32))), upstream="http://upstream.test",
                  client=upstream, stats_token="selftest", openai_upstream="http://upstream.test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1:7788")
    auth = {"x-api-key": "sk-ant-selftest-not-a-key", "anthropic-version": "2023-06-01"}
    body = {
        "model": "claude-opus-5-5", "max_tokens": 64,
        "system": [{"type": "text", "text": f"Environment: GITHUB_TOKEN={canary}"}],
        "messages": [
            {"role": "user", "content": f"Why does `gh auth login --with-token {canary}` fail?"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": f"echo {canary}"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": f"{canary}\n"}]},
        ],
    }
    results: list[tuple[str, bool]] = []

    def check(name, ok):
        results.append((name, bool(ok)))

    r = await client.post("/v1/messages", json=body, headers=auth)
    out = r.json() if r.status_code == 200 else {}
    content = out.get("content", [{}, {}, {}])
    check("request forwarded (200)", r.status_code == 200)
    check("assistant text restored", canary in content[0].get("text", ""))
    check("Write tool input restored", canary in json.dumps(content[1].get("input", {})))
    check("WebFetch tool input keeps the placeholder", canary not in json.dumps(content[2].get("input", {}))
          and PLACEHOLDER.search(json.dumps(content[2].get("input", {}))))
    check("auth header passed through", any(b"x-api-key: sk-ant-selftest-not-a-key" in b for b in mock.received))

    r = await client.post("/v1/messages/count_tokens", json=body, headers=auth)
    check("count_tokens forwarded", r.status_code == 200)
    r = await client.post("/v1/messages", json={**body, "stream": True}, headers=auth)
    streamed = _assemble_stream(r.text) if r.status_code == 200 else {}
    check("streamed text restored (placeholder split across deltas)", canary in streamed.get(0, ""))
    check("streamed Write input restored", canary in streamed.get(1, ""))
    check("streamed WebFetch input keeps the placeholder", canary not in streamed.get(2, canary) and PLACEHOLDER.search(streamed.get(2, "")))

    # The Responses API (Codex, pi, opencode): a zstd body in two frames, as Codex may send it.
    import zstandard

    oauth = {"authorization": "Bearer sk-selftest-not-a-key"}
    rbody = {"model": "gpt-5.5", "instructions": f"Environment: GITHUB_TOKEN={canary}",
             "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"Why does {canary} fail?"}]}]}
    raw = json.dumps(rbody).encode()
    z = zstandard.ZstdCompressor()
    r = await client.post("/openai/v1/responses", content=z.compress(raw[: len(raw) // 2]) + z.compress(raw[len(raw) // 2:]),
                          headers={**oauth, "content-type": "application/json", "content-encoding": "zstd"})
    items = r.json().get("output", []) if r.status_code == 200 else [{}] * 4
    check("Responses API: zstd request in two frames forwarded (200)", r.status_code == 200)
    check("Responses API: text restored", canary in json.dumps(items[0]))
    check("Responses API: function call restored", canary in items[1].get("arguments", ""))
    check("Responses API: MCP call keeps the placeholder", canary not in items[2].get("arguments", canary)
          and PLACEHOLDER.search(items[2].get("arguments", "")))
    check("Responses API: shell call restored", canary in json.dumps(items[3].get("action", {})))
    r = await client.post("/openai/v1/responses", json={**rbody, "stream": True}, headers=oauth)
    events = _responses_events(r.text) if r.status_code == 200 else []
    deltas = "".join(e.get("delta", "") for e in events if e.get("type") == "response.output_text.delta")
    done = {e["type"]: e for e in events if e.get("type", "").endswith("_arguments.done")}
    check("Responses API: streamed text restored (placeholder split across deltas)", canary in deltas)
    check("Responses API: streamed function call restored",
          canary in done.get("response.function_call_arguments.done", {}).get("arguments", ""))
    check("Responses API: streamed MCP call keeps the placeholder",
          canary not in done.get("response.mcp_call_arguments.done", {}).get("arguments", canary))

    sent = len(mock.received)
    r = await client.post("/v1/messages", content=b'{"messages": [' + canary.encode(), headers=auth)
    check("malformed JSON rejected, not sent", r.status_code == 400 and len(mock.received) == sent)
    r = await client.post("/v1/files", content=canary.encode(), headers=auth)
    check("unscanned endpoint with a body blocked", r.status_code == 403 and len(mock.received) == sent)
    r = await client.post("/v1/messages", json=body, headers={**auth, "host": "attacker.example"})
    check("non-local Host rejected", r.status_code == 403 and len(mock.received) == sent)

    class Broken:
        def detect(self, text):
            raise RuntimeError("detector down")

    broken = Proxy(Redactor(Broken(), Vault(secrets.token_bytes(32))), upstream="http://upstream.test", client=upstream)
    bclient = httpx.AsyncClient(transport=httpx.ASGITransport(app=broken.app), base_url="http://127.0.0.1:7788")
    r = await bclient.post("/v1/messages", json=body, headers=auth)
    check("detector failure blocks the request", r.status_code == 500 and len(mock.received) == sent)

    r = await client.get("/_keepmum/stats", headers={"authorization": "Bearer selftest"})
    stats = r.text
    check("stats feed served with the token", r.status_code == 200 and json.loads(stats)["redacted"].get("SECRET", 0) >= 4)
    check("stats feed holds no value or placeholder", canary not in stats and not PLACEHOLDER.search(stats))
    r = await client.get("/_keepmum/stats")
    check("stats feed refused without the token", r.status_code == 401)

    leaked = [f for f in _forms(canary) if any(f in b for b in mock.received)]
    check("CANARY NEVER REACHED UPSTREAM (raw, escaped, base64, url, body)", not leaked)
    await client.aclose()
    await bclient.aclose()
    await upstream.aclose()
    if verbose:
        for name, ok in results:
            print(f"  {'✓' if ok else '✗'} {name}")
    return all(ok for _, ok in results)


def _responses_events(text: str) -> list[dict]:
    return [json.loads(line[5:].strip()) for line in text.splitlines() if line.startswith("data:")]


def _assemble_stream(text: str) -> dict[int, str]:
    """Concatenated text or tool JSON per block index."""
    blocks: dict[int, str] = {}
    for raw in text.split("\n\n"):
        data = next((line[5:].strip() for line in raw.splitlines() if line.startswith("data:")), None)
        if not data:
            continue
        d = json.loads(data)
        if d.get("type") == "content_block_delta":
            delta = d["delta"]
            blocks[d["index"]] = blocks.get(d["index"], "") + delta.get("text", delta.get("partial_json", ""))
    return blocks
