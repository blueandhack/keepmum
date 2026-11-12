"""`keepmum doctor`: what it reports for each situation on the user's Mac.

Everything doctor probes is faked: the macOS version and CPU, pf-serve and its model, the
coding agents on PATH, `claude --version` and `sysctl`, the port check, the running service,
the agents' protect status, the extensions scan and the optional checks' models. HOME is a
temporary folder, and no Claude Code settings outside it are read."""

import json
import os
import pathlib
import platform
import subprocess
from types import SimpleNamespace

import pytest

from keepmum import doctor, extensions, guard, harm, launch, llamacpp, local
from keepmum.audit import AuditLog, audit_file
from keepmum.detect import pfserve

REAL_FIND = doctor._find
CLAUDE_SETTINGS = ("settings.json", "settings.local.json", "managed-settings.json")


class FakeSocket:
    def __init__(self, machine):
        self.machine = machine

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def connect_ex(self, address):
        self.machine.probed.append(address)
        return 0 if self.machine.port_in_use else 61  # 61: ECONNREFUSED on macOS


class FakePf:
    """pf-serve: finds the canary where a real model would, or misses it by one character."""

    device = "Metal"

    def __init__(self, machine, binary, model):
        if machine.pf_error:
            raise machine.pf_error
        self.closed = False
        self.machine = machine
        machine.pf_started.append(self)

    def detect(self, text):
        start = text.index(" is ") + 4
        end = text.index(",", start)
        return [SimpleNamespace(start=start, end=end if self.machine.pf_catches else end - 1)]

    def close(self):
        self.closed = True


@pytest.fixture
def mac(tmp_path, monkeypatch):
    """A healthy Mac: macOS 15 on Apple silicon, the model and Claude Code installed, nothing in
    the way. Each test changes one thing and looks at what doctor says."""
    home, proj = tmp_path / "home", tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(proj)
    for k in (*launch.BYPASS_KEYS, "CLAUDE_CONFIG_DIR", "XDG_DATA_HOME", "KEEPMUM_LLAMA_SERVER"):
        monkeypatch.delenv(k, raising=False)

    real_read = pathlib.Path.read_text

    def read_text(self, *a, **kw):  # Claude Code settings outside the test's folder are never read
        if self.name in CLAUDE_SETTINGS and not self.resolve().is_relative_to(tmp_path.resolve()):
            raise FileNotFoundError(str(self))
        return real_read(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "read_text", read_text)

    m = SimpleNamespace(home=home, proj=proj, macos="15.1", cpu="arm64", binary=tmp_path / "pf-serve",
                        model=tmp_path / "model.gguf", pf_catches=True, pf_error=None, pf_started=[],
                        agents={"claude": "/fake/bin/claude"}, claude_version="2.1.0 (Claude Code)", commands=[],
                        port=17788, port_in_use=False, probed=[], serve_port=None, clients={},
                        extensions={"items": [], "watching": True, "problems": []}, memsize=48 * 2**30,
                        injection_installed=True, harm_installed=True, llama_server=tmp_path / "llama-server",
                        local_models={}, local_kind="Ollama")
    m.cfg_dir = home / ".config" / "keepmum"
    m.cfg_dir.mkdir(parents=True)

    def config(text=""):
        f = m.cfg_dir / "config.toml"
        f.write_text(f"[server]\nport = {m.port}\n" + text)
        f.chmod(0o600)
        return f

    m.config = config
    config()

    monkeypatch.setattr(platform, "mac_ver", lambda: (m.macos, ("", "", ""), ""))
    monkeypatch.setattr(platform, "machine", lambda: m.cpu)
    monkeypatch.setattr(pfserve, "find_binary", lambda: m.binary)
    monkeypatch.setattr(pfserve, "find_model", lambda: m.model)
    monkeypatch.setattr(pfserve, "PfServe", lambda binary, model: FakePf(m, binary, model))
    monkeypatch.setattr(doctor, "_find", lambda name: m.agents.get(name))

    def run(cmd, **kw):
        m.commands.append(cmd)
        if cmd[0] == "sysctl":
            return subprocess.CompletedProcess(cmd, 0, stdout=str(m.memsize))
        if cmd[1:] == ["--version"]:
            if isinstance(m.claude_version, Exception):
                raise m.claude_version
            return subprocess.CompletedProcess(cmd, 0, stdout=m.claude_version + "\n")
        if cmd[0] == "git":  # the project folder is not a git repository
            return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="fatal: not a git repository")
        raise AssertionError(f"doctor ran {cmd}")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(doctor, "socket", SimpleNamespace(socket=lambda: FakeSocket(m)))
    monkeypatch.setattr(launch, "shared_serve_port", lambda cfg, need="messages": m.serve_port)

    import keepmum.clients

    monkeypatch.setattr(keepmum.clients, "protect_clients", lambda action, port: m.clients)

    def check():
        if isinstance(m.extensions, Exception):
            raise m.extensions
        return m.extensions

    monkeypatch.setattr(extensions, "check", check)
    monkeypatch.setattr(guard, "installed", lambda: m.injection_installed)
    monkeypatch.setattr(harm, "installed", lambda size: m.harm_installed)
    monkeypatch.setattr(llamacpp, "find_server", lambda: m.llama_server)
    monkeypatch.setattr(local, "models", lambda url: m.local_models.get(url))
    monkeypatch.setattr(local, "kind", lambda url: m.local_kind)
    return m


