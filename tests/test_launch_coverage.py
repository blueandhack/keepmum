"""launch.py beyond the end-to-end runs in test_launch.py: the runtime file, the service lookup,
signal forwarding, and every way the VS Code wrapper decides what to do (run in-process, with
the child process, the proxy and the service faked)."""

import json
import logging
import os
import pathlib
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from keepmum import config as config_mod
from keepmum import launch
from keepmum.audit import AuditLog, read
from keepmum.config import Config
from keepmum.detect.pipeline import ModelUnavailable

CLAUDE_SETTINGS = ("settings.json", "settings.local.json", "managed-settings.json")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """HOME and the working folder in tmp_path; no Claude Code settings outside it are read."""
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(proj)
    for k in (*launch.BYPASS_KEYS, "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(k, raising=False)
    real_read = pathlib.Path.read_text

    def read_text(self, *a, **kw):
        if self.name in CLAUDE_SETTINGS and not self.resolve().is_relative_to(tmp_path.resolve()):
            raise FileNotFoundError(str(self))
        return real_read(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "read_text", read_text)
    return home


def user_settings(home, env):
    f = home / ".claude" / "settings.json"
    f.parent.mkdir(exist_ok=True)
    f.write_text(json.dumps({"env": env}) if isinstance(env, dict) else env)
    return f


# --- bypass_risks and protected -------------------------------------------------------------


def test_bypass_risks_names_a_gateway_in_user_settings(home):
    user_settings(home, {"ANTHROPIC_BASE_URL": "https://gw.corp.example"})
    [risk] = launch.bypass_risks(pathlib.Path.cwd(), 7788)
    assert "sets env ANTHROPIC_BASE_URL to https://gw.corp.example" in risk
    assert "`keepmum protect on` puts keepmum in front of it" in risk


def test_bypass_risks_accepts_keepmum_itself_only_on_its_port(home):
    user_settings(home, {"ANTHROPIC_BASE_URL": "http://127.0.0.1:7788"})
    assert launch.bypass_risks(pathlib.Path.cwd(), 7788) == []
    assert len(launch.bypass_risks(pathlib.Path.cwd(), 9999)) == 1  # another port is not ours
    assert len(launch.bypass_risks(pathlib.Path.cwd())) == 1  # nor with no port to compare


def test_bypass_risks_skips_settings_without_an_env_object(home):
    user_settings(home, {"x": 1})
    (home / ".claude" / "settings.json").write_text(json.dumps({"env": ["ANTHROPIC_BASE_URL"]}))
    (pathlib.Path.cwd() / ".claude").mkdir()
    (pathlib.Path.cwd() / ".claude" / "settings.local.json").write_text("[1, 2]")  # not an object
    assert launch.bypass_risks(pathlib.Path.cwd(), 7788) == []


def test_bypass_risks_allows_the_exclusion_keepmum_wrote(home, monkeypatch):
    from keepmum import protect

    proj = pathlib.Path.cwd()
    (proj / ".claude").mkdir()
    local = proj / ".claude" / "settings.local.json"
    local.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}}))
    monkeypatch.setattr(protect, "is_excluded", lambda path: None)
    assert len(launch.bypass_risks(proj, 7788)) == 1  # not excluded: a risk
    monkeypatch.setattr(protect, "is_excluded", lambda path: "https://api.anthropic.com")
    assert launch.bypass_risks(proj, 7788) == []  # `keepmum exclude` put it there
    local.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://other.example"}}))
    assert len(launch.bypass_risks(proj, 7788)) == 1  # a different URL is still a risk


def test_protected_follows_claude_settings(home):
    cfg = Config(port=7788)
    assert launch.protected(cfg) is False  # no settings file
    user_settings(home, {"ANTHROPIC_BASE_URL": "http://127.0.0.1:7788"})
    assert launch.protected(cfg) is True
    user_settings(home, "{not json")
    assert launch.protected(cfg) is False  # a broken file is not protection


