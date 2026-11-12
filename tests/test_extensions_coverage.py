"""extensions.py: the less common shapes of each agent's settings, unreadable files, and removals
that empty a hook group or cannot be made."""

import json

import pytest

from keepmum import extensions as ext


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".codex").mkdir()
    return tmp_path


def rows(home=None):
    return {(r["agent"], r["kind"], r["name"]): r for r in ext.check()["items"]}


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


# --- small helpers -----------------------------------------------------------------------------

def test_server_detail_for_each_shape():
    assert ext._server_detail("not a dict") == ""
    assert ext._server_detail({"httpUrl": "https://h.example"}) == "https://h.example"
    assert ext._server_detail({"command": ["npx", "-y", "srv"]}) == "npx -y srv"
    assert ext._server_detail({"command": "node", "args": ["a.js", 3]}) == "node a.js 3"
    assert ext._server_detail({}) == ""


def test_short_cuts_long_text_and_folds_whitespace():
    assert ext._short("a\n  b") == "a b"
    long = ext._short("x" * 200, 10)
    assert len(long) == 10 and long.endswith("…")


def test_an_item_id_is_stable_and_as_dict_says_removable():
    a = ext.Item("Codex", "plugin", "p", "", "f", loc=["toml", "plugins", "p"])
    b = ext.Item("Codex", "plugin", "p", "", "other file")
    assert a.id == b.id and len(a.id) == 10
    assert a.as_dict()["removable"] is True and "loc" not in a.as_dict()
    assert b.as_dict()["removable"] is False
    assert ext.Item("Codex", "plugin", "p", "", "f", id="fixed").id == "fixed"


def test_header_path_of_toml_headers():
    assert ext._header_path('[hooks.state."a:b"]') == ["hooks", "state", "a:b"]
    assert ext._header_path("[[profiles.list]]") == ["profiles", "list"]
    assert ext._header_path("[not toml") is None
    assert ext._header_path("key = 1") is None


# --- Claude Code -------------------------------------------------------------------------------

def test_claude_marketplaces_status_line_and_mcp_servers(home):
    write_json(home / ".claude" / "settings.json", {
        "enabledPlugins": {"off@x": False, "bare@x": True},
        "extraKnownMarketplaces": {"team": {"source": {"source": "github", "repo": "team/plugins"}},
                                   "odd": {"source": "https://odd.example"}, "none": None},
        "statusLine": {"type": "command", "command": "~/bin/status.sh"},
        "hooks": {"Stop": [{"hooks": ["not a hook", {"type": "prompt", "prompt": "summarize"}]}, "junk"]},
    })
    write_json(home / ".claude" / "plugins" / "known_marketplaces.json", {
        "team": {"source": {"repo": "team/plugins"}},  # also in settings: listed once
        "claude-plugins-official": {"source": {"repo": "anthropics/claude-plugins-official"}},
        "vendor": {"source": {"url": "https://vendor.example/market.git"}},
        "weird": "not a dict"})
    write_json(home / ".claude.json", {"mcpServers": {"gh": {"type": "http", "url": "https://api.github.example/mcp"}}})
    r = rows()
    assert ("Claude Code", "plugin", "off@x") not in r
    assert r[("Claude Code", "plugin", "bare@x")]["detail"] == ""
    assert r[("Claude Code", "marketplace", "team")]["detail"] == "team/plugins"
    assert r[("Claude Code", "marketplace", "odd")]["detail"] == "https://odd.example"
    assert r[("Claude Code", "marketplace", "none")]["detail"] == ""
    assert r[("Claude Code", "status line", "statusLine")]["detail"] == "~/bin/status.sh"
    assert r[("Claude Code", "hook", "Stop")]["detail"] == "summarize"
    vendor = r[("Claude Code", "marketplace", "vendor")]
    assert vendor["detail"] == "https://vendor.example/market.git" and not vendor["removable"]
    assert vendor["how"] == "in Claude Code: /plugin marketplace remove vendor"
    assert r[("Claude Code", "marketplace", "weird")]["detail"] == ""
    assert ("Claude Code", "marketplace", "claude-plugins-official") not in r
    gh = r[("Claude Code", "MCP server", "gh")]
    assert gh["detail"] == "https://api.github.example/mcp" and gh["removable"]


