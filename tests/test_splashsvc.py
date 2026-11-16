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
    calls, loaded = [], []

    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[1] == "print":  # launchd still knows it: loaded, or not yet let go after a bootout
            return subprocess.CompletedProcess(cmd, 0 if loaded else 113, "", "")
        if cmd[1] == "bootstrap":
            loaded.append(True)
        if cmd[1] == "bootout":
            loaded.clear()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(splashsvc.subprocess, "run", run)
    monkeypatch.setattr(splashsvc.time, "sleep", lambda s: None)
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
    assert [c[1] for c in calls] == ["print", "bootstrap"] and calls[1][3] == str(path)  # nothing loaded to unload
    assert splashsvc.managed() == {"model": SPEC, "port": 8000, "idle": "5m", "context": 65536}
    assert splashsvc.stop() is True and not path.exists() and splashsvc.managed() is None
    assert splashsvc.stop() is False  # nothing left to stop


def test_start_again_waits_for_launchd_to_let_the_old_one_go(mac, monkeypatch):
    """bootout returns while Splash is still stopping; a bootstrap then fails with 5 (Input/output
    error), which once left Splash stopped."""
    home, calls = mac
    splashsvc.start(SPEC, 8000)
    calls.clear()
    real = splashsvc.subprocess.run
    state = {"stopping": 2, "refusals": 1}

    def slow(cmd, **kw):
        if cmd[1] == "print" and state["stopping"] and any(c[1] == "bootout" for c in calls):
            state["stopping"] -= 1
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[1] == "bootstrap" and state["refusals"]:
            state["refusals"] -= 1
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 5, "", "Bootstrap failed: 5: Input/output error")
        return real(cmd, **kw)

    monkeypatch.setattr(splashsvc.subprocess, "run", slow)
    splashsvc.start(SPEC, 8000, context=262144)
    assert [c[1] for c in calls] == ["print", "bootout", "print", "print", "print", "bootstrap", "bootstrap"]
    assert splashsvc.managed()["context"] == 262144


def test_start_says_why_it_cannot(mac, monkeypatch):
    monkeypatch.setattr(splashsvc, "find", lambda: None)
    with pytest.raises(splashsvc.SplashError, match="brew install incoai/tap/splash"):
        splashsvc.start(SPEC, 8000)
    monkeypatch.setattr(splashsvc, "find", lambda: "/opt/homebrew/bin/splash")
    monkeypatch.setattr(splashsvc.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 5, "", "Bootstrap failed: 5"))
    with pytest.raises(splashsvc.SplashError, match="Bootstrap failed"):
        splashsvc.start(SPEC, 8000)


LOG = ("Selected Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ3_M.gguf from HauhauCS/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive.\n"
       "Fetching 1 file(s), 1.05 GB, from incoai/Qwen3.6-35B-A3B-DFlash2@51ef7b6923ad; cached files are reused.\n"
       "Fetching 2 files:   0%|          | 0/2 [00:00<?, ?it/s]\rFetching 2 files: 100%|##########| 2/2 [00:16<00:00]\n"
       "Fetching 1 file(s), 15.44 GB, from HauhauCS/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive@f12a584fecbe; cached files are reused.\n"
       "Fetching 1 files:   0%|          | 0/1 [00:00<?, ?it/s]")


def test_a_download_shows_how_much_has_come(mac):
    """Hugging Face's counter reads 0/1 until a 15 GB file is done (Splash's log is not a terminal):
    what has come is the .incomplete file's size in its cache."""
    log = splashsvc.log_path()
    log.parent.mkdir(parents=True)
    log.write_text(LOG)
    assert splashsvc.progress() == "downloading Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive, 15.44 GB"
    blobs = splashsvc.hub_cache() / "models--HauhauCS--Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive" / "blobs"
    blobs.mkdir(parents=True)
    with (blobs / "4dd9a27c.067b8881.incomplete").open("wb") as f:
        f.truncate(4_200_000_000)  # sparse: no 4 GB written
    assert splashsvc.progress() == "downloading Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive: 4.2 of 15.4 GB (27%)"
    log.write_text(LOG + "\rFetching 1 files: 100%|##########| 1/1 [09:12<00:00]\n12:55:47 Loading · the model\n")
    assert splashsvc.progress() == "Loading · the model"


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


