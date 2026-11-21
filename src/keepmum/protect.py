"""`keepmum protect`: route every Claude Code session through `keepmum serve`.

Sets `env.ANTHROPIC_BASE_URL` in Claude Code's user settings file. Claude Code reads that file in
the terminal, VS Code, JetBrains and the desktop app, and a value there replaces the same
variable exported in the shell. A running session takes a new value as soon as the file is saved,
but keeps the last one it read when the key is removed (seen Nov 21, 2026 with Claude Code 2.1.310
and 2.1.330: a session switched A -> B at once, and stayed on B after the key went). So `off` does
not remove keepmum's value while sessions that read it run: it sets the address keepmum forwarded
to (Anthropic, or the gateway keepmum was in front of), which every running session takes at once,
and the line goes once no session from before is left (`tidy`). Project settings can still
override it, which `bypass_risks` reports.

When it already sends Claude Code to another gateway (Magpie, LiteLLM), keepmum goes in front of it
(gateway.py): the value becomes keepmum's route for that gateway, and `off` puts the gateway back.

Only that one key is touched. The file is strict JSON, written atomically, with a backup of the
previous version next to it.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

KEY = "ANTHROPIC_BASE_URL"


class ProtectError(RuntimeError):
    pass


def settings_path() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"


def keepmum_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def is_keepmum_url(value: str, port: int) -> bool:
    """Our own proxy: loopback on keepmum's port, with no path."""
    try:
        u = urlsplit(value)
        return (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost") and u.port == port
                and u.path in ("", "/"))
    except ValueError:
        return False


@dataclass
class Status:
    path: Path
    on: bool
    value: str | None  # what the user settings file sets ANTHROPIC_BASE_URL to, if anything
    via: str | None = None  # the gateway keepmum forwards Claude Code to, when it is in front of one

    def as_dict(self) -> dict:
        return {"settings": str(self.path), "on": self.on, "base_url": self.value, **({"via": self.via} if self.via else {})}


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except ValueError as e:
        raise ProtectError(f"{path} is not valid JSON ({e}); fix it first") from e
    if not isinstance(data, dict) or not isinstance(data.get("env", {}), dict):
        raise ProtectError(f"{path} does not hold a settings object with an env object")
    return data


