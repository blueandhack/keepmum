"""llamacpp.py without a real llama-server: finding the binary, a start that waits for /health, one
that never comes up, and a stop that has to kill."""

import subprocess
from pathlib import Path

import pytest

from keepmum import llamacpp


def test_find_server_prefers_keepmum_llama_server(tmp_path, monkeypatch):
    exe = tmp_path / "llama-server"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("KEEPMUM_LLAMA_SERVER", str(exe))
    assert llamacpp.find_server() == exe


def test_find_server_finds_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("KEEPMUM_LLAMA_SERVER", raising=False)
    monkeypatch.setattr(llamacpp.os, "access", lambda p, mode: False)
    assert llamacpp.find_server() is None


class FakeProc:
    def __init__(self, hangs=False):
        self.hangs, self.terminated, self.killed, self.pid = hangs, False, False, 4242

    def poll(self):
        return 0 if self.terminated or self.killed else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        if self.hangs:
            raise subprocess.TimeoutExpired("sh", timeout)
        return 0

    def kill(self):
        self.killed = True


class Reply:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def no_processes(monkeypatch):
    procs, commands = [], []

    def popen(cmd, **kw):
        commands.append(cmd)
        procs.append(FakeProc())
        return procs[-1]

    monkeypatch.setattr(llamacpp.subprocess, "Popen", popen)
    monkeypatch.setattr(llamacpp.time, "sleep", lambda s: None)
    monkeypatch.setattr(llamacpp, "_running", set())
    return procs, commands


def test_start_waits_until_health_answers_200(no_processes, monkeypatch):
    procs, commands = no_processes
    replies = iter([OSError("refused"), Reply(503), Reply(200)])
    asked = []

    def urlopen(url, timeout=None):
        asked.append(url)
        r = next(replies)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(llamacpp.urllib.request, "urlopen", urlopen)
    s = llamacpp.Server(Path("/bin/llama-server"), Path("/m.gguf"), ["--embedding"])
    s.start()
    assert len(asked) == 3 and all(u == s.url + "/health" for u in asked)
    assert s.running() and s in llamacpp._running
    cmd = commands[0]
    assert cmd[:3] == ["/bin/sh", "-c", llamacpp.SUPERVISE]
    assert cmd[cmd.index("-m") + 1] == "/m.gguf" and cmd[-1] == "--embedding"
    assert cmd[cmd.index("--port") + 1] == s.url.rsplit(":", 1)[1]
    s.stop()
    assert procs[0].terminated and not s.running() and s not in llamacpp._running


def test_a_server_that_never_answers_is_stopped_with_the_error_given(no_processes, monkeypatch):
    procs, _ = no_processes

    class Failed(Exception):
        pass

    monkeypatch.setattr(llamacpp.Server, "START_S", 0)
    s = llamacpp.Server(Path("/bin/llama-server"), Path("/m.gguf"), [], Failed)
    with pytest.raises(Failed, match="did not start"):
        s.start()
    assert procs[0].terminated and s._proc is None and s not in llamacpp._running


def test_stop_kills_a_supervisor_that_does_not_exit_and_stopping_twice_is_fine(monkeypatch):
    monkeypatch.setattr(llamacpp, "_running", set())
    s = llamacpp.Server(Path("/x"), Path("/m"), [])
    s._proc = proc = FakeProc(hangs=True)
    llamacpp._running.add(s)
    s.stop()
    assert proc.terminated and proc.killed and s._proc is None and not llamacpp._running
    s.stop()
    assert not s.running()


def test_every_running_server_is_stopped_at_exit(monkeypatch):
    stopped = []

    class S:
        def stop(self):
            stopped.append(self)
            llamacpp._running.discard(self)

    a, b = S(), S()
    monkeypatch.setattr(llamacpp, "_running", {a, b})
    llamacpp._stop_all()
    assert set(stopped) == {a, b} and not llamacpp._running