# --- The runtime file -----------------------------------------------------------------------


def test_pid_alive():
    assert launch._pid_alive(os.getpid())
    assert not launch._pid_alive(True) and not launch._pid_alive(False)  # bools are not pids
    assert not launch._pid_alive(0) and not launch._pid_alive(-1)


@pytest.mark.parametrize("error, alive", [(ProcessLookupError, False), (PermissionError, True)])
def test_pid_alive_when_kill_refuses(monkeypatch, error, alive):
    def kill(pid, sig):
        assert sig == 0  # only ever probed, never signalled
        raise error

    monkeypatch.setattr(os, "kill", kill)
    assert launch._pid_alive(12345) is alive


def _runtime(**info):
    f = launch.runtime_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(info))
    return f


def test_a_running_service_keeps_the_runtime_file(home):
    f = _runtime(pid=os.getppid(), port=7788, mode="serve")
    launch._write_runtime(5555, "vscode")
    assert json.loads(f.read_text())["port"] == 7788  # still points at the service


def test_a_dead_service_or_broken_file_is_replaced(home, monkeypatch):
    monkeypatch.setattr(launch, "_pid_alive", lambda pid: False)
    f = _runtime(pid=4242, port=7788, mode="serve")
    launch._write_runtime(5555, "vscode")
    info = json.loads(f.read_text())
    assert info["port"] == 5555 and info["pid"] == os.getpid() and info["mode"] == "vscode"
    assert f.stat().st_mode & 0o777 == 0o600
    f.write_text("{oops")
    launch._write_runtime(6666)
    assert json.loads(f.read_text())["port"] == 6666 and "mode" not in json.loads(f.read_text())


def test_serve_always_writes_the_runtime_file(home):
    f = _runtime(pid=os.getppid(), port=7788, mode="serve")
    launch._write_runtime(5555, "serve")
    assert json.loads(f.read_text())["port"] == 5555


def test_clear_runtime_removes_only_its_own(home):
    launch._clear_runtime()  # nothing there: no error
    f = _runtime(pid=os.getpid() + 1, port=1)
    launch._clear_runtime()
    assert f.exists()  # another process's
    _runtime(pid=os.getpid(), port=1)
    launch._clear_runtime()
    assert not f.exists()


# --- BackgroundServer -----------------------------------------------------------------------


async def _app(scope, receive, send):
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})


def test_a_server_that_never_starts_raises_and_writes_nothing(home):
    server = launch.BackgroundServer(SimpleNamespace(app=_app))
    server.thread = threading.Thread(target=lambda: None)  # uvicorn exits at once, never "started"
    try:
        with pytest.raises(RuntimeError, match="did not start"):
            server.start()
    finally:
        server.sock.close()
    assert not launch.runtime_file().exists()


def test_stop_clears_secrets_closes_the_model_and_audits(home, tmp_path):
    pf = SimpleNamespace(closed=False)
    pf.close = lambda: setattr(pf, "closed", True)
    vault = SimpleNamespace(cleared=False)
    vault.clear = lambda: setattr(vault, "cleared", True)
    proxy = SimpleNamespace(app=_app, audit=AuditLog(tmp_path / "audit.jsonl"), _observe=lambda: False,
                            info={"mode": "vscode", "version": "9.9", "pii": True, "detector": "rules"},
                            redactor=SimpleNamespace(vault=vault, detector=SimpleNamespace(pf=pf)))
    server = launch.BackgroundServer(proxy)
    server.start()
    assert json.loads(launch.runtime_file().read_text())["mode"] == "vscode"
    real_sock = server.sock

    class Closed:
        def close(self):
            raise OSError("already closed")

    server.sock = Closed()  # a socket that fails to close does not stop the cleanup
    try:
        server.stop()
    finally:
        real_sock.close()
    assert vault.cleared and pf.closed and not launch.runtime_file().exists()
    start, stop = read(tmp_path / "audit.jsonl")
    assert start["event"] == "start" and start["port"] == server.port and start["mode"] == "vscode"
    assert start["version"] == "9.9" and start["pii"] is True and start["observe"] is False
    assert stop["event"] == "stop" and "port" not in stop and stop["pid"] == os.getpid()


