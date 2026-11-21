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
    # Codex's own provider, pointed at keepmum: every conversation Codex starts or reopens goes there,
    # whichever provider it started with, and no provider of keepmum's own is added.
    assert st["on"] and data["openai_base_url"] == "http://127.0.0.1:7788/openai/v1" and data["model"] == "gpt-5"
    assert "model_provider" not in data and "model_providers" not in data
    assert data["mcp_servers"]["docs"]["command"] == "docs-mcp"
    clients.codex_enable(PORT)  # twice: still one line
    assert cfg.read_text().count("openai_base_url") == 1
    assert not clients.codex_disable(PORT)["on"]
    assert cfg.read_text() == original


def test_codex_takes_one_url_for_either_login(homes):
    """A ChatGPT login and an API key go to the same URL: the proxy tells them apart by the request,
    so a login that changes needs nothing done."""
    cfg = homes / "codex" / "config.toml"
    clients.codex_enable(PORT)
    chatgpt = cfg.read_text()
    (homes / "codex" / "auth.json").write_text(json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "x"}))
    st = clients.codex_status(PORT)
    assert st["on"] and "warning" not in st
    clients.codex_enable(PORT)
    assert cfg.read_text() == chatgpt


OLD = """model_provider = "keepmum"  # keepmum protect
model = "gpt-5"

# >>> keepmum protect: routes Codex through keepmum; `keepmum protect off` removes this block
# keepmum protect: on since 2026-11-15T10:00:00-08:00
[model_providers.keepmum]
name = "keepmum"
base_url = "http://127.0.0.1:7788/chatgpt/backend-api/codex"
wire_api = "responses"
requires_openai_auth = true
supports_websockets = false
http_headers = {"x-keepmum-client" = "codex"}
# <<< keepmum protect
"""


def test_codex_from_an_earlier_version_moves_over_and_keeps_its_conversations(homes):
    """Up to 0.1.0a12 keepmum selected a provider of its own. Codex keeps the provider a conversation
    started with, so that provider stays defined, going wherever Codex's own goes, on or off."""
    cfg = homes / "codex" / "config.toml"
    cfg.write_text(OLD)
    assert clients.codex_status(PORT)["on"]  # the earlier way goes through keepmum too
    st = clients.codex_upgrade(PORT)
    data = tomllib.loads(cfg.read_text())
    assert st["on"] and "model_provider" not in data and data["openai_base_url"] == "http://127.0.0.1:7788/openai/v1"
    legacy = data["model_providers"]["keepmum"]
    assert legacy["base_url"] == "http://127.0.0.1:7788/openai/v1" and legacy["supports_websockets"] is False
    assert legacy["requires_openai_auth"] is True and "on since" not in cfg.read_text()
    assert clients.codex_upgrade(PORT) is None  # nothing more to move
    # Off: they resume straight to OpenAI like every other conversation (no base_url: the login's own).
    clients.codex_disable(PORT)
    data = tomllib.loads(cfg.read_text())
    assert "openai_base_url" not in data and "base_url" not in data["model_providers"]["keepmum"]
    assert data["model"] == "gpt-5" and "model_provider" not in data
    clients.codex_disable(PORT)  # again: the same file
    assert tomllib.loads(cfg.read_text()) == data
    # On again: through keepmum; one block either way.
    clients.codex_enable(PORT)
    assert cfg.read_text().count(clients.CODEX_BEGIN) == 1
    assert tomllib.loads(cfg.read_text())["model_providers"]["keepmum"]["base_url"] == "http://127.0.0.1:7788/openai/v1"
    (homes / "codex" / "none").mkdir()
    assert clients.codex_upgrade(PORT) is None


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
    assert args[:2] == ["-c", 'openai_base_url="http://127.0.0.1:7788/openai/v1"']
    assert not any("model_provider" in a for a in args)


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


def test_codex_warning_on_port_change_and_claude_other_gateway(homes, monkeypatch, tmp_path):
    cfg = homes / "codex" / "config.toml"
    clients.codex_enable(PORT)
    assert "warning" not in clients.codex_status(PORT)
    st = clients.codex_status(8899)  # keepmum moved to another port: said, with what to do
    assert not st["on"] and "keepmum protect on" in st["warning"]
    clients.codex_enable(8899)  # on again: the new port, still one line
    assert tomllib.loads(cfg.read_text())["openai_base_url"] == "http://127.0.0.1:8899/openai/v1"
    assert cfg.read_text().count("openai_base_url") == 1

    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://gateway.corp.internal"}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    st = clients.protect_clients("status", PORT, ["claude"])["claude"]
    assert not st["on"] and st["other"] == "https://gateway.corp.internal"


