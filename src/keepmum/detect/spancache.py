"""The Privacy Filter's results, kept on disk across restarts.

Clients resend the whole conversation every turn, and the pipeline keeps the model's spans per text
in memory (Pipeline._model_cache), so only new text goes to the model. A restart emptied it: the
first request of a resumed session sent everything to the model again, 4.4 s for a 591 KB, 433-message
Claude Code session where the next request took 14 ms (Nov 22, 2026, M5 Pro; 94% of it the model).
This keeps the same entries in a file, so that request takes the rules' time alone.

What the file holds: for each text, a keyed hash of it (HMAC-SHA256 with a key derived from keepmum's
salt, vault.install_salt) and where the model found something in it: start, end, type, score. No
text and no value. A plain hash would let someone with the file check a guessed text (did
`DB_PASSWORD=hunter2` pass through keepmum?); the keyed one needs the salt for that. The positions
alone, without the text, say no more than "some text had a secret at character 412". It is not
encrypted: the key would come from the salt beside it, readable by the same user, as are the
agents' own transcripts, which hold every value in the clear.

Where: ~/Library/Caches/keepmum/model-spans.json (XDG_CACHE_HOME or ~/.cache elsewhere), the folder
0700, the file 0600, out of Time Machine. At most KEEP texts, the newest, as in memory. The file
names the model, pf-serve and the format it was written for (`namespace`): another model, another
build of pf-serve, or another threshold starts it over rather than reusing what it did not find.
Writes go off the request path, SAVE_AFTER seconds after a new result, and at exit; each write
merges what another keepmum process wrote in the meantime (the app's service, `keepmum claude`), the
newest kept, and replaces the file whole. Anything wrong with it (unreadable, another format,
another namespace, a field out of shape) and it is not used: the model reads the text again.
"""

from __future__ import annotations

import atexit
import hashlib
import hmac
import json
import logging
import os
import sys
import threading
from pathlib import Path

from . import TYPES, Span

FORMAT = 1
KEEP = 4096  # texts, as the pipeline keeps in memory (pipeline.MODEL_CACHE)
SAVE_AFTER = 5.0  # seconds after a new result
FILE = "model-spans.json"

log = logging.getLogger("keepmum")


def cache_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "keepmum"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "keepmum"


