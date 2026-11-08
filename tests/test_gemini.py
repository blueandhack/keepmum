"""Gemini CLI: the Gemini API and Code Assist, scanned and restored like the other APIs."""

import json
import os
import secrets
import stat
import sys
import textwrap

import anyio
import httpx
import pytest
from typer.testing import CliRunner

from keepmum import launch, selftest
from keepmum.detect.pipeline import Pipeline
from keepmum.detect.rules import RulesDetector
from keepmum.gemini import GeminiStreamRestorer, restore_gemini
from keepmum.proxy import Proxy
from keepmum.redact import PLACEHOLDER_HINT, Redactor
from keepmum.stats import Stats
from keepmum.vault import PLACEHOLDER, Vault


def redactor():
    return Redactor(Pipeline(RulesDetector(), None), Vault(secrets.token_bytes(32)))


def test_every_content_is_masked_and_protocol_fields_are_kept():
    canary = selftest.make_canary()
    r = redactor()
    body = {"contents": [
        {"role": "user", "parts": [{"text": f"token {canary}"}, {"inlineData": {"mimeType": "image/png", "data": "iVBOR"}}]},
        {"role": "model", "parts": [{"text": f"thinking about {canary}", "thought": True, "thoughtSignature": "sig"},
                                    {"functionCall": {"name": "run_shell_command", "args": {"command": f"echo {canary}"}},
                                     "thoughtSignature": "sig2"}]},
        {"role": "user", "parts": [{"functionResponse": {"id": "c1", "name": "run_shell_command", "response": {"output": canary}}}]}],
        "system_instruction": {"parts": [{"text": f"GITHUB_TOKEN={canary}"}]}, "tools": [{"functionDeclarations": []}]}
    out = r.redact_gemini_request(body, hint=True)
    sent = json.dumps(out)
    thought = out["contents"][1]["parts"][0]
    # Only the thought, which goes back unchanged as the model wrote it, may still hold the value.
    assert sent.count(canary) == 1 and thought == body["contents"][1]["parts"][0]
    call = out["contents"][1]["parts"][1]
    assert list(call["functionCall"]["args"]) == ["command"] and call["thoughtSignature"] == "sig2"
    assert out["contents"][0]["parts"][1] == body["contents"][0]["parts"][1]  # an image passes through
    assert out["system_instruction"]["parts"][-1] == {"text": PLACEHOLDER_HINT} and "systemInstruction" not in out
    assert out["tools"] == body["tools"]


def test_the_hint_goes_only_where_a_system_instruction_is_accepted():
    r = redactor()
    contents = [{"role": "user", "parts": [{"text": "hi"}]}]
    assert "systemInstruction" not in r.redact_gemini_request({"contents": contents})  # countTokens
    wrapped = r.redact_gemini_request({"model": "m", "project": "p", "request": {"contents": contents}}, hint=True)
    assert wrapped["request"]["systemInstruction"]["parts"] == [{"text": PLACEHOLDER_HINT}] and "systemInstruction" not in wrapped
    embed = r.redact_gemini_request({"requests": [{"model": "models/e", "content": {"parts": [{"text": "x"}]}}]})
    assert "systemInstruction" not in json.dumps(embed)


def placeholder_for(r, value="ghp_" + "a1B2c3D4" * 4 + "Zz9"):
    return r.vault.placeholder("SECRET", value), value


@pytest.mark.parametrize("wrapped", [False, True])
def test_a_placeholder_split_at_any_point_across_stream_chunks_is_restored(wrapped):
    r = redactor()
    ph, value = placeholder_for(r)
    text = f"use {ph} now"
    for cut in range(len(text) + 1):
        def event(t, **extra):
            chunk = {"candidates": [{"index": 0, "content": {"role": "model", "parts": [{"text": t}]}, **extra}]}
            return f"data: {json.dumps({'response': chunk} if wrapped else chunk)}\r\n\r\n".encode()

        s = GeminiStreamRestorer(r.vault)
        out = s.feed(event(text[:cut])) + s.feed(event(text[cut:], finishReason="STOP")) + s.close()
        chunks = selftest._responses_events(out.decode())
        got = "".join(p.get("text", "") for c in chunks for p in (c.get("response", c))["candidates"][0]["content"]["parts"])
        assert got == f"use {value} now", cut
        assert all(("response" in c) == wrapped for c in chunks)


def test_text_held_when_the_stream_ends_is_sent_and_signatures_are_kept():
    r = redactor()
    ph, value = placeholder_for(r)
    s = GeminiStreamRestorer(r.vault)
    signed = {"candidates": [{"index": 0, "content": {"parts": [{"text": ph[:5], "thoughtSignature": "sig"}]}}]}
    first = s.feed(f"data: {json.dumps(signed)}\n\n".encode())
    assert b'"thoughtSignature": "sig"' in first  # the text is held back, the signature is not
    rest = s.feed(f"data: {json.dumps({'candidates': [{'index': 0, 'content': {'parts': [{'text': ph[5:]}]}}]})}\n\n".encode())
    tail = s.close()
    assert value in (first + rest + tail).decode() and ph not in (first + rest + tail).decode()


def test_a_stream_without_sse_is_one_json_array_restored_whole():
    r = redactor()
    ph, value = placeholder_for(r)
    chunks = [{"candidates": [{"content": {"parts": [{"text": f"a {ph}"}]}}]}]
    assert value in json.dumps(restore_gemini(chunks, r.vault))


