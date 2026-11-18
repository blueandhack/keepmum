"""Placeholders and the in-memory vault that maps them back to real values.

A placeholder is `KEEPMUM_<TYPE>_<id>`, where id is the first 8 hex digits of
HMAC-SHA256(install salt, type + NUL + value), extended to 12 on a collision. The same value
always gets the same placeholder, which keeps Anthropic's prompt cache valid across turns.

The salt is secret: with it, anyone holding a placeholder could test guesses of a weak
password offline. It lives in ~/.config/keepmum/salt (mode 0600), shared by every keepmum process.
The vault lives in process memory only: never logged, never written to disk.

It holds the MAX_VALUES values used most recently. An agent sends its whole conversation every
turn, real values and all (keepmum put them back in what it got), so a value still in use is masked
again and stays; what goes is from conversations that ended. Without a bound it grew by every new
value for as long as keepmum ran: 135 MB in 2,000 agent requests (Nov 17, 2026). A value let go
leaves at once, from every table, pattern and log: the vault holds no copy of it after. If it comes
back it is masked again with the same placeholder, an HMAC of it, as after a restart.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from collections import OrderedDict, deque
from pathlib import Path

from .detect import TYPES


def salt_file() -> Path:
    return Path.home() / ".config" / "keepmum" / "salt"


# Known values shorter than this are not re-masked by exact match: too likely to occur by chance.
MIN_KNOWN = 4
# The values the vault keeps, the least recently used going first: thousands of conversations' worth.
# Over it, the oldest EVICT_SHARE go together, and the known pass's patterns are built again then.
MAX_VALUES = 10_000
EVICT_SHARE = 0.1
# The known-values pass matches with a few patterns, in levels as a log-structured store keeps
# them: a new value goes into the smallest, and a full level is merged into the next, LEVEL_GROWTH
# times as large. Building a pattern takes time in proportion to its values (415 ms for 20,000), and
# every new value used to rebuild one over all of them, which Python's re module then kept, up to
# 512 (48 MB after 1,000 requests). This way a new value costs about a millisecond, at any size.
FIRST_LEVEL, LEVEL_GROWTH = 32, 8
# The first level is no pattern: its values are looked for one by one (str.find), which for a few
# dozen is faster than a regex and needs no building. A regex rebuilt for each new value took about
# a seventh of keepmum's CPU under load; the larger levels are built only as they merge.
# The values learned most recently, in order: a text scanned before needs looking at only for those
# learned since (Vault.learned_since). An agent resends its whole conversation every turn.
LEARNED_LOG = 512
_WORD = re.compile(r"[A-Za-z0-9]")


def occurrences(value: str, text: str) -> list[tuple[int, int]]:
    """Where `value` is in `text` not inside a longer word, as the known pattern finds it."""
    out, at = [], text.find(value)
    while at >= 0:
        end = at + len(value)
        if not (at and _WORD.match(text, at - 1)) and not (end < len(text) and _WORD.match(text, end)):
            out.append((at, end))
        at = text.find(value, at + 1)
    return out

PLACEHOLDER = re.compile(r"KEEPMUM_(" + "|".join(TYPES) + r")_([0-9a-f]{8}(?:[0-9a-f]{4})?)")
# A placeholder as a model may rewrite it, building a file name or a slug: lower case, hyphens.
SPELLED = re.compile(r"(?i)keepmum[_-](" + "|".join(TYPES) + r")[_-]([0-9a-f]{8}(?:[0-9a-f]{4})?)(?![0-9a-f])")
_KEEPMUM = re.compile(r"(?i)keepmum")


def canonical(text: str, vault: Vault) -> str:
    """text with every placeholder this vault made written exactly, however the model spelled it
    (`keepmum-secret-1a2b3c4d` is `KEEPMUM_SECRET_1a2b3c4d`). The hex digits are an HMAC: a spelling
    of a placeholder the vault knows is that placeholder. Others are left as they are."""
    if not _KEEPMUM.search(text):
        return text

    def fix(m):
        hex_ = m.group(2)
        exact = f"KEEPMUM_{m.group(1).upper()}_{hex_.lower()}"
        if exact == m.group(0):
            return exact
        if vault.value(exact) is not None:
            return exact
        if len(hex_) == 12 and vault.value(exact[:-4]) is not None:  # 8 digits, then text that is hex
            return exact[:-4] + hex_[8:]
        return m.group(0)

    return SPELLED.sub(fix, text)


def install_salt() -> bytes:
    """The per-install HMAC key, in ~/.config/keepmum/salt (0600), shared by every keepmum on this Mac.

    Every process must use the same salt: a placeholder made by one (the app's service, `keepmum
    claude`, the VS Code wrapper) is only restored by a process that derives the same one. The
    Keychain cannot be shared silently, since an item is readable without a prompt only by the
    program that created it, and each install of keepmum is a different program.
    """
    path = salt_file()
    if path.exists():
        return bytes.fromhex(path.read_text().strip())
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    salt = secrets.token_bytes(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # another keepmum created it first: use theirs
        return bytes.fromhex(path.read_text().strip())
    with os.fdopen(fd, "w") as f:
        f.write(salt.hex())
    return salt


def _compile(pattern: str) -> re.Pattern[str]:
    """re.compile without re's cache, which would keep up to 512 large patterns alive."""
    compiler = getattr(re, "_compiler", None)
    return compiler.compile(pattern, 0) if compiler is not None else re.compile(pattern)


def _trie_pattern(values) -> str:
    """One regex alternative for `values`, as a prefix tree: matching tries a branch per character
    rather than every value at every position. Where one value is a prefix of another, the longer
    one is tried first, as a longest-first alternation does."""
    trie: dict = {}
    for v in values:
        node = trie
        for ch in v:
            node = node.setdefault(ch, {})
        node[""] = True

    def emit(node: dict) -> str:
        ends = "" in node
        branches = []
        for ch, child in node.items():
            if ch == "":
                continue
            run = ch
            while len(child) == 1 and "" not in child:  # a chain of single children: one literal
                (nxt, child), = child.items()
                run += nxt
            branches.append(re.escape(run) + emit(child))
        if not branches:
            return ""
        body = branches[0] if len(branches) == 1 else "(?:" + "|".join(branches) + ")"
        return f"(?:{body})?" if ends else body

    return emit(trie)


class _Level:
    __slots__ = ("values", "pattern")

    def __init__(self, values: list[str]):
        self.values = values
        self.pattern: re.Pattern[str] | None = None

    def compiled(self) -> re.Pattern[str]:
        if self.pattern is None:
            self.pattern = _compile(rf"(?<![A-Za-z0-9])(?:{_trie_pattern(self.values)})(?![A-Za-z0-9])")
        return self.pattern


class _Found:
    """A match from the first level, read as a regex match is."""

    __slots__ = ("_a", "_b", "_v")

    def __init__(self, a: int, b: int, v: str):
        self._a, self._b, self._v = a, b, v

    def start(self) -> int:
        return self._a

    def end(self) -> int:
        return self._b

    def group(self) -> str:
        return self._v


class _Known:
    """Every masked value's exact occurrences, not inside a longer word: a masked "Anna" leaves
    "Annapolis" alone. The levels hold only the vault's values (_evict builds them again when it
    lets some go); a match the vault no longer keeps is passed over all the same, without hiding a
    shorter value that starts there."""

    def __init__(self, levels: list[_Level], keeps):
        self._plain = list(levels[0].values) if levels else []
        self._patterns = [level.compiled() for level in levels[1:] if level.values]
        self._keeps = keeps

    def finditer(self, text: str):
        # The first level as a pattern would match: leftmost first, the longest there, none overlapping.
        found = sorted(((a, b, v) for v in self._plain if self._keeps(v) for a, b in occurrences(v, text)),
                       key=lambda m: (m[0], m[0] - m[1]))
        end = -1
        for a, b, v in found:
            if a >= end:
                yield _Found(a, b, v)
                end = b
        yield from self._levels_finditer(text)

    def sub(self, repl, text: str) -> str:
        """As re.Pattern.sub: each match replaced by repl(match), leftmost first, the longest
        there, none overlapping, as one pattern over every value would."""
        found = sorted(self.finditer(text), key=lambda m: (m.start(), m.start() - m.end()))
        out, at = [], 0
        for m in found:
            if m.start() >= at:
                out += [text[at:m.start()], repl(m)]
                at = m.end()
        return "".join(out) + text[at:]

    def _levels_finditer(self, text: str):
        for p in self._patterns:
            pos = 0
            while (m := p.search(text, pos)) is not None:
                if self._keeps(m.group()):
                    yield m
                    pos = m.end()
                    continue
                # A value let go: a shorter one still kept may start here ("Anna" in "Anna
                # Smith"), and another may start inside it, so the scan goes on from the next place.
                end = m.end() - 1
                while end > m.start() and (short := p.match(text, m.start(), end)) is not None:
                    if self._keeps(short.group()) and not (short.end() < len(text) and _WORD.match(text, short.end())):
                        yield short
                        break
                    end = short.end() - 1
                pos = m.start() + 1


class Vault:
    def __init__(self, salt: bytes | None = None, max_values: int = MAX_VALUES):
        self._salt = salt if salt is not None else install_salt()
        self._max = max_values
        self._values: dict[str, str] = {}  # placeholder -> value
        self._placeholders: dict[tuple[str, str], str] = {}  # (type, value) -> placeholder
        self._kinds: OrderedDict[str, str] = OrderedDict()  # value -> type it was first masked as; least recently used first
        # value -> the detectors that found it (rules:<id>, user:<name>, pf-serve), and the tool
        # servers whose results held it (mcp__gmail). Frozen sets, one per distinct set (_shared):
        # most values share one or two, and a set each cost about 200 bytes a value.
        self._sources: dict[str, frozenset[str]] = {}
        self._origins: dict[str, frozenset[str]] = {}
        self._sets: dict[frozenset[str], frozenset[str]] = {}
        self._forms: dict[str, str] = {}  # a value as it reads escaped (encode.escaped_forms) -> its placeholder
        self._made: dict[str, list[str]] = {}  # value -> its placeholders and escaped forms, to let it go whole
        self._levels: list[_Level] = []  # the known pass's patterns, smallest first (FIRST_LEVEL)
        self._in_levels = 0  # values and forms in them, gone ones too
        self._known: _Known | None = None
        self._learned = 0  # values and forms learned so far: Vault.mark()
        self._log: deque[str] = deque(maxlen=LEARNED_LOG)  # the last of them, in order

    def __repr__(self) -> str:  # never show values, even in a traceback
        return f"<Vault {len(self._values)} values>"

    def __len__(self) -> int:
        return len(self._values)

    def placeholder(self, kind: str, value: str, sources=(), origin: str | None = None) -> str:
        if value in self._forms and value not in self._kinds:  # a value keepmum put back escaped
            ph = self._forms[value]
            self._touch(self._values.get(ph))
            return ph
        found = {x for x in sources if x and x != "known"}
        had = self._sources.get(value)
        if had is None or not found <= had:
            self._sources[value] = self._shared((had or frozenset()) | found)
        if origin and origin not in self._origins.get(value, ()):
            self._origins[value] = self._shared(self._origins.get(value, frozenset()) | {origin})
        key = (kind, value)
        if key in self._placeholders:
            self._touch(value)
            return self._placeholders[key]
        digest = hmac.new(self._salt, f"{kind}\0{value}".encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()
        ph = f"KEEPMUM_{kind}_{digest[:8]}"
        if self._values.get(ph, value) != value:  # collision with a different value
            ph = f"KEEPMUM_{kind}_{digest[:12]}"
        self._values[ph] = value
        self._placeholders[key] = ph
        made = self._made.setdefault(value, [])
        made.append(ph)
        if value not in self._kinds:
            self._kinds[value] = kind
            self._learn(value)
        self._touch(value)
        from .encode import escaped_forms

        for form in escaped_forms(value):
            if len(form) >= MIN_KNOWN and form not in self._forms:
                self._forms[form] = ph
                made.append(form)
                self._learn(form)
        if len(self._kinds) > self._max:
            self._evict()
        return ph

    def _shared(self, items: frozenset[str]) -> frozenset[str]:
        if len(self._sets) > 4096:  # combinations of detectors and tool servers: a few, in practice
            self._sets.clear()
        return self._sets.setdefault(items, items)

    def _touch(self, value: str | None) -> None:
        if value is not None and value in self._kinds:
            self._kinds.move_to_end(value)

    def _keeps(self, value: str) -> bool:
        return value in self._kinds or value in self._forms

    def _learn(self, value: str) -> None:
        """A new value for the known pass: into the smallest level, a full one merged up."""
        if len(value) < MIN_KNOWN:
            return
        self._learned += 1
        self._log.append(value)
        if not self._levels:
            self._levels.append(_Level([]))
        self._levels[0].values.append(value)
        self._levels[0].pattern = None
        self._in_levels += 1
        i, size = 0, FIRST_LEVEL
        while len(self._levels[i].values) > size:  # full: into the next level up, made if need be
            if i + 1 == len(self._levels):
                self._levels.append(_Level([]))
            level, up = self._levels[i], self._levels[i + 1]
            up.values += [v for v in level.values if self._keeps(v)]
            up.pattern = None
            level.values, level.pattern = [], None
            i, size = i + 1, size * LEVEL_GROWTH
        self._known = None

    def _evict(self) -> None:
        """Let the oldest values go, EVICT_SHARE of the cap at once, and build the known pass's
        patterns again from what is left: a value let go is in no pattern after, and no log. One
        rebuild per many values costs little (about 0.2 s for 9,000, once per 1,000 new ones); one
        per value cost as much each time."""
        keep = max(0, self._max - max(1, int(self._max * EVICT_SHARE)))
        while len(self._kinds) > keep:
            self._forget(next(iter(self._kinds)))
        live = [v for v in [*self._kinds, *self._forms] if len(v) >= MIN_KNOWN]
        # Into the level whose size holds them all, under empty smaller ones: new values merge up
        # into it as they would have, and the first level stays a short list.
        at, size = 1, FIRST_LEVEL * LEVEL_GROWTH
        while size < len(live):
            at, size = at + 1, size * LEVEL_GROWTH
        self._levels = [_Level([]) for _ in range(at)] + [_Level(live)]
        self._in_levels = len(live)
        # The log of what was learned held some of them too: texts scanned before look again in full.
        self._log.clear()
        self._learned += LEARNED_LOG + 1
        self._known = None

    def _forget(self, value: str) -> None:
        """Let a value go, with its placeholders and escaped forms (the patterns: _evict)."""
        self._kinds.pop(value, None)
        self._sources.pop(value, None)
        self._origins.pop(value, None)
        for item in self._made.pop(value, ()):
            if item.startswith("KEEPMUM_") and self._values.get(item) == value:
                del self._values[item]
                self._placeholders.pop((item.split("_")[1], value), None)
            elif self._forms.get(item) is not None and self._values.get(self._forms[item]) in (value, None):
                del self._forms[item]

    def value(self, placeholder: str) -> str | None:
        return self._values.get(placeholder)

    def near(self, placeholder: str, digits: int = 2) -> str | None:
        """A placeholder this vault made that `placeholder` (which it did not make) is a typo of:
        same type and length, at most `digits` hex digits different. None when there is none."""
        kind, hex_ = placeholder.split("_")[1], placeholder.split("_")[2]
        for ph in self._values:
            p_kind, p_hex = ph.split("_")[1], ph.split("_")[2]
            if p_kind == kind and len(p_hex) == len(hex_) and sum(a != b for a, b in zip(p_hex, hex_)) <= digits:
                return ph
        return None

    def mark(self) -> int:
        """How many values (and forms) the known pass has learned: for learned_since."""
        return self._learned

    def keeps(self, value: str) -> bool:
        """Whether a value found by an earlier known pass is still the vault's."""
        return self._keeps(value)

    def learned_since(self, mark: int) -> list[str] | None:
        """The values learned after `mark`, or None when that is further back than the log goes."""
        n = self._learned - mark
        if n < 0 or n > len(self._log):
            return None
        return list(self._log)[len(self._log) - n:] if n else []

    def known_values(self) -> _Known | None:
        """Every masked value's exact occurrences, for the known-values pass (`finditer`). A match
        must not sit inside a longer word: a masked "Anna" leaves "Annapolis" alone."""
        if self._known is None and self._in_levels:
            self._known = _Known(self._levels, self._keeps)
        return self._known

    def _real(self, value: str) -> str:
        """The value itself, for one of its escaped forms."""
        return self._values[self._forms[value]] if value in self._forms and value not in self._kinds else value

    def kind_of(self, value: str) -> str:
        return self._kinds.get(self._real(value), "SECRET")

    def sources_of(self, value: str) -> frozenset[str]:
        return self._sources.get(self._real(value), frozenset())

    def origins_of(self, value: str | None) -> frozenset[str]:
        """The tool servers that returned this value: they may have it back (tools.py)."""
        return self._origins.get(self._real(value), frozenset()) if value is not None else frozenset()

    def clear(self) -> None:
        self._values.clear()
        self._placeholders.clear()
        self._kinds.clear()
        self._sources.clear()
        self._origins.clear()
        self._sets.clear()
        self._forms.clear()
        self._made.clear()
        self._levels, self._in_levels = [], 0
        self._log.clear()
        self._learned += LEARNED_LOG + 1  # every earlier mark now looks again in full
        self._known = None
