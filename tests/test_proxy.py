import json
import logging
import secrets

import anyio
import httpx
import pytest

from keepmum import selftest
from keepmum.detect.pipeline import Pipeline
from keepmum.detect.rules import RulesDetector
from keepmum.proxy import Proxy
from keepmum.redact import Redactor
from keepmum.vault import Vault

API_KEY = "sk-ant-test-not-a-real-key"


def rules_pipeline():
    return Pipeline(RulesDetector(), None)


def test_selftest_canary_never_reaches_upstream():
    assert anyio.run(selftest.run, rules_pipeline(), False)


def make(upstream_app=None, transport=None):
    mock = selftest.MockUpstream()
    upstream = httpx.AsyncClient(transport=transport or httpx.ASGITransport(app=upstream_app or mock.app), base_url="http://up")
    proxy = Proxy(Redactor(rules_pipeline(), Vault(secrets.token_bytes(32))), upstream="http://up", client=upstream, stats_token="t")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1:7788")
    return mock, client


def body_with(canary):
    return {"model": "m", "max_tokens": 8, "messages": [{"role": "user", "content": f"export GITHUB_TOKEN={canary}"}]}


def test_logs_never_hold_values_or_auth(caplog):
    canary = selftest.make_canary()

    async def go():
        mock, client = make()
        await client.post("/v1/messages", json=body_with(canary), headers={"x-api-key": API_KEY})
        await client.post("/v1/files", content=canary.encode(), headers={"x-api-key": API_KEY})

    with caplog.at_level(logging.DEBUG):
        anyio.run(go)
    text = caplog.text
    assert "redacted" in text  # the request was logged...
    assert canary not in text and API_KEY not in text and "KEEPMUM_" not in text  # ...without values


def test_upstream_down_is_a_502_not_a_raw_send():
    def refuse(request):
        raise httpx.ConnectError("down")

    async def go():
        _, client = make(transport=httpx.MockTransport(refuse))
        return await client.post("/v1/messages", json=body_with(selftest.make_canary()))

    r = anyio.run(go)
    assert r.status_code == 502 and r.json()["type"] == "error"


def test_upstream_errors_pass_through():
    def overloaded(request):
        return httpx.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}})

    async def go():
        _, client = make(transport=httpx.MockTransport(overloaded))
        return await client.post("/v1/messages", json=body_with("x"))

    r = anyio.run(go)
    assert r.status_code == 529 and r.json()["error"]["type"] == "overloaded_error"


def test_bodyless_get_is_forwarded():
    async def go():
        mock, client = make()
        r = await client.get("/v1/models")
        return r, mock

    r, mock = anyio.run(go)
    assert r.status_code == 200 and mock.received and mock.received[0].startswith(b"/v1/models")


@pytest.mark.parametrize("host, ok", [("127.0.0.1:7788", True), ("localhost:7788", True), ("[::1]:7788", True),
                                      ("localhost.attacker.example", False), ("192.168.1.5:7788", False)])
def test_host_header(host, ok):
    async def go():
        _, client = make()
        return await client.get("/_keepmum/health", headers={"host": host})

    assert (anyio.run(go).status_code == 200) is ok


def test_query_string_is_kept():
    async def go():
        mock, client = make()
        await client.post("/v1/messages?beta=true", json=body_with("x"))
        return mock

    assert anyio.run(go).received[0].startswith(b"/v1/messages?beta=true")


def test_unicode_survives_the_round_trip():
    async def go():
        _, client = make()
        r = await client.post("/v1/messages", json={"model": "m", "messages": [{"role": "user", "content": "héllo 漢字 🙂"}]})
        return r

    r = anyio.run(go)
    assert r.status_code == 200 and json.loads(r.content)["content"][0]["text"].startswith("The token")


def test_lone_surrogate_in_json_escape_does_not_500():
    canary = selftest.make_canary()

    async def go():
        mock, client = make()
        raw = ('{"model":"m","max_tokens":8,"messages":[{"role":"user","content":"\\ud800 export GITHUB_TOKEN='
               + canary + '"}]}').encode()
        r = await client.post("/v1/messages", content=raw, headers={"content-type": "application/json"})
        return r, mock

    r, mock = anyio.run(go)
    assert r.status_code == 200 and not any(canary.encode() in b for b in mock.received)


def test_a_request_is_clean_when_its_new_text_has_nothing_to_mask():
    canary = selftest.make_canary()

    async def go():
        _, client = make()
        first = body_with(canary)
        await client.post("/v1/messages", json=first)
        # The next turn resends the conversation (with the secret) and adds harmless text: clean.
        msgs = first["messages"] + [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "thanks"}]
        await client.post("/v1/messages", json={**first, "messages": msgs})
        # The secret pasted again in new text is new sensitive data, even though it is known.
        msgs = msgs + [{"role": "assistant", "content": "sure"}, {"role": "user", "content": f"again: {canary}"}]
        await client.post("/v1/messages", json={**first, "messages": msgs})
        r = await client.get("/_keepmum/stats", headers={"authorization": "Bearer t"})
        return r.json()

    stats = anyio.run(go)
    assert [e["new"] > 0 for e in stats["recent"]] == [True, False, True]
    assert stats["funnel"]["with_findings"] == 3 and stats["funnel"]["with_new"] == 2 and stats["funnel"]["clean"] == 1



