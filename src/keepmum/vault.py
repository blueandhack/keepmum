"""Placeholders and the in-memory vault that maps them back to real values.

A placeholder is `KEEPMUM_<TYPE>_<id>`, where id is the first 8 hex digits of
HMAC-SHA256(install salt, type + NUL + value), extended to 12 on a collision. The same value
always gets the same placeholder, which keeps Anthropic's prompt cache valid across turns.

The salt is secret: with it, anyone holding a placeholder could test guesses of a weak
password offline. It lives in ~/.config/keepmum/salt (mode 0600), shared by every keepmum process.
The vault lives in process memory only: never logged, never written to disk.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from pathlib import Path

from .detect import TYPES

KEYCHAIN_SERVICE = "sotto"  # where versions named sotto, before the salt file, kept it
KEYCHAIN_ACCOUNT = "install-salt"
_IMPORT_HOME = Path.home()
SALT_FILE = _IMPORT_HOME / ".config" / "keepmum" / "salt"


def salt_file() -> Path:
    if SALT_FILE != _IMPORT_HOME / ".config" / "keepmum" / "salt":
        return SALT_FILE
    return Path.home() / ".config" / "keepmum" / "salt"

# Known values shorter than this are not re-masked by exact match: too likely to occur by chance.
MIN_KNOWN = 4

PLACEHOLDER = re.compile(r"KEEPMUM_(" + "|".join(TYPES) + r")_([0-9a-f]{8}(?:[0-9a-f]{4})?)")


def _salt_from_file(path: Path, initial: bytes | None = None) -> bytes:
    if path.exists():
        return bytes.fromhex(path.read_text().strip())
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    salt = initial or secrets.token_bytes(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # another keepmum created it first: use theirs
        return bytes.fromhex(path.read_text().strip())
    with os.fdopen(fd, "w") as f:
        f.write(salt.hex())
    return salt


def _keychain_usable() -> bool:
    """Whether the login Keychain may be used, without ever showing a Keychain dialog.

    The Keychain belongs to the real user; with HOME pointed elsewhere (tests, sandboxes)
    macOS cannot find it and would offer to "reset to defaults". keepmum never lets the
    Security framework prompt: if the Keychain cannot be used silently, it uses the file.
    """
    import ctypes
    import ctypes.util
    import pwd

    try:
        real_home = pwd.getpwuid(os.getuid()).pw_dir
        if os.path.realpath(Path.home()) != os.path.realpath(real_home):
            return False
        security = ctypes.CDLL(ctypes.util.find_library("Security"))
        security.SecKeychainSetUserInteractionAllowed(ctypes.c_bool(False))
        return True
    except Exception:
        return False


def install_salt() -> bytes:
    """The per-install HMAC key, in ~/.config/keepmum/salt (0600), shared by every keepmum on this Mac.

    Every process must use the same salt: a placeholder made by one (the app's service, `keepmum
    claude`, the VS Code wrapper) is only restored by a process that derives the same one. The
    Keychain cannot be shared silently, since an item is readable without a prompt only by the
    program that created it, and each install of keepmum is a different program. A salt that
    earlier versions kept only in the Keychain is copied to the file once, when readable.
    """
    path = salt_file()
    if path.exists():
        return _salt_from_file(path)
    legacy = None
    if _keychain_usable():
        try:
            import keyring

            stored = keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
            legacy = bytes.fromhex(stored) if stored else None
        except Exception:
            legacy = None
    return _salt_from_file(path, legacy)


class Vault:
    def __init__(self, salt: bytes | None = None):
        self._salt = salt if salt is not None else install_salt()
        self._values: dict[str, str] = {}  # placeholder -> value
        self._placeholders: dict[tuple[str, str], str] = {}  # (type, value) -> placeholder
        self._kinds: dict[str, str] = {}  # value -> type it was first masked as
        self._sources: dict[str, set[str]] = {}  # value -> the detectors that found it (rules:<id>, user:<name>, pf-serve)
        self._known: re.Pattern[str] | None = None

    def __repr__(self) -> str:  # never show values, even in a traceback
        return f"<Vault {len(self._values)} values>"

    def __len__(self) -> int:
        return len(self._values)

    def placeholder(self, kind: str, value: str, sources=()) -> str:
        self._sources.setdefault(value, set()).update(x for x in sources if x and x != "known")
        key = (kind, value)
        if key in self._placeholders:
            return self._placeholders[key]
        digest = hmac.new(self._salt, f"{kind}\0{value}".encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()
        ph = f"KEEPMUM_{kind}_{digest[:8]}"
        if self._values.get(ph, value) != value:  # collision with a different value
            ph = f"KEEPMUM_{kind}_{digest[:12]}"
        self._values[ph] = value
        self._placeholders[key] = ph
        self._kinds.setdefault(value, kind)
        self._known = None
        return ph

    def value(self, placeholder: str) -> str | None:
        return self._values.get(placeholder)

    def known_values(self) -> re.Pattern[str] | None:
        """One regex over every masked value, longest first, for the known-values pass.

        A match must not sit inside a longer word: a masked "Anna" leaves "Annapolis" alone.
        """
        if self._known is None:
            values = sorted((v for v in self._kinds if len(v) >= MIN_KNOWN), key=len, reverse=True)
            if values:
                alternation = "|".join(re.escape(v) for v in values)
                self._known = re.compile(rf"(?<![A-Za-z0-9])(?:{alternation})(?![A-Za-z0-9])")
        return self._known

    def kind_of(self, value: str) -> str:
        return self._kinds.get(value, "SECRET")

    def sources_of(self, value: str) -> frozenset[str]:
        return frozenset(self._sources.get(value, ()))

    def clear(self) -> None:
        self._values.clear()
        self._placeholders.clear()
        self._kinds.clear()
        self._sources.clear()
        self._known = None