# --- build_proxy ----------------------------------------------------------------------------


def test_build_proxy_follows_config_changes_except_a_fixed_pii(home, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[detect]\npii = true\n")
    fixed = launch.build_proxy(config_mod.load(path), pii=True, rules_only=True, upstream="http://up",
                               gateways={"run-only": "http://127.0.0.1:3425"})
    free = launch.build_proxy(config_mod.load(path), rules_only=True, upstream="http://up")
    assert fixed.info["pii"] and free.info["pii"]
    path.write_text('[detect]\npii = false\n[guard]\ncommands = "block"\n[gateways]\ncorp = "https://gw.corp.example"\n'
                    '[restore]\nBash = "restore"\n')
    fixed._observe(), free._observe()  # the file changed: settings apply on the next request
    assert fixed.info["pii"] is True and free.info["pii"] is False  # --pii holds for this run
    assert fixed.info["checks"]["commands"] == "block"
    assert fixed.gateways == {"corp": "https://gw.corp.example", "run-only": "http://127.0.0.1:3425"}
    assert free.gateways == {"corp": "https://gw.corp.example"}


# --- Logging, running Claude ----------------------------------------------------------------


def test_log_to_file_is_private_and_takes_over_the_root_logger(home):
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        launch.log_to_file("debug")
        launch.log_to_file("info")  # the file already there: kept, not recreated
        logging.getLogger("keepmum").info("hello from the wrapper")
        for h in root.handlers:
            h.flush()
    finally:
        for h in root.handlers:
            h.close()
        root.handlers[:], level = saved
        root.setLevel(level)
    lf = launch.log_file()
    assert lf.stat().st_mode & 0o777 == 0o600 and "INFO hello from the wrapper" in lf.read_text()
    assert logging.getLogger("httpx").level == logging.WARNING


def test_run_claude_without_claude_on_path(home, monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError, match="not on PATH"):
        launch.run_claude(SimpleNamespace(), [])
    assert not launch.runtime_file().exists()  # no proxy was started


def test_run_child_forwards_term_and_hup_and_ignores_ctrl_c_while_it_runs(monkeypatch):
    import subprocess

    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}

    class Child:
        def __init__(self, command, env):
            self.command, self.env, self.sent, self.done = command, env, [], False

        def poll(self):
            return 0 if self.done else None

        def send_signal(self, signum):
            self.sent.append(signum)

        def wait(self):
            assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN  # Ctrl-C reaches Claude, not keepmum
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            signal.getsignal(signal.SIGHUP)(signal.SIGHUP, None)
            self.done = True
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)  # after it exited: nothing to pass on
            return 3

    children = []
    monkeypatch.setattr(subprocess, "Popen", lambda command, env: children.append(Child(command, env)) or children[-1])
    assert launch._run_child(["claude", "-p"], {"A": "1"}) == 3
    [child] = children
    assert child.command == ["claude", "-p"] and child.env == {"A": "1"}
    assert child.sent == [signal.SIGTERM, signal.SIGHUP]
    assert {s: signal.getsignal(s) for s in before} == before  # handlers put back


# --- shared_serve_port: a fake service on localhost -----------------------------------------


@pytest.fixture
def service(home):
    """A stats endpoint on 127.0.0.1 answering like `keepmum serve` to the right token."""
    state = SimpleNamespace(info={"mode": "serve", "apis": ["messages", "responses"]}, status=200, seen=[])

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state.seen.append((self.path, self.headers.get("authorization")))
            ok = self.headers.get("authorization") == "Bearer tok"
            body = json.dumps(state.info).encode() if ok else b"{}"
            self.send_response(state.status if ok else 401)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    t.start()
    state.cfg = Config(port=httpd.server_address[1])
    state.token = home / ".config" / "keepmum" / "stats-token"
    state.token.parent.mkdir(parents=True)
    state.token.write_text("tok\n")
    yield state
    httpd.shutdown()
    httpd.server_close()


