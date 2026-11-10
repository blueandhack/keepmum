"""llama-server never outlives keepmum (llamacpp.Server): stopped with it, at exit, and within seconds
of its process going away however it went, kill -9 included. A fake server stands in for llama.cpp."""

import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from keepmum import llamacpp

FAKE = textwrap.dedent("""\
    import http.server, sys
    port = int(sys.argv[sys.argv.index("--port") + 1])
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
        do_POST = do_GET
        def log_message(self, *a): pass
    http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
""")


@pytest.fixture
def fake(tmp_path):
    script = tmp_path / "llama-server"
    script.write_text(f"#!{sys.executable}\n" + FAKE)
    script.chmod(0o755)
    return script


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie (exited, not yet reaped) counts as gone
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def gone_within(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.2)
    return False


def server_pid(supervisor: int) -> int:
    for _ in range(50):
        out = subprocess.run(["pgrep", "-P", str(supervisor)], capture_output=True, text=True).stdout.split()
        found = [int(p) for p in out if "--port" in subprocess.run(["ps", "-ww", "-o", "command=", "-p", p], capture_output=True, text=True).stdout]
        if found:
            return found[0]
        time.sleep(0.1)
    raise AssertionError("no server under the supervisor")


def test_a_server_answers_and_stop_ends_it_and_its_supervisor(fake, tmp_path):
    s = llamacpp.Server(fake, tmp_path / "m.gguf", [])
    s.start()
    supervisor = s._proc.pid
    pid = server_pid(supervisor)
    assert s.running() and s.post("/health", {}) == {}
    assert s in llamacpp._running
    s.stop()
    assert gone_within(pid, 5) and gone_within(supervisor, 5) and s not in llamacpp._running


def test_a_server_that_exits_ends_its_supervisor(fake, tmp_path):
    s = llamacpp.Server(fake, tmp_path / "m.gguf", [])
    s.start()
    pid = server_pid(s._proc.pid)
    os.kill(pid, signal.SIGKILL)
    assert gone_within(s._proc.pid, llamacpp.POLL_S + 5) and not s.running()
    s.stop()


@pytest.mark.parametrize("how", ["exit", "kill"])
def test_the_server_never_outlives_keepmum(fake, tmp_path, how):
    """keepmum exits normally (the exit handler stops it), or is killed outright (the supervisor sees it gone)."""
    child = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
        import sys, time
        from pathlib import Path
        from keepmum import llamacpp
        s = llamacpp.Server(Path({str(fake)!r}), Path("m.gguf"), [])
        s.start()
        print(s._proc.pid, flush=True)
        if {how!r} == "exit":
            sys.exit(0)
        time.sleep(60)
    """)], stdout=subprocess.PIPE, text=True)
    supervisor = int(child.stdout.readline())
    if how == "kill":
        pid = server_pid(supervisor)
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=10)
        assert gone_within(pid, llamacpp.POLL_S + 5), f"llama-server {pid} outlived keepmum, killed"
    else:
        child.wait(timeout=10)  # its exit handler has stopped the server, and so the supervisor
        assert not subprocess.run(["pgrep", "-P", str(supervisor)], capture_output=True, text=True).stdout.strip()
    assert gone_within(supervisor, 5)