def test_claude_plugin_without_hooks_or_servers_and_with_unreadable_parts(home):
    plugin = home / "plugin"
    (plugin / "hooks").mkdir(parents=True)
    (plugin / "hooks" / "hooks.json").write_text("{broken")
    write_json(plugin / ".mcp.json", {"mcpServers": {}})
    write_json(home / ".claude" / "settings.json", {"enabledPlugins": {"p@m": True}})
    write_json(home / ".claude" / "plugins" / "installed_plugins.json", {"plugins": {"p@m": [{"installPath": str(plugin)}]}})
    p = rows()[("Claude Code", "plugin", "p@m")]
    assert p["detail"] == "" and p["adds_context"] is False


def test_an_unreadable_claude_json_is_reported(home):
    (home / ".claude.json").write_text("{nope")
    out = ext.check()
    assert any(".claude.json: not readable (JSONDecodeError)" in p for p in out["problems"])


def test_claude_config_dir_moves_claude_json(home, monkeypatch):
    base = home / "elsewhere"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(base))
    write_json(base / ".claude.json", {"mcpServers": {"x": {"command": "x"}}})
    assert rows()[("Claude Code", "MCP server", "x")]["file"] == str(base / ".claude.json")


def test_empty_global_instructions_are_not_listed(home):
    (home / ".claude" / "CLAUDE.md").write_text("  \n\n")
    assert not any(k[1] == "instructions" for k in rows())


def test_a_skills_folder_lists_only_folders_with_a_skill(home):
    (home / ".claude" / "skills" / "real").mkdir(parents=True)
    (home / ".claude" / "skills" / "real" / "SKILL.md").write_text("x")
    (home / ".claude" / "skills" / "empty").mkdir()
    (home / ".claude" / "skills" / "file.txt").write_text("x")
    assert [k[2] for k in rows() if k[1] == "skill"] == ["real"]


# --- Codex -------------------------------------------------------------------------------------

def test_codex_hooks_notify_and_disabled_plugins(home):
    (home / ".codex" / "config.toml").write_text(
        'notify = ["say", "done"]\n\n'
        '[plugins."off@x"]\nenabled = false\n\n'
        '[plugins."on@x"]\n\n'
        '[hooks.state."odd"]\ntrusted_hash = "x"\n\n'
        '[[hooks.session_start]]\ncommand = "cat ctx.md"\n')
    r = rows()
    assert ("Codex", "plugin", "off@x") not in r and ("Codex", "plugin", "on@x") in r
    trusted = r[("Codex", "trusted hook", "odd")]
    assert trusted["adds_context"] is False and trusted["detail"] == "odd"
    hook = r[("Codex", "hook", "session_start")]
    assert hook["adds_context"] and not hook["removable"] and "cat ctx.md" in hook["detail"]
    notify = r[("Codex", "hook", "notify")]
    assert notify["detail"] == "say done" and notify["how"].startswith("edit notify in ")


def test_codex_notify_as_a_string(home):
    (home / ".codex" / "config.toml").write_text('notify = "notify-send"\n')
    assert rows()[("Codex", "hook", "notify")]["detail"] == "notify-send"


def test_an_unreadable_codex_config_is_reported(home):
    (home / ".codex" / "config.toml").write_text("[[[ broken")
    assert any("config.toml: not readable (TOMLDecodeError)" in p for p in ext.check()["problems"])


def test_codex_home_moves_its_folder(home, monkeypatch):
    other = home / "codex2"
    other.mkdir()
    (other / "config.toml").write_text('[mcp_servers.x]\nurl = "https://x.example"\n')
    monkeypatch.setenv("CODEX_HOME", str(other))
    assert rows()[("Codex", "MCP server", "x")]["file"] == str(other / "config.toml")


# --- Cursor, Gemini, Antigravity, Copilot, opencode ----------------------------------------------

def test_cursor_hooks_and_gemini_extensions(home):
    write_json(home / ".cursor" / "hooks.json", {"version": 1, "hooks": {
        "sessionStart": [{"command": "./ctx.sh"}], "afterFileEdit": ["./fmt.sh"], "odd": "not a list"}})
    ext_dir = home / ".gemini" / "extensions"
    (ext_dir / "real").mkdir(parents=True)
    (ext_dir / "real" / "gemini-extension.json").write_text("{}")
    (ext_dir / "bare").mkdir()
    (ext_dir / "stray.txt").write_text("")
    r = rows()
    assert r[("Cursor", "hook", "sessionStart")]["adds_context"] is True
    assert r[("Cursor", "hook", "afterFileEdit")]["detail"] == "./fmt.sh"
    assert r[("Cursor", "hook", "afterFileEdit")]["adds_context"] is False
    assert not any(k[:2] == ("Cursor", "hook") and k[2] == "odd" for k in r)
    assert [k[2] for k in r if k[:2] == ("Gemini CLI", "extension")] == ["real"]
    assert r[("Gemini CLI", "extension", "real")]["how"] == "gemini extensions uninstall real"


