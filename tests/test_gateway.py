"""keepmum in front of another gateway: agent -> keepmum -> gateway (Magpie, LiteLLM) -> provider."""

import json
import secrets
import tomllib

import anyio
import httpx
import pytest
from typer.testing import CliRunner

from keepmum import clients, config as config_mod, gateway, launch, protect, selftest
from keepmum.audit import config_changes, config_state, summary
from keepmum.audit import read as audit_read
from keepmum.cli import app
from keepmum.detect.pipeline import Pipeline
from keepmum.detect.rules import RulesDetector
from keepmum.protect import ProtectError
from keepmum.proxy import Proxy
from keepmum.redact import Redactor
from keepmum.vault import Vault

PORT = 7788
MAGPIE = "http://127.0.0.1:3425"
ROUTE = f"http://127.0.0.1:{PORT}/gateway/local-3425"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    for k in launch.BYPASS_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(clients.shutil, "which", lambda name: None)
    for d in ("claude", "codex", "pi", "xdg/opencode"):
        (tmp_path / d).mkdir(parents=True)
    return tmp_path


def adopter(home):
    def write(name, url):
        config_mod.set_value("gateways", name, url)

    return gateway.Adopter(PORT, config_mod.load().gateways, write)


# --- Config --------------------------------------------------------------------------------

def test_config_gateways_are_checked(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[gateways]\nlocal-3425 = "http://127.0.0.1:3425/"\ncorp = "https://llm.corp.example"\n')
    assert config_mod.load(path).gateways == {"local-3425": MAGPIE, "corp": "https://llm.corp.example"}
    for bad, why in [('x = "http://llm.corp.example"', "must use https"), ('x = "http://127.0.0.1:7788"', "keepmum itself"),
                     ('X = "https://a.example"', "lowercase"), ('x = "https://a.example?k=1"', "query"),
                     ('x = "ftp://a"', r"http\(s\)")]:
        path.write_text(f"[gateways]\n{bad}\n")
        with pytest.raises(config_mod.ConfigError, match=why):
            config_mod.load(path)


def test_names_and_routes():
    gws = {}
    assert gateway.name_for("http://127.0.0.1:3425/v1", gws) == "local-3425"
    assert gateway.name_for("https://openrouter.ai/api", gws) == "openrouter"
    assert gateway.name_for("https://api.example.com/v1", {"example": "https://other.example"}) == "example-2"
    gws = {"local-3425": MAGPIE}
    assert gateway.find("http://127.0.0.1:3425/v1", gws) == ("local-3425", "/v1")
    assert gateway.find("http://127.0.0.1:34250", gws) is None  # not a path boundary
    assert gateway.unroute(f"{ROUTE}/v1", PORT, gws) == f"{MAGPIE}/v1"
    assert gateway.unroute(f"{ROUTE}/v1", PORT, {}) is None
    assert gateway.is_ours(ROUTE, PORT) and gateway.is_ours("http://127.0.0.1:7788", PORT)
    assert not gateway.is_ours(MAGPIE, PORT)


# --- The proxy ------------------------------------------------------------------------------

def make_proxy(mock):
    up = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock.app), base_url="http://up")
    proxy = Proxy(Redactor(Pipeline(RulesDetector(), None), Vault(secrets.token_bytes(32))), upstream="https://api.anthropic.com",
                  client=up, stats_token="t")
    proxy.gateways = {"gw": "http://gw"}
    return proxy, httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1:7788")


def test_every_api_is_scanned_on_its_way_to_the_gateway():
    canary = selftest.make_canary()
    mock = selftest.MockUpstream()

    async def go():
        proxy, client = make_proxy(mock)
        msg = await client.post("/gateway/gw/v1/messages", json={
            "model": "deepseek/deepseek-v4-pro", "max_tokens": 9, "messages": [{"role": "user", "content": f"key {canary}"}]},
            headers={"x-api-key": "magpie"})
        resp = await client.post("/gateway/gw/v1/responses", json={
            "model": "moonshot/kimi", "input": [{"role": "user", "content": [{"type": "input_text", "text": f"key {canary}"}]}]})
        gem = await client.post("/gateway/gw/v1beta/models/glm:generateContent",
                                json={"contents": [{"role": "user", "parts": [{"text": f"key {canary}"}]}]})
        chat = await client.post("/gateway/gw/v1/chat/completions", json={"messages": [{"role": "user", "content": canary}]})
        unknown = await client.post("/gateway/nope/v1/messages", json={"messages": []})
        models = await client.get("/gateway/gw/v1/models")
        stats = (await client.get("/_keepmum/stats", headers={"authorization": "Bearer t"})).json()
        return msg, resp, gem, chat, unknown, models, stats

    msg, resp, gem, chat, unknown, models, stats = anyio.run(go)
    for r in (msg, resp, gem):
        assert r.status_code == 200 and canary in r.text  # restored on the way back
    assert chat.status_code == 403 and unknown.status_code == 404  # not scanned / no such gateway: never sent
    assert models.status_code == 200  # no body: passes
    sent = b"".join(mock.received)
    assert canary.encode() not in sent
    paths = [r.split(b"?")[0] for r in mock.received]
    assert paths == [b"/v1/messages", b"/v1/responses", b"/v1beta/models/glm:generateContent", b"/v1/models"]
    assert b"x-api-key: magpie" in mock.received[0]  # the gateway's own key, passed on
    assert {e["provider"] for e in stats["recent"] if e.get("outcome") != "blocked"} == {"gw"}