def test_codex_notes_conversations_open_when_switched_on_until_they_close(homes, monkeypatch, tmp_path):
    """A conversation open as Codex is switched on keeps the route it started with: status says so
    while it stays open, and nothing else; one started or reopened since goes through keepmum."""
    rollout = str(homes / "codex" / "sessions" / "2026" / "11" / "21" / "rollout-2026-11-21T09-00-00-abc.jsonl")
    open_now = {(4242, 31, rollout)}
    monkeypatch.setattr(clients, "_codex_open", lambda pids=None: {o for o in open_now if pids is None or o[0] in pids})
    monkeypatch.setattr(clients, "_codex_open_file", lambda: tmp_path / "codex-open.json")
    st = clients.codex_enable(PORT)
    assert st["on"] and st["warning"].startswith(
        "1 Codex conversation open since before protection went on still goes straight to OpenAI until closed")
    clients.codex_enable(PORT)  # on again: already on, nothing new noted
    open_now.add((4242, 40, rollout.replace("abc", "def")))  # opened since: through keepmum, not flagged
    assert clients.codex_status(PORT)["warning"].startswith("1 Codex conversation")
    open_now.add((5151, 12, rollout.replace("abc", "ghi")))
    assert clients.codex_status(PORT)["warning"].startswith("1 Codex conversation")
    open_now.discard((4242, 31, rollout))  # closed: the note goes, for good
    assert "warning" not in clients.codex_status(PORT) and not (tmp_path / "codex-open.json").exists()
    clients.codex_disable(PORT)
    open_now.clear()
    open_now.update({(1, 3, rollout), (2, 4, rollout)})
    clients.codex_enable(PORT)  # two open as it goes on again
    assert clients.codex_status(PORT)["warning"].startswith("2 Codex conversations open since before protection went "
                                                            "on still go straight")
    clients.codex_disable(PORT)  # off forgets them
    assert not (tmp_path / "codex-open.json").exists()
    open_now.clear()
    clients.codex_enable(PORT)  # nothing open: nothing noted
    assert "warning" not in clients.codex_status(PORT) and not (tmp_path / "codex-open.json").exists()
    (tmp_path / "codex-open.json").write_text("not json")
    assert clients._codex_still_open() == []


def test_codex_conversation_reader_reads_ps_and_lsof(monkeypatch, tmp_path):
    import subprocess

    monkeypatch.undo()  # conftest stubs the reader for every test; this one checks the real one
    ps = ("  101 /opt/homebrew/bin/codex resume\n  202 /Users/me/.vscode/extensions/openai.chatgpt-26/bin/codex app-server\n"
          "  303 /usr/bin/python codex.py\n  404 /bin/zsh -c codex\nnot a ps line\n")
    rollout = "/Users/me/.codex/sessions/2026/11/21/rollout-2026-11-21T09-00-00-abc.jsonl"
    lsof = f"p101\nf3\nn/dev/ttys001\nf31\nn{rollout}\np202\nf40\nn/Users/me/.codex/log/codex-tui.log\nfcwd\nn/x\n"
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0 if cmd[0] == "ps" else 1, ps if cmd[0] == "ps" else lsof, "")

    monkeypatch.setattr(clients.subprocess, "run", run)
    assert clients._codex_pids() == [101, 202]
    assert clients._codex_open() == {(101, 31, rollout)}
    assert calls[-1][:6] == ["lsof", "-n", "-P", "-a", "-p", "101,202"]
    assert clients._codex_open([]) == set()
    monkeypatch.setattr(clients.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no ps")))
    assert clients._codex_pids() == [] and clients._codex_open([1]) == set()
    # The note is kept with keepmum's own files, and a write that fails is not an error.
    monkeypatch.setattr(clients, "_codex_open", lambda pids=None: {(1, 2, rollout)})
    monkeypatch.setattr(clients, "_codex_open_file", lambda: tmp_path / "ro" / "codex-open.json")
    ro = tmp_path / "ro"
    ro.mkdir(mode=0o500)
    clients._codex_note_open()  # cannot create it: no error
    assert not clients._codex_open_file().exists()
    ro.chmod(0o700)
    clients._codex_open_file().write_text("{}")
    ro.chmod(0o500)
    clients._codex_forget_open()  # cannot remove it: no error either
    ro.chmod(0o700)
    assert clients._codex_open_file().exists()


