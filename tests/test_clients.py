import json
import os
import sys
import tomllib

import pytest

from keepmum import clients
from keepmum.protect import ProtectError

PORT = 7788


@pytest.fixture
def homes(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    (tmp_path / "codex").mkdir()
    (tmp_path / "pi").mkdir()
    # Whether codex, pi or opencode is on this machine's PATH must not matter: a tool counts as installed
    # here only through the config files each test writes.
    monkeypatch.setattr(clients.shutil, "which", lambda name: None)
    return tmp_path


def test_codex_on_off_keeps_the_rest_of_the_config(homes):
    cfg = homes / "codex" / "config.toml"
    original = 'model = "gpt-5"\napproval_policy = "never"\n\n[mcp_servers.docs]\ncommand = "docs-mcp"\n'
    cfg.write_text(original)
    st = clients.codex_enable(PORT)
    data = tomllib.loads(cfg.read_text())
    assert st["on"] and data["model_provider"] == "keepmum" and data["model"] == "gpt-5"
    assert data["mcp_servers"]["docs"]["command"] == "docs-mcp"
    prov = data["model_providers"]["keepmum"]
    assert prov["base_url"] == "http://127.0.0.1:7788/chatgpt/backend-api/codex"  # no auth.json: ChatGPT login
    assert prov["supports_websockets"] is False and prov["requires_openai_auth"] is True
    assert prov["http_headers"] == {"x-keepmum-client": "codex"}
    clients.codex_enable(PORT)  # twice: still one block
    assert cfg.read_text().count("[model_providers.keepmum]") == 1
    assert not clients.codex_disable(PORT)["on"]
    assert cfg.read_text() == original


def test_codex_api_key_login_goes_to_the_openai_route(homes):
    (homes / "codex" / "auth.json").write_text(json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "x"}))
    clients.codex_enable(PORT)
    prov = tomllib.loads((homes / "codex" / "config.toml").read_text())["model_providers"]["keepmum"]
    assert prov["base_url"] == "http://127.0.0.1:7788/openai/v1"


def test_codex_leaves_another_provider_alone(homes):
    cfg = homes / "codex" / "config.toml"
    cfg.write_text('model_provider = "azure"\n')
    with pytest.raises(ProtectError, match="model_provider 'azure'"):
        clients.codex_enable(PORT)
    assert cfg.read_text() == 'model_provider = "azure"\n'
    assert clients.codex_status(PORT)["other"] == "azure"


def test_pi_on_off_and_the_users_own_proxy(homes):
    models = homes / "pi" / "models.json"
    original = {"providers": {"openai": {"baseUrl": "https://my-gateway.corp.internal/v1"},
                              "ollama": {"baseUrl": "http://localhost:11434/v1", "api": "openai-completions"}}}
    models.write_text(json.dumps(original))
    st = clients.pi_enable(PORT)
    data = json.loads(models.read_text())["providers"]
    assert st["providers"] == ["anthropic", "openai-codex"] and st["other"] == ["openai"]
    assert data["anthropic"] == {"baseUrl": "http://127.0.0.1:7788", "headers": {"x-keepmum-client": "pi"}}
    assert data["openai"] == original["providers"]["openai"]  # someone else's proxy: untouched
    clients.pi_disable(PORT)
    assert json.loads(models.read_text()) == original