def test_start_takes_a_context_or_leaves_it_to_splash(mac):
    args = lambda: plistlib.loads(splashsvc.plist_path().read_bytes())["ProgramArguments"]
    splashsvc.start(SPEC, 8000, context=262144)
    assert args()[args().index("--max-context") + 1] == "256K" and splashsvc.managed()["context"] == 262144
    splashsvc.start(SPEC, 8000, context=100000)
    assert "100000" in args() and splashsvc.managed()["context"] == 100000
    splashsvc.start(SPEC, 8000, context=None)
    assert "--max-context" not in args() and splashsvc.managed()["context"] is None
    assert splashsvc.parse_context_flag("1M") == 1_000_000 and splashsvc.parse_context_flag("lots") is None


def test_restart_keeps_what_it_does_not_change(mac):
    assert splashsvc.restart(context=131072) is None  # keepmum runs no Splash
    splashsvc.start(SPEC, 8001, "10m", 32768)
    splashsvc.restart(context=131072)
    assert splashsvc.managed() == {"model": SPEC, "port": 8001, "idle": "10m", "context": 131072}
    splashsvc.restart(idle="off")
    assert splashsvc.managed() == {"model": SPEC, "port": 8001, "idle": "off", "context": 131072}


def test_context_info_reads_splash_status(monkeypatch):
    import httpx

    status = {"maximum_context_tokens": 65536,
              "memory_plan": {"model": {"maximum_context_tokens": 262144, "kv_page_tokens": 32, "kv_page_bytes": 332800}}}

    class Reply:
        def __init__(self, code, body):
            self.status_code, self.body = code, body

        def json(self):
            if self.body is None:
                raise ValueError("not JSON")
            return self.body

    replies = iter([Reply(200, status), Reply(200, {"ready": True}), Reply(404, None), Reply(200, None)])
    monkeypatch.setattr(httpx, "get", lambda url, timeout: next(replies))
    assert splashsvc.context_info("http://localhost:8000/") == {"context": 65536, "max": 262144, "bytes_per_token": 10400}
    assert splashsvc.context_info("http://localhost:8000") is None  # an older Splash that does not say
    assert splashsvc.context_info("http://localhost:8000") is None
    assert splashsvc.context_info("http://localhost:8000") is None

    def down(url, timeout):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", down)
    assert splashsvc.context_info("http://localhost:8000") is None


@pytest.fixture
def splash(mac, monkeypatch):
    """Splash answering at localhost:8000 with keepmum's pick and a 64k context of the model's 256k."""
    info = {"context": 65536, "max": 262144, "bytes_per_token": 10400.0}
    monkeypatch.setattr(local, "models", lambda url, timeout=3.0: [SPEC] if url.endswith(":8000") else None)
    monkeypatch.setattr(local, "kind", lambda url, timeout=1.5: "Splash" if url.endswith(":8000") else None)
    monkeypatch.setattr(splashsvc, "context_info", lambda url, timeout=2.0: info)
    return mac[0], info


