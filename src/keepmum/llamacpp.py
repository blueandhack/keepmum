"""llama-server (llama.cpp), the runtime of both optional checks: found, started, asked, stopped.

Each check's model runs in a llama-server process of its own on a free local port, started on the
first check and stopped when idle (guard.Idle). It never outlives keepmum: a small sh supervisor
(SUPERVISE) stops it within seconds of keepmum's process going away, however it went (macOS has no
parent-death signal), and keepmum stops it at exit. Before, a keepmum that exited first, as
`keepmum claude` does at the end of a session, left the model running and holding its memory. The Mac app bundles a build from a pinned llama.cpp
(native/llama-server/build.sh) and points KEEPMUM_LLAMA_SERVER at it; on the command line, keepmum
uses its own copy or Homebrew's.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

from .detect.pfserve import data_dir

INSTALL_HINT = "brew install llama.cpp, or set KEEPMUM_LLAMA_SERVER"
# Runs the server ("$@") while keepmum's process ($1) lives: stops it when keepmum is gone (checked
# every POLL_S), when asked to stop (TERM, INT), or ends when the server ends.
POLL_S = 2
SUPERVISE = ('keepmum=$1; shift; "$@" & server=$!; '
             'trap \'kill "$server" 2>/dev/null; wait "$server"; exit 0\' TERM INT HUP; '
             'while kill -0 "$keepmum" 2>/dev/null && kill -0 "$server" 2>/dev/null; do sleep ' + str(POLL_S) + ' & wait $!; done; '
             'kill "$server" 2>/dev/null; wait "$server"')
_running: set = set()  # Servers started and not stopped, for the exit handler


@atexit.register
def _stop_all() -> None:
    for server in list(_running):
        server.stop()


def find_server() -> Path | None:
    """$KEEPMUM_LLAMA_SERVER (the Mac app's own build), else keepmum's copy, else Homebrew's llama.cpp."""
    for p in (os.environ.get("KEEPMUM_LLAMA_SERVER"), data_dir() / "bin" / "llama-server", shutil.which("llama-server"),
              "/opt/homebrew/bin/llama-server", "/usr/local/bin/llama-server"):
        if p and os.access(p, os.X_OK):
            return Path(p)
    return None


class Server:
    """One llama-server process for one model, on 127.0.0.1. `args` are the model's own options
    (context, batch, --embedding); `error` is the exception type its failures raise."""

    START_S = 60.0

    def __init__(self, server: Path, model: Path, args: list[str], error: type[Exception] = RuntimeError):
        self.server, self.model, self.args, self.error = server, model, args, error
        self._proc: subprocess.Popen | None = None
        self.url = ""

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        command = [str(self.server), "-m", str(self.model), "--host", "127.0.0.1", "--port", str(port),
                   "-ngl", "99", "--no-webui", *self.args]
        self._proc = subprocess.Popen(["/bin/sh", "-c", SUPERVISE, "keepmum-llama", str(os.getpid()), *command],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _running.add(self)
        deadline = time.monotonic() + self.START_S
        while time.monotonic() < deadline and self._proc.poll() is None:
            try:
                with urllib.request.urlopen(self.url + "/health", timeout=1) as r:
                    if r.status == 200:
                        return
            except OSError:
                time.sleep(0.1)
        self.stop()
        raise self.error("llama-server did not start")

    def post(self, path: str, body: dict, timeout: float = 60):
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def stop(self) -> None:
        _running.discard(self)
        if self._proc is not None:
            self._proc.terminate()  # the supervisor stops the server, then exits
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
