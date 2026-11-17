"""Running Splash for local mode in the background: a LaunchAgent keepmum writes and removes.

`keepmum guard local splash --start` writes ~/Library/LaunchAgents/app.keepmum.splash.plist, which
runs `splash serve` with the chosen model, restarts it if it crashes and starts it again at login,
and loads it now. Splash frees its memory after `idle` without a request (keepmum asks for 5
minutes, as Ollama keeps a model) and loads it again in a few seconds when one comes. The first
start downloads the model and its draft, which the log shows (~/Library/Logs/keepmum/splash.log).
`--stop` unloads it and removes the file.

Context: keepmum starts Splash with a 64k context (`--max-context`), and `keepmum guard local
context` starts it again with another, up to the model's own window (262,144 tokens for keepmum's
pick); `default` leaves it to Splash, which takes that window when memory holds it. Splash takes
the memory for a long context only while a request that long runs: about 10 KB a token for
keepmum's pick, whose attention is in 10 of its 40 layers. Past the window Splash would stretch it
(YaRN), which changes every answer, so keepmum does not offer it.
"""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import time
from pathlib import Path

LABEL = "app.keepmum.splash"
DEFAULT_IDLE = "5m"
DEFAULT_CONTEXT = 65536  # tokens; None: Splash's own (the model's window when memory holds it)
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
            "install": "brew install incoai/tap/splash", "signed_in": signed_in()}


# Signed in to Hugging Face, its downloads go faster: Splash's downloader (huggingface_hub) warns
# "You are sending unauthenticated requests to the HF Hub. Please set a HF_TOKEN to enable higher
# rate limits and faster downloads." It reads the token from this file, which `hf auth login`
# writes; the LaunchAgent gets no HF_TOKEN from a shell.
_TOKEN = re.compile(r"hf_[A-Za-z0-9]{20,}")


def token_path() -> Path:
    return hub_cache().parent / "token"


def signed_in() -> bool:
    try:
        return bool(token_path().read_text().strip())
    except OSError:
        return False


def save_hf_token(text: str) -> Path:
    """Save a Hugging Face token where Splash's downloader looks, readable only by this user."""
    token = text.strip()
    if not _TOKEN.fullmatch(token):
        raise ValueError("that is not a Hugging Face token: they start with hf_ (huggingface.co/settings/tokens)")
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    path.chmod(0o600)
    return path


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


def context_flag(context: int) -> str:
    """Tokens as `--max-context` takes them: 64K (K is 1024 tokens to Splash), or a number."""
    return f"{context // 1024}K" if context % 1024 == 0 else str(context)


def parse_context_flag(text: str | None) -> int | None:
    m = re.fullmatch(r"(\d+)([KkMm]?)", text or "")
    if not m:
        return None
    return int(m.group(1)) * {"": 1, "k": 1024, "m": 1_000_000}[m.group(2).lower()]


def arguments(exe: str, spec: str, port: int, idle: str = DEFAULT_IDLE, context: int | None = DEFAULT_CONTEXT) -> list[str]:
    size = [] if context is None else ["--max-context", context_flag(context)]
    return [exe, "serve", "--model", spec, "--language-only", "--no-webui", *size, "--port", str(port), "--idle-release", idle]


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def start(spec: str, port: int, idle: str = DEFAULT_IDLE, context: int | None = DEFAULT_CONTEXT) -> Path:
    """Write the LaunchAgent for `spec` on `port` and load it now, replacing one keepmum wrote before.
    `context` is its context in tokens, or None for Splash's own."""
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
            "ProgramArguments": arguments(exe, spec, port, idle, context),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},  # again after a crash, not after a stop
            "ThrottleInterval": 30,
            "ProcessType": "Interactive",
            "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
            "StandardOutPath": str(log),
            "StandardErrorPath": str(log),
        }, f)
    _unload()
    for attempt in range(5):  # launchd can refuse (5: Input/output error) while it still tears the old one down
        r = _launchctl("bootstrap", _domain(), str(plist))
        if r.returncode == 0:
            return plist
        time.sleep(1 + attempt)
    raise SplashError(f"launchctl could not start it: {(r.stderr or r.stdout).strip() or r.returncode}")


def _loaded() -> bool:
    return _launchctl("print", f"{_domain()}/{LABEL}").returncode == 0


def _unload(wait: float = 30.0) -> None:
    """Unload the agent if it is loaded, and wait until launchd has let it go: bootout returns while
    Splash is still stopping, and a bootstrap then fails."""
    if not _loaded():
        return
    _launchctl("bootout", f"{_domain()}/{LABEL}")
    deadline = time.monotonic() + wait
    while _loaded() and time.monotonic() < deadline:
        time.sleep(0.25)


def stop() -> bool:
    """Unload the LaunchAgent and remove it; True if there was one."""
    plist = plist_path()
    _unload()
    if plist.exists():
        plist.unlink()
        return True
    return False


