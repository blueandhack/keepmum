"""How fast local turns go: `[guard] local_keep_loaded` (Ollama's keep_alive after each request,
Splash's --idle-release) and `local_answer_thinking` (a local answer without thinking first)."""

import json
import secrets

import anyio
import httpx
import pytest
from typer.testing import CliRunner

from keepmum import cli, guard, splashsvc
from keepmum.config import ConfigError, load, set_value
from keepmum.detect.pipeline import Pipeline
from keepmum.detect.rules import RulesDetector
from keepmum.local import (
    Server,
    keep_alive,
    parse_keep_loaded,
    servers_for,
    set_keep_alive,
    thinking_fields,
    without_thinking,
)
from keepmum.proxy import Proxy
from keepmum.redact import Redactor
from keepmum.vault import Vault

from test_local import ATTACK, Flags, answer, conversation


def run(bodies, speed=("5m", True), ollama=True, refuse_fields=False, path="/v1/messages", servers=None):
    """Flagged requests through a proxy whose local server is mocked: Ollama (it answers /api/version
    and /api/tags) or another server, which may refuse the fields that turn thinking off."""
    seen = {"local": [], "keep": [], "probe": []}
    servers = servers or [Server("http://localhost:11434", "qwen3:0.6b")]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/api/version", "/api/tags"):
            seen["probe"].append(request.url.path)
            if not ollama:
                return httpx.Response(404)
            return httpx.Response(200, json={"version": "0.40.16"} if request.url.path == "/api/version" else {"models": []})
        body = json.loads(request.content)
        if request.url.path == "/api/generate":
            seen["keep"].append(body)
            return httpx.Response(200, json={"done": True, "done_reason": "load"})
        seen["local"].append((request.url.path, body))
        if refuse_fields and any(k in body for k in ("thinking", "reasoning", "reasoning_effort", "chat_template_kwargs")):
            return httpx.Response(400, json={"error": "unknown field"})
        if request.url.path == "/v1/responses":
            return httpx.Response(200, json={"id": "local", "object": "response", "status": "completed", "model": body["model"],
                                             "output": [{"type": "message", "role": "assistant",
                                                         "content": [{"type": "output_text", "text": "kept local"}]}]})
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, json={"id": "c", "object": "chat.completion", "model": body["model"],
                                             "choices": [{"index": 0, "finish_reason": "stop",
                                                          "message": {"role": "assistant", "content": "kept local"}}]})
        return answer(body, body["model"])

    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        proxy = Proxy(Redactor(Pipeline(RulesDetector(), None), Vault(secrets.token_bytes(32))), upstream="http://up",
                      client=client, stats_token="t", guards=[guard.Guard("local", Flags(), name="injection")],
                      local=lambda check: servers_for(servers, check), speed=lambda: speed)
        agent = httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1:7788")
        rs = [await agent.post(path, json=b, headers={"x-api-key": "k", "anthropic-version": "2023-06-01"}) for b in bodies]
        await proxy.settle()
        return rs, seen

    return anyio.run(go)


def responses_body(result: str) -> dict:
    return {"model": "gpt-x", "reasoning": {"effort": "medium", "summary": "auto"},
            "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Read it"}]},
                      {"type": "function_call", "call_id": "c1", "name": "fetch", "arguments": "{}"},
                      {"type": "function_call_output", "call_id": "c1", "output": result}]}


# --- thinking -------------------------------------------------------------------------------------

def test_a_local_answer_thinks_as_the_agent_asked_by_default():
    body = {**conversation(ATTACK), "thinking": {"type": "enabled", "budget_tokens": 2000}}
    rs, seen = run([body])
    assert rs[0].status_code == 200
    assert seen["local"][0][1]["thinking"] == {"type": "enabled", "budget_tokens": 2000}


def test_thinking_off_disables_it_in_the_messages_api():
    body = {**conversation(ATTACK), "thinking": {"type": "enabled", "budget_tokens": 2000}}
    rs, seen = run([body], speed=("5m", False))
    assert rs[0].status_code == 200
    assert seen["local"][0][1]["thinking"] == {"type": "disabled"}