def checks():
    return doctor.checks()


def line(results, text):
    """The (status, text) of the one check that mentions `text`."""
    found = [(s, t) for s, t in results if text in t]
    assert len(found) == 1, f"{text!r} in {found or results}"
    return found[0]


def failed(results):
    return [t for s, t in results if s == doctor.FAIL]


# --- The whole report -----------------------------------------------------------------------


def test_a_healthy_mac_passes_every_check(mac, capsys):
    r = checks()
    assert failed(r) == [] and [t for s, t in r if s == doctor.WARN] == []
    assert line(r, "macOS 15.1")[0] == doctor.OK and line(r, "CPU arm64")[0] == doctor.OK
    assert line(r, "model runs on Metal and catches a canary")[0] == doctor.OK
    assert mac.pf_started[0].closed  # the model process is stopped after the check
    assert line(r, "Claude Code 2.1.0 (Claude Code)")[0] == doctor.OK
    assert line(r, "nothing routes Claude Code around keepmum")[0] == doctor.OK
    assert line(r, "protection off")[0] == doctor.OK
    assert line(r, f"port {mac.port} free")[0] == doctor.OK
    assert mac.probed == [("127.0.0.1", mac.port)]  # the configured port, not the default
    assert line(r, "install salt not created yet")[0] == doctor.OK
    assert line(r, "audit log not started yet")[0] == doctor.OK
    # read-only: doctor creates nothing in HOME
    assert sorted(p.name for p in mac.cfg_dir.iterdir()) == ["config.toml"]

    assert doctor.run() == 0
    out = capsys.readouterr().out
    assert out.endswith("all checks passed\n") and f"  {doctor.OK} macOS 15.1 (14 or later)\n" in out


def test_an_old_mac_or_intel_cpu_fails_and_the_exit_code_says_so(mac, capsys):
    mac.macos, mac.cpu = "13.6", "x86_64"
    r = checks()
    assert line(r, "macOS 13.6")[0] == doctor.FAIL
    assert line(r, "CPU x86_64 (Apple silicon required)")[0] == doctor.FAIL
    assert doctor.run() == 1
    assert capsys.readouterr().out.endswith("2 check(s) failed\n")


def test_not_macos_at_all_reports_unknown(mac):
    mac.macos = ""
    assert line(checks(), "macOS unknown")[0] == doctor.FAIL


def test_json_report_names_statuses(mac, capsys):
    mac.cpu = "x86_64"
    assert doctor.run(as_json=True) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["failed"] == 1
    assert {"status": "fail", "text": "CPU x86_64 (Apple silicon required)"} in report["checks"]


# --- Config ---------------------------------------------------------------------------------


def test_a_bad_config_fails_and_skips_what_needs_it(mac):
    (mac.cfg_dir / "config.toml").write_text("[detect]\njudge = true\n")
    r = checks()
    status, text = line(r, "config:")
    assert status == doctor.FAIL and "unknown key detect.judge" in text
    assert mac.probed == [("127.0.0.1", 7788)]  # falls back to the default port
    assert not any(c[0] == "sysctl" for c in mac.commands)  # the optional checks need a config
    assert line(r, "protection off")[0] == doctor.OK