def managed() -> dict | None:
    """The model, port, idle release and context (tokens; None: Splash's own) keepmum runs Splash
    with, or None if it does not."""
    try:
        with plist_path().open("rb") as f:
            args = plistlib.load(f).get("ProgramArguments", [])
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    get = lambda flag: args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else None
    port = get("--port")
    return {"model": get("--model"), "port": int(port) if port and port.isdigit() else None, "idle": get("--idle-release"),
            "context": parse_context_flag(get("--max-context"))}


def restart(**changes) -> dict | None:
    """Start the Splash keepmum runs again with `changes` (idle, context), keeping the rest; None if
    keepmum runs none. Splash reads them when it starts, and loads its model again in a few seconds."""
    m = managed()
    if not m or not m["model"] or not m["port"]:
        return None
    m = {**m, **changes}
    start(m["model"], m["port"], m["idle"] or DEFAULT_IDLE, m["context"])
    return m


def context_info(url: str, timeout: float = 2.0) -> dict | None:
    """What a Splash at `url` says of its context: the limit it runs with, the model's own window,
    and its cache's bytes a token; None if nothing answers or it does not say."""
    import httpx

    try:
        r = httpx.get(url.rstrip("/") + "/status", timeout=timeout)
        d = r.json() if r.status_code == 200 else {}
        model = (d.get("memory_plan") or {}).get("model") or {}
        tokens, page = model.get("kv_page_tokens"), model.get("kv_page_bytes")
        now, most = d.get("maximum_context_tokens"), model.get("maximum_context_tokens")
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    if not isinstance(now, int) or not isinstance(most, int):
        return None
    per = page / tokens if isinstance(tokens, int) and tokens > 0 and isinstance(page, (int, float)) else None
    return {"context": now, "max": most, "bytes_per_token": per}


# Splash says what it downloads, then Hugging Face's counter of files: "Fetching 1 file(s), 15.44 GB,
# from HauhauCS/Qwen3.6-…@f12a584fecbe; …", then "Fetching 1 files:   0%| | 0/1". The counter moves
# when a file is done, and Splash's log is not a terminal, so nothing else is written meanwhile: a
# 15 GB model reads 0% until it is all there (a new Mac, Nov 16, 2026). How much has come: what
# Hugging Face's xet downloader says it has received, or the size of the download's .incomplete file
# in its cache, whichever is more. xet fetches 64 pieces at once and writes the file only as they
# join up, so the file lags far behind (268 MB written of 2.5 GB come), and on a slow line stays at
# 0 for a long time (another new Mac, the same day).
_FETCHING = re.compile(r"Fetching \d+ file\(s\), ([\d.]+) ([KMGT]?B), from ([\w.-]+/[\w.-]+)@")
_COUNTER = re.compile(r"^Fetching \d+ files?:\s+(\d+)%")
_UNITS = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}
_REQUEST = re.compile(r"· (?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) /")


def hub_cache() -> Path:
    """Where Splash's downloads go: Hugging Face's cache, as the LaunchAgent leaves it (its default)."""
    return Path.home() / ".cache" / "huggingface" / "hub"


def downloaded(repo: str) -> int:
    """Bytes of the file being downloaded now from `repo`, in Hugging Face's cache: the newest
    .incomplete. A download that stopped is not picked up again: its .incomplete stays, under a
    name of its own, and the next one starts over."""
    blobs = hub_cache() / f"models--{repo.replace('/', '--')}" / "blobs"
    try:
        newest = max((p.stat() for p in blobs.glob("*.incomplete")), key=lambda st: st.st_mtime, default=None)
    except OSError:
        return 0
    return newest.st_size if newest else 0


def cached(spec: str) -> bool | None:
    """Whether a Splash model (owner/repo[:VARIANT]) has a file of its downloaded: its first start
    then loads it rather than downloading it. None: not a Hugging Face model."""
    repo = spec.split(":", 1)[0]
    if repo.count("/") != 1:
        return None
    blobs = hub_cache() / f"models--{repo.replace('/', '--')}" / "blobs"
    try:
        return any(not p.name.endswith(".incomplete") and p.stat().st_size > 100_000_000 for p in blobs.iterdir())
    except OSError:
        return False


_DOWNLOAD_TO = re.compile(rb'"message":\s*"Download file to path"')
_OBSERVED = re.compile(rb"observed bytes sent so far = (\d+)")
_DEST = re.compile(rb'"dest_path":\s*"\\"([^"\\]+)')  # its value quoted again: "dest_path":"\"/Users/…\""


def received(repo: str) -> int:
    """Bytes xet has received for `repo`'s download, from its newest log: its counter is the whole
    process's (the draft model's too), so what it read when the download to `repo`'s cache began
    is taken off."""
    readings = received_over_time(repo)
    return readings[-1][1] if readings else 0


