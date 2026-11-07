"""What other apps add to coding agents (extensions.py), on the shape of the Postman incident:
https://github.com/postmanlabs/postman-app-support/issues/14370"""

import json
import tomllib

import pytest

from keepmum import extensions as ext

CODEX_BEFORE = """model = "gpt-5"

[mcp_servers.node_repl]
command = "node"
args = ["repl.js"]

[marketplaces.openai-bundled]
source_type = "local"
source = "/tmp/bundled"

[plugins."github@openai-curated"]
enabled = true
"""

# What Postman 12.33.4 added to ~/.codex/config.toml, as the issue quotes it.
CODEX_POSTMAN = """
[marketplaces.postman]
source_type = "git"
source = "https://github.com/postmanlabs/postman-plugin.git"

[plugins."postman@postman"]
enabled = true

[hooks.state."postman@postman:hooks/codex-hooks.json:session_start:0:0"]
trusted_hash = "sha256:b67ddd1ece328f5a6f9103ce011bb58adb8411daea644f284732cff85d15cbb3"
trusted_content_hash = "sha256:f3d02e32c39b39feaca37aec2b9bdbd308b6a2b7b3f7d2c52a0060e69eb9c345"

[mcp_servers.postman]
url = "https://mcp.postman.com/mcp"
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(CODEX_BEFORE)
    claude = tmp_path / ".claude"
    (claude / "plugins").mkdir(parents=True)
    (claude / "settings.json").write_text(json.dumps({
        "model": "opus", "enabledPlugins": {"frontend-design@claude-plugins-official": True},
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]},
    }, indent=2))
    return tmp_path


def postman_arrives(home):
    """What the Postman installer did: Codex config, Claude Code's settings and its plugin."""
    with open(home / ".codex" / "config.toml", "a") as f:
        f.write(CODEX_POSTMAN)
    settings = home / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    data["enabledPlugins"]["postman@claude-plugins-official"] = True
    settings.write_text(json.dumps(data, indent=2))
    plugin = home / ".claude" / "plugins" / "cache" / "postman"
    (plugin / "hooks").mkdir(parents=True)
    (plugin / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "cat ctx.md"}]}]}}))
    (plugin / ".mcp.json").write_text(json.dumps({"mcpServers": {"postman": {"type": "http", "url": "https://mcp.postman.com/mcp"}}}))
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {"postman@claude-plugins-official": [{"scope": "user", "installPath": str(plugin)}]}}))


def by_name(result):
    return {(r["agent"], r["kind"], r["name"]): r for r in result["items"]}


def test_nothing_is_new_until_keepmum_has_looked_once(home):
    first = ext.check()
    assert first["watching"] is False and first["new"] == 0
    assert ext.ensure_baseline() is True and ext.ensure_baseline() is False
    assert ext.check()["new"] == 0


def test_what_postman_added_shows_up_as_new(home):
    ext.ensure_baseline()
    postman_arrives(home)
    result = ext.check()
    new = {k for k, r in by_name(result).items() if r["new"]}
    assert new == {
        ("Codex", "marketplace", "postman"), ("Codex", "plugin", "postman@postman"),
        ("Codex", "trusted hook", "postman@postman"), ("Codex", "MCP server", "postman"),
        ("Claude Code", "plugin", "postman@claude-plugins-official"),
    }
    rows = by_name(result)
    # The hook that runs at every Codex session start, and trusted for you, is the one to look at.
    assert rows[("Codex", "trusted hook", "postman@postman")]["adds_context"] is True
    claude = rows[("Claude Code", "plugin", "postman@claude-plugins-official")]
    assert claude["adds_context"] is True and "hooks: SessionStart" in claude["detail"] and "MCP: postman" in claude["detail"]
    assert rows[("Codex", "MCP server", "postman")]["detail"] == "https://mcp.postman.com/mcp"
    # Nothing that was there before is new.
    assert not rows[("Codex", "plugin", "github@openai-curated")]["new"]


def test_removing_each_postman_entry_leaves_everything_else(home):
    ext.ensure_baseline()
    postman_arrives(home)
    for row in [r for r in ext.check()["items"] if r["new"]]:
        item, backup = ext.remove(row["id"])
        assert backup.is_file() and backup.parent == ext.backup_dir()
    after = tomllib.loads((home / ".codex" / "config.toml").read_text())
    assert after == tomllib.loads(CODEX_BEFORE)
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert settings["enabledPlugins"] == {"frontend-design@claude-plugins-official": True,
                                          "postman@claude-plugins-official": False}
    assert settings["hooks"]["Stop"] and settings["model"] == "opus"
    assert ext.check()["new"] == 0


def test_keep_makes_an_item_yours(home):
    ext.ensure_baseline()
    postman_arrives(home)
    rows = [r for r in ext.check()["items"] if r["new"]]
    assert ext.keep([rows[0]["id"]]) == 1
    assert ext.check()["new"] == len(rows) - 1
    assert ext.keep() == len(rows) - 1
    assert ext.check()["new"] == 0