def test_an_unreadable_mcp_file_is_reported(home):
    (home / ".copilot").mkdir()
    (home / ".copilot" / "mcp-config.json").write_text("{bad")
    assert any("mcp-config.json: not readable" in p for p in ext.check()["problems"])


def test_opencode_servers_and_plugins_from_commented_json(home):
    oc = home / ".config" / "opencode"
    oc.mkdir(parents=True)
    (oc / "opencode.jsonc").write_text('{\n  // my servers\n  "mcp": {"docs": {"type": "remote", "url": "https://d.example"},},\n'
                                       '  "plugin": ["opencode-vendor"],\n}\n')
    (oc / "opencode.json").write_text("{not json at all")
    out = ext.check()
    r = {(x["agent"], x["kind"], x["name"]): x for x in out["items"]}
    assert r[("opencode", "MCP server", "docs")]["detail"] == "https://d.example"
    assert r[("opencode", "plugin", "opencode-vendor")]["how"] == f"edit plugin in {oc / 'opencode.jsonc'}"
    assert any("opencode.json: not readable" in p for p in out["problems"])


def test_antigravity_hook_groups_of_odd_shapes(home):
    config = home / ".gemini" / "config"
    write_json(config / "hooks.json", {
        "mixed": {"enabled": True, "PreInvocation": [{"command": "./a.sh"}, "junk", {"hooks": [{"command": "./b.sh"}, {}]}],
                  "PostToolUse": "not a list"},
        "notagroup": "x"})
    hook = rows()[("Antigravity CLI", "hook", "mixed")]
    assert hook["detail"] == "PreInvocation: ./a.sh, ./b.sh" and hook["adds_context"]
    assert not any(k[2] == "notagroup" for k in rows())


def test_an_unreadable_antigravity_plugin_manifest_is_reported_and_named_by_its_folder(home):
    plugin = home / ".gemini" / "config" / "plugins" / "folder-name"
    (plugin / "skills" / "nope").mkdir(parents=True)  # no SKILL.md: not a skill
    (plugin / "plugin.json").write_text("{bad")
    out = ext.check()
    r = {(x["agent"], x["kind"], x["name"]): x for x in out["items"]}
    assert r[("Antigravity CLI", "plugin", "folder-name")]["detail"] == ""
    assert any("plugin.json: not readable" in p for p in out["problems"])


def test_a_reader_that_fails_is_reported_and_the_others_still_read(home, monkeypatch):
    def _codex(out, problems):
        raise RuntimeError("odd file")

    monkeypatch.setattr(ext, "_codex", _codex)
    write_json(home / ".cursor" / "mcp.json", {"mcpServers": {"c": {"url": "https://c.example"}}})
    items, problems = ext.scan()
    assert "codex: RuntimeError" in problems and any(i.name == "c" for i in items)


# --- state -------------------------------------------------------------------------------------

def test_a_corrupt_state_file_means_not_watching(home):
    ext.state_file().parent.mkdir(parents=True)
    ext.state_file().write_text('{"known": []}')
    assert ext.check()["watching"] is False
    ext._first_look_for_new_agents()  # nothing to update
    assert ext.state_file().read_text() == '{"known": []}'


def test_a_baseline_taken_by_another_process_meanwhile_is_kept(home, monkeypatch):
    calls = {"n": 0}
    real = ext._load

    def load():
        calls["n"] += 1
        if calls["n"] == 2:  # another keepmum took the first look between the two checks
            ext._save({"version": 1, "since": 1, "agents": list(ext.AGENTS), "known": {"x": {"name": "x"}}})
        return real()

    monkeypatch.setattr(ext, "_load", load)
    assert ext.ensure_baseline() is False
    assert json.loads(ext.state_file().read_text())["known"] == {"x": {"name": "x"}}