# --- Putting keepmum in front, and taking it out ------------------------------------------------

def test_claude_code_goes_through_keepmum_to_its_gateway_and_back(home):
    s = home / "claude" / "settings.json"
    s.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": MAGPIE, "ANTHROPIC_AUTH_TOKEN": "magpie"}}))
    with pytest.raises(ProtectError, match="puts keepmum in front"):
        protect.enable(PORT)  # without an adopter: refused, as before
    st = protect.enable(PORT, adopter(home))
    assert st.on and st.via == MAGPIE
    assert json.loads(s.read_text())["env"] == {"ANTHROPIC_BASE_URL": ROUTE, "ANTHROPIC_AUTH_TOKEN": "magpie"}
    assert config_mod.load().gateways == {"local-3425": MAGPIE}
    assert launch.bypass_risks(home, PORT) == []  # the route is keepmum's own
    st = protect.disable(PORT)
    assert not st.on and json.loads(s.read_text())["env"]["ANTHROPIC_BASE_URL"] == MAGPIE
    # `keepmum claude` still refuses to start (the setting would send it around keepmum), and says what to do.
    assert [r for r in launch.bypass_risks(home, PORT) if MAGPIE in r and "`keepmum protect on` puts keepmum in front" in r]


CODEX = f'''model_provider = "magpie"
model = "deepseek/deepseek-v4-pro"

[model_providers.magpie]
name = "magpie"
base_url = "{MAGPIE}/v1"
env_key = "MAGPIE_KEY"
wire_api = "responses"
http_headers = {{ "x-magpie-agent" = "codex" }}
'''


def test_codex_keeps_its_provider_with_keepmum_in_front(home):
    cfg = home / "codex" / "config.toml"
    cfg.write_text(CODEX)
    st = clients.codex_enable(PORT, adopter(home))
    data = tomllib.loads(cfg.read_text())
    prov = data["model_providers"]["keepmum"]
    assert st["on"] and st["via"] == f"{MAGPIE}/v1"
    assert data["model_provider"] == "keepmum" and data["model_providers"]["magpie"]["base_url"] == f"{MAGPIE}/v1"
    assert prov["base_url"] == f"{ROUTE}/v1" and prov["env_key"] == "MAGPIE_KEY" and prov["supports_websockets"] is False
    assert prov["http_headers"] == {"x-magpie-agent": "codex", "x-keepmum-client": "codex"}
    assert "requires_openai_auth" not in prov  # the gateway's key, not an OpenAI login
    clients.codex_enable(PORT, adopter(home))  # again: still in front of magpie, one block
    assert cfg.read_text().count("[model_providers.keepmum]") == 1 and '(was "magpie")' in cfg.read_text()
    assert not clients.codex_disable(PORT)["on"]
    assert cfg.read_text() == CODEX


def test_codex_selection_stays_on_its_own_line(home):
    """keepmum's model_provider line takes the place of the user's, so `off` gives back the same file."""
    cfg = home / "codex" / "config.toml"
    for text in ('model = "gpt-5.5"\n' + CODEX.replace('model = "deepseek/deepseek-v4-pro"\n', ""),
                 '# my settings\nmodel = "gpt-5.5"\napproval_policy = "never"\n'):
        cfg.write_text(text)
        clients.codex_enable(PORT, adopter(home))
        lines = cfg.read_text().splitlines()
        want = 1 if "magpie" in text else 0
        assert lines[want].startswith('model_provider = "keepmum"'), lines
        clients.codex_enable(PORT, adopter(home))
        assert cfg.read_text().splitlines()[want] == lines[want]
        clients.codex_disable(PORT)
        assert cfg.read_text() == text


def test_codex_chat_completions_and_local_models_are_refused(home):
    cfg = home / "codex" / "config.toml"
    cfg.write_text(CODEX.replace('wire_api = "responses"', 'wire_api = "chat"'))
    with pytest.raises(ProtectError, match="Chat Completions"):
        clients.codex_enable(PORT, adopter(home))
    assert config_mod.load().gateways == {}  # nothing added for a provider keepmum cannot serve
    cfg.write_text('model_provider = "ollama"\n')
    with pytest.raises(ProtectError, match="on this Mac"):
        clients.codex_enable(PORT, adopter(home))