def test_protect_reports_each_client(homes, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    (homes / "codex" / "config.toml").write_text("not = [valid")
    (homes / "pi" / "models.json").write_text("{}")
    out = clients.protect_clients("on", PORT)
    assert out["claude"]["on"] and out["pi"]["on"]
    assert "not valid TOML" in out["codex"]["error"]  # one broken config never stops the others


def test_keepmum_codex_runs_codex_through_the_proxy(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from keepmum import launch
    from keepmum.cli import app

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(launch, "shared_serve_port", lambda cfg, need="messages": 7788)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "codex"
    fake.write_text(f"#!{sys.executable}\nimport json, os, sys\nopen(os.environ['OUT'], 'w').write(json.dumps(sys.argv[1:]))\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("OUT", str(tmp_path / "out"))
    r = CliRunner().invoke(app, ["codex", "exec", "hi"])
    args = json.loads((tmp_path / "out").read_text())
    assert r.exit_code == 0 and args[-2:] == ["exec", "hi"]
    assert 'model_provider="keepmum"' in args and 'model_providers.keepmum.supports_websockets=false' in args
    assert 'model_providers.keepmum.base_url="http://127.0.0.1:7788/chatgpt/backend-api/codex"' in args


def test_a_tool_that_is_not_installed_is_skipped(homes):
    out = clients.protect_clients("on", PORT, ["codex", "pi", "opencode"])
    off = {"installed": False, "on": False}
    assert out == {"codex": off, "pi": off, "opencode": off}
    assert not (homes / "codex" / "config.toml").exists() and not (homes / "pi" / "models.json").exists()
    assert not (homes / "xdg-config" / "opencode").exists()


def opencode_dir(homes):
    d = homes / "xdg-config" / "opencode"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_opencode_on_off_keeps_the_rest_of_the_config(homes):
    cfg = opencode_dir(homes) / "opencode.json"
    original = {"$schema": "https://opencode.ai/config.json", "model": "anthropic/claude-sonnet-5-5",
                "provider": {"anthropic": {"options": {"timeout": 600000}}}}
    cfg.write_text(json.dumps(original))
    st = clients.opencode_enable(PORT)
    data = json.loads(cfg.read_text())
    assert st["on"] and st["providers"] == ["anthropic", "openai"] and data["model"] == original["model"]
    assert data["provider"]["anthropic"]["options"] == {
        "timeout": 600000, "baseURL": "http://127.0.0.1:7788/v1", "headers": {"x-keepmum-client": "opencode"}}
    assert data["provider"]["openai"]["options"]["baseURL"] == "http://127.0.0.1:7788/openai/v1"
    clients.opencode_enable(PORT)  # twice: the same file
    assert json.loads(cfg.read_text()) == data
    assert not clients.opencode_disable(PORT)["on"]
    assert json.loads(cfg.read_text()) == original


def test_opencode_new_config_and_a_gateway_set_in_jsonc(homes):
    d = opencode_dir(homes)
    # opencode.jsonc is read after opencode.json, so its base URL wins: keepmum reports it, never edits it.
    jsonc = '{\n  // the team gateway\n  "provider": {"openai": {"options": {"baseURL": "https://gw.corp.internal/v1",},},},\n}\n'
    (d / "opencode.jsonc").write_text(jsonc)
    st = clients.opencode_enable(PORT)
    data = json.loads((d / "opencode.json").read_text())
    assert st["providers"] == ["anthropic"] and st["other"] == ["openai"]
    assert data["$schema"] == "https://opencode.ai/config.json" and "openai" not in data["provider"]
    assert (d / "opencode.jsonc").read_text() == jsonc


def test_opencode_v2_providers_entry_counts_as_the_users_own(homes):
    cfg = opencode_dir(homes) / "opencode.json"
    cfg.write_text(json.dumps({"providers": {"anthropic": {"settings": {"baseURL": "https://proxy.example/v1"}}}}))
    st = clients.opencode_enable(PORT)
    assert st["providers"] == ["openai"] and st["other"] == ["anthropic"]


def test_opencode_refuses_to_rewrite_a_json_file_with_comments(homes):
    cfg = opencode_dir(homes) / "opencode.json"
    text = '{\n  // mine\n  "model": "openai/gpt-5.5"\n}\n'
    cfg.write_text(text)
    with pytest.raises(ProtectError, match="not plain JSON"):
        clients.opencode_enable(PORT)
    assert cfg.read_text() == text
    assert clients.opencode_status(PORT) == {"installed": True, "on": False, "providers": [], "config": str(cfg)}


def test_opencode_1x_chatgpt_login_is_reported(homes, monkeypatch):
    opencode_dir(homes)
    auth = homes / "xdg-data" / "opencode" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps({"openai": {"type": "oauth", "refresh": "r", "access": "a", "expires": 0}}))
    clients.opencode_enable(PORT)
    monkeypatch.setattr(clients, "opencode_major", lambda: 1)
    assert "chatgpt.com" in clients.opencode_status(PORT)["warning"]
    monkeypatch.setattr(clients, "opencode_major", lambda: 2)  # 2.x keeps the base URL
    assert "warning" not in clients.opencode_status(PORT)


def test_jsonc_reader():
    text = '{"a": "http://x//y", /* note */ "b": [1, 2,], // end\n "c": "say \\"hi\\" // not a comment",}'
    assert clients._jsonc(text) == {"a": "http://x//y", "b": [1, 2], "c": 'say "hi" // not a comment'}


def test_codex_warning_on_port_or_login_change_and_claude_other_gateway(homes, monkeypatch, tmp_path):
    cfg = homes / "codex" / "config.toml"
    cfg.write_text('[model_providers.keepmum]\nname = "old"\nbase_url = "http://127.0.0.1:9999/openai/v1"\n')
    clients.codex_enable(PORT)
    assert cfg.read_text().count("[model_providers.keepmum]") == 1
    assert "warning" not in clients.codex_status(PORT)
    # Switching login mode or port surfaces a warning so the user knows to re-run protect on.
    (homes / "codex" / "auth.json").write_text(json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "x"}))
    assert "keepmum protect on" in clients.codex_status(PORT)["warning"]
    assert "keepmum protect on" in clients.codex_status(8899)["warning"]

    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://gateway.corp.internal"}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    st = clients.protect_clients("status", PORT, ["claude"])["claude"]
    assert not st["on"] and st["other"] == "https://gateway.corp.internal"