def make_observing(flag):
    mock = selftest.MockUpstream()
    upstream = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock.app), base_url="http://up")
    proxy = Proxy(Redactor(rules_pipeline(), Vault(secrets.token_bytes(32))), upstream="http://up", client=upstream,
                  stats_token="t", observe=flag)
    return mock, httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1:7788")


def test_observe_mode_counts_but_sends_the_request_unchanged():
    canary = selftest.make_canary()

    async def go():
        mock, client = make_observing(True)
        sent = json.dumps(body_with(canary)).encode()
        r1 = await client.post("/v1/messages", content=sent, headers={"content-type": "application/json"})
        r2 = await client.post("/v1/messages/count_tokens", content=b"not json")  # malformed: passed on, not rejected
        r3 = await client.post("/v1/files", content=b"data")  # an endpoint keepmum does not scan
        stats = (await client.get("/_keepmum/stats", headers={"authorization": "Bearer t"})).json()
        return mock, sent, (r1, r2, r3), stats

    mock, sent, responses, stats = anyio.run(go)
    assert any(b.endswith(sent) for b in mock.received) and any(b.endswith(b"not json") for b in mock.received)
    assert all(r.status_code != 403 for r in responses)
    assert stats["observe"] is True and stats["requests"].get("blocked") is None
    assert stats["requests"]["observed"] == 3 and stats["redacted"].get("SECRET", 0) >= 1


def test_observe_flag_is_read_per_request(tmp_path):
    from keepmum.config import ObserveFlag, set_detect_flag

    cfg = tmp_path / "config.toml"
    cfg.write_text("[server]\nport = 7788\n\n[detect]\npii = true\n")
    flag = ObserveFlag(cfg, False)
    canary = selftest.make_canary()

    async def send(client):
        await client.post("/v1/messages", json=body_with(canary))

    mock, client = make_observing(flag)
    anyio.run(send, client)
    assert not any(canary.encode() in b for b in mock.received)  # masking
    set_detect_flag("observe", True, cfg)
    anyio.run(send, client)
    assert any(canary.encode() in b for b in mock.received)  # observing, no restart
    assert "pii = true" in cfg.read_text() and "port = 7788" in cfg.read_text()
    cfg.write_text("[detect]\nobserve = maybe\n")  # invalid config: back to masking
    mock.received.clear()
    anyio.run(send, client)
    assert not any(canary.encode() in b for b in mock.received)



def test_history_survives_and_pages(tmp_path):
    from keepmum.stats import History, Stats

    hist = History(tmp_path / "history.jsonl", keep=50)
    canary = selftest.make_canary()

    def make_with_history():
        mock = selftest.MockUpstream()
        upstream = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock.app), base_url="http://up")
        proxy = Proxy(Redactor(rules_pipeline(), Vault(secrets.token_bytes(32))), upstream="http://up", client=upstream,
                      stats=Stats(history=hist), stats_token="t")
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1:7788")

    async def go():
        client = make_with_history()
        await client.post("/v1/messages", json=body_with(canary))
        for i in range(4):
            await client.post("/v1/messages", json={"model": "m", "max_tokens": 8, "messages": [{"role": "user", "content": f"hi {i}"}]})
        client = make_with_history()  # a restart: a new process, the same file
        h = lambda q: client.get(f"/_keepmum/history{q}", headers={"authorization": "Bearer t"})
        first = (await h("?limit=3")).json()["events"]
        older = (await h(f"?limit=3&before={first[-1]['ts']}")).json()["events"]
        masked = (await h("?kind=masked")).json()["events"]
        denied = await client.get("/_keepmum/history")
        return first, older, masked, denied

    first, older, masked, denied = anyio.run(go)
    assert len(first) == 3 and len(older) == 2 and first[0]["ts"] >= first[-1]["ts"] > older[0]["ts"]
    assert len(masked) == 1 and masked[0]["new"] > 0
    assert denied.status_code == 401
    text = (tmp_path / "history.jsonl").read_text()
    assert canary not in text and "KEEPMUM_" not in text  # counts only
    assert (tmp_path / "history.jsonl").stat().st_mode & 0o777 == 0o600