def test_one_codex_run_goes_in_front_of_its_provider(home):
    (home / "codex" / "config.toml").write_text(CODEX)
    provider, extra = clients.codex_run_provider(9999, {})
    assert extra == {"local-3425": MAGPIE} and provider["base_url"] == "http://127.0.0.1:9999/gateway/local-3425/v1"
    provider, extra = clients.codex_run_provider(9999, {"magpie": MAGPIE})
    assert extra == {} and provider["base_url"] == "http://127.0.0.1:9999/gateway/magpie/v1"


def test_pi_and_opencode_go_through_keepmum_to_their_gateways(home):
    models = home / "pi" / "models.json"
    models.write_text(json.dumps({"providers": {"anthropic": {"baseUrl": MAGPIE}, "openai-codex": {"baseUrl": "https://x.example"}}}))
    oc = home / "xdg" / "opencode" / "opencode.json"
    oc.write_text(json.dumps({"provider": {"anthropic": {"options": {"baseURL": f"{MAGPIE}/v1", "apiKey": "magpie"}}}}))
    a = adopter(home)
    st = clients.pi_enable(PORT, a)
    pi = json.loads(models.read_text())["providers"]
    assert pi["anthropic"]["baseUrl"] == ROUTE and pi["openai"]["baseUrl"] == f"http://127.0.0.1:{PORT}/openai/v1"
    assert pi["openai-codex"] == {"baseUrl": "https://x.example"}  # a subscription route: left alone
    assert st["via"] == MAGPIE and st["other"] == ["openai-codex"] and st["via_providers"] == ["anthropic"]
    assert clients.via_phrase(st) == f"; anthropic then goes to {MAGPIE}"  # openai goes to OpenAI, not there
    st = clients.opencode_enable(PORT, a)
    opts = json.loads(oc.read_text())["provider"]["anthropic"]["options"]
    assert opts["baseURL"] == f"{ROUTE}/v1" and opts["apiKey"] == "magpie" and st["via"] == f"{MAGPIE}/v1"
    # opencode's OpenAI provider had no gateway: it goes through keepmum to OpenAI, never to Magpie.
    assert json.loads(oc.read_text())["provider"]["openai"]["options"]["baseURL"] == f"http://127.0.0.1:{PORT}/openai/v1"
    assert config_mod.load().gateways == {"local-3425": MAGPIE}  # one gateway, shared by both
    clients.pi_disable(PORT)
    clients.opencode_disable(PORT)
    assert json.loads(models.read_text())["providers"] == {"anthropic": {"baseUrl": MAGPIE}, "openai-codex": {"baseUrl": "https://x.example"}}
    assert json.loads(oc.read_text())["provider"] == {"anthropic": {"options": {"baseURL": f"{MAGPIE}/v1", "apiKey": "magpie"}}}


def test_a_gateway_app_that_writes_itself_back_is_caught(home):
    s = home / "claude" / "settings.json"
    s.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": MAGPIE}}))
    protect.enable(PORT, adopter(home))
    s.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": MAGPIE}}))  # the gateway's app switches a model
    st = clients.protect_clients("status", PORT, ["claude"])["claude"]
    assert not st["on"] and st["around"] == MAGPIE


# --- The command line and the audit log -------------------------------------------------------------

def test_protect_on_from_the_command_line_is_audited(home):
    (home / "claude" / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": MAGPIE}}))
    (home / "codex" / "config.toml").write_text(CODEX)
    r = CliRunner().invoke(app, ["protect", "on", "--client", "claude", "--client", "codex"])
    assert r.exit_code == 0, r.output
    assert f"then {MAGPIE}" in r.output and f"then {MAGPIE}/v1" in r.output
    entries = audit_read()
    config = [e for e in entries if e["event"] == "config"]
    assert config and any(c["setting"] == "gateways" and c["weakens"] for c in config[-1]["changes"])
    assert config[-1]["by"].startswith("keepmum protect on")
    prot = [e for e in entries if e["event"] == "protect"][-1]
    assert prot["via"] == {"Claude Code": MAGPIE, "Codex": f"{MAGPIE}/v1"}
    assert f"Codex through keepmum to {MAGPIE}/v1" in summary(prot)
    status = json.loads(CliRunner().invoke(app, ["status", "--json"]).output)
    assert status["gateways"] == {"local-3425": MAGPIE} and status["clients"]["claude"]["via"] == MAGPIE


def test_gateway_changes_show_in_config_changes():
    a, b = config_mod.Config(), config_mod.Config(gateways={"local-3425": MAGPIE})
    (change,) = config_changes(config_state(a), config_state(b))
    assert change == {"setting": "gateways", "added": [f"local-3425 = {MAGPIE}"], "removed": [], "weakens": True}