def make(tmp_path, observe=False):
    mock = selftest.MockUpstream()
    up = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock.app), base_url="http://up")
    stats = Stats()
    proxy = Proxy(redactor(), upstream="http://up", client=up, stats=stats, stats_token="t", observe=observe,
                  gemini_upstream="https://generativelanguage.googleapis.com", code_assist_upstream="https://cloudcode-pa.googleapis.com")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1:7788")
    return mock, client, stats


def gbody(canary):
    return {"contents": [{"role": "user", "parts": [{"text": f"token {canary}"}]}]}


def test_stats_name_gemini_cli_and_google():
    canary = selftest.make_canary()

    async def go():
        mock, client, stats = make(None)
        r = await client.post("/gemini/v1beta/models/m:streamGenerateContent", json=gbody(canary),
                              headers={"user-agent": "GeminiCLI/0.66.0/gemini-2.5-pro (darwin; arm64)"})
        return r, mock, stats

    r, mock, stats = anyio.run(go)
    assert r.status_code == 200 and isinstance(r.json(), list)  # no alt=sse: a JSON array, restored
    assert canary not in b"".join(mock.received).decode() and canary in r.text
    event = stats.events[-1]
    assert (event["client"], event["provider"]) == ("gemini", "google")


def test_observe_mode_sends_gemini_requests_as_they_came(tmp_path):
    canary = selftest.make_canary()

    async def go():
        mock, client, _ = make(tmp_path, observe=True)
        await client.post("/codeassist/v1internal:streamGenerateContent?alt=sse",
                          json={"model": "m", "request": gbody(canary)})
        return mock

    assert canary in b"".join(anyio.run(go).received).decode()


def test_antigravity_is_scanned_and_only_its_known_setup_calls_pass():
    canary = selftest.make_canary()

    async def go():
        mock, client, _ = make(None)
        chat = await client.post("/antigravity/v1internal:streamGenerateContent?alt=sse",
                                 json={"model": "m", "project": "p", "request": gbody(canary)})
        setup = await client.post("/antigravity/v1internal:fetchUserInfo", json={"project": "p"})
        unknown = await client.post("/antigravity/v1internal:recordTrajectoryAnalytics", json={"text": canary})
        # Antigravity's setup calls are not Gemini CLI's: /codeassist still refuses them.
        gemini_cli = await client.post("/codeassist/v1internal:fetchUserInfo", json={"project": "p"})
        return mock, chat, setup, unknown, gemini_cli

    mock, chat, setup, unknown, gemini_cli = anyio.run(go)
    assert chat.status_code == 200 and canary in chat.text
    assert setup.status_code == 200
    assert unknown.status_code == 403 and gemini_cli.status_code == 403
    assert canary not in b"".join(mock.received).decode()


def test_gemini_env_points_every_sign_in_at_keepmum_and_keeps_a_gateway():
    env = launch.gemini_env(7788, {"PATH": "/bin", "GOOGLE_GEMINI_BASE_URL": "https://gw.example.com"})
    assert env["GOOGLE_GEMINI_BASE_URL"] == "http://127.0.0.1:7788/gemini"
    assert env["CODE_ASSIST_ENDPOINT"] == "http://127.0.0.1:7788/codeassist"
    assert env["GOOGLE_VERTEX_BASE_URL"] == "http://127.0.0.1:7788/vertex" and env["PATH"] == "/bin"
    assert launch.gemini_upstreams({"GOOGLE_GEMINI_BASE_URL": "https://gw.example.com/"}, 7788) == {"/gemini": "https://gw.example.com"}
    assert launch.gemini_upstreams({"CODE_ASSIST_ENDPOINT": "http://127.0.0.1:7788/codeassist"}, 7788) == {}


class _App:
    def __init__(self, app):
        self.app = app


def test_keepmum_gemini_end_to_end(tmp_path, monkeypatch):
    """`keepmum gemini` runs a stand-in `gemini` that posts as Gemini CLI does, to the address
    keepmum gave it. (That this address wins over Gemini CLI's .env files is Gemini CLI's own
    rule, read in its code: a .env value applies only to a variable not already set.)"""
    from keepmum.cli import app

    monkeypatch.setenv("HOME", str(tmp_path))
    for k in launch.BYPASS_KEYS:
        monkeypatch.delenv(k, raising=False)
    cfg = tmp_path / ".config" / "keepmum"
    cfg.mkdir(parents=True)
    (cfg / "config.toml").write_text("[detect]\nmodel = false\n")
    mock = selftest.MockUpstream()
    upstream = launch.BackgroundServer(_App(mock.app))
    upstream.start()
    canary = selftest.make_canary()
    out = tmp_path / "reply.txt"
    fake = tmp_path / "bin" / "gemini"
    fake.parent.mkdir()
    fake.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, os, urllib.request
        assert os.environ["CODE_ASSIST_ENDPOINT"].endswith("/codeassist")
        base = os.environ["GOOGLE_GEMINI_BASE_URL"]
        body = json.dumps({{"contents": [{{"role": "user", "parts": [{{"text": "token {canary}"}}]}}]}}).encode()
        req = urllib.request.Request(base + "/v1beta/models/m:generateContent", data=body,
                                     headers={{"content-type": "application/json", "user-agent": "GeminiCLI/0.66.0"}})
        open({str(out)!r}, "w").write(urllib.request.urlopen(req).read().decode())
        """))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
    # A company gateway in the environment: keepmum scans, then forwards there.
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", f"http://127.0.0.1:{upstream.port}")
    try:
        result = CliRunner().invoke(app, ["gemini"])
    finally:
        upstream.stop()
    assert result.exit_code == 0, result.output
    sent = b"".join(mock.received).decode()
    assert "generateContent" in sent and canary not in sent and PLACEHOLDER.search(sent)
    assert canary in out.read_text()  # the reply came back restored