def test_keep_without_a_state_starts_one_and_keeps_only_the_ids_given(home):
    write_json(home / ".cursor" / "mcp.json", {"mcpServers": {"a": {"url": "https://a"}, "b": {"url": "https://b"}}})
    r = rows()
    a = r[("Cursor", "MCP server", "a")]["id"]
    assert ext.keep([a]) == 1
    known = json.loads(ext.state_file().read_text())["known"]
    assert list(known) == [a]
    after = rows()
    assert after[("Cursor", "MCP server", "b")]["new"] and not after[("Cursor", "MCP server", "a")]["new"]
    assert after[("Cursor", "MCP server", "a")]["since"] == known[a]["since"]


def test_check_lists_what_went_away(home):
    write_json(home / ".cursor" / "mcp.json", {"mcpServers": {"a": {"url": "https://a"}}})
    ext.ensure_baseline()
    write_json(home / ".cursor" / "mcp.json", {"mcpServers": {}})
    gone = ext.check()["gone"]
    assert [g["name"] for g in gone] == ["a"] and gone[0]["agent"] == "Cursor"
    assert ext.keep() == 0 and json.loads(ext.state_file().read_text())["known"] == {}


# --- removing ------------------------------------------------------------------------------------

def test_removing_the_last_claude_hook_of_an_event_removes_the_event(home):
    settings = home / ".claude" / "settings.json"
    write_json(settings, {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "a"}]}],
                                    "Stop": [{"hooks": [{"type": "command", "command": "b"}]}]}})
    ext.ensure_baseline()
    item, backup = ext.remove(rows()[("Claude Code", "hook", "SessionStart")]["id"])
    assert item.name == "SessionStart" and backup.exists()
    data = json.loads(settings.read_text())
    assert list(data["hooks"]) == ["Stop"]
    assert item.id not in json.loads(ext.state_file().read_text())["known"]


def test_removing_a_hook_keeps_the_rest_of_its_group(home):
    settings = home / ".claude" / "settings.json"
    write_json(settings, {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "a"},
                                                        {"type": "command", "command": "b"}]}]}})
    hooks = [r for r in ext.check()["items"] if (r["agent"], r["kind"]) == ("Claude Code", "hook")]
    assert [h["detail"] for h in hooks] == ["a", "b"]
    first = hooks[0]
    ext.remove(first["id"])
    assert json.loads(settings.read_text())["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": "b"}]}]


def test_removing_a_plugin_disables_it_and_a_status_line_is_deleted(home):
    settings = home / ".claude" / "settings.json"
    write_json(settings, {"enabledPlugins": {"p@m": True}, "statusLine": {"command": "s.sh"}})
    ext.remove(rows()[("Claude Code", "plugin", "p@m")]["id"])
    ext.remove(rows()[("Claude Code", "status line", "statusLine")]["id"])
    assert json.loads(settings.read_text()) == {"enabledPlugins": {"p@m": False}}


def test_remove_an_unknown_or_unremovable_item(home):
    with pytest.raises(ext.ExtensionError, match="no item nope"):
        ext.remove("nope")
    (home / ".claude" / "CLAUDE.md").write_text("rules\n")
    with pytest.raises(ext.ExtensionError, match="keepmum does not edit"):
        ext.remove(rows()[("Claude Code", "instructions", "CLAUDE.md")]["id"])


def test_a_file_changed_between_scan_and_remove_is_left_alone(home, monkeypatch):
    settings = home / ".claude" / "settings.json"
    write_json(settings, {"statusLine": {"command": "s.sh"}})
    item_id = rows()[("Claude Code", "status line", "statusLine")]["id"]
    real = ext._remove_json

    def changed(path, loc):
        path.write_text("{}")  # someone else took it out meanwhile
        return real(path, loc)

    monkeypatch.setattr(ext, "_remove_json", changed)
    with pytest.raises(ext.ExtensionError, match=r"could not take it out \(KeyError\); nothing was changed"):
        ext.remove(item_id)
    assert not ext.backup_dir().exists()


def test_removing_a_codex_server_that_is_an_array_of_tables_neighbour(home):
    cfg = home / ".codex" / "config.toml"
    cfg.write_text('[[profiles]]\nname = "a"\n\n[mcp_servers.x]\nurl = "https://x"\n\n[mcp_servers.y]\nurl = "https://y"\n')
    ext.remove(rows()[("Codex", "MCP server", "x")]["id"])
    text = cfg.read_text()
    assert "[mcp_servers.x]" not in text and "[mcp_servers.y]" in text and "[[profiles]]" in text