def test_thinking_off_sets_no_effort_in_the_responses_api():
    rs, seen = run([responses_body(ATTACK)], speed=("5m", False), path="/openai/v1/responses")
    assert rs[0].status_code == 200
    assert seen["local"][0][0] == "/v1/responses" and seen["local"][0][1]["reasoning"] == {"effort": "none"}


def test_thinking_off_in_chat_completions_uses_the_same_fields_as_a_handback():
    servers = [Server("http://localhost:11434", "qwen3:0.6b", api="chat")]
    rs, seen = run([conversation(ATTACK)], speed=("5m", False), servers=servers)
    assert rs[0].status_code == 200
    sent = seen["local"][0][1]
    assert seen["local"][0][0] == "/v1/chat/completions"
    assert sent["reasoning_effort"] == "none" and sent["chat_template_kwargs"] == {"enable_thinking": False}


def test_a_server_that_refuses_the_fields_is_asked_again_without_them():
    rs, seen = run([conversation(ATTACK)], speed=("5m", False), refuse_fields=True)
    assert rs[0].status_code == 200
    assert [p for p, _ in seen["local"]] == ["/v1/messages", "/v1/messages"]
    assert "thinking" in seen["local"][0][1] and "thinking" not in seen["local"][1][1]


def test_without_thinking_and_its_fields():
    assert without_thinking({"a": 1}, "messages") == {"a": 1, "thinking": {"type": "disabled"}}
    assert without_thinking({"a": 1}, "gemini") == {"a": 1}
    assert thinking_fields("chat") == ("reasoning_effort", "chat_template_kwargs")
    assert thinking_fields("responses") == ("reasoning",)
    d = without_thinking({}, "chat")
    d["chat_template_kwargs"]["enable_thinking"] = True  # a copy: the constant stays as it was
    assert without_thinking({}, "chat")["chat_template_kwargs"] == {"enable_thinking": False}


# --- keep loaded ----------------------------------------------------------------------------------

def test_ollama_is_told_how_long_to_keep_the_model_after_a_local_answer():
    rs, seen = run([conversation(ATTACK), conversation(ATTACK)], speed=("30m", True))
    assert rs[0].status_code == 200
    assert seen["keep"] == [{"model": "qwen3:0.6b", "keep_alive": "30m"}] * 2
    assert seen["probe"] == ["/api/version", "/api/tags"]  # asked once per server


def test_always_is_minus_one():
    _, seen = run([conversation(ATTACK)], speed=("always", True))
    assert seen["keep"] == [{"model": "qwen3:0.6b", "keep_alive": -1}]
    assert keep_alive("always") == -1 and keep_alive("15m") == "15m"


def test_another_server_gets_no_keep_alive():
    _, seen = run([conversation(ATTACK)], speed=("30m", True), ollama=False)
    assert seen["keep"] == []


def test_parse_keep_loaded():
    for ok in ("5m", "15m", "30m", "1h", "always", "90s", "2h"):
        assert parse_keep_loaded(ok) == ok
    for bad in ("", "0m", "forever", 5, "5", "-1", None):
        with pytest.raises(ValueError):
            parse_keep_loaded(bad)


