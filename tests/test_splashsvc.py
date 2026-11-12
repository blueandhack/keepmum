import json
import plistlib
import subprocess

import pytest
from typer.testing import CliRunner

from keepmum import local, splashsvc
from keepmum.cli import app
from keepmum.config import load
from keepmum.local import Server

SPEC = "HauhauCS/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:IQ3_M"


@pytest.fixture
def mac(tmp_path, monkeypatch):
    """A HOME, a splash on PATH, and launchctl recorded instead of run."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".config" / "keepmum").mkdir(parents=True)
    (tmp_path / ".config" / "keepmum" / "config.toml").write_text("")
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(splashsvc.subprocess, "run", run)
    monkeypatch.setattr(splashsvc, "find", lambda: "/opt/homebrew/bin/splash")
    monkeypatch.setattr(local, "models", lambda url, timeout=3.0: None)
    return tmp_path, calls


def test_start_writes_a_launch_agent_and_loads_it(mac):
    home, calls = mac
    path = splashsvc.start(SPEC, 8000)
    assert path == home / "Library" / "LaunchAgents" / "app.keepmum.splash.plist"
    agent = plistlib.loads(path.read_bytes())
    assert agent["Label"] == "app.keepmum.splash" and agent["RunAtLoad"] is True
    assert agent["KeepAlive"] == {"SuccessfulExit": False}
    assert agent["ProgramArguments"] == ["/opt/homebrew/bin/splash", "serve", "--model", SPEC, "--language-only", "--no-webui",
                                         "--max-context", "64K", "--port", "8000", "--idle-release", "5m"]
    assert agent["StandardOutPath"] == str(home / "Library" / "Logs" / "keepmum" / "splash.log")
    assert [c[1] for c in calls] == ["bootout", "bootstrap"] and calls[1][3] == str(path)
    assert splashsvc.managed() == {"model": SPEC, "port": 8000, "idle": "5m"}
    assert splashsvc.stop() is True and not path.exists() and splashsvc.managed() is None
    assert splashsvc.stop() is False  # nothing left to stop


def test_start_says_why_it_cannot(mac, monkeypatch):
    monkeypatch.setattr(splashsvc, "find", lambda: None)
    with pytest.raises(splashsvc.SplashError, match="brew install incoai/tap/splash"):
        splashsvc.start(SPEC, 8000)
    monkeypatch.setattr(splashsvc, "find", lambda: "/opt/homebrew/bin/splash")
    monkeypatch.setattr(splashsvc.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 5, "", "Bootstrap failed: 5"))
    with pytest.raises(splashsvc.SplashError, match="Bootstrap failed"):
        splashsvc.start(SPEC, 8000)


def test_progress_reads_the_last_line_of_the_log(mac):
    home, _ = mac
    assert splashsvc.progress() is None
    log = splashsvc.log_path()
    log.parent.mkdir(parents=True)
    log.write_text("Fetching 1 file(s), 15.44 GB\nFetching 1 files:   0%|  | 0/1\r Fetching 1 files: 40%|####  | 0/1\n"
                   "00:14:44 Warming up · 9 of 11 steps\n")
    assert splashsvc.progress() == "Warming up · 9 of 11 steps"


def test_the_cli_starts_and_stops_splash_in_the_background(mac):
    home, calls = mac
    r = CliRunner().invoke(app, ["guard", "local", "splash", "--start", "--idle", "10m"])
    assert r.exit_code == 0, r.output
    assert "starting in the background" in r.stderr and "after 10m" in r.stderr
    assert load(home / ".config" / "keepmum" / "config.toml").guard_local_servers == [Server("http://localhost:8000", SPEC)]
    assert splashsvc.managed()["idle"] == "10m"
    row = json.loads(CliRunner().invoke(app, ["guard", "local", "--json"]).stdout)["servers"][0]
    assert row["managed"] is True and row["start"].startswith("splash serve")
    r = CliRunner().invoke(app, ["guard", "local", "splash", "--stop"])
    assert r.exit_code == 0 and "no longer starts at login" in r.stderr and splashsvc.managed() is None
    row = json.loads(CliRunner().invoke(app, ["guard", "local", "--json"]).stdout)["servers"][0]
    assert row["managed"] is False
    assert CliRunner().invoke(app, ["guard", "local", "splash", "--start", "--idle", "soon"]).exit_code == 2


def test_the_cli_says_when_splash_is_missing(mac, monkeypatch):
    monkeypatch.setattr(splashsvc, "find", lambda: None)
    r = CliRunner().invoke(app, ["guard", "local", "splash", "--start"])
    assert r.exit_code == 1 and "brew install incoai/tap/splash" in r.stderr
