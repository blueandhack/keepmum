"""`keepmum protect`: route every Claude Code session through `keepmum serve`.

Sets `env.ANTHROPIC_BASE_URL` in Claude Code's user settings file. Claude Code reads that file in
the terminal, VS Code, JetBrains and the desktop app, and a value there replaces the same
variable exported in the shell. A running session picks up the change when the file is saved;
removing it reaches only sessions started afterwards. Project settings can still override it,
which `bypass_risks` reports.

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

    def as_dict(self) -> dict:
        return {"settings": str(self.path), "on": self.on, "base_url": self.value}


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


def status(port: int) -> Status:
    path = settings_path()
    value = _read(path).get("env", {}).get(KEY)
    return Status(path, isinstance(value, str) and is_keepmum_url(value, port), value)


def enable(port: int) -> Status:
    path = settings_path()
    data = _read(path)
    env = data.setdefault("env", {})
    current = env.get(KEY)
    if current is not None and not (isinstance(current, str) and is_keepmum_url(current, port)):
        raise ProtectError(f"{path} already sends Claude Code to {current}; set it as keepmum's upstream "
                           "([server] upstream in ~/.config/keepmum/config.toml) and remove it there first")
    if current != keepmum_url(port):
        env[KEY] = keepmum_url(port)
        _write(path, data)
    return status(port)


def disable(port: int) -> Status:
    path = settings_path()
    data = _read(path)
    env = data.get("env", {})
    current = env.get(KEY)
    if current is not None and isinstance(current, str) and is_keepmum_url(current, port):
        del env[KEY]
        if not env:
            data.pop("env")
        _write(path, data)
    return status(port)


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