def _file_digest(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def namespace(args: list[str]) -> str:
    """What the spans depend on, from pf-serve's arguments [binary, model, threshold]: pf-serve's own
    digest (its patches change spans), the model file as it lies (keepmum pins and checks it when
    it downloads it; hashing 1.6 GB at each start would take seconds), the threshold and FORMAT."""
    binary, model, threshold = args[0], args[1], args[2]
    st = os.stat(model)
    parts = [str(FORMAT), _file_digest(binary), Path(model).name, str(st.st_size), str(int(st.st_mtime)), threshold]
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


class SpanStore:
    """The model's spans per text, on disk: `load` at start, `changed` after a new result."""

    def __init__(self, salt: bytes, space: str, path: Path | None = None, keep: int = KEEP,
                 save_after: float = SAVE_AFTER, source: str = "pf-serve"):
        self._key = hmac.new(salt, b"keepmum: the model's spans on disk", hashlib.sha256).digest()
        self.namespace = space
        self.path = path or cache_dir() / FILE
        self.keep, self.save_after, self.source = keep, save_after, source
        self._snapshot = None  # what to write: a function returning [(key, spans)], oldest first
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self.saves = 0
        atexit.register(self.flush)

    @classmethod
    def for_model(cls, pf, salt: bytes, path: Path | None = None) -> "SpanStore | None":
        """A store for a started PfServe, or None if what it runs cannot be read."""
        try:
            return cls(salt, namespace(pf.args), path, source=pf.name)
        except (OSError, IndexError, AttributeError) as e:
            log.warning("the model's results are not kept on disk (%s)", type(e).__name__)
            return None

    def key_of(self, text: str) -> bytes:
        return hmac.new(self._key, text.encode("utf-8", "surrogatepass"), hashlib.sha256).digest()

    def load(self) -> list[tuple[bytes, tuple[Span, ...]]]:
        """The kept entries, oldest first; none if the file is missing or not one this store wrote."""
        return [(k, tuple(Span(a, b, t, c, self.source) for a, b, t, c in spans)) for k, spans in self._read()]

    def _read(self) -> list[tuple[bytes, list]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            log.warning("the model's saved results are not used (%s): the model reads the text again", type(e).__name__)
            return []
        if not (isinstance(data, dict) and data.get("format") == FORMAT and data.get("namespace") == self.namespace
                and isinstance(data.get("entries"), list)):
            return []  # another model, pf-serve or format: started over
        out = []
        for entry in data["entries"][-self.keep:]:
            spans = _entry(entry)
            if spans is None:
                log.warning("the model's saved results are not used (an entry out of shape): the model reads the text again")
                return []
            out.append((bytes.fromhex(entry[0]), spans))
        return out

    def changed(self, snapshot) -> None:
        """A new result: write `snapshot()` SAVE_AFTER seconds from now, once for every result in between."""
        with self._lock:
            self._snapshot = snapshot
            if self._timer is None:
                self._timer = threading.Timer(self.save_after, self.flush)
                self._timer.daemon = True
                self._timer.start()

    def flush(self) -> None:
        """Write what changed, merged with what the file holds now."""
        with self._lock:
            snapshot, self._snapshot, self._timer = self._snapshot, None, None
        if snapshot is None:
            return
        try:
            self._write(snapshot())
        except Exception as e:  # a cache: never a request's or the engine's failure
            log.warning("the model's results were not saved (%s)", type(e).__name__)

    def _write(self, mine: list[tuple[bytes, tuple[Span, ...]]]) -> None:
        merged: dict[bytes, list] = {k: spans for k, spans in self._read()}  # another process's, older
        for k, spans in mine:
            merged.pop(k, None)
            merged[k] = [[s.start, s.end, s.type, round(float(s.confidence), 4)] for s in spans]
        entries = [[k.hex(), spans] for k, spans in list(merged.items())[-self.keep:]]
        folder = self.path.parent
        if not folder.exists():
            folder.mkdir(parents=True, mode=0o700)
            _out_of_backups(folder)
        tmp = folder / f".{self.path.name}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"format": FORMAT, "namespace": self.namespace, "entries": entries}, f, separators=(",", ":"))
            os.replace(tmp, self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        self.saves += 1


def _entry(entry) -> tuple | None:
    """[key hex, [[start, end, type, score], ...]] checked field by field; None if out of shape."""
    if not (isinstance(entry, list) and len(entry) == 2 and isinstance(entry[0], str) and len(entry[0]) == 64
            and isinstance(entry[1], list)):
        return None
    try:
        bytes.fromhex(entry[0])
    except ValueError:
        return None
    spans = []
    for s in entry[1]:
        if not (isinstance(s, list) and len(s) == 4 and all(type(x) is int for x in s[:2]) and 0 <= s[0] < s[1]
                and s[2] in TYPES and isinstance(s[3], (int, float)) and not isinstance(s[3], bool)):
            return None
        spans.append((s[0], s[1], s[2], float(s[3])))
    return tuple(spans)


def _out_of_backups(folder: Path) -> None:
    """Kept out of Time Machine, as Splash keeps its cache: the attribute `tmutil addexclusion` sets,
    set directly (tmutil took 11 s here, Nov 22)."""
    if sys.platform != "darwin":
        return
    import ctypes
    import plistlib

    value = plistlib.dumps("com.apple.backupd", fmt=plistlib.FMT_BINARY)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.setxattr(str(folder).encode(), b"com.apple.metadata:com_apple_backup_excludeItem", value, len(value), 0, 0)
    except (OSError, AttributeError):
        pass