def test_no_config_file_means_defaults(mac):
    (mac.cfg_dir / "config.toml").unlink()
    assert line(checks(), "config (none: defaults); pii=off")[0] == doctor.OK


def test_config_shows_its_path_and_pii(mac):
    path = mac.config("[detect]\npii = true\n")
    assert line(checks(), f"config {path}; pii=on")[0] == doctor.OK


# --- The model ------------------------------------------------------------------------------


def test_missing_model_and_binary_fail_when_the_model_is_on(mac):
    mac.binary = mac.model = None
    r = checks()
    assert line(r, "pf-serve missing: run native/pf-serve/build.sh")[0] == doctor.FAIL
    assert line(r, "model missing: run `keepmum setup`")[0] == doctor.FAIL
    assert mac.pf_started == []


def test_with_the_model_off_missing_files_only_warn(mac):
    mac.config("[detect]\nmodel = false\n")
    mac.model = None
    r = checks()
    assert line(r, "model off: the rules work alone")[0] == doctor.OK
    assert line(r, "model missing")[0] == doctor.WARN
    assert line(r, "pf-serve")[0] == doctor.OK
    assert failed(r) == []


def test_a_model_that_misses_the_canary_fails(mac):
    mac.pf_catches = False
    r = checks()
    assert line(r, "model loaded but missed the canary")[0] == doctor.FAIL
    assert mac.pf_started[0].closed


def test_a_model_that_will_not_start_fails_with_the_reason(mac):
    mac.pf_error = RuntimeError("pf-serve exited with code 1")
    assert line(checks(), "model did not run (pf-serve exited with code 1)")[0] == doctor.FAIL


# --- Files: salt, private files, audit log --------------------------------------------------


@pytest.mark.parametrize("mode, status", [(0o600, doctor.OK), (0o644, doctor.FAIL)])
def test_install_salt_must_be_private(mac, mode, status):
    salt = mac.cfg_dir / "salt"
    salt.write_bytes(b"x" * 32)
    salt.chmod(mode)
    assert line(checks(), f"install salt in {salt} (mode {mode:o})")[0] == status


def test_private_files_readable_by_others_fail(mac):
    token = mac.cfg_dir / "stats-token"
    token.write_text("tok")
    token.chmod(0o644)
    assert line(checks(), f"{token} is readable by other users (mode 644)")[0] == doctor.FAIL
    token.chmod(0o600)
    assert not any("readable by other users" in t for _, t in checks())


def test_a_config_readable_by_others_only_warns(mac):
    (mac.cfg_dir / "config.toml").chmod(0o644)
    status, text = line(checks(), "readable by other users")
    assert status == doctor.WARN and "chmod 600 recommended" in text


def test_an_intact_audit_log_passes_and_an_edited_one_fails(mac):
    log = AuditLog()
    log.append("start", mode="serve")
    log.append("stop", mode="serve")
    assert line(checks(), "audit log intact (2 entries)")[0] == doctor.OK

    f = audit_file()
    lines = f.read_text().splitlines()
    entry = json.loads(lines[0])
    entry["mode"] = "vscode"
    f.write_text("\n".join([json.dumps(entry), *lines[1:]]) + "\n")
    status, text = line(checks(), "audit log changed outside keepmum")
    assert status == doctor.FAIL and "seq 1" in text and "`keepmum audit verify`" in text


# --- Coding agents --------------------------------------------------------------------------


def test_claude_without_a_version_is_still_found(mac):
    mac.claude_version = ""
    assert line(checks(), "Claude Code found")[0] == doctor.OK


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired(["claude"], 20), OSError("exec format error")])
def test_claude_version_failing_only_warns(mac, error):
    mac.claude_version = error
    assert line(checks(), "`claude --version` failed")[0] == doctor.WARN


def test_no_claude_but_another_agent_warns(mac):
    mac.agents = {"codex": "/fake/bin/codex"}
    assert line(checks(), "`claude` not on PATH (other coding agents found)")[0] == doctor.WARN


