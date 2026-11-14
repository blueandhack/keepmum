"""The CLI's less-used commands and error paths: skills, handback, sensitivity, guard remove, log,
destinations, types, rules, tools, exclude/include, status, extensions and the audit log. Every
test runs in a HOME of its own; nothing reaches Ollama, launchd, the network or a download."""

import json
import shutil

import pytest
from typer.testing import CliRunner

from keepmum import cli as climod
from keepmum.cli import app
from keepmum.config import load
from keepmum.local import read_skill, skills_dir


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("KEEPMUM_CALLER", raising=False)
    path = tmp_path / ".config" / "keepmum" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text("")
    return path


def run(*args):
    return CliRunner().invoke(app, [str(a) for a in args])


@pytest.fixture
def no_writes(monkeypatch):
    """Every settings write fails, as on a full disk or a read-only home."""
    def fail(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(climod, "_update_config", fail)


def audit_entries(home):
    from keepmum.audit import read

    return read(home.parent / "audit.jsonl")


# --- Skills ---------------------------------------------------------------------------------------

def test_skill_remove_delete_says_when_the_folder_cannot_be_deleted(home, monkeypatch):
    assert run("guard", "local", "skill", "new", "s", "--text", "Do it.").exit_code == 0
    folder = skills_dir() / "s"

    def busy(path, *a, **k):
        raise OSError("resource busy")

    monkeypatch.setattr(shutil, "rmtree", busy)
    r = run("guard", "local", "skill", "remove", "1", "--delete")
    assert r.exit_code == 1 and f"removed, but {folder} could not be deleted: resource busy" in r.stderr
    assert load(home).guard_local_skills == [] and folder.exists()  # off the list, the files left


def test_skill_text_and_text_file_are_one_or_the_other(home, tmp_path):
    (tmp_path / "t.md").write_text("Text.")
    r = run("guard", "local", "skill", "new", "s", "--text", "x", "--text-file", tmp_path / "t.md")
    assert r.exit_code == 2 and "give --text or --text-file, not both" in r.stderr
    r = run("guard", "local", "skill", "new", "s", "--text-file", tmp_path / "gone.md")
    assert r.exit_code == 2 and "keepmum guard local skill new: " + str(tmp_path / "gone.md") in r.stderr
    (tmp_path / "bin.md").write_bytes(b"\xff\xfe\x00bad")
    r = run("guard", "local", "skill", "new", "s", "--text-file", tmp_path / "bin.md")
    assert r.exit_code == 2 and "bin.md" in r.stderr
    assert load(home).guard_local_skills == [] and not skills_dir().exists()
    assert run("guard", "local", "skill", "new", "s", "--text-file", tmp_path / "t.md").exit_code == 0
    assert read_skill(str(skills_dir() / "s"))["text"] == "Text."
    r = run("guard", "local", "skill", "edit", "1", "--text", "y", "--text-file", tmp_path / "t.md")
    assert r.exit_code == 2 and "keepmum guard local skill edit: give --text or --text-file" in r.stderr


def test_a_new_skill_without_a_name_or_that_cannot_be_written_is_not_added(home, monkeypatch):
    from keepmum import local

    r = run("guard", "local", "skill", "new", "  ", "--text", "Do it.")
    assert r.exit_code == 2 and "a skill needs a name" in r.stderr
    assert load(home).guard_local_skills == [] and "local_skills" not in home.read_text()

    def read_only(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(local, "write_skill", read_only)
    r = run("guard", "local", "skill", "new", "s", "--text", "Do it.")
    assert r.exit_code == 1 and "keepmum guard local skill new: read-only file system" in r.stderr
    assert load(home).guard_local_skills == []


def test_editing_a_skill_whose_file_is_gone_writes_it_again_given_a_name(home):
    assert run("guard", "local", "skill", "new", "s", "--description", "Old.", "--text", "Do it.").exit_code == 0
    (skills_dir() / "s" / "SKILL.md").unlink()
    r = run("guard", "local", "skill", "edit", "1", "--text", "New.")
    assert r.exit_code == 2 and "a skill needs a name" in r.stderr  # nothing to keep the old name from
    r = run("guard", "local", "skill", "edit", "1", "--name", "back", "--text", "New.")
    assert r.exit_code == 0 and "written" in r.stderr
    assert read_skill(str(skills_dir() / "s")) | {"file": None} == {"name": "back", "description": "", "text": "New.",
                                                                      "file": None}


def test_skill_show_prints_the_skill_or_says_why_it_cannot(home):
    assert run("guard", "local", "skill", "new", "careful", "--description", "Be careful.", "--text", "Answer briefly.").exit_code == 0
    r = run("guard", "local", "skill", "show", "1")
    file = skills_dir() / "careful" / "SKILL.md"
    assert r.exit_code == 0 and r.stdout == f"careful: Be careful.\n{file}\n\nAnswer briefly.\n"
    assert run("guard", "local", "skill", "edit", "1", "--description", "").exit_code == 0
    assert run("guard", "local", "skill", "show", "1").stdout.startswith(f"careful\n{file}\n")  # no description, no colon
    file.unlink()
    r = run("guard", "local", "skill", "show", "1")
    assert r.exit_code == 1 and "keepmum guard local skill show:" in r.stderr and "not found" in r.stderr


def test_skill_example_says_when_its_folder_cannot_be_written(home, monkeypatch):
    from keepmum import local

    def read_only():
        raise OSError("read-only file system")

    monkeypatch.setattr(local, "add_example", read_only)
    r = run("guard", "local", "skill", "example")
    assert r.exit_code == 1 and "keepmum guard local skill example: read-only file system" in r.stderr
    assert load(home).guard_local_skills == []


# --- Skill assist (the local model and the request to it are faked) --------------------------------

@pytest.fixture
def assist(home, monkeypatch):
    from keepmum import local, skillassist

    home.write_text('[guard]\nlocal_servers = [{ url = "http://localhost:11434", model = "m" }, '
                    '{ url = "http://localhost:1234", model = "n" }]\n')
    monkeypatch.setattr(local, "models", lambda url, timeout=3.0: ["m", "n"])
    monkeypatch.setattr(local, "kind", lambda url, timeout=1.5: "Ollama")
    calls = []

    def ask(server, kind, draft, request, progress=None, timeout=600.0):
        calls.append((server.url, kind, draft, request))
        for n in calls_progress:
            progress(n)
        if isinstance(reply[0], Exception):
            raise reply[0]
        return reply[0]

    calls_progress: list[int] = []
    reply = [{"name": "new", "description": "", "text": "Written.", "note": ""}]
    monkeypatch.setattr(skillassist, "ask", ask)
    return {"calls": calls, "progress": calls_progress, "reply": reply}


def test_assist_refuses_a_draft_file_it_cannot_read(assist, tmp_path):
    for content in ("not json", "[1, 2]"):  # not JSON; JSON but not an object
        (tmp_path / "d.json").write_text(content)
        r = run("guard", "local", "skill", "assist", "--draft-file", tmp_path / "d.json", "--request", "x")
        assert r.exit_code == 2 and f"keepmum guard local skill assist: {tmp_path / 'd.json'}:" in r.stderr
    r = run("guard", "local", "skill", "assist", "--draft-file", tmp_path / "gone.json", "--request", "x")
    assert r.exit_code == 2 and assist["calls"] == []


def test_assist_says_when_the_skill_to_start_from_cannot_be_read(assist):
    assert run("guard", "local", "skill", "new", "s", "--text", "Do it.").exit_code == 0
    (skills_dir() / "s" / "SKILL.md").unlink()
    r = run("guard", "local", "skill", "assist", "1", "--request", "x")
    assert r.exit_code == 1 and "not found" in r.stderr and assist["calls"] == []


def test_assist_prints_the_skill_and_shows_progress_only_every_40_characters(assist):
    assist["progress"][:] = [80, 100, 10]  # 100 is too soon after 80; 10 is a new attempt
    r = run("guard", "local", "skill", "assist", "--request", "Write one.", "--server", "2")
    assert r.exit_code == 0, r.output
    assert "asking n at http://localhost:1234" in r.stderr
    assert "writing: 80 characters" in r.stderr and "writing: 10 characters" in r.stderr and "100" not in r.stderr
    assert r.stdout == "new\n\nWritten.\n" and "(" not in r.stderr.split("characters")[-1]  # no note, no description
    assert assist["calls"] == [("http://localhost:1234", "Ollama", {"name": "", "description": "", "text": ""}, "Write one.")]


def test_assist_says_what_went_wrong_with_the_model(assist):
    from keepmum.skillassist import AssistError

    assist["reply"][0] = AssistError("the model wrote nothing")
    r = run("guard", "local", "skill", "assist", "--request", "x")
    assert r.exit_code == 1 and "keepmum guard local skill assist: the model wrote nothing" in r.stderr
    assert r.stdout == ""


def test_assist_save_refuses_a_result_that_is_no_skill(assist):
    assert run("guard", "local", "skill", "new", "s", "--text", "Do it.").exit_code == 0
    assist["reply"][0] = {"name": "", "description": "", "text": "", "note": ""}  # the model returned no instructions
    r = run("guard", "local", "skill", "assist", "1", "--request", "x", "--save")
    assert r.exit_code == 1 and "a skill needs instructions" in r.stderr and "saved" not in r.stderr
    assert read_skill(str(skills_dir() / "s"))["text"] == "Do it."


# --- Handback, sensitivity and the checks ---------------------------------------------------------

def test_handback_says_when_it_cannot_write_the_setting(home, no_writes):
    r = run("guard", "local", "handback", "summary")
    assert r.exit_code == 1 and "keepmum guard local handback: disk full" in r.stderr
    assert load(home).guard_local_handback != "summary"


def test_sensitivity_says_when_it_cannot_write_the_setting(home, no_writes):
    r = run("guard", "sensitivity", "harm", "low")
    assert r.exit_code == 1 and "keepmum guard sensitivity: disk full" in r.stderr
    r = run("guard", "sensitivity", "injection", "0.3")
    assert r.exit_code == 1 and "keepmum guard sensitivity: disk full" in r.stderr
    cfg = load(home)
    assert cfg.guard_harm_sensitivity == "medium" and cfg.guard_injection_threshold == 0.222


def test_the_commands_check_needs_no_model_and_takes_no_local_mode(home):
    r = run("guard", "commands", "block")
    assert r.exit_code == 0 and "commands: block (a flagged command is replaced" in r.stderr
    assert load(home).guard_commands == "block"
    r = run("guard", "commands", "local")
    assert r.exit_code == 2 and "use warn, block or off" in r.stderr
    assert load(home).guard_commands == "block"


def test_the_harm_check_refuses_an_unknown_model(home):
    r = run("guard", "harm", "warn", "--model", "70b")
    assert r.exit_code == 2 and "--model is small, 0.6b or 4b" in r.stderr
    assert load(home).guard_harm == "off"


def test_guard_remove_turns_the_check_off_deletes_its_model_and_records_it(home):
    from keepmum import guard

    home.write_text('[guard]\ninjection = "warn"\n')
    guard.model_dir().mkdir(parents=True)
    (guard.model_dir() / "model.gguf").write_bytes(b"x")
    r = run("guard", "remove", "injection")
    assert r.exit_code == 0 and f"removed {guard.model_dir()}" in r.stderr and "injection: off" in r.stderr
    assert not guard.model_dir().exists() and load(home).guard_injection == "off"
    [entry] = [e for e in audit_entries(home) if e["event"] == "guard-model"]
    assert entry["action"] == "removed" and entry["model"] == guard.model_dir().name and entry["licence"] == guard.LICENCE


def test_guard_remove_with_nothing_there_changes_nothing(home):
    before = home.read_text()
    r = run("guard", "remove", "harm")
    assert r.exit_code == 0 and "nothing to remove" in r.stderr and home.read_text() == before
    r = run("guard", "remove", "commands")
    assert r.exit_code == 2 and "use injection or harm" in r.stderr


# --- log --------------------------------------------------------------------------------------------

def test_log_status_shows_the_setting_without_writing(home):
    r = run("log", "previews")
    assert r.exit_code == 0 and r.stderr == "previews: the Logs page shows the first and last character of long masked values\n"
    assert run("log", "context", "off").exit_code == 0
    r = run("log", "context", "status")
    assert r.exit_code == 0 and r.stderr == "context: off; no text around masked values is kept\n"
    assert load(home).log_context is False


def test_log_says_when_it_cannot_write_the_setting(home, no_writes):
    r = run("log", "previews", "off")
    assert r.exit_code == 1 and "keepmum log: disk full" in r.stderr
    assert load(home).log_previews is True


# --- destinations -----------------------------------------------------------------------------------

def test_destinations_lists_the_hosts_added(home):
    r = run("destinations")
    assert r.exit_code == 0 and "no hosts added" in r.stderr
    assert run("destinations", "add", "github-token", "GHE.corp.example.", "--yes").exit_code == 0
    r = run("destinations")
    assert r.exit_code == 0 and r.stderr == "github-token: ghe.corp.example\n"


def test_destinations_says_when_it_cannot_write_the_setting(home, no_writes):
    r = run("destinations", "add", "github-token", "ghe.corp.example", "--yes")
    assert r.exit_code == 1 and "keepmum destinations: disk full" in r.stderr
    assert load(home).destinations == {}


# --- types ------------------------------------------------------------------------------------------

def test_types_refuses_an_unknown_action_or_type(home):
    for args in (["show"], ["pass", "NAMES"], ["mask"]):
        r = run("types", *args)
        assert r.exit_code == 2 and "use list, or mask|pass" in r.stderr, args
    assert "pass" not in home.read_text()


def test_types_lists_each_type_with_personal_data_off(home):
    r = run("types")
    assert r.exit_code == 0
    assert "SECRET   mask" in r.stderr and "PERSON   off: personal data is off" in r.stderr
    assert "Personal data is off" not in r.stderr  # only said when a personal type is named


def test_types_says_when_it_cannot_write_the_setting(home, no_writes):
    r = run("types", "pass", "PERSON")
    assert r.exit_code == 1 and "keepmum types: disk full" in r.stderr
    assert load(home).pass_types == []


# --- rules ------------------------------------------------------------------------------------------

def test_rules_list_shows_every_kind_of_rule(home):
    assert run("rules", "mask", r"ACME-\d+", "--name", "acme ids", "--type", "account").exit_code == 0
    assert run("rules", "allow", "localhost-token").exit_code == 0
    assert run("rules", "allow", "--pattern", r"test-[a-z]+").exit_code == 0
    assert run("rules", "off", "email").exit_code == 0
    cfg = load(home)
    assert cfg.mask_rules == [{"name": "acme ids", "pattern": r"ACME-\d+", "type": "ACCOUNT"}]
    assert cfg.allow_strings == ["localhost-token"] and cfg.allow_patterns == [r"test-[a-z]+"] and cfg.off_rules == ["email"]
    r = run("rules", "list")
    assert r.exit_code == 0
    assert r.stderr.splitlines() == ["mask  0  acme ids  ACCOUNT  /ACME-\\d+/", "allow 0  'localhost-token'",
                                     "allow-pattern 0  /test-[a-z]+/",
                                     "off   email  (built-in; `keepmum rules on email` turns it back on)"]


def test_rules_refuse_a_bad_pattern_type_or_number(home):
    before = home.read_text()
    r = run("rules", "mask", "(unclosed")
    assert r.exit_code == 2 and "keepmum rules: bad pattern" in r.stderr
    r = run("rules", "mask", "x+", "--type", "COLOUR")
    assert r.exit_code == 2 and "--type must be one of SECRET" in r.stderr
    r = run("rules", "allow", "--pattern", "[a-")
    assert r.exit_code == 2 and "bad pattern" in r.stderr
    for args in (["mask", "0"], ["allow", "3"], ["nothing", "0"]):
        r = run("rules", "remove", *args)
        assert r.exit_code == 2 and "no such rule" in r.stderr, args
    assert home.read_text() == before


def test_rules_builtin_lists_each_rule_and_whether_it_is_on(home):
    assert run("rules", "off", "email").exit_code == 0
    lines = run("rules", "builtin").stderr.splitlines()
    assert "on   SECRET   private-key" in lines and "OFF  EMAIL    email" in lines


def test_rules_say_when_they_cannot_write_the_setting(home, no_writes):
    r = run("rules", "allow", "x")
    assert r.exit_code == 1 and "keepmum rules: disk full" in r.stderr
    assert load(home).allow_strings == []


# --- tools ------------------------------------------------------------------------------------------

@pytest.fixture
def servers(home):
    from keepmum.tools import ToolRegistry

    reg = ToolRegistry(home.parent / "tools.json")
    reg.observe([("mcp__claude_ai_Gmail__search_threads", "Search mail", {}), ("WebSearch", "Search the web", {})],
                "claude-code", lambda name: True)
    reg.note_kept("mcp__claude_ai_Gmail", ["PERSON", "EMAIL"])
    return reg


def test_tools_with_no_servers_says_when_they_show_up(home):
    r = run("tools")
    assert r.exit_code == 0 and "no network tools seen yet" in r.stderr


def test_tools_lists_each_server_its_setting_and_what_it_kept(servers):
    assert run("tools", "allow", "gmail", "PHONE").exit_code == 0
    lines = {line.split()[0]: line for line in run("tools").stderr.splitlines()}
    gmail = lines["Gmail"]
    assert "values it gave you, and PHONE" in gmail and "2 kept this week: EMAIL, PERSON" in gmail
    assert "(new)" not in gmail  # changing its setting reviews it
    assert "(new)" in lines["WebSearch"] and "kept" not in lines["WebSearch"]


def test_tools_names_the_wider_rule_a_server_takes_its_setting_from(servers, home):
    home.write_text('[restore]\n"mcp__*" = "keep"\n')
    gmail = next(line for line in run("tools").stderr.splitlines() if line.startswith("Gmail"))
    assert "placeholders only" in gmail and "from mcp__*" in gmail


def test_tools_set_refuses_an_unknown_setting_or_server(servers, home):
    r = run("tools", "set", "gmail", "everything")
    assert r.exit_code == 2 and "SETTING must be placeholders, own, contacts or default" in r.stderr
    r = run("tools", "reviewed", "nosuch")
    assert r.exit_code == 2 and "no server 'nosuch' seen yet" in r.stderr
    assert "restore" not in home.read_text()


def test_tools_say_when_they_cannot_write_the_setting(servers, home, no_writes):
    r = run("tools", "set", "gmail", "own")
    assert r.exit_code == 1 and "keepmum tools: disk full" in r.stderr
    assert next(s for s in servers.snapshot({}) if s["label"] == "Gmail")["new"]  # not marked reviewed either


def test_tools_reviewed_marks_one_server_or_all(servers):
    assert run("tools", "reviewed", "gmail").exit_code == 0
    assert {s["label"]: s["new"] for s in servers.snapshot({})} == {"Gmail": False, "WebSearch": True}
    assert run("tools", "reviewed").exit_code == 0
    assert not any(s["new"] for s in servers.snapshot({}))


# --- exclude and include ----------------------------------------------------------------------------

def test_exclude_and_include_a_project(home, tmp_path):
    from keepmum.protect import KEY

    project = tmp_path / "proj"
    project.mkdir()
    r = run("exclude", project)
    assert r.exit_code == 0 and f"excluded {project.resolve()}" in r.stderr and "https://api.anthropic.com" in r.stderr
    local = project / ".claude" / "settings.local.json"
    assert json.loads(local.read_text())["env"][KEY] == "https://api.anthropic.com"
    r = run("exclude", "--list")
    assert r.stdout == f"{project.resolve()}  -> https://api.anthropic.com\n"
    r = run("include", project)
    assert r.exit_code == 0 and f"included {project.resolve()} again" in r.stderr
    assert KEY not in json.loads(local.read_text()).get("env", {})
    r = run("include", project)
    assert r.exit_code == 1 and "is not excluded" in r.stderr
    events = [e["event"] for e in audit_entries(home)]
    assert events.count("exclude") == 1 and events.count("include") == 1


def test_exclude_list_marks_a_project_whose_setting_was_removed(home, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    assert run("exclude", project).exit_code == 0
    (project / ".claude" / "settings.local.json").write_text("{}")
    assert run("exclude", "--list").stdout.endswith("(setting removed: goes through keepmum)\n")


def test_exclude_uses_the_upstream_when_claude_codes_settings_cannot_be_read(home, tmp_path):
    from keepmum.protect import KEY

    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{not json")
    home.write_text('[server]\nupstream = "https://llm.corp.example"\n')
    project = tmp_path / "proj"
    project.mkdir()
    r = run("exclude", project)
    assert r.exit_code == 0 and "goes straight to https://llm.corp.example" in r.stderr
    assert json.loads((project / ".claude" / "settings.local.json").read_text())["env"][KEY] == "https://llm.corp.example"


def test_exclude_refuses_a_project_already_sent_elsewhere(home, tmp_path):
    from keepmum.protect import KEY

    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.local.json").write_text(json.dumps({"env": {KEY: "https://other.example"}}))
    r = run("exclude", project)
    assert r.exit_code == 1 and "keepmum exclude:" in r.stderr and "https://other.example" in r.stderr
    assert run("exclude", "--list").stdout == "" and not any(e["event"] == "exclude" for e in audit_entries(home))


# --- status and extensions --------------------------------------------------------------------------

def test_status_reports_problems_instead_of_failing(home, monkeypatch):
    from keepmum import clients, extensions, launch, protect

    def unreadable(port, gateways=None):
        raise protect.ProtectError("settings.json is not valid JSON")

    def broken():
        raise RuntimeError("scan failed")

    monkeypatch.setattr(protect, "status", unreadable)
    monkeypatch.setattr(clients, "protect_clients", lambda action, port, only=None, adopt=None: {})
    monkeypatch.setattr(launch, "shared_serve_port", lambda cfg: None)
    monkeypatch.setattr(extensions, "check", broken)
    r = run("status")
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["protect"] == {"error": "settings.json is not valid JSON"}
    assert out["extensions"] == {"watching": False, "new": [], "error": "RuntimeError"}
    assert out["serve_running"] is False and out["clients"] == {}


def test_extensions_lists_what_went_away_and_what_could_not_be_read(home, monkeypatch):
    from keepmum import extensions

    monkeypatch.setattr(extensions, "ensure_baseline", lambda: False)
    monkeypatch.setattr(extensions, "check", lambda: {
        "watching": True, "items": [], "new": 0, "gone": [{"agent": "Codex", "kind": "hook", "name": "notify"}],
        "problems": ["~/.cursor/mcp.json: not valid JSON"]})
    r = run("extensions")
    assert r.exit_code == 0
    assert r.stdout.startswith("no plugins, hooks or MCP servers in Claude Code")
    assert "gone since you last looked: Codex hook notify" in r.stdout
    assert r.stderr == "not read: ~/.cursor/mcp.json: not valid JSON\n"


def test_extensions_keep_with_nothing_new_records_nothing(home, monkeypatch):
    from keepmum import extensions

    monkeypatch.setattr(extensions, "ensure_baseline", lambda: False)
    monkeypatch.setattr(extensions, "keep", lambda ids=None: 0)
    r = run("extensions", "keep")
    assert r.exit_code == 0 and "nothing new to keep" in r.stderr
    assert audit_entries(home) == []


# --- audit ------------------------------------------------------------------------------------------

def test_audit_on_an_empty_log(home):
    r = run("audit", "list")
    assert r.exit_code == 0 and "no audit entries in that period" in r.stderr
    r = run("audit", "head")
    assert r.exit_code == 1 and "the audit log is empty" in r.stderr
    r = run("audit", "verify", "--json")
    assert r.exit_code == 0 and json.loads(r.stdout)["ok"] is True and json.loads(r.stdout)["entries"] == 0
    r = run("audit", "keep")
    assert r.exit_code == 0 and r.stderr == "the audit log keeps entries for 90 days\n"


def test_audit_list_takes_a_date_no_limit_and_json(home):
    for value in ("a", "b", "c"):
        assert run("rules", "allow", value).exit_code == 0
    entries = json.loads(run("audit", "list", "--since", "2020-01-01", "--limit", "0", "--json").stdout)
    assert [e["seq"] for e in entries] == [1, 2, 3] and all("summary" in e and "weakens" in e for e in entries)
    assert len(json.loads(run("audit", "list", "--since", "", "--limit", "2", "--json").stdout)) == 2  # no --since: all
    future = json.loads(run("audit", "list", "--since", "2999-01-01", "--json").stdout)
    assert future == []
    r = run("audit", "list", "--since", "last week")
    assert r.exit_code == 2 and "--since takes 30d, 12h, 90m or a date" in r.stderr


def test_audit_head_json_is_the_newest_entry(home):
    assert run("rules", "allow", "a").exit_code == 0
    assert run("rules", "allow", "b").exit_code == 0
    last = audit_entries(home)[-1]
    head = json.loads(run("audit", "head", "--json").stdout)
    assert head == {"seq": last["seq"], "hash": last["hash"], "ts": last["ts"]}
    assert run("audit", "head").stdout.strip() == f"{last['seq']}:{last['hash']}"


def test_audit_verify_lists_twenty_problems_then_counts_the_rest(home, tmp_path):
    bad = tmp_path / "export.jsonl"
    bad.write_text("not an entry\n" * 25)
    r = run("audit", "verify", "--file", bad)
    assert r.exit_code == 1 and "AUDIT LOG BROKEN: 25 problem(s)" in r.stderr
    assert r.stderr.count("not a readable entry") == 20 and "… and 5 more" in r.stderr
    bad.write_text("not an entry\n" * 2)
    r = run("audit", "verify", "--file", bad)
    assert r.exit_code == 1 and r.stderr.count("not a readable entry") == 2 and "more" not in r.stderr


def test_audit_export_refuses_an_unknown_format(home, tmp_path):
    r = run("audit", "export", "--format", "xml", "-o", tmp_path / "out.xml")
    assert r.exit_code == 2 and "--format is jsonl or csv" in r.stderr and not (tmp_path / "out.xml").exists()
