"""The command line's error paths, option checks and less-used commands (cli.py, its first part):
each test runs in a HOME of its own, with every model, server, download and child process faked."""

import json
import logging
import tomllib

import pytest
from typer.testing import CliRunner

from keepmum import local, localmodels
from keepmum.cli import app
from keepmum.local import Server

cli = CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A HOME of the test's own, with nothing on localhost answering; returns the config's path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(local, "models", lambda url, timeout=3.0: None)
    monkeypatch.setattr(local, "kind", lambda url, timeout=1.5: "local server")
    path = tmp_path / ".config" / "keepmum" / "config.toml"
    path.parent.mkdir(parents=True)
    return path


def write(path, text):
    path.write_text(text)
    return path


def loaded(path):
    return tomllib.loads(path.read_text())


def no_extensions(monkeypatch, new=()):
    from keepmum import extensions

    monkeypatch.setattr(extensions, "ensure_baseline", lambda: True)
    monkeypatch.setattr(extensions, "check", lambda: {"items": [dict(r, new=True) for r in new]})


class FakeServer:
    """BackgroundServer's shape, with no socket."""

    def __init__(self, proxy, port=0):
        self.proxy, self.port, self.events = proxy, port or 5555, []
        FakeServer.last = self

    def start(self):
        self.events.append("start")

    def stop(self):
        self.events.append("stop")


class FakeProxy:
    upstream = "https://upstream.example"

    def __init__(self):
        self.prefixed = {}


def fake_launch(monkeypatch, proxy=None, runs=None, code=0):
    """build_proxy, log_to_file, BackgroundServer and _run_child faked; runs collects each child."""
    from keepmum import launch

    proxy = proxy or FakeProxy()
    built = []

    def build(cfg, **kw):
        built.append(kw)
        return proxy

    def run_child(command, env):
        runs.append((command, env))
        return code

    monkeypatch.setattr(launch, "build_proxy", build)
    monkeypatch.setattr(launch, "log_to_file", lambda level: None)
    monkeypatch.setattr(launch, "BackgroundServer", FakeServer)
    if runs is not None:
        monkeypatch.setattr(launch, "_run_child", run_child)
    return built


# --- scan, config, pipeline -------------------------------------------------------------------


class FakePipeline:
    def __init__(self, spans=()):
        self.spans, self.seen = list(spans), []

    def detect(self, text):
        self.seen.append(text)
        return self.spans


def test_scan_says_which_file_it_cannot_read_and_skips_binaries(home, tmp_path, monkeypatch):
    from keepmum.detect import pipeline

    fake = FakePipeline()
    monkeypatch.setattr(pipeline, "default_pipeline", lambda **kw: fake)
    binary = tmp_path / "blob.bin"
    binary.write_bytes(b"\0secret=abc")
    r = cli.invoke(app, ["scan", str(binary)])
    assert r.exit_code == 0 and fake.seen == []  # a binary file is never read as text
    r = cli.invoke(app, ["scan", str(tmp_path / "missing.txt")])
    assert r.exit_code == 2 and "missing.txt" in r.output and "No such file" in r.output


def test_a_bad_config_stops_a_command_with_its_reason(home):
    write(home, "[detect\n")
    r = cli.invoke(app, ["pii"])
    assert r.exit_code == 2 and "keepmum: bad config:" in r.output


def test_a_model_that_is_not_there_stops_scan_and_selftest(home, monkeypatch):
    from keepmum.detect import pipeline

    def unavailable(**kw):
        raise pipeline.ModelUnavailable("the model is not downloaded")

    monkeypatch.setattr(pipeline, "default_pipeline", unavailable)
    for command in (["scan", "-"], ["selftest"]):
        r = cli.invoke(app, command, input="x")
        assert r.exit_code == 2 and f"keepmum {command[0]}: the model is not downloaded" in r.output


@pytest.mark.parametrize("ok, code, said", [(True, 0, "selftest passed"), (False, 1, "SELFTEST FAILED")])
def test_selftest_reports_whether_the_canary_leaked(home, monkeypatch, ok, code, said):
    from keepmum import selftest
    from keepmum.detect import pipeline

    fake, got = FakePipeline(), []
    monkeypatch.setattr(pipeline, "default_pipeline", lambda **kw: got.append(kw) or fake)

    async def run(detector, verbose=True):
        assert detector is fake
        return ok

    monkeypatch.setattr(selftest, "run", run)
    r = cli.invoke(app, ["selftest", "--rules-only"])
    assert r.exit_code == code and said in r.output and got[0]["rules_only"] is True


def test_a_settings_write_still_happens_when_the_audit_lock_fails(home, monkeypatch):
    from keepmum.audit import AuditLog

    def broken(self):
        raise PermissionError("audit folder is read only")

    monkeypatch.setattr(AuditLog, "_locked", broken)
    r = cli.invoke(app, ["pii", "on"])
    assert r.exit_code == 0 and loaded(home)["detect"]["pii"] is True


def test_a_settings_write_is_not_audited_when_the_file_then_fails_to_load(home, monkeypatch):
    """Another writer can leave the file unreadable between keepmum's write and its read: the
    audit entry is skipped, and the command still says what it set."""
    from keepmum import audit, config

    real = config.set_value

    def then_broken(section, key, value, path=None):
        real(section, key, value, path)
        path.write_text(path.read_text() + "[broken\n")

    write(home, "[detect]\n")
    monkeypatch.setattr(config, "set_value", then_broken)
    r = cli.invoke(app, ["observe", "on"])
    assert r.exit_code == 0 and "UNMASKED" in r.output
    assert "detect.observe" not in json.dumps(audit.read())