def test_the_cli_sets_splashs_context_by_starting_it_again(splash):
    home, info = splash
    assert CliRunner().invoke(app, ["guard", "local", "splash", "--start", "--context", "32k"]).exit_code == 0
    assert splashsvc.managed()["context"] == 32768
    row = json.loads(CliRunner().invoke(app, ["guard", "local", "--json"]).stdout)["servers"][0]
    assert (row["kind"], row["context"], row["context_max"], row["context_bytes"]) == ("Splash", 65536, 262144, 10400.0)
    shown = CliRunner().invoke(app, ["guard", "local", "context"])
    assert shown.exit_code == 0 and "64k tokens (it takes up to 256k tokens)" in shown.stdout and "256k (the model's longest)" in shown.stdout
    r = CliRunner().invoke(app, ["guard", "local", "context", "256k"])
    assert r.exit_code == 0, r.output
    assert "Splash starts again with a 256k-token context" in r.stderr and "up to about 2.5 GB" in r.stderr
    assert splashsvc.managed() == {"model": SPEC, "port": 8000, "idle": "5m", "context": 262144}
    r = CliRunner().invoke(app, ["guard", "local", "context", "default", "--server", "1"])
    assert r.exit_code == 0 and "Splash's own context" in r.stderr and splashsvc.managed()["context"] is None
    r = CliRunner().invoke(app, ["guard", "local", "context", "512k"])
    assert r.exit_code == 2 and "at most 256k tokens" in r.stderr
    assert CliRunner().invoke(app, ["guard", "local", "splash", "--context", "lots"]).exit_code == 2
    # keep loaded changes keep the context
    assert CliRunner().invoke(app, ["guard", "local", "context", "128k"]).exit_code == 0
    assert CliRunner().invoke(app, ["guard", "local", "speed", "--keep-loaded", "30m"]).exit_code == 0
    assert splashsvc.managed() == {"model": SPEC, "port": 8000, "idle": "30m", "context": 131072}


def test_the_cli_cannot_restart_a_splash_it_does_not_run(splash):
    home, info = splash
    (home / ".config" / "keepmum" / "config.toml").write_text(f'[guard]\nlocal_servers = [{{ url = "http://localhost:8000", model = "{SPEC}" }}]\n')
    r = CliRunner().invoke(app, ["guard", "local", "context", "128k"])
    assert r.exit_code == 1 and "keepmum does not run this Splash" in r.stderr
    assert f"splash serve --model {SPEC} --language-only --max-context 128K --port 8000" in r.stderr


def test_the_cli_sets_the_context_of_a_splash_that_is_still_starting(splash, monkeypatch):
    home, info = splash
    assert CliRunner().invoke(app, ["guard", "local", "splash", "--start"]).exit_code == 0
    monkeypatch.setattr(local, "models", lambda url, timeout=3.0: None)
    monkeypatch.setattr(splashsvc, "context_info", lambda url, timeout=2.0: None)
    shown = CliRunner().invoke(app, ["guard", "local", "context", "--server", "1"])
    assert shown.exit_code == 0 and "not answering yet" in shown.stdout
    r = CliRunner().invoke(app, ["guard", "local", "context", "128k"])  # found without --server, though it does not answer
    assert r.exit_code == 0 and splashsvc.managed()["context"] == 131072 and "GB" not in r.stderr


def test_the_cli_says_when_splash_is_missing(mac, monkeypatch):
    monkeypatch.setattr(splashsvc, "find", lambda: None)
    r = CliRunner().invoke(app, ["guard", "local", "splash", "--start"])
    assert r.exit_code == 1 and "brew install incoai/tap/splash" in r.stderr


@pytest.mark.parametrize("version, chip, why", [
    ("27.2.1", "Apple M5 Pro", None),
    ("26.4", "Apple M3", None),
    ("26.3.1", "Apple M5 Pro", "macOS 26.4 or later"),
    ("27.0", "Apple M2 Max", "an M3 or newer chip"),
    ("27.0", "Intel(R) Core(TM) i9", "an M3 or newer chip"),
])
def test_why_not_checks_the_chip_and_macos(monkeypatch, version, chip, why):
    import platform

    monkeypatch.setattr(platform, "mac_ver", lambda: (version, ("", "", ""), "arm64"))
    monkeypatch.setattr(splashsvc.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, chip + "\n", ""))
    got = splashsvc.why_not()
    assert got is None if why is None else why in got


def test_the_listing_says_whether_splash_can_run(mac, monkeypatch):
    monkeypatch.setattr(splashsvc, "why_not", lambda: None)
    data = json.loads(CliRunner().invoke(app, ["guard", "local", "--json"]).stdout)
    assert data["splash"] == {"installed": True, "supported": True, "why": None, "install": "brew install incoai/tap/splash"}