def test_shared_serve_port_finds_a_running_service(service):
    assert launch.shared_serve_port(service.cfg) == service.cfg.port
    assert launch.shared_serve_port(service.cfg, need="responses") == service.cfg.port
    assert service.seen[0] == ("/_keepmum/stats", "Bearer tok")


def test_shared_serve_port_ignores_what_is_not_this_users_service(service):
    service.token.write_text("someone-else")
    assert launch.shared_serve_port(service.cfg) is None  # wrong token: 401
    service.token.write_text("tok")
    service.info = {"mode": "vscode"}
    assert launch.shared_serve_port(service.cfg) is None  # a per-session proxy, not the service
    service.info, service.status = {"mode": "serve"}, 500
    assert launch.shared_serve_port(service.cfg) is None


def test_an_old_service_only_serves_the_messages_api(service):
    service.info = {"mode": "serve"}  # started before it listed its APIs
    assert launch.shared_serve_port(service.cfg) == service.cfg.port
    assert launch.shared_serve_port(service.cfg, need="responses") is None


def test_shared_serve_port_without_a_token_or_a_service(home):
    assert launch.shared_serve_port(Config(port=7788)) is None  # no token file: no request made
    token = home / ".config" / "keepmum" / "stats-token"
    token.parent.mkdir(parents=True)
    token.write_text("tok")
    import socket

    with socket.socket() as s:  # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert launch.shared_serve_port(Config(port=port)) is None


# --- wrap_main: what the VS Code wrapper does in each situation ------------------------------


@pytest.fixture
def wrapper(home, tmp_path, monkeypatch):
    claude = tmp_path / "claude"
    claude.write_text("#!/bin/sh\n")
    claude.chmod(0o755)
    w = SimpleNamespace(claude=str(claude), children=[], through=[], built=[], logs=[], serve_port=None,
                        protected=False, child_code=5, child_error=None, build_error=None)

    def run_child(command, env):
        if w.child_error:
            raise w.child_error
        w.children.append((command, env))
        return w.child_code

    def build_proxy(cfg, upstream=None, mode="serve"):
        if w.build_error:
            raise w.build_error
        w.built.append({"port": cfg.port, "upstream": upstream, "mode": mode})
        return "the proxy"

    def run_through(proxy, command):
        w.through.append((proxy, command))
        return 6

    monkeypatch.setattr(launch, "_run_child", run_child)
    monkeypatch.setattr(launch, "build_proxy", build_proxy)
    monkeypatch.setattr(launch, "run_through", run_through)
    monkeypatch.setattr(launch, "log_to_file", lambda level: w.logs.append(level))
    monkeypatch.setattr(launch, "shared_serve_port", lambda cfg: w.serve_port)
    monkeypatch.setattr(launch, "protected", lambda cfg: w.protected)

    def run(*args):
        monkeypatch.setattr(sys, "argv", ["keepmum-claude-wrapper", *args])
        return launch.wrap_main()

    w.run = run
    return w


def test_wrapper_needs_a_command(wrapper, capsys):
    assert wrapper.run() == 2
    out = capsys.readouterr()
    assert out.out == "" and "usage: keepmum-claude-wrapper" in out.err


def test_wrapper_refuses_a_bad_config(wrapper, home, capsys):
    (home / ".config" / "keepmum").mkdir(parents=True)
    (home / ".config" / "keepmum" / "config.toml").write_text("[server]\nport = \"x\"\n")
    assert wrapper.run(wrapper.claude) == 2
    out = capsys.readouterr()
    assert out.out == "" and "keepmum: bad config:" in out.err and "server.port must be int" in out.err
    assert wrapper.children == wrapper.through == []