@pytest.mark.parametrize("command", ["observe", "model", "pii"])
def test_on_off_status_commands_check_their_word_and_report_a_failed_write(home, monkeypatch, command):
    from keepmum import config
    from keepmum.detect import pfserve

    monkeypatch.setattr(pfserve, "find_binary", lambda: "/x/pf-serve")
    monkeypatch.setattr(pfserve, "find_model", lambda: "/x/model")
    r = cli.invoke(app, [command, "maybe"])
    assert r.exit_code == 2 and f"keepmum {command}: use on, off or status" in r.output
    r = cli.invoke(app, [command, "status"])
    assert r.exit_code == 0 and "Running proxies" not in r.output

    def fails(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(config, "set_value", fails)
    r = cli.invoke(app, [command, "on"])
    assert r.exit_code == 1 and f"keepmum {command}: disk full" in r.output
    assert not home.exists()


def test_status_words_follow_the_config(home):
    write(home, "[detect]\nobserve = true\nmodel = false\npii = true\n")
    assert "UNMASKED" in cli.invoke(app, ["observe"]).output
    assert "rules only" in cli.invoke(app, ["model"]).output
    assert "personal data: masked" in cli.invoke(app, ["pii"]).output


def test_model_on_needs_the_model_downloaded(home, monkeypatch):
    from keepmum.detect import pfserve

    monkeypatch.setattr(pfserve, "find_binary", lambda: None)
    monkeypatch.setattr(pfserve, "find_model", lambda: None)
    r = cli.invoke(app, ["model", "on"])
    assert r.exit_code == 2 and "keepmum setup" in r.output and not home.exists()


# --- setup ----------------------------------------------------------------------------------------


def test_setup_downloads_the_model_and_shows_progress_once_per_percent(home, monkeypatch, tmp_path):
    from keepmum.detect import pfserve

    def download(progress):
        for done in (0, 1, 2, 500, 1000):  # 1 and 2 bytes are still 0%
            progress(done, 1000)
        return tmp_path / "model.bin"

    monkeypatch.setattr(pfserve, "download_model", download)
    monkeypatch.setattr(pfserve, "find_binary", lambda: tmp_path / "pf-serve")
    r = cli.invoke(app, ["setup"])
    assert r.exit_code == 0 and "model ready:" in r.output
    assert r.output.count("\r  0% of") == 1 and "50% of" in r.output and "100% of" in r.output


def test_setup_without_pf_serve_or_with_a_failed_download_fails(home, monkeypatch, tmp_path):
    from keepmum.detect import pfserve

    monkeypatch.setattr(pfserve, "download_model", lambda progress: tmp_path / "model.bin")
    monkeypatch.setattr(pfserve, "find_binary", lambda: None)
    r = cli.invoke(app, ["setup"])
    assert r.exit_code == 1 and "pf-serve is not installed yet" in r.output

    def fails(progress):
        raise pfserve.PfServeError("checksum mismatch")

    monkeypatch.setattr(pfserve, "download_model", fails)
    r = cli.invoke(app, ["setup"])
    assert r.exit_code == 1 and "keepmum setup: checksum mismatch" in r.output


# --- claude, codex, gemini, agy, serve ---------------------------------------------------------


@pytest.fixture
def unprotected(monkeypatch):
    from keepmum import launch, protect

    monkeypatch.setattr(protect, "is_excluded", lambda path: None)
    monkeypatch.setattr(launch, "bypass_risks", lambda cwd, port=None: [])
    monkeypatch.setattr(launch, "protected", lambda cfg: False)
    no_extensions(monkeypatch)


def test_claude_with_protection_on_needs_claude_on_path(home, monkeypatch, unprotected):
    from keepmum import launch

    monkeypatch.setattr(launch, "protected", lambda cfg: True)
    monkeypatch.setattr(launch, "shared_serve_port", lambda cfg, need="messages": 4321)
    monkeypatch.setattr("shutil.which", lambda name: None)
    r = cli.invoke(app, ["claude"])
    assert r.exit_code == 2 and "`claude` is not on PATH" in r.output


def test_claude_puts_keepmum_in_front_of_a_gateway_already_set(home, monkeypatch, unprotected):
    from keepmum import launch

    built = fake_launch(monkeypatch)
    got = []
    monkeypatch.setattr(launch, "run_claude", lambda proxy, args: got.append(args) or 7)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.corp.example")
    r = cli.invoke(app, ["claude", "-p", "hi"])
    assert r.exit_code == 7 and got == [["-p", "hi"]]
    assert built[0]["upstream"] == "https://gateway.corp.example" and built[0]["mode"] == "claude"


def test_claude_reports_a_launch_failure_or_a_missing_model(home, monkeypatch, unprotected):
    from keepmum import launch
    from keepmum.detect.pipeline import ModelUnavailable

    fake_launch(monkeypatch)

    def missing(proxy, args):
        raise FileNotFoundError("claude: not found")

    monkeypatch.setattr(launch, "run_claude", missing)
    r = cli.invoke(app, ["claude"])
    assert r.exit_code == 2 and "keepmum claude: claude: not found" in r.output

    def unavailable(cfg, **kw):
        raise ModelUnavailable("no model")

    monkeypatch.setattr(launch, "build_proxy", unavailable)
    r = cli.invoke(app, ["claude"])
    assert r.exit_code == 2 and "keepmum claude: no model" in r.output


def test_a_start_tells_of_new_extensions_and_survives_a_broken_check(home, monkeypatch, unprotected):
    from keepmum import extensions, launch

    fake_launch(monkeypatch)
    monkeypatch.setattr(launch, "run_claude", lambda proxy, args: 0)
    items = [{"agent": "Claude Code", "kind": "mcp", "name": f"s{i}"} for i in range(4)]
    no_extensions(monkeypatch, new=items)
    r = cli.invoke(app, ["claude"])
    assert r.exit_code == 0 and "keepmum: 4 new in your coding agents' settings" in r.output
    assert "Claude Code mcp s0, Claude Code mcp s1, Claude Code mcp s2 and more" in r.output

    def broken():
        raise RuntimeError("unreadable settings")

    monkeypatch.setattr(extensions, "check", broken)
    r = cli.invoke(app, ["claude"])
    assert r.exit_code == 0 and "new in your coding agents" not in r.output


@pytest.mark.parametrize("command", ["codex", "gemini", "agy"])
def test_an_agent_that_is_not_installed_is_named(home, monkeypatch, command):
    monkeypatch.setattr("shutil.which", lambda name: None)
    r = cli.invoke(app, [command])
    assert r.exit_code == 2 and f"`{command}` is not on PATH" in r.output


def test_codex_refuses_a_provider_it_cannot_put_keepmum_in_front_of(home, monkeypatch):
    from keepmum import clients
    from keepmum.protect import ProtectError

    no_extensions(monkeypatch)
    monkeypatch.setattr("shutil.which", lambda name: "/bin/" + name)

    def refuse(port, gateways):
        raise ProtectError("its provider is not one keepmum knows")

    monkeypatch.setattr(clients, "codex_run_provider", refuse)
    r = cli.invoke(app, ["codex"])
    assert r.exit_code == 2 and "keepmum codex: its provider is not one keepmum knows" in r.output


def test_codex_runs_its_own_proxy_for_a_gateway_the_config_does_not_know(home, monkeypatch):
    from keepmum import clients

    no_extensions(monkeypatch)
    monkeypatch.setattr("shutil.which", lambda name: "/bin/" + name)
    calls = []

    def provider(port, gateways):
        calls.append((port, dict(gateways)))
        return {"base_url": f"http://127.0.0.1:{port}/corp"}, {"corp": "https://corp.example"}

    monkeypatch.setattr(clients, "codex_run_provider", provider)
    monkeypatch.setattr(clients, "codex_args", lambda port, provider: ["-c", provider["base_url"]])
    runs = []
    built = fake_launch(monkeypatch, runs=runs, code=3)
    r = cli.invoke(app, ["codex", "exec", "hi"])
    assert r.exit_code == 3 and built[0]["gateways"] == {"corp": "https://corp.example"}
    assert runs[0][0] == ["/bin/codex", "-c", "http://127.0.0.1:5555/corp", "exec", "hi"]
    assert calls[-1] == (5555, {"corp": "https://corp.example"}) and FakeServer.last.events == ["start", "stop"]


@pytest.mark.parametrize("command, need, var", [("gemini", "gemini", "GOOGLE_GEMINI_BASE_URL"),
                                                ("agy", "antigravity", "CLOUD_CODE_URL")])
def test_gemini_and_agy_use_the_running_service_when_there_is_one(home, monkeypatch, command, need, var):
    from keepmum import launch

    no_extensions(monkeypatch)
    for k in ("GOOGLE_GEMINI_BASE_URL", "CODE_ASSIST_ENDPOINT", "CLOUD_CODE_URL", "AGY_LLM_GATEWAY_URL",
              "AGY_LLM_GATEWAY_WIRE_PROTOCOL", "AGY_LLM_GATEWAY_PROXY_URL", "AGY_LLM_GATEWAY_CLIENT_CERT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("shutil.which", lambda name: "/bin/" + name)
    asked = []
    monkeypatch.setattr(launch, "shared_serve_port", lambda cfg, need="messages": asked.append(need) or 4321)
    runs = []
    built = fake_launch(monkeypatch, runs=runs, code=0)
    r = cli.invoke(app, [command, "--yolo"])
    assert r.exit_code == 0 and asked == [need] and not built
    command_line, env = runs[0]
    assert command_line == [f"/bin/{command}", "--yolo"] and env[var].startswith("http://127.0.0.1:4321")


def test_serve_starts_logs_to_a_file_and_stops_on_ctrl_c(home, monkeypatch):
    import signal

    from keepmum import extensions, launch

    class Thread:
        def join(self):
            raise KeyboardInterrupt

    class Server(FakeServer):
        def __init__(self, proxy, port=0):
            super().__init__(proxy, port)
            self.thread, self.server = Thread(), type("S", (), {"should_exit": False})()

    def broken():
        raise RuntimeError("cannot read settings")

    monkeypatch.setattr(extensions, "ensure_baseline", broken)  # never stops a start
    built = fake_launch(monkeypatch)
    logged = []
    monkeypatch.setattr(launch, "log_to_file", lambda level: logged.append(level))
    monkeypatch.setattr(launch, "BackgroundServer", Server)
    handlers = {}
    monkeypatch.setattr(signal, "signal", lambda sig, handler: handlers.__setitem__(sig, handler))
    r = cli.invoke(app, ["serve", "--port", "9999", "--log-file", "--rules-only", "--upstream", "https://u.example"])
    assert r.exit_code == 0 and "listening on http://127.0.0.1:9999 -> https://upstream.example" in r.output
    assert logged == ["info"] and Server.last.events == ["start", "stop"]
    assert built[0]["rules_only"] is True and built[0]["upstream"] == "https://u.example" and built[0]["mode"] == "serve"
    handlers[signal.SIGTERM]()
    assert Server.last.server.should_exit is True


def test_serve_logs_to_stderr_and_quietens_the_http_libraries(home, monkeypatch):
    import signal

    from keepmum import launch

    class Thread:
        def join(self):
            pass

    class Server(FakeServer):
        def __init__(self, proxy, port=0):
            super().__init__(proxy, port)
            self.thread, self.server = Thread(), None

    no_extensions(monkeypatch)
    fake_launch(monkeypatch)
    monkeypatch.setattr(launch, "BackgroundServer", Server)
    monkeypatch.setattr(signal, "signal", lambda sig, handler: None)
    configured = []
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: configured.append(kw))
    httpx_log = logging.getLogger("httpx")
    monkeypatch.setattr(httpx_log, "level", httpx_log.level)
    write(home, "[log]\nlevel = \"debug\"\n")
    r = cli.invoke(app, ["serve"])
    assert r.exit_code == 0 and configured[0]["level"] == "DEBUG" and httpx_log.level == logging.WARNING
    assert Server.last.port == 7788


# --- protect -----------------------------------------------------------------------------------


def test_protect_refuses_gemini_and_unknown_words(home):
    r = cli.invoke(app, ["protect", "on", "--client", "gemini"])
    assert r.exit_code == 2 and "run `keepmum gemini` instead" in r.output
    for args in (["protect", "maybe"], ["protect", "on", "--client", "cursor"]):
        r = cli.invoke(app, args)
        assert r.exit_code == 2 and "use on, off or status" in r.output, args


def test_protect_on_reports_each_agent_and_audits_what_changed(home, monkeypatch):
    from keepmum import audit, clients

    result = {"claude": {"installed": True, "on": True, "providers": ["anthropic"], "via": "corp",
                         "warning": "a project setting overrides this"},
              "codex": {"installed": True, "error": "config.toml is not valid"},
              "pi": {"installed": False},
              "opencode": {"installed": True, "on": False, "other_url": ["https://a.example", "https://b.example"]}}
    seen = []
    monkeypatch.setattr(clients, "protect_clients", lambda action, port, only, adopt=None: seen.append((action, only, adopt)) or result)
    r = cli.invoke(app, ["protect", "on"])
    assert r.exit_code == 1  # an agent that failed
    out = r.output
    assert "Claude Code  on: goes through keepmum (anthropic), then corp" in out
    assert "warning: a project setting overrides this" in out
    assert "Codex        error: config.toml is not valid" in out and "pi           not installed" in out
    assert "opencode     off: goes to https://a.example, https://b.example directly" in out
    assert "Claude Code sessions switch now" in out
    assert seen[0][0] == "on" and seen[0][1] is None and seen[0][2] is not None
    entries = [e for e in audit.read() if e.get("event") == "protect"]
    assert entries and entries[-1]["clients"] == ["claude", "opencode"] and entries[-1]["action"] == "on"


def test_protect_off_and_json(home, monkeypatch):
    from keepmum import audit, clients

    result = {"claude": {"installed": True, "on": False}}
    seen = []
    monkeypatch.setattr(clients, "protect_clients", lambda action, port, only, adopt=None: seen.append((only, adopt)) or result)
    r = cli.invoke(app, ["protect", "off", "--client", "claude"])
    assert r.exit_code == 0 and "Claude Code  off\n" in r.output and "keep using keepmum until they restart" in r.output
    assert seen[0] == (["claude"], None)  # nothing is adopted when turning off
    assert [e["action"] for e in audit.read() if e.get("event") == "protect"] == ["off"]
    r = cli.invoke(app, ["protect", "--json"])
    assert r.exit_code == 0 and json.loads(r.stdout) == result
    assert len([e for e in audit.read() if e.get("event") == "protect"]) == 1  # status is not audited


# --- guard status, setup, modes -------------------------------------------------------------


@pytest.fixture
def models_ready(monkeypatch):
    from keepmum import guard, harm, llamacpp

    monkeypatch.setattr(guard, "installed", lambda: True)
    monkeypatch.setattr(harm, "installed", lambda size: True)
    monkeypatch.setattr(llamacpp, "find_server", lambda: "/opt/llama-server")


def test_guard_status_names_the_model_and_the_local_servers(home, models_ready):
    from keepmum import guard

    write(home, '[guard]\ninjection = "local"\nlocal_servers = [{ url = "http://localhost:11434", model = "a" },'
                ' { url = "http://localhost:1234", model = "", checks = ["harm"] }]\n')
    r = cli.invoke(app, ["guard"])
    assert r.exit_code == 0 and f"  model: {guard.ATTRIBUTION}" in r.output
    assert "local servers (for local mode, tried in order): a at http://localhost:11434; " \
           "(no model) at http://localhost:1234 for harm" in r.output
    assert "runtime (both): /opt/llama-server" in r.output


def test_guard_status_in_local_mode_with_no_server_says_how_to_add_one(home, monkeypatch):
    from keepmum import guard, harm, llamacpp

    monkeypatch.setattr(guard, "installed", lambda: False)
    monkeypatch.setattr(harm, "installed", lambda size: False)
    monkeypatch.setattr(llamacpp, "find_server", lambda: None)
    write(home, '[guard]\nharm = "local"\n')
    r = cli.invoke(app, ["guard"])
    assert r.exit_code == 0 and "none (`keepmum guard local add --model <name>`)" in r.output
    assert "not set up (`keepmum guard setup injection`)" in r.output and "llama-server not found" in r.output


def test_guard_setup_checks_its_words(home):
    for args in (["guard", "setup", "commands"], ["guard", "setup", "harm", "--model", "70b"]):
        r = cli.invoke(app, args)
        assert r.exit_code == 2 and "use `injection`, or `harm` with --model" in r.output, args


def test_guard_setup_injection_downloads_audits_and_says_what_is_missing(home, monkeypatch, tmp_path):
    from keepmum import audit, guard, llamacpp

    def download(progress):
        for done in (0, 10, 10, 100):
            progress(done, 100)
        return tmp_path / "guard.gguf"

    monkeypatch.setattr(guard, "download", download)
    monkeypatch.setattr(guard, "installed", lambda: True)
    monkeypatch.setattr(llamacpp, "find_server", lambda: None)
    r = cli.invoke(app, ["guard", "setup", "injection"])
    assert r.exit_code == 0 and "model ready:" in r.output and r.output.count("10% of") == 1
    assert "llama-server is not installed" in r.output and "keepmum guard injection warn" in r.output
    entry = [e for e in audit.read() if e.get("event") == "guard-model"][-1]
    assert entry["model"] == f"{guard.REPO}@{guard.REVISION}" and entry["action"] == "installed"


def test_guard_setup_reports_a_failed_download(home, monkeypatch):
    from keepmum import audit, guard, harm

    def fails(*a):
        raise OSError("connection reset")

    monkeypatch.setattr(guard, "download", fails)
    monkeypatch.setattr(harm, "download", fails)
    for args in (["guard", "setup", "injection"], ["guard", "setup", "harm"]):
        r = cli.invoke(app, args)
        assert r.exit_code == 1 and "keepmum guard setup: connection reset" in r.output, args
    assert not [e for e in audit.read() if e.get("event") == "guard-model"]


def test_guard_setup_harm_with_another_size_says_how_to_switch(home, monkeypatch, models_ready, tmp_path):
    from keepmum import audit, harm

    got = []
    monkeypatch.setattr(harm, "download", lambda size, progress: got.append(size) or tmp_path / f"{size}.gguf")
    r = cli.invoke(app, ["guard", "setup", "harm", "--model", "4b"])
    assert r.exit_code == 0 and got == ["4b"]
    assert "The harm check uses small: `keepmum guard harm warn --model 4b` switches." in r.output
    repo, commit, file, *_ = harm.MODELS["4b"]
    assert [e for e in audit.read() if e.get("event") == "guard-model"][-1]["model"] == f"{repo}@{commit}/{file}"
    assert not home.exists()  # setting up a model does not switch the check


def test_harm_model_set_reads_only_a_readable_file(tmp_path):
    from keepmum.cli import _harm_model_set

    assert _harm_model_set(None) is False
    assert _harm_model_set(tmp_path / "nowhere.toml") is False
    assert _harm_model_set(write(tmp_path / "bad.toml", "[guard\n")) is False
    assert _harm_model_set(write(tmp_path / "ok.toml", '[guard]\nharm_model = "4b"\n')) is True


def test_guard_modes_check_the_word_the_model_and_the_local_server(home, monkeypatch):
    from keepmum import guard, llamacpp

    r = cli.invoke(app, ["guard", "injection", "loud"])
    assert r.exit_code == 2 and "keepmum guard injection: use warn, block, local or off" in r.output
    monkeypatch.setattr(guard, "installed", lambda: False)
    r = cli.invoke(app, ["guard", "injection", "warn"])
    assert r.exit_code == 2 and "keepmum guard setup injection" in r.output
    monkeypatch.setattr(guard, "installed", lambda: True)
    monkeypatch.setattr(llamacpp, "find_server", lambda: None)
    r = cli.invoke(app, ["guard", "injection", "block"])
    assert r.exit_code == 2 and "llama-server is not installed" in r.output
    monkeypatch.setattr(llamacpp, "find_server", lambda: "/opt/llama-server")
    r = cli.invoke(app, ["guard", "injection", "local"])
    assert r.exit_code == 2 and "no local server with a model for the injection check" in r.output
    assert not home.exists()


def test_local_mode_says_why_no_server_can_answer(home, monkeypatch, models_ready):
    write(home, '[guard]\nlocal_servers = [{ url = "http://localhost:11434", model = "a" },'
                ' { url = "http://localhost:1234", model = "b" }]\n')
    monkeypatch.setattr(local, "models", lambda url, timeout=3.0: ["x"] if url.endswith("1234") else None)
    r = cli.invoke(app, ["guard", "injection", "local"])
    assert r.exit_code == 2
    assert "nothing answers at http://localhost:11434; local server at http://localhost:1234 has no model b" in r.output
    assert 'injection = "local"' not in home.read_text()


def test_local_mode_with_a_server_that_answers_adds_the_example_skills_once(home, monkeypatch, models_ready):
    write(home, '[guard]\nlocal_servers = [{ url = "http://localhost:11434", model = "a" }]\n')
    monkeypatch.setattr(local, "models", lambda url, timeout=3.0: ["a"])
    r = cli.invoke(app, ["guard", "injection", "local"])
    assert r.exit_code == 0 and "injection: local" in r.output and "Added keepmum's example skills" in r.output
    cfg = loaded(home)["guard"]
    assert cfg["injection"] == "local" and len(cfg["local_skills"]) == len(local.EXAMPLES)


def test_local_mode_goes_on_even_when_the_example_skills_cannot_be_written(home, monkeypatch, models_ready):
    write(home, '[guard]\nlocal_servers = [{ url = "http://localhost:11434", model = "a" }]\n')
    monkeypatch.setattr(local, "models", lambda url, timeout=3.0: ["a"])

    def fails():
        raise OSError("read-only folder")

    monkeypatch.setattr(local, "add_example", fails)
    r = cli.invoke(app, ["guard", "harm", "local"])
    assert r.exit_code == 0 and "note: keepmum's example skills could not be written: read-only folder" in r.output
    cfg = loaded(home)["guard"]
    assert cfg["harm"] == "local" and "local_skills" not in cfg


def test_a_failed_mode_write_is_reported(home, monkeypatch, models_ready):
    from keepmum import config

    def fails(*a, **kw):
        raise config.ConfigError("cannot write")

    monkeypatch.setattr(config, "set_value", fails)
    r = cli.invoke(app, ["guard", "commands", "warn"])
    assert r.exit_code == 1 and "keepmum guard commands: cannot write" in r.output


# --- guard local: servers ------------------------------------------------------------------------


def test_guard_local_with_no_servers_says_how_to_get_one(home):
    r = cli.invoke(app, ["guard", "local"])
    assert r.exit_code == 0 and "no local servers: `keepmum guard local pull`" in r.output


def test_guard_local_lists_models_problems_and_the_context(home, monkeypatch):
    write(home, '[guard]\nlocal_servers = [{ url = "http://localhost:11434", model = "qwen3:8b" },'
                ' { url = "http://localhost:1234", model = "m", checks = ["harm"], api = "chat" },'
                ' { url = "http://localhost:8080", model = "z" }]\n')
    have = {"http://localhost:11434": ["qwen3:8b"], "http://localhost:1234": []}
    monkeypatch.setattr(local, "models", lambda url, timeout=3.0: have.get(url))
    monkeypatch.setattr(local, "kind", lambda url, timeout=1.5: "Ollama" if url.endswith("11434") else "LM Studio")
    monkeypatch.setattr(localmodels, "show", lambda url, model, timeout=5.0: {"context": 32768, "max": 131072, "parent": None})
    r = cli.invoke(app, ["guard", "local"])
    assert r.exit_code == 0
    out = r.output
    assert "1. qwen3:8b at http://localhost:11434 (Ollama)" in out
    assert "   context: 32k tokens (it takes up to 128k tokens) (`keepmum guard local context`)" in out
    assert "   models there: qwen3:8b" in out
    assert "2. m at http://localhost:1234 (LM Studio), harm only, api chat" in out
    assert "   models there: none yet\n   LM Studio at http://localhost:1234 has no model m" in out
    assert "3. z at http://localhost:8080\n   nothing answers there\n" in out
    assert "A flagged request goes to the first that answers" in out


def test_guard_local_refuses_a_url_off_this_mac(home):
    r = cli.invoke(app, ["guard", "local", "--url", "http://example.com:11434"])
    assert r.exit_code == 2 and "--url must be on this Mac" in r.output and not home.exists()


def test_guard_local_add_and_set_check_their_options(home):
    write(home, '[guard]\nlocal_servers = [{ url = "http://localhost:11434", model = "a" }]\n')
    before = home.read_text()
    for args, said in ((["add", "--preset", "vllm"], "--preset vllm: use ollama, lmstudio, llamacpp, mlx"),
                       (["add", "--api", "grpc"], "--api grpc: use auto, native, chat"),
                       (["set", "1", "--url", "https://example.com"], "--url must be on this Mac"),
                       (["set", "2"], "no server 2 (1 to 1)")):
        r = cli.invoke(app, ["guard", "local", *args])
        assert r.exit_code == 2 and said in r.output, args
    assert home.read_text() == before


def test_a_failed_server_write_is_reported(home, monkeypatch):
    from keepmum import config

    def fails(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(config, "set_value", fails)
    r = cli.invoke(app, ["guard", "local", "add", "--model", "a"])
    assert r.exit_code == 1 and "keepmum guard local: disk full" in r.output


def test_guard_local_picks_as_json(home):
    r = cli.invoke(app, ["guard", "local", "picks", "--json"])
    data = json.loads(r.stdout)
    assert r.exit_code == 0 and data["default_pick"] == localmodels.DEFAULT_PICK
    assert [p["id"] for p in data["picks"]] == [p.id for p in localmodels.PICKS]


def test_removing_the_last_server_of_a_local_check_warns(home, monkeypatch):
    write(home, '[guard]\nharm = "local"\nlocal_servers = [{ url = "http://localhost:11434", model = "a" }]\n')
    r = cli.invoke(app, ["guard", "local", "remove", "1"])
    assert r.exit_code == 0 and "note: the harm check is in local mode with no server that can answer" in r.output
    assert "injection check" not in r.output and loaded(home)["guard"]["local_servers"] == []


# --- guard local pull and context -------------------------------------------------------------


@pytest.fixture
def ollama(monkeypatch):
    """An Ollama at localhost:11434 holding `have`; pulls, copies and deletions recorded."""
    state = {"have": ["base:latest"], "pulled": [], "copies": [], "deleted": []}
    monkeypatch.setattr(local, "models", lambda url, timeout=3.0: state["have"])
    monkeypatch.setattr(local, "kind", lambda url, timeout=1.5: "Ollama")

    def pull(url, name, progress=None, timeout=30.0):
        state["pulled"].append(name)
        for done in (10, 5, 50, 120):  # a later layer can lower the share: never shown going back
            progress(done, 100)

    def with_context(url, model, context, timeout=120.0):
        state["copies"].append((model, context))
        return model if context is None else localmodels.copy_name(model, context)

    monkeypatch.setattr(localmodels, "pull", pull)
    monkeypatch.setattr(localmodels, "with_context", with_context)
    monkeypatch.setattr(localmodels, "delete_copy", lambda url, model: state["deleted"].append(model) or True)
    monkeypatch.setattr(localmodels, "show", lambda url, model, timeout=5.0: None)
    monkeypatch.setattr(localmodels, "base_of", lambda url, model: model)
    return state


def test_pull_checks_its_options(home, ollama):
    for args, said in ((["pull", "x", "--context", "huge"], "huge: give a size"),
                       (["pull", "hf.co/owner/repo", "--quant", "Q4 K"], "--quant Q4 K"),
                       (["pull", "--server", "1"], "no server 1: none is set")):
        r = cli.invoke(app, ["guard", "local", *args])
        assert r.exit_code == 2 and said in r.output, args
    assert ollama["pulled"] == []


def test_pull_with_nothing_answering_says_to_open_ollama(home, monkeypatch, ollama):
    monkeypatch.setattr(local, "models", lambda url, timeout=3.0: None)
    r = cli.invoke(app, ["guard", "local", "pull", "qwen3:8b"])
    assert r.exit_code == 1 and "nothing answers at http://localhost:11434: open the Ollama app" in r.output


def test_pull_shows_progress_only_going_up(home, ollama):
    r = cli.invoke(app, ["guard", "local", "pull", "qwen3:8b", "--no-use"])
    assert r.exit_code == 0 and ollama["pulled"] == ["qwen3:8b"] and "qwen3:8b: downloaded" in r.output
    assert "10% of" in r.output and "5% of" not in r.output and "50% of" in r.output and "100% of" in r.output
    assert ollama["copies"] == [] and not home.exists()  # --no-use: the config is left alone


def test_pull_reports_a_failed_download_or_context(home, monkeypatch, ollama):
    def fails(url, name, progress=None, timeout=30.0):
        raise localmodels.PullError("manifest unknown")

    monkeypatch.setattr(localmodels, "pull", fails)
    r = cli.invoke(app, ["guard", "local", "pull", "qwen3:8b"])
    assert r.exit_code == 1 and "keepmum guard local pull: manifest unknown" in r.output

    def no_context(url, model, context, timeout=120.0):
        raise localmodels.PullError("Ollama answered 500")

    monkeypatch.setattr(localmodels, "with_context", no_context)
    r = cli.invoke(app, ["guard", "local", "pull", "base"])  # already there as base:latest
    assert r.exit_code == 1 and "base: already on Ollama" in r.output
    assert "base is downloaded, but its context could not be set: Ollama answered 500" in r.output
    assert not home.exists()


def test_pull_onto_a_numbered_server_replaces_and_drops_keepmums_old_copy(home, ollama):
    old = "keepmum/old:ctx32k"
    write(home, f'[guard]\nlocal_servers = [{{ url = "http://localhost:11434", model = "{old}" }}]\n')
    ollama["have"] = ["base", old]
    r = cli.invoke(app, ["guard", "local", "pull", "base", "--server", "1", "--context", "16k"])
    assert r.exit_code == 0 and ollama["pulled"] == []
    used = localmodels.copy_name("base", 16384)
    assert loaded(home)["guard"]["local_servers"] == [{"url": "http://localhost:11434", "model": used}]
    assert ollama["deleted"] == [old] and f"local server 1: {used}" in r.output and "16k tokens" in r.output


def test_context_checks_its_size_and_server(home, ollama):
    r = cli.invoke(app, ["guard", "local", "context", "1k"])
    assert r.exit_code == 2 and "from 2k to 1024k" in r.output
    r = cli.invoke(app, ["guard", "local", "context"])
    assert r.exit_code == 1 and "no Ollama server with a model" in r.output
    r = cli.invoke(app, ["guard", "local", "context", "--server", "3"])
    assert r.exit_code == 2 and "no server 3" in r.output


def test_context_needs_an_ollama_that_answers_with_the_model(home, monkeypatch, ollama):
    write(home, '[guard]\nlocal_servers = [{ url = "http://localhost:1234", model = "m" },'
                ' { url = "http://localhost:11434", model = "gone" }]\n')
    monkeypatch.setattr(local, "kind", lambda url, timeout=1.5: "Ollama" if url.endswith("11434") else "LM Studio")
    r = cli.invoke(app, ["guard", "local", "context", "32k", "--server", "1"])
    assert r.exit_code == 1 and "server 1 is not an Ollama that answers; set the context in its own app" in r.output
    r = cli.invoke(app, ["guard", "local", "context", "32k"])  # the first Ollama with a model: server 2
    assert r.exit_code == 1 and "server 2 has no model there yet" in r.output
    assert ollama["copies"] == []


def test_context_shows_the_size_or_reports_a_failed_change(home, monkeypatch, ollama):
    write(home, '[guard]\nlocal_servers = [{ url = "http://localhost:11434", model = "base:latest" }]\n')
    before = home.read_text()
    r = cli.invoke(app, ["guard", "local", "context", "--server", "1"])
    assert r.exit_code == 0 and "1. base:latest: Ollama's own setting: its longest unless you changed it" in r.output

    def fails(url, model, context, timeout=120.0):
        raise localmodels.PullError("model too large")

    monkeypatch.setattr(localmodels, "with_context", fails)
    r = cli.invoke(app, ["guard", "local", "context", "32k"])
    assert r.exit_code == 1 and "keepmum guard local context: model too large" in r.output
    assert home.read_text() == before


# --- guard local skill -------------------------------------------------------------------------


def test_a_failed_skill_write_is_reported(home, monkeypatch, tmp_path):
    from keepmum import config

    skill = tmp_path / "mine" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text("---\nname: mine\ndescription: d\n---\nBe brief.\n")

    def fails(*a, **kw):
        raise config.ConfigError("cannot write")

    monkeypatch.setattr(config, "set_value", fails)
    r = cli.invoke(app, ["guard", "local", "skill", "add", str(skill), "--in-place"])
    assert r.exit_code == 1 and "keepmum guard local skill: cannot write" in r.output


def test_a_skill_that_cannot_be_copied_is_not_added(home, tmp_path):
    skill = tmp_path / "mine" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text("---\nname: mine\ndescription: d\n---\nBe brief.\n")
    local.skills_dir().parent.mkdir(parents=True, exist_ok=True)
    local.skills_dir().write_text("not a folder")
    r = cli.invoke(app, ["guard", "local", "skill", "add", str(skill)])
    assert r.exit_code == 1 and "keepmum guard local skill add: could not copy it to" in r.output
    assert not home.exists()


def test_claude_refuses_to_start_when_it_could_go_around_the_proxy(home, monkeypatch, unprotected):
    from keepmum import launch

    built = fake_launch(monkeypatch)
    monkeypatch.setattr(launch, "bypass_risks", lambda cwd, port=None: ["ANTHROPIC_BASE_URL is set in .claude/settings.json"])
    r = cli.invoke(app, ["claude"])
    assert r.exit_code == 2 and "refusing to start, Claude Code could bypass the proxy:\n  ANTHROPIC_BASE_URL" in r.output
    assert not built


def test_protect_status_changes_and_audits_nothing(home, monkeypatch):
    from keepmum import audit, clients

    result = {"claude": {"installed": True, "on": True}, "codex": {"installed": False}}
    monkeypatch.setattr(clients, "protect_clients", lambda action, port, only, adopt=None: result)
    r = cli.invoke(app, ["protect"])
    assert r.exit_code == 0 and "Claude Code  on: goes through keepmum\n" in r.output
    assert "switch now" not in r.output and "until they restart" not in r.output
    result["claude"] = {"installed": True, "error": "unreadable"}
    r = cli.invoke(app, ["protect", "on"])  # nothing changed: nothing to audit
    assert r.exit_code == 1 and not [e for e in audit.read() if e.get("event") == "protect"]


def test_harm_needs_its_model_downloaded(home, monkeypatch, models_ready):
    from keepmum import harm

    monkeypatch.setattr(harm, "installed", lambda size: size != "4b")
    r = cli.invoke(app, ["guard", "harm", "warn", "--model", "4b"])
    assert r.exit_code == 2 and "the 4b model is not downloaded: run `keepmum guard setup harm --model 4b` first" in r.output


def test_guard_setup_harm_of_the_size_in_use_does_not_suggest_a_switch(home, monkeypatch, models_ready, tmp_path):
    from keepmum import harm

    monkeypatch.setattr(harm, "download", lambda size, progress: tmp_path / f"{size}.gguf")
    r = cli.invoke(app, ["guard", "setup", "harm"])
    assert r.exit_code == 0 and "switches" not in r.output and "`keepmum guard harm warn`" in r.output


def test_guard_local_with_one_server_says_nothing_of_order(home):
    write(home, '[guard]\nlocal_servers = [{ url = "http://localhost:8080", model = "z" }]\n')
    r = cli.invoke(app, ["guard", "local"])
    assert r.exit_code == 0 and "1. z at http://localhost:8080" in r.output and "first that answers" not in r.output