def test_config_reads_both(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[guard]\nlocal_keep_loaded = "1h"\nlocal_answer_thinking = false\n')
    cfg = load(p)
    assert cfg.guard_local_keep_loaded == "1h" and cfg.guard_local_answer_thinking is False
    assert load(tmp_path / "none.toml").guard_local_keep_loaded == "5m"
    p.write_text('[guard]\nlocal_keep_loaded = "soon"\n')
    with pytest.raises(ConfigError, match="local_keep_loaded"):
        load(p)
    p.write_text('[guard]\nlocal_answer_thinking = "no"\n')
    with pytest.raises(ConfigError):
        load(p)


def test_set_keep_alive_only_for_a_loaded_model(monkeypatch):
    calls = []

    def get(url, timeout):
        calls.append(("get", url))
        return httpx.Response(200, json={"models": [{"name": "qwen3:0.6b"}]})

    def post(url, json, timeout):
        calls.append(("post", url, json))
        return httpx.Response(200)

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(httpx, "post", post)
    assert set_keep_alive("http://localhost:11434/", "qwen3:0.6b", "always") is True
    assert calls[-1] == ("post", "http://localhost:11434/api/generate", {"model": "qwen3:0.6b", "keep_alive": -1})
    calls.clear()
    assert set_keep_alive("http://localhost:11434", "other:1b", "30m") is False  # not loaded: not loaded by this either
    assert [c[0] for c in calls] == ["get"]

    def down(url, timeout):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", down)
    assert set_keep_alive("http://localhost:11434", "qwen3:0.6b", "30m") is False


def test_splash_idle_release():
    assert splashsvc.idle_release("always") == "off" and splashsvc.idle_release("30m") == "30m"
    assert splashsvc.arguments("splash", "a/b:Q4", 8000, "off")[-2:] == ["--idle-release", "off"]


# --- the command ----------------------------------------------------------------------------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[guard]\nlocal_servers = [{ url = "http://localhost:11434", model = "qwen3:0.6b" }]\n')
    monkeypatch.setenv("KEEPMUM_CONFIG", str(cfg))
    monkeypatch.setattr(cli, "_config", lambda: load(cfg))
    monkeypatch.setattr(cli, "_update_config", lambda path, section, key, value, command: set_value(section, key, value, cfg))
    told, started = [], []
    monkeypatch.setattr("keepmum.local.set_keep_alive", lambda url, model, value: told.append((url, model, value)) or True)
    monkeypatch.setattr(splashsvc, "managed", lambda: None)
    monkeypatch.setattr(splashsvc, "start", lambda spec, port, idle: started.append((spec, port, idle)))
    return cfg, told, started


def test_speed_shows_the_defaults(home):
    r = CliRunner().invoke(cli.app, ["guard", "local", "speed"])
    assert r.exit_code == 0, r.output
    assert "keep loaded: 5m after the last request (default)" in r.output and "think first (default)" in r.output
    r = CliRunner().invoke(cli.app, ["guard", "local", "speed", "--json"])
    assert json.loads(r.stdout) == {"keep_loaded": "5m", "answer_thinking": True}


def test_speed_sets_both_and_tells_loaded_ollama_models(home):
    cfg, told, started = home
    r = CliRunner().invoke(cli.app, ["guard", "local", "speed", "--keep-loaded", "always", "--thinking", "off", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout) == {"keep_loaded": "always", "answer_thinking": False}
    assert told == [("http://localhost:11434", "qwen3:0.6b", "always")] and started == []
    text = cfg.read_text()
    assert 'local_keep_loaded = "always"' in text and "local_answer_thinking = false" in text
    r = CliRunner().invoke(cli.app, ["guard", "local", "speed", "--keep-loaded", "5m", "--thinking", "on"])
    assert r.exit_code == 0 and "local_keep_loaded" not in cfg.read_text() and "local_answer_thinking" not in cfg.read_text()


def test_speed_starts_the_splash_keepmum_runs_again(home, monkeypatch):
    _, _, started = home
    monkeypatch.setattr(splashsvc, "managed", lambda: {"model": "o/r:IQ3_M", "port": 8000, "idle": "5m"})
    r = CliRunner().invoke(cli.app, ["guard", "local", "speed", "--keep-loaded", "always"])
    assert r.exit_code == 0 and started == [("o/r:IQ3_M", 8000, "off")]
    assert "Splash starts again" in r.output


def test_speed_refuses_bad_values(home):
    assert CliRunner().invoke(cli.app, ["guard", "local", "speed", "--thinking", "maybe"]).exit_code == 2
    assert CliRunner().invoke(cli.app, ["guard", "local", "speed", "--keep-loaded", "forever"]).exit_code == 2


def test_splash_start_uses_the_setting(home, monkeypatch):
    cfg, _, started = home
    cfg.write_text(cfg.read_text() + 'local_keep_loaded = "1h"\n')
    monkeypatch.setattr(cli, "_save_servers", lambda *a, **k: None)
    r = CliRunner().invoke(cli.app, ["guard", "local", "splash", "--start"])
    assert r.exit_code == 0, r.output
    assert started and started[0][2] == "1h"