def _write(path: Path, data: dict, backup: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        shutil.copy2(path, path.with_name(path.name + ".keepmum-backup"))
    tmp = path.with_name(path.name + ".keepmum-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    if path.exists():
        os.chmod(tmp, path.stat().st_mode & 0o777)
    tmp.replace(path)


def _gateways() -> dict[str, str]:
    from .config import ConfigError, load

    try:
        return load().gateways
    except ConfigError:
        return {}


def status(port: int, gateways: dict[str, str] | None = None) -> Status:
    from .gateway import is_ours, unroute

    path = settings_path()
    tidy()
    value = _read(path).get("env", {}).get(KEY)
    if value is not None and value == _switched_off():
        value = None  # keepmum's own, from `off`: Claude Code goes where it would without keepmum
    via = unroute(value, port, _gateways() if gateways is None else gateways) if isinstance(value, str) else None
    return Status(path, is_ours(value, port), value, via)


def enable(port: int, adopt=None) -> Status:
    """Point Claude Code at keepmum. Already pointed at another gateway: `adopt(url, who)` returns
    keepmum's route for it (gateway.Adopter); without `adopt`, refused."""
    from .gateway import is_ours

    path = settings_path()
    data = _read(path)
    env = data.setdefault("env", {})
    current = env.get(KEY)
    if current is not None and current == _switched_off():
        current = None  # what `off` set, not a gateway of the user's
    _forget_off()
    want = keepmum_url(port)
    if current is not None and not is_ours(current, port):
        if adopt is None or not isinstance(current, str):
            raise ProtectError(f"{path} already sends Claude Code to {current}; `keepmum protect on` puts keepmum in front of it")
        want = adopt(current, "Claude Code")
    elif current is not None:
        want = current  # already keepmum's, straight or in front of a gateway
    if env.get(KEY) != want:
        env[KEY] = want
        _write(path, data)
    return status(port, getattr(adopt, "gateways", None))


def disable(port: int, upstream: str | None = None) -> Status:
    """Take keepmum out: Claude Code goes back to the gateway keepmum was in front of, or to
    `upstream` (Anthropic), running sessions too, at once. A value, not a removed key, is what a
    running session takes; the line is keepmum's, and goes once the sessions from before are gone."""
    from .gateway import is_ours, unroute

    path = settings_path()
    data = _read(path)
    env = data.get("env", {})
    current = env.get(KEY)
    if is_ours(current, port):
        back = unroute(current, port, _gateways())
        if back:
            env[KEY] = back
        else:
            env[KEY] = upstream or _upstream()
            _note_off(env[KEY])
        _write(path, data)
        tidy()  # no session running from before: nothing to switch, and the line goes now
    return status(port)


# --- After `off` ------------------------------------------------------------------------------

def _off_file() -> Path:
    return Path.home() / ".config" / "keepmum" / "claude-off.json"


def _upstream() -> str:
    try:
        from .config import load

        return load().upstream
    except Exception:  # an unreadable config: where keepmum forwards by default
        return "https://api.anthropic.com"


def _note_off(value: str) -> None:
    import time

    f = _off_file()
    try:
        f.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        f.write_text(json.dumps({"value": value, "since": time.time()}))
    except OSError:
        pass


def _off_note() -> dict | None:
    try:
        note = json.loads(_off_file().read_text())
        return note if isinstance(note, dict) and isinstance(note.get("value"), str) else None
    except (OSError, ValueError):
        return None


def _switched_off() -> str | None:
    """The value `off` set in place of keepmum's, while it is there to switch running sessions."""
    note = _off_note()
    return note["value"] if note else None


def _forget_off() -> None:
    try:
        _off_file().unlink(missing_ok=True)
    except OSError:
        pass


def claude_sessions_since(when: float) -> int | None:
    """How many Claude Code processes started before `when` still run (the CLI, an editor's, the
    desktop app's); None if `ps` cannot say."""
    import re
    import subprocess
    from datetime import datetime

    try:
        res = subprocess.run(["ps", "-A", "-o", "lstart=,args="], capture_output=True, text=True, timeout=5,
                             env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    n = 0
    for line in res.stdout.splitlines():
        parts = line.split(None, 5)  # lstart is "Sat Nov 21 14:36:36 2026"
        if len(parts) < 6:
            continue
        program = parts[5].split(" -", 1)[0].strip()
        if not (re.search(r"(?:^|/)claude$", program) or "/claude/versions/" in program):
            continue
        try:
            started = datetime.strptime(" ".join(parts[:5]), "%a %b %d %H:%M:%S %Y").timestamp()
        except ValueError:
            continue
        n += started <= when
    return n


def tidy() -> bool:
    """Take out the line `off` set once no Claude Code session from before it runs: one started
    since never read keepmum's value, and the key gone, it goes wherever it would anyway. True if
    taken out. Left as it is if it changed since (someone else's now), and while `ps` cannot tell."""
    note = _off_note()
    if note is None:
        return False
    path = settings_path()
    try:
        data = _read(path)
    except ProtectError:
        return False
    env = data.get("env") if isinstance(data.get("env"), dict) else {}
    if env.get(KEY) != note["value"]:
        _forget_off()
        return False
    if claude_sessions_since(float(note.get("since") or 0)) != 0:
        return False
    del env[KEY]
    if not env:
        data.pop("env", None)
    _write(path, data)
    _forget_off()
    return True


# --- Excluded projects -----------------------------------------------------------------------
#
# A project is excluded by setting env.ANTHROPIC_BASE_URL in its `.claude/settings.local.json`
# to where keepmum would have forwarded (the upstream). Project local settings outrank the user
# settings `enable` writes, the file stays out of git, and Claude Code applies its env once the
# folder is trusted (until then, the project still goes through keepmum). keepmum records each
# exclusion so it can list them and tell its own value from someone else's.

def excluded_file() -> Path:
    return Path.home() / ".config" / "keepmum" / "excluded.json"


def project_root(path: Path) -> Path:
    """Where Claude Code keeps a project's local settings: the git repository's root (the main
    checkout's, for a worktree), else the folder itself; also the folder itself when the root is
    the home folder or not owned by this user."""
    import subprocess

    path = path.resolve()
    try:
        out = subprocess.run(["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir",
                              "--show-toplevel"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return path
    lines = out.stdout.split("\n")
    if out.returncode != 0 or len(lines) < 2:
        return path
    common, top = Path(lines[0]), Path(lines[1])
    root = common.parent if common.name == ".git" else top
    if root == Path.home().resolve() or any(p.exists() and p.stat().st_uid != os.getuid() for p in (root, root / ".git")):
        return path
    return root


def _local_settings(root: Path) -> Path:
    return root / ".claude" / "settings.local.json"


def _registry() -> dict[str, str]:
    try:
        data = json.loads(excluded_file().read_text())
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_registry(reg: dict[str, str]) -> None:
    f = excluded_file()
    f.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = f.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(json.dumps(reg, indent=2, sort_keys=True) + "\n")
    tmp.replace(f)


def is_excluded(path: Path) -> str | None:
    """The URL Claude Code goes to directly in the project holding `path`, if keepmum excluded it."""
    root = project_root(path)
    url = _registry().get(str(root))
    if url is None:
        return None
    try:
        value = _read(_local_settings(root)).get("env", {}).get(KEY)
    except ProtectError:
        return None
    return url if value == url else None


def _keep_out_of_git(root: Path, file: Path) -> None:
    """Never let the exclusion be committed: refuse a tracked file, and add an untracked one to
    the repository's own exclude list (.git/info/exclude, which is not committed either)."""
    import subprocess

    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10)

    try:
        if git("rev-parse", "--git-dir").returncode != 0:
            return
        rel = str(file.relative_to(root))
        if git("ls-files", "--error-unmatch", rel).returncode == 0:
            raise ProtectError(f"{file} is tracked by git; an exclusion there would be committed for everyone")
        if git("check-ignore", "-q", rel).returncode != 0:
            common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
            info = common / "info" / "exclude"
            info.parent.mkdir(parents=True, exist_ok=True)
            with open(info, "a") as f:
                f.write(f"\n# added by keepmum exclude\n/{rel}\n")
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ProtectError(f"could not check git for {root}: {e}") from e


def exclude(path: Path, upstream: str) -> Path:
    """Send Claude Code in this project straight to `upstream`, around keepmum. Returns the root."""
    root = project_root(path)
    file = _local_settings(root)
    data = _read(file)
    current = data.get("env", {}).get(KEY)
    if current is not None and current != upstream:
        raise ProtectError(f"{file} already sends Claude Code to {current}; remove it there first")
    _keep_out_of_git(root, file)
    if current != upstream:
        data.setdefault("env", {})[KEY] = upstream
        _write(file, data, backup=False)  # a backup in the project could end up committed
    reg = _registry()
    reg[str(root)] = upstream
    _save_registry(reg)
    return root


def include(path: Path) -> Path:
    """Undo `exclude`: remove keepmum's value from the project's local settings."""
    root = project_root(path)
    reg = _registry()
    url = reg.pop(str(root), None)
    if url is None:
        raise ProtectError(f"{root} is not excluded")
    file = _local_settings(root)
    try:
        data = _read(file)
    except ProtectError:
        data = None
    if data is not None and data.get("env", {}).get(KEY) == url:
        del data["env"][KEY]
        if not data["env"]:
            data.pop("env")
        _write(file, data, backup=False)
    _save_registry(reg)
    return root


def excluded_projects() -> list[dict]:
    out = []
    for root, url in sorted(_registry().items()):
        try:
            active = _read(_local_settings(Path(root))).get("env", {}).get(KEY) == url
        except ProtectError:
            active = False
        out.append({"path": root, "url": url, "active": active})
    return out
