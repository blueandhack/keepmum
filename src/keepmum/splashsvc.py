"""Running Splash for local mode in the background: a LaunchAgent keepmum writes and removes.

`keepmum guard local splash --start` writes ~/Library/LaunchAgents/app.keepmum.splash.plist, which
runs `splash serve` with the chosen model, restarts it if it crashes and starts it again at login,
and loads it now. Splash frees its memory after `idle` without a request (keepmum asks for 5
minutes, as Ollama keeps a model) and loads it again in a few seconds when one comes. The first
start downloads the model and its draft, which the log shows (~/Library/Logs/keepmum/splash.log).
`--stop` unloads it and removes the file.
"""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
from pathlib import Path

LABEL = "app.keepmum.splash"
DEFAULT_IDLE = "5m"
SEARCH = ("/opt/homebrew/bin/splash", "/usr/local/bin/splash")


class SplashError(Exception):
    pass


def why_not() -> str | None:
    """Why Splash cannot run on this Mac, or None: it needs an M3 or newer chip and macOS 26.4 or later."""
    import platform

    version = tuple(int(x) for x in re.findall(r"\d+", platform.mac_ver()[0])[:2]) or (0,)
    if version < (26, 4):
        return f"Splash needs macOS 26.4 or later (this Mac has {platform.mac_ver()[0] or 'another system'})"
    chip = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
    m = re.match(r"Apple M(\d+)", chip)
    if not m or int(m.group(1)) < 3:
        return f"Splash needs an M3 or newer chip (this Mac has {chip or 'another chip'})"
    return None


def status() -> dict:
    """For the app: whether Splash is installed, and whether this Mac can run it."""
    why = why_not()
    return {"installed": find() is not None, "supported": why is None, "why": why,
            "install": "brew install incoai/tap/splash"}


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def log_path() -> Path:
    return Path.home() / "Library" / "Logs" / "keepmum" / "splash.log"


def find() -> str | None:
    """The splash executable: on PATH, or where Homebrew puts it (a LaunchAgent has a short PATH)."""
    return shutil.which("splash") or next((p for p in SEARCH if os.access(p, os.X_OK)), None)


def idle_release(keep_loaded: str) -> str:
    """`[guard] local_keep_loaded` as Splash's --idle-release: "always" is off."""
    return "off" if keep_loaded == "always" else keep_loaded


def arguments(exe: str, spec: str, port: int, idle: str = DEFAULT_IDLE) -> list[str]:
    return [exe, "serve", "--model", spec, "--language-only", "--no-webui", "--max-context", "64K",
            "--port", str(port), "--idle-release", idle]


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def start(spec: str, port: int, idle: str = DEFAULT_IDLE) -> Path:
    """Write the LaunchAgent for `spec` on `port` and load it now, replacing one keepmum wrote before."""
    exe = find()
    if exe is None:
        raise SplashError("Splash is not installed: `brew install incoai/tap/splash` (an M3 or newer Mac, macOS 26.4 or later)")
    log = log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    plist = plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    with plist.open("wb") as f:
        plistlib.dump({
            "Label": LABEL,
            "ProgramArguments": arguments(exe, spec, port, idle),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},  # again after a crash, not after a stop
            "ThrottleInterval": 30,
            "ProcessType": "Interactive",
            "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
            "StandardOutPath": str(log),
            "StandardErrorPath": str(log),
        }, f)
    _launchctl("bootout", f"{_domain()}/{LABEL}")  # one loaded before; fails harmlessly when there is none
    r = _launchctl("bootstrap", _domain(), str(plist))
    if r.returncode != 0:
        raise SplashError(f"launchctl could not start it: {(r.stderr or r.stdout).strip() or r.returncode}")
    return plist


def stop() -> bool:
    """Unload the LaunchAgent and remove it; True if there was one."""
    plist = plist_path()
    _launchctl("bootout", f"{_domain()}/{LABEL}")
    if plist.exists():
        plist.unlink()
        return True
    return False


def managed() -> dict | None:
    """The model and port keepmum runs Splash with, or None if it does not."""
    try:
        with plist_path().open("rb") as f:
            args = plistlib.load(f).get("ProgramArguments", [])
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    get = lambda flag: args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else None
    port = get("--port")
    return {"model": get("--model"), "port": int(port) if port and port.isdigit() else None, "idle": get("--idle-release")}


def progress() -> str | None:
    """What Splash is doing, from the last line of its log: downloading, warming up, ready."""
    try:
        with log_path().open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 4096))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    lines = [line.strip() for part in tail.splitlines() for line in part.split("\r") if line.strip()]
    if not lines:
        return None
    last = re.sub(r"^\d\d:\d\d:\d\d ", "", lines[-1])  # Splash's own time stamp
    return last[:160]