def test_flows_follow_each_client_out_and_back():
    from keepmum.stats import client_of, flows, provider_of

    assert client_of("claude-cli/2.1.310 (external, cli)") == "claude-code"
    assert client_of("codex_cli_rs/0.50.0") == "codex" and client_of("") == "unknown"
    assert provider_of("https://api.anthropic.com") == "anthropic" and provider_of("https://chatgpt.com/backend-api") == "openai"
    canary = selftest.make_canary()

    async def go():
        _, client = make()
        cc = {"user-agent": "claude-cli/2.1.310 (external, cli)"}
        await client.post("/v1/messages", json=body_with(canary), headers=cc)  # masked; the mock echoes a placeholder back
        await client.post("/v1/messages", json={"model": "m", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]},
                          headers={"user-agent": "pi-coding-agent/1.0"})  # clean
        await client.post("/v1/files", content=b"x", headers=cc)  # blocked
        await client.get("/api/hello", headers=cc)  # bodyless: not part of the flow
        r = await client.get("/_keepmum/flows?window=600", headers={"authorization": "Bearer t"})
        return r.json()

    f = anyio.run(go)
    assert {c["id"]: c["sent"] for c in f["clients"]} == {"claude-code": 2, "pi": 1}
    out = {(o["client"], o["provider"], o["kind"]): o["n"] for o in f["out"]}
    assert out == {("claude-code", "up", "masked"): 1, ("pi", "up", "clean"): 1, ("claude-code", None, "blocked"): 1}  # the mock upstream
    back = {(b["client"], b["restored"]): (b["n"], b["values"]) for b in f["back"]}
    assert back[("claude-code", True)][0] == 1 and back[("claude-code", True)][1] >= 1 and back[("pi", False)] == (1, 0)
    assert f["providers"] == [{"id": "up", "name": "up", "replies": 2}]


def _held_upstream(gate, reply):
    """An upstream that answers only once `gate` is set: a request stays out at the provider."""
    async def handler(request):
        await gate.wait()
        return reply(request)
    return httpx.MockTransport(handler)


async def _wait_for_flight(client, auth):
    for _ in range(200):
        live = (await client.get("/_keepmum/live", headers=auth)).json()
        if live["inflight"]:
            return live
        await anyio.sleep(0.005)
    raise AssertionError("the request never showed as out at the provider")


def test_live_feed_shows_a_request_while_the_provider_answers():
    canary = selftest.make_canary()
    auth = {"authorization": "Bearer t"}
    message = {"id": "msg_1", "type": "message", "role": "assistant", "content": [{"type": "text", "text": "ok"}]}

    async def go():
        gate = anyio.Event()
        _, client = make(transport=_held_upstream(gate, lambda r: httpx.Response(200, json=message)))
        async with anyio.create_task_group() as tg:
            tg.start_soon(lambda: client.post("/v1/messages", json=body_with(canary)))
            during = await _wait_for_flight(client, auth)
            gate.set()
        after = (await client.get("/_keepmum/live", headers=auth)).json()
        since = (await client.get("/_keepmum/live", params={"after": after["recent"][-1]["ts"]}, headers=auth)).json()
        refused = await client.get("/_keepmum/live")
        return during, after, since, refused

    during, after, since, refused = anyio.run(go)
    [flight] = during["inflight"]
    assert flight["kind"] == "masked" and flight["client"] and flight["provider"]
    assert canary not in json.dumps(during) and during["recent"] == []
    # The reply is done: out of flight, and its event carries the flight's number.
    assert after["inflight"] == [] and after["recent"][-1]["flight"] == flight["id"]
    assert after["recent"][-1]["outcome"] == "forwarded"
    assert since["recent"] == []  # only events newer than `after`
    assert refused.status_code == 401


def test_a_streamed_reply_stays_in_flight_until_its_last_chunk():
    auth = {"authorization": "Bearer t"}
    sse = 'event: message_stop\ndata: {"type": "message_stop"}\n\n'

    async def go():
        gate = anyio.Event()
        transport = _held_upstream(gate, lambda r: httpx.Response(200, stream=httpx.ByteStream(sse.encode()),
                                                                headers={"content-type": "text/event-stream"}))
        _, client = make(transport=transport)
        async with anyio.create_task_group() as tg:
            tg.start_soon(lambda: client.post("/v1/messages", json={**body_with("nothing secret"), "stream": True}))
            during = await _wait_for_flight(client, auth)
            gate.set()
        after = (await client.get("/_keepmum/live", headers=auth)).json()
        return during, after

    during, after = anyio.run(go)
    [flight] = during["inflight"]
    assert flight["kind"] == "clean"
    assert after["inflight"] == [] and after["recent"][-1]["flight"] == flight["id"]


def test_flight_numbers_stay_out_of_the_history_file(tmp_path):
    from keepmum.stats import History, Stats

    hist = History(tmp_path / "history.jsonl")
    stats = Stats(history=hist)
    n = stats.depart(("claude-code", "anthropic"), "clean")
    assert [f["id"] for f in stats.live()["inflight"]] == [n]
    stats.record("/v1/messages", "forwarded", 200, scan_ms=1.0, origin=("claude-code", "anthropic"), flight=n)
    assert stats.live()["inflight"] == [] and stats.live()["recent"][-1]["flight"] == n
    assert "flight" not in hist.events()[-1]