_STAMP = re.compile(rb'"timestamp":\s*"([^"]+)"')


def received_over_time(repo: str) -> list[tuple[float, int]]:
    """(time, bytes received for `repo`) for each reading of xet's counter since its download began."""
    logs = hub_cache().parent / "xet" / "logs"
    try:
        newest = max(logs.glob("xet_*.log"), key=lambda p: p.stat().st_mtime, default=None)
        data = newest.read_bytes() if newest else b""
    except OSError:
        return 0
    target = f"models--{repo.replace('/', '--')}/blobs/".encode()
    start, earlier = None, []
    for m in _DOWNLOAD_TO.finditer(data):
        line = data[m.start():data.find(b"\n", m.start())]
        if target in line:
            start = m.start()
            break
        if path := _DEST.search(line):
            earlier.append(Path(path.group(1).decode(errors="replace")))
    if start is None:
        return 0
    # Taken off: the counter's last reading before, or what the earlier downloads came to (xet does
    # not always write a reading between one file's end and the next one's start).
    before = [int(m.group(1)) for m in _OBSERVED.finditer(data, 0, start)]
    done = sum(_size(p.with_name(p.name.split(".")[0])) or _size(p) for p in earlier)
    base = max(before[-1] if before else 0, done)
    out = []
    for m in _OBSERVED.finditer(data, start):
        line_start = data.rfind(b"\n", 0, m.start()) + 1
        stamp = _STAMP.search(data, line_start, m.start())
        when = _when(stamp.group(1).decode()) if stamp else None
        if when is not None:
            out.append((when, max(0, int(m.group(1)) - base)))
    return out


def _when(stamp: str) -> float | None:
    from datetime import datetime

    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def pace(readings: list[tuple[float, int]], now: float | None = None) -> tuple[float | None, float]:
    """From xet's readings: bytes a second over the last two minutes or so (None: too few to say),
    and how long nothing new has come. xet gets data in spurts: over half a minute, the same
    download read 2 MB/s, then 53 (Nov 16, 2026)."""
    if not readings:
        return None, 0.0
    now = time.time() if now is None else now
    t1, b1 = readings[-1]
    since = next(t for t, b in readings if b == b1)  # the first reading at today's count
    t0, b0 = next(((t, b) for t, b in reversed(readings) if t <= t1 - 120), readings[0])
    rate = (b1 - b0) / (t1 - t0) if t1 - t0 >= 5 else None
    return rate, max(0.0, now - since)


def _left(seconds: float) -> str:
    if seconds < 60:
        return "under a minute left"
    minutes = round(seconds / 60)
    return f"about {minutes} min left" if minutes < 60 else f"about {minutes // 60} h {minutes % 60:02d} min left"


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def progress() -> str | None:
    """What Splash is doing, from the last line of its log: downloading (with how much has come),
    warming up, ready."""
    try:
        with log_path().open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 16384))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    lines = [line.strip() for part in tail.splitlines() for line in part.split("\r") if line.strip()]
    # Not what Splash is doing: its lines about one request, such as "Error · not_found · GET
    # /api/tags", which keepmum's own look at what server answers there writes as it becomes ready.
    lines = [line for line in lines if not _REQUEST.search(line)]
    if not lines:
        return None
    last = re.sub(r"^\d\d:\d\d:\d\d ", "", lines[-1])  # Splash's own time stamp
    counter = _COUNTER.match(last)
    fetching = next((m for m in map(_FETCHING.search, reversed(lines)) if m), None)
    if counter and int(counter.group(1)) < 100 and fetching:
        size, unit, repo = float(fetching.group(1)), fetching.group(2), fetching.group(3)
        total = size * _UNITS[unit]
        # The row has two short lines and its model menu names the model: what it downloads, in a
        # word, leaves room for the numbers.
        m = managed()
        main = m["model"].split(":", 1)[0] if m and m["model"] else None
        name = repo.rsplit("/", 1)[-1]
        name = ("the model" if repo == main else "its draft model" if main
                else name if len(name) <= 28 else name[:27] + "\u2026")
        readings = received_over_time(repo)
        got = max(downloaded(repo), readings[-1][1] if readings else 0)
        if not got or not total:
            return f"downloading {name}, {size:g} {unit}"
        out = f"downloading {name}: {got / 1e9:.1f} of {total / 1e9:.1f} GB ({min(99, int(100 * got / total))}%)"
        rate, quiet = pace(readings)
        if quiet >= 90:
            out += f", nothing new for {round(quiet / 60)} min"
        elif rate:
            out += f", {rate / 1e6:.1f} MB/s" if rate < 10e6 else f", {rate / 1e6:.0f} MB/s"
            out += ", " + _left(max(0.0, total - got) / rate)
        return out
    return last[:160]