def test_no_agent_at_all_fails(mac):
    mac.agents = {}
    assert line(checks(), "`claude` not on PATH")[0] == doctor.FAIL


def test_find_prefers_path_then_installer_folders(tmp_path, monkeypatch):
    import shutil

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(shutil, "which", lambda name: "/on/path/claude" if name == "claude" else None)
    assert REAL_FIND("claude") == "/on/path/claude"
    bun = tmp_path / ".bun" / "bin" / "opencode"
    bun.parent.mkdir(parents=True)
    bun.write_text("#!/bin/sh\n")
    bun.chmod(0o755)
    real_access = os.access
    # Only this test's folder: the Mac's own /opt/homebrew/bin and /usr/local/bin are not looked at.
    monkeypatch.setattr(os, "access", lambda p, mode: str(p).startswith(str(tmp_path)) and real_access(p, mode))
    assert REAL_FIND("opencode") == str(bun)
    bun.chmod(0o644)  # there, but not executable
    assert REAL_FIND("opencode") is None


# --- Routes around keepmum and protection ---------------------------------------------------


def test_a_project_setting_that_routes_around_keepmum_fails(mac):
    (mac.proj / ".claude").mkdir()
    (mac.proj / ".claude" / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://elsewhere"}}))
    status, text = line(checks(), "routes around keepmum")
    assert status == doctor.FAIL and "sets env ANTHROPIC_BASE_URL" in text


def test_an_excluded_project_warns(mac, monkeypatch):
    from keepmum import protect

    monkeypatch.setattr(protect, "is_excluded", lambda path: "https://api.anthropic.com")
    status, text = line(checks(), "this project is excluded")
    assert status == doctor.WARN and "straight to https://api.anthropic.com" in text


def _protect_on(mac):
    settings = mac.home / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{mac.port}"}}))


def test_protection_on_but_the_service_down_fails(mac):
    _protect_on(mac)
    r = checks()
    assert line(r, "keepmum serve is not running: Claude Code cannot connect")[0] == doctor.FAIL
    assert line(r, "nothing routes Claude Code around keepmum")[0] == doctor.OK  # keepmum's own URL is no risk


def test_protection_on_with_the_service_up_passes(mac):
    _protect_on(mac)
    mac.serve_port = mac.port
    assert line(checks(), "protection on: every Claude Code session goes through keepmum serve")[0] == doctor.OK


def test_agents_status_lines(mac):
    mac.clients = {"claude": {"installed": True, "on": False},
                   "codex": {"installed": True, "on": True, "via": "https://gw.corp.example"},
                   "pi": {"installed": True, "on": True},  # straight to keepmum: no line of its own
                   "opencode": {"installed": True, "on": False, "around": "https://gw.corp.example"}}
    r = checks()
    assert line(r, "Codex goes through keepmum, then https://gw.corp.example")[0] == doctor.OK
    status, text = line(r, "opencode goes to https://gw.corp.example directly, around keepmum")
    assert status == doctor.FAIL and "`keepmum protect on`" in text
    assert not any(t.startswith("pi ") for _, t in r)


def test_anthropic_base_url_in_the_environment_warns(mac, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gw.corp.example")
    assert line(checks(), "ANTHROPIC_BASE_URL is set")[0] == doctor.WARN


# --- Agent extensions -----------------------------------------------------------------------


def test_extensions_before_keepmum_first_looks(mac):
    mac.extensions = {"items": [{"new": False}] * 3, "watching": False, "problems": []}
    status, text = line(checks(), "agent extensions: 3 in your agents' settings; keepmum starts watching")
    assert status == doctor.OK


def test_new_extensions_and_problems_warn(mac):
    mac.extensions = {"watching": True, "problems": ["~/.codex/config.toml is not valid TOML"], "items": [
        {"new": True, "agent": "Codex", "kind": "hook", "name": "postman", "adds_context": True, "id": "codex-hook-1"},
        {"new": False, "agent": "Claude Code", "kind": "plugin", "name": "old", "adds_context": False, "id": "x"}]}
    r = checks()
    status, text = line(r, "new Codex hook postman")
    assert status == doctor.WARN and "adds instructions to every session" in text
    assert "`keepmum extensions remove codex-hook-1`" in text
    assert line(r, "not valid TOML")[0] == doctor.WARN
    assert not any("none new since" in t for _, t in r)


def test_no_new_extensions_passes(mac):
    mac.extensions = {"watching": True, "problems": [], "items": [{"new": False}] * 2}
    assert line(checks(), "agent extensions: 2 in your agents' settings, none new")[0] == doctor.OK


def test_unreadable_extensions_only_warn(mac):
    mac.extensions = PermissionError("denied")
    assert line(checks(), "could not read the agents' settings (PermissionError)")[0] == doctor.WARN


# --- The port and the running keepmum -------------------------------------------------------


def _runtime(mac, **info):
    f = launch.runtime_file()
    f.write_text(json.dumps(info))
    f.chmod(0o600)


def test_a_port_in_use_by_something_else_warns(mac):
    mac.port_in_use = True
    assert line(checks(), f"port {mac.port} in use for `keepmum serve`")[0] == doctor.WARN


def test_a_port_in_use_by_keepmum_itself_is_fine(mac):
    mac.port_in_use = True
    _runtime(mac, pid=4242, port=mac.port)
    r = checks()
    assert line(r, f"keepmum running (pid 4242, port {mac.port})")[0] == doctor.OK
    assert not any(f"port {mac.port}" in t and "keepmum serve" in t for _, t in r)


def test_keepmum_running_on_another_port_still_checks_this_one(mac):
    _runtime(mac, pid=4242, port=9999)
    r = checks()
    assert line(r, "keepmum running (pid 4242, port 9999)")[0] == doctor.OK
    assert line(r, f"port {mac.port} free")[0] == doctor.OK


def test_a_broken_runtime_file_is_ignored(mac):
    launch.runtime_file().write_text("{not json")
    launch.runtime_file().chmod(0o600)
    r = checks()
    assert not any("keepmum running" in t for _, t in r) and line(r, f"port {mac.port} free")[0] == doctor.OK


# --- Optional checks ------------------------------------------------------------------------


def test_optional_checks_off_say_nothing(mac):
    r = checks()
    assert not any(k in t for _, t in r for k in ("injection check", "harm check", "commands check", "local mode"))


def test_injection_check_ready(mac):
    mac.config('[guard]\ninjection = "block"\n')
    status, text = line(checks(), "injection check block")
    assert status == doctor.OK and "Prompt Injection Guard Small" in text and doctor.GUARD_MEMORY["injection"] in text


def test_injection_check_without_its_model_fails(mac):
    mac.config('[guard]\ninjection = "warn"\n')
    mac.injection_installed = False
    assert line(checks(), "injection check warn: model not set up (`keepmum guard setup injection`)")[0] == doctor.FAIL


def test_injection_check_without_llama_server_fails(mac):
    mac.config('[guard]\ninjection = "warn"\n')
    mac.llama_server = None
    assert line(checks(), "injection check warn: llama-server not found")[0] == doctor.FAIL


def test_commands_check_needs_no_model(mac):
    from keepmum.commands import RULES, SENDS

    mac.config('[guard]\ncommands = "warn"\n')
    status, text = line(checks(), "commands check warn")
    assert status == doctor.OK and f"{len(RULES) + len(SENDS)} rules" in text and "no model" in text


def test_harm_check_ready(mac):
    mac.config('[guard]\nharm = "block"\nharm_model = "small"\n')
    status, text = line(checks(), "harm check block")
    assert status == doctor.OK and harm.NAMES["small"] in text and "0.4 GB while in use" in text


def test_harm_check_not_downloaded_fails_with_the_command(mac):
    mac.config('[guard]\nharm = "warn"\nharm_model = "0.6b"\n')
    mac.harm_installed = False
    status, text = line(checks(), "harm check warn")
    assert status == doctor.FAIL and "`keepmum guard setup harm --model 0.6b`" in text


def test_harm_check_without_llama_server_fails(mac):
    mac.config('[guard]\nharm = "warn"\nharm_model = "small"\n')
    mac.llama_server = None
    assert line(checks(), "harm check warn: llama-server not found")[0] == doctor.FAIL


@pytest.mark.parametrize("gb, status", [(16, doctor.WARN), (64, doctor.OK)])
def test_the_4b_harm_model_warns_on_a_small_mac(mac, gb, status):
    mac.config('[guard]\nharm = "block"\nharm_model = "4b"\n')
    mac.memsize = gb * 2**30
    found, text = line(checks(), "harm check block")
    assert found == status and ("a lot for this Mac's 16 GB" in text) == (gb == 16)


def test_local_mode_without_a_server_fails(mac):
    mac.config('[guard]\nharm = "local"\n')
    status, text = line(checks(), "local mode (harm)")
    assert status == doctor.FAIL and "no local server with a model for it" in text


LOCAL = '[guard]\ninjection = "local"\nharm = "local"\nlocal_servers = [{ url = "http://localhost:11434", model = "qwen3:8b" }]\n'


def test_local_mode_with_a_ready_server_passes(mac):
    mac.config(LOCAL)
    mac.local_models = {"http://localhost:11434": ["qwen3:8b"]}
    r = checks()
    for check in ("injection", "harm"):
        status, text = line(r, f"local mode ({check}), 1 of 1")
        assert status == doctor.OK and "qwen3:8b at http://localhost:11434, without tools" in text
    # the injection check still flags with its own model; local mode only answers what it flags
    assert line(r, "injection check local: Prompt Injection Guard Small")[0] == doctor.OK


def test_local_mode_with_the_model_not_pulled_warns_then_fails(mac):
    mac.config(LOCAL)
    mac.local_models = {"http://localhost:11434": ["llama3:8b"]}
    r = checks()
    status, text = line(r, "local mode (harm), 1 of 1")
    assert status == doctor.WARN and "Ollama at http://localhost:11434 has no model qwen3:8b: `ollama pull qwen3:8b`" in text
    assert line(r, "local mode (harm): none of its local servers can answer")[0] == doctor.FAIL


def test_local_mode_with_nothing_listening(mac):
    mac.config(LOCAL)
    r = checks()  # models() finds no server
    assert "nothing answers at http://localhost:11434" in line(r, "local mode (injection), 1 of 1")[1]
    assert line(r, "local mode (injection): none of its local servers can answer")[0] == doctor.FAIL


def test_local_mode_with_both_old_and_new_keys_warns(mac):
    mac.config(LOCAL + 'local_url = "http://localhost:1234"\n')
    mac.local_models = {"http://localhost:11434": ["qwen3:8b"]}
    assert line(checks(), "only local_servers is used")[0] == doctor.WARN


# --- Small helpers --------------------------------------------------------------------------


def test_guard_keys_both(tmp_path):
    f = tmp_path / "config.toml"
    assert not doctor.guard_keys_both(None)
    assert not doctor.guard_keys_both(f)  # no such file
    f.write_text("[guard\n")
    assert not doctor.guard_keys_both(f)  # not TOML
    f.write_text('[guard]\nlocal_url = "http://localhost:1"\n')
    assert not doctor.guard_keys_both(f)
    f.write_text('[guard]\nlocal_servers = []\nlocal_model = "m"\n')
    assert doctor.guard_keys_both(f)


def test_restore_settings_that_reach_every_server_warn(mac):
    mac.config('[restore]\n"mcp__*" = "restore"\n"mcp__claude_ai_*" = ["EMAIL"]\n"mcp__files__*" = "restore"\n')
    r = checks()
    assert line(r, "restore.mcp__* gives every value, secrets included")[0] == doctor.WARN
    assert line(r, "restore.mcp__claude_ai_* gives EMAIL to every tool")[0] == doctor.WARN
    assert not any("restore.mcp__files__*" in t for _, t in r)  # one server's own setting is fine


def test_a_server_whose_tools_changed_is_reported(monkeypatch):
    import keepmum.tools

    class Registry:
        def snapshot(self, policy):
            return [{"label": "files", "paused": True, "changed": ["read", "write"]},
                    {"label": "github", "paused": False, "changed": []}]

    monkeypatch.setattr(keepmum.tools, "ToolRegistry", Registry)
    out = doctor._tool_warnings({"Bash": "keep", "mcp__*": "keep"})
    assert out == ["files: its tools changed (read, write), so it gets placeholders only; "
                   "`keepmum tools trust files` once you have checked them"]