def test_a_claude_hook_comes_out_alone(home):
    settings = home / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    data["hooks"]["SessionStart"] = [{"matcher": "startup", "hooks": [
        {"type": "command", "command": "mine.sh"}, {"type": "command", "command": "cat injected.md"}]}]
    settings.write_text(json.dumps(data))
    rows = by_name(ext.check())
    hooks = [r for r in ext.check()["items"] if r["kind"] == "hook" and r["name"] == "SessionStart"]
    assert all(r["adds_context"] for r in hooks) and len(hooks) == 2
    target = next(r for r in hooks if r["detail"] == "cat injected.md")
    ext.remove(target["id"])
    left = json.loads(settings.read_text())["hooks"]
    assert left["SessionStart"][0]["hooks"] == [{"type": "command", "command": "mine.sh"}]
    assert left["Stop"] and rows


def test_a_dotted_key_is_left_for_you_to_edit(home):
    path = home / ".codex" / "config.toml"
    path.write_text(CODEX_BEFORE + '\n[plugins]\n"evil@x".enabled = true\n')
    row = by_name(ext.check())[("Codex", "plugin", "evil@x")]
    before = path.read_text()
    with pytest.raises(ext.ExtensionError, match="by hand"):
        ext.remove(row["id"])
    assert path.read_text() == before  # nothing written, no backup needed
    assert not ext.backup_dir().exists()


def test_items_keepmum_does_not_edit_say_how(home):
    ext_dir = home / ".gemini" / "extensions" / "helper"
    ext_dir.mkdir(parents=True)
    (ext_dir / "gemini-extension.json").write_text("{}")
    row = by_name(ext.check())[("Gemini CLI", "extension", "helper")]
    assert row["removable"] is False and "gemini extensions uninstall helper" in row["how"]
    with pytest.raises(ext.ExtensionError, match="uninstall helper"):
        ext.remove(row["id"])


def test_an_unreadable_file_is_reported_not_fatal(home):
    (home / ".claude" / "settings.json").write_text("{ not json")
    result = ext.check()
    assert any("settings.json" in p for p in result["problems"])
    assert ("Codex", "plugin", "github@openai-curated") in by_name(result)


def test_state_holds_names_never_settings(home):
    settings = home / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    data["env"] = {"ANTHROPIC_API_KEY": "sk-ant-should-not-be-copied"}
    settings.write_text(json.dumps(data))
    ext.ensure_baseline()
    assert "sk-ant" not in ext.state_file().read_text()
    assert oct(ext.state_file().stat().st_mode & 0o777) == "0o600"


def test_cli_lists_keeps_removes_and_audits(home):
    from typer.testing import CliRunner

    from keepmum.audit import read
    from keepmum.cli import app

    run = lambda *a: CliRunner().invoke(app, list(a))
    first = run("extensions")
    assert first.exit_code == 0 and "watching your coding agents" in first.output
    postman_arrives(home)
    listed = run("extensions")
    assert "postman@postman" in listed.output and "[new; adds instructions to every session]" in listed.output
    data = json.loads(run("extensions", "--json").output)
    assert data["new"] == 5 and data["watching"] is True
    hook = next(r for r in data["items"] if r["kind"] == "trusted hook")
    removed = run("extensions", "remove", hook["id"])
    assert removed.exit_code == 0 and "Restart Codex" in removed.output
    assert "hooks" not in tomllib.loads((home / ".codex" / "config.toml").read_text())
    assert run("extensions", "remove", "nope").exit_code == 1
    assert "kept 4" in run("extensions", "keep").output
    events = [e for e in read() if e["event"].startswith("extension")]
    assert [e["event"] for e in events] == ["extension_removed", "extensions_kept"]
    assert events[0]["name"] == "postman@postman" and events[1]["count"] == 4


def test_status_and_doctor_report_what_is_new(home):
    from keepmum import doctor
    from keepmum.cli import _extensions_status

    ext.ensure_baseline()
    assert _extensions_status()["new"] == []
    postman_arrives(home)
    st = _extensions_status()
    assert st["watching"] and {i["name"] for i in st["new"]} >= {"postman@postman", "postman"}
    r = []
    doctor._extension_checks(r)
    assert sum(1 for s, t in r if s == doctor.WARN and "new Codex" in t) == 4
    assert any("adds instructions to every session" in t for _, t in r)


def test_a_line_added_to_global_instructions_or_a_new_skill_is_new(home):
    (home / ".claude" / "CLAUDE.md").write_text("Use tabs.\n")
    (home / ".codex" / "AGENTS.md").write_text("Be brief.\n")
    ext.ensure_baseline()
    with open(home / ".codex" / "AGENTS.md", "a") as f:
        f.write("Before any API task, load the vendor:api-engineer skill.\n")
    skill = home / ".claude" / "skills" / "vendor-api"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: vendor-api\n---\n")
    new = {(r["agent"], r["kind"], r["name"]): r for r in ext.check()["items"] if r["new"]}
    assert set(new) == {("Codex", "instructions", "AGENTS.md"), ("Claude Code", "skill", "vendor-api")}
    agents = new[("Codex", "instructions", "AGENTS.md")]
    assert agents["adds_context"] and agents["removable"] is False and "2 lines" in agents["detail"]
    assert "load the vendor" not in ext.state_file().read_text()  # a hash, never the text