def test_wrapper_refuses_on_a_bypass_risk(wrapper, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    assert wrapper.run(wrapper.claude) == 2
    assert "could bypass the proxy: environment variable CLAUDE_CODE_USE_VERTEX is set" in capsys.readouterr().err
    assert wrapper.children == wrapper.through == [] and wrapper.logs == []


def test_wrapper_refuses_a_file_it_cannot_run(wrapper, tmp_path, capsys):
    plain = tmp_path / "notes.txt"
    plain.write_text("hi")
    assert wrapper.run(str(plain)) == 2
    assert f"cannot run {plain}: not an executable file" in capsys.readouterr().err
    assert wrapper.children == wrapper.through == []


def test_wrapper_runs_claude_as_is_in_an_excluded_project(wrapper, monkeypatch, capsys):
    from keepmum import protect

    monkeypatch.setattr(protect, "is_excluded", lambda path: "https://api.anthropic.com")
    monkeypatch.setenv("SOME_VAR", "kept")
    wrapper.serve_port = 7788  # even with the service up, an excluded project goes straight to the API
    assert wrapper.run(wrapper.claude, "--resume") == 5
    [(command, env)] = wrapper.children
    assert command == [wrapper.claude, "--resume"] and env["SOME_VAR"] == "kept" and "ANTHROPIC_BASE_URL" not in env
    assert wrapper.built == [] and capsys.readouterr().out == ""


def test_wrapper_in_an_excluded_project_reports_a_failed_start(wrapper, monkeypatch, capsys):
    from keepmum import protect

    monkeypatch.setattr(protect, "is_excluded", lambda path: "https://api.anthropic.com")
    wrapper.child_error = PermissionError(13, "Permission denied")
    assert wrapper.run(wrapper.claude) == 2
    assert f"cannot run {wrapper.claude}: Permission denied" in capsys.readouterr().err


def test_wrapper_uses_a_running_service(wrapper, capsys):
    wrapper.serve_port = 7788
    assert wrapper.run(wrapper.claude, "-p", "hi") == 5
    [(command, env)] = wrapper.children
    assert command == [wrapper.claude, "-p", "hi"] and env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:7788"
    assert wrapper.built == [] and wrapper.logs == ["info"] and capsys.readouterr().out == ""


def test_wrapper_refuses_when_protected_and_the_service_is_down(wrapper, capsys):
    wrapper.protected = True
    assert wrapper.run(wrapper.claude) == 2
    assert launch.PROTECTED_BUT_DOWN in capsys.readouterr().err
    assert wrapper.children == wrapper.through == [] and wrapper.built == []


def test_wrapper_starts_its_own_proxy_chained_to_the_editors_upstream(wrapper, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gw.corp.example")
    assert wrapper.run(wrapper.claude, "--verbose") == 6
    assert wrapper.built == [{"port": 7788, "upstream": "https://gw.corp.example", "mode": "vscode"}]
    assert wrapper.through == [("the proxy", [wrapper.claude, "--verbose"])]
    assert capsys.readouterr().out == ""


def test_wrapper_refuses_when_the_model_is_unavailable(wrapper, capsys):
    wrapper.build_error = ModelUnavailable("the Privacy Filter model is not downloaded: run `keepmum setup`")
    assert wrapper.run(wrapper.claude) == 2
    assert "keepmum: the Privacy Filter model is not downloaded" in capsys.readouterr().err
    assert wrapper.through == []


def test_wrapper_reports_a_failed_start_with_the_service(wrapper, capsys):
    wrapper.serve_port = 7788
    wrapper.child_error = FileNotFoundError(2, "No such file or directory")
    assert wrapper.run(wrapper.claude) == 2
    err = capsys.readouterr().err
    assert f"cannot run {wrapper.claude}: No such file or directory" in err and "Traceback" not in err
