"""L1: regex rules, Shannon entropy, and Luhn and IBAN checksums."""

from __future__ import annotations

import math
import re
import tomllib
from collections import Counter
from dataclasses import dataclass
from functools import cache
from importlib import resources
from pathlib import Path

from . import Span

SOURCE = "rules"
# Checks that are not regex rules, by the name they report (rules:entropy) and their type.
CHECKS = {"entropy": "SECRET", "luhn": "ACCOUNT", "iban": "ACCOUNT"}


@cache
def builtin_rules() -> dict[str, str]:
    """Every built-in rule and check that can be turned off ([rules] off), with its type."""
    raw = tomllib.loads(resources.files("keepmum.detect").joinpath("rules.toml").read_text())
    return {**{r["id"]: r.get("type", "SECRET") for r in raw.get("rule", [])}, **CHECKS}


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    regex: re.Pattern[str]
    group: int = 0
    keywords: tuple[str, ...] = ()
    confidence: float = 0.99
    type: str = "SECRET"
    filters: str | bool = "placeholder"
    prose_guard: bool = False
    min_classes: int = 0  # of lowercase, uppercase, digits, other: below this the value is a word, not a secret
    user: bool = False  # written by the user ([rules] mask), reported as user:<name>
    # "assignment": the regex ends where the value starts (its groups: "key", the name, and "open",
    # an opening quote if any), and assignment_value finds where the value ends.
    scan: str | None = None


# What may follow the end of an unquoted value: the end, a space, punctuation, another escape, or
# the next KEY= (in text that is itself a JSON or JavaScript string, after an escaped new line).
_AFTER_VALUE = re.compile(r"""$|[\s,;)\]}>"'`\\+#*\[{<-]|[A-Za-z_][\w.-]*[ \t]*[=:]""")
_AFTER_QUOTE = re.compile(r"""$|[\s,;)\]}>"'`\\+]""")
_UPPER_KEY = re.compile(r"[A-Z0-9_.-]*[A-Z][A-Z0-9_.-]*")


def assignment_value(text: str, start: int, opening: str, key: str, limit: int = 200) -> int | None:
    """Where the value of `key = value` that starts at `start` ends, or None: it is no value.

    Quoted (`opening` is its quote as the text writes it: `"`, or `\\"` in a JSON string): the value
    runs to the same quote on the same line, spaces and all. A value with spaces is taken after an
    environment-style key (DB_PASSWORD="p@ss w0rd $X"), or when it is a few words mixing three kinds
    of character; not UI text ("passwordHint": "Use at least 8 characters").

    Unquoted: the value runs to whitespace, `,`, `;` or a backtick. A quote, or an escape in text
    that is itself a JSON or JavaScript string (`\\n`, `\\"`), ends it only where what follows looks
    like an end: `Tr0ub4dor"&3x` is one value; in `ghp_…\\nLOG_LEVEL=info` the value is the token.
    """
    n = len(text)
    stop = min(n, start + limit)
    if opening:
        close = text.find(opening, start, stop + len(opening))
        while close > start and len(opening) == 1 and text[close - 1] == "\\":  # an escaped quote inside
            close = text.find(opening, close + 1, stop + 1)
        if close < 0 or "\n" in text[start:close]:
            return None
        value = text[start:close]
        if any(c.isspace() for c in value):
            words = value.split()
            if not (_UPPER_KEY.fullmatch(key) or (len(words) <= 4 and char_classes("".join(words)) >= 3)):
                return None
        return close
    i = start
    while i < stop:
        c = text[i]
        if c.isspace() or c in ",;`":
            break
        if c == "\\":
            # A run of backslashes and the character after it is one escape, at whatever depth the
            # text is escaped: `\n` in a JSON string, `\\n` in JSON inside a JSON string.
            j = i
            while j < n and text[j] == "\\":
                j += 1
            nxt = text[j] if j < n else ""
            if (nxt and nxt in "nrt" and _AFTER_VALUE.match(text, j + 1)) or (nxt and nxt in "\"'" and _AFTER_QUOTE.match(text, j + 1)):
                break
            i = j + 1
            continue
        if c in "\"'" and _AFTER_QUOTE.match(text, i + 1):
            break
        i += 1
    return i


def closes_a_string(text: str, at: int, quote: str) -> bool:
    """Whether the quote at `at`, right after `KEY=`, closes a string the same word opened.

    In `grep '^GITHUB_TOKEN=' .env > out && echo 'REGION=eu'` the quote after `=` ends the grep
    pattern; read as the start of a value, ` .env > out && echo ` was masked as a secret (a live
    Claude Code run, base-01). Only the word the key is in is counted, so an apostrophe elsewhere on
    the line ("don't") changes nothing, and a quoted key (`"API_KEY": "…"`) is balanced.
    """
    word = at
    while word > 0 and not text[word - 1].isspace():
        word -= 1
    before = text[word:at]
    if len(quote) == 1:
        before = before.replace("\\" + quote, "")
    return before.count(quote) % 2 == 1


class Allowlist:
    def __init__(self, strings=(), patterns=()):
        self._builtin = (frozenset(strings), [re.compile(p) for p in patterns])
        self.strings, self.patterns = self._builtin[0], list(self._builtin[1])

    def extend(self, strings=(), patterns=()) -> None:
        """Add user entries; the built-in ones always stay."""
        self.strings = self.strings | frozenset(strings)
        self.patterns += [re.compile(p) for p in patterns]

    def set_user(self, strings=(), patterns=()) -> None:
        """Replace the user entries (the config changed); the built-in ones always stay."""
        self.strings = self._builtin[0] | frozenset(strings)
        self.patterns = self._builtin[1] + [re.compile(p) for p in patterns]

    def allows(self, value: str) -> bool:
        if value in self.strings:
            return True
        return any(p.fullmatch(value) for p in self.patterns)

    def filter(self, text: str, spans: list[Span]) -> list[Span]:
        return [s for s in spans if not self.allows(text[s.start : s.end])]


# File names and paths: what an agent's tools list and read all day. The entropy check and the model
# take some for secrets (bench/results/PHASE0, guard-20261104-231405.md); a key has no extension.
FILE_EXT = ("md|markdown|mdx|txt|rst|adoc|org|json|jsonl|ndjson|toml|ya?ml|ini|cfg|conf|lock|csv|tsv|xml|plist|"
            "py|pyi|ipynb|swift|mm?|h|hh|hpp|c|cc|cpp|rs|go|java|kt|kts|scala|rb|php|cs|lua|dart|ex|exs|"
            "js|mjs|cjs|jsx|ts|mts|cts|tsx|vue|svelte|astro|html?|css|scss|sass|less|sh|bash|zsh|fish|ps1|sql|"
            "svg|png|jpe?g|gif|webp|ico|icns|pdf|gguf|safetensors|onnx|dmg|pkg|zip|tar|gz|tgz|bz2|xz|whl|"
            "log|patch|diff|tmpl|j2|proto|graphql|tf|hcl|gradle|cmake|mk|dockerfile|storyboard|xib|xcconfig")
_FILE = re.compile(rf"(?:[\w.~@+-]*/)*[\w.@+-]*[A-Za-z0-9]\.(?:{FILE_EXT})", re.I)
_EXT_AFTER = re.compile(rf"\.(?:{FILE_EXT})(?![\w])", re.I)
_ESCAPES_AT_END = re.compile(r'(?:\\[nrt"\\/])+$')
# Detectors that guess at secrets from a string's look, not from a token's known format.
GUESSES = frozenset({"rules:entropy", "pf-serve"})


_JOINED = re.compile(r"(?<=[A-Za-z])_(?=[A-Za-z])")


def file_view(text: str) -> str:
    """text as the model should read it: in file names, an underscore between letters is a space
    (Margaret Thornbury_resume.md). The model reads `Thornbury_resume` as one word and either misses
    the name or takes the whole file name for it. Same length, so spans map back as they are."""
    if "_" not in text:
        return text
    return _FILE.sub(lambda m: _JOINED.sub(" ", m.group()), text)


# A piece of a path as people name things: one word in one case, or a few joined by - _ .
_WORDISH = r"(?:[a-z][a-z0-9]*|[A-Z][A-Z0-9]*|[A-Z][a-z0-9]+|[0-9]+)"
_PATH_OF_WORDS = re.compile(rf"\.{{0,2}}/?(?:{_WORDISH}(?:[-_.]{_WORDISH})*/)+{_WORDISH}(?:[-_.]{_WORDISH})*/?")


def is_path(value: str) -> bool:
    """value is a path of named pieces, with or without an extension (bench/results/PHASE0); not a
    key that happens to hold a slash (wJalrXUtnFEMI/K7MDENG/…, whose pieces mix case at random)."""
    return "/" in value and bool(_PATH_OF_WORDS.fullmatch(value))


def is_file_name(text: str, start: int, end: int) -> bool:
    """text[start:end] is a file name or path: it ends in a known extension, or one follows it."""
    value = _ESCAPES_AT_END.sub("", text[start:end])  # a path in JSON text: notes/plan.md\\n
    if m := _EXT_AFTER.match(text, end):
        value += m.group()
    return bool(_FILE.fullmatch(value))


def shannon_entropy(value: str) -> float:
    n = len(value)
    if n == 0:
        return 0.0
    return -sum(c / n * math.log2(c / n) for c in Counter(value).values())


def char_classes(value: str) -> int:
    """How many of lowercase, uppercase, digits and other characters a value uses."""
    return (any(c.islower() for c in value) + any(c.isupper() for c in value) + any(c.isdigit() for c in value)
            + any(not c.isalnum() for c in value))


def luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def iban_valid(iban: str) -> bool:
    """ISO 13616 mod-97: move the first four characters to the end, letters become 10..35."""
    s = iban[4:] + iban[:4]
    return int("".join(str(int(c, 36)) for c in s)) % 97 == 1


# '=' only as trailing base64 padding, so `NAME=value` never becomes one candidate.
_PROSE_WORD = re.compile(r"[a-z]+")
_PROSE_NEXT = re.compile(r" [a-z]")
_ENTROPY_CANDIDATE = re.compile(r"[A-Za-z0-9+/_-]{20,}={0,2}")
_DIGEST_PREFIX = re.compile(r"(?i)(?:sha(?:1|224|256|384|512)|md5)[-:]")
_HEX = re.compile(r"[0-9a-fA-F]+")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# 13-19 digits, optionally grouped by single spaces or hyphens.
# Public keys and certificates are long base64 by design; entropy hits inside them are noise.
_PUBLIC_PEM = re.compile(r"-----BEGIN (?:PUBLIC KEY|RSA PUBLIC KEY|CERTIFICATE|X509 CRL|SSH2 PUBLIC KEY|PGP PUBLIC KEY BLOCK)-----[\s\S]*?-----END [A-Z0-9 ]+-----")
_CARD = re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])")
# Issuer prefixes for Visa, Mastercard, Amex, Discover; cuts Luhn-valid noise like timestamps.
_CARD_IIN = re.compile(r"4|5[1-5]|2[2-7]|3[47]|6(?:011|5)")
# Country code, check digits, then the account part, compact or in groups of four.
_IBAN = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?)(?![A-Za-z0-9])")
# Countries in the SWIFT IBAN registry.
_IBAN_COUNTRIES = frozenset(
    "AD AE AL AT AZ BA BE BG BH BI BR BY CH CR CY CZ DE DJ DK DO EE EG ES FI FK FO FR GB GE GI GL GR GT HN HR HU IE IL "
    "IQ IS IT JO KW KZ LB LC LI LT LU LV LY MC MD ME MK MN MR MT MU NI NL NO OM PK PL PS PT QA RO RS RU SA SC SD SE SI "
    "SK SM SO ST SV TL TN TR UA VA VG XK YE".split()
)


class RulesDetector:
    name = SOURCE

    def __init__(self, path: Path | None = None):
        if path is None:
            raw = resources.files("keepmum.detect").joinpath("rules.toml").read_text()
        else:
            raw = path.read_text()
        cfg = tomllib.loads(raw)
        self.rules = [
            Rule(
                id=r["id"],
                regex=re.compile(r["regex"]),
                group=r.get("group", 0),
                keywords=tuple(k.lower() for k in r.get("keywords", ())),
                confidence=r.get("confidence", 0.99),
                type=r.get("type", "SECRET"),
                filters=r.get("filters", "placeholder"),
                prose_guard=r.get("prose_guard", False),
                min_classes=r.get("min_classes", 0),
                scan=r.get("scan"),
            )
            for r in cfg.get("rule", [])
        ]
        allow = cfg.get("allowlist", {})
        self.allowlist = Allowlist(allow.get("strings", ()), allow.get("patterns", ()))
        f = cfg.get("filters", {})
        self._placeholder_filters = [re.compile(f[k]) for k in ("stopwords", "placeholder", "repeated", "public") if k in f]
        self._all_filters = self._placeholder_filters + [re.compile(f[k]) for k in ("reference", "numeric") if k in f]
        self._prose_filters = self._all_filters + [re.compile(f[k]) for k in ("identifier",) if k in f]
        self.entropy = cfg.get("entropy", {"enabled": False})
        if self.entropy.get("enabled"):
            self._skip_before = re.compile(self.entropy["skip_before"])
        self.luhn = cfg.get("luhn", {"enabled": False})
        self.iban = cfg.get("iban", {"enabled": False})
        self.user_rules: list[Rule] = []
        self.off: frozenset[str] = frozenset()  # built-in rules and checks turned off ([rules] off)

    def set_user_rules(self, rules: list[dict]) -> None:
        """Your own patterns ([rules] mask): always masked, never filtered as placeholders.
        A pattern with a group masks group 1, else the whole match."""
        out = []
        for r in rules:
            rx = re.compile(r["pattern"])
            out.append(Rule(id=r["name"], regex=rx, group=1 if rx.groups else 0, type=r.get("type", "SECRET"),
                            confidence=1.0, filters=False, user=True))
        self.user_rules = out

    def looks_like_placeholder(self, value: str, level: str | bool = "placeholder") -> bool:
        if not level:
            return False
        filters = {"all": self._all_filters, "prose": self._prose_filters}.get(level, self._placeholder_filters)
        return any(p.search(value) for p in filters)

    def detect(self, text: str, personal: bool = True, allowlist: bool = True) -> list[Span]:
        """Spans over `text`. With `personal` off, only secrets and your own rules: the pipeline
        would drop personal data anyway."""
        lowered = text.lower()
        spans: list[Span] = []
        for rule in self.rules + self.user_rules:
            if not rule.user and (rule.id in self.off or not personal and rule.type != "SECRET"):
                continue
            if rule.keywords and not any(k in lowered for k in rule.keywords):
                continue
            for m in rule.regex.finditer(text):
                if rule.scan == "assignment":
                    start, opening = m.end(), m.group("open") or ""
                    if opening and closes_a_string(text, start - len(opening), opening):
                        continue
                    end = assignment_value(text, start, opening, m.group("key"))
                    if end is None or end - start < 6:
                        continue
                else:
                    start, end = m.span(rule.group)
                if start < 0 or start == end:
                    continue
                value = text[start:end]
                # Code closes calls after a value (`flag=False))`): judge the value without them.
                if self.looks_like_placeholder(value.rstrip(")]}>"), rule.filters):
                    continue
                if rule.prose_guard and _PROSE_WORD.fullmatch(value) and _PROSE_NEXT.match(text, end):
                    continue
                if rule.min_classes and char_classes(value) < rule.min_classes:
                    continue
                spans.append(Span(start, end, rule.type, rule.confidence, f"user:{rule.id}" if rule.user else f"{SOURCE}:{rule.id}"))
        if self.entropy.get("enabled") and "entropy" not in self.off:
            spans.extend(self._entropy_spans(text))
        if personal and self.luhn.get("enabled") and "luhn" not in self.off:
            spans.extend(self._card_spans(text))
        if personal and self.iban.get("enabled") and "iban" not in self.off:
            spans.extend(self._iban_spans(text))
        return self.allowlist.filter(text, spans) if allowlist else spans

    def _entropy_spans(self, text: str) -> list[Span]:
        e = self.entropy
        out = []
        public = [m.span() for m in _PUBLIC_PEM.finditer(text)] if "-----BEGIN" in text else []
        for m in _ENTROPY_CANDIDATE.finditer(text):
            if any(a <= m.start() < b for a, b in public):
                continue
            value = m.group().strip("=-_")
            n = len(value)
            if n < e["min_len"] or n > e["max_len"]:
                continue
            if _HEX.fullmatch(value) or _UUID.search(value) or _DIGEST_PREFIX.match(value):
                continue
            has_lower = any(c.islower() for c in value)
            has_upper = any(c.isupper() for c in value)
            has_digit = any(c.isdigit() for c in value)
            classes = has_lower + has_upper + has_digit
            if classes < 2 or (classes == 2 and n < 32):
                continue
            if shannon_entropy(value) < e["min_ratio"] * math.log2(min(n, 64)):
                continue
            if self._skip_before.search(text, max(0, m.start() - 24), m.start()):
                continue
            after = text[m.end() : m.end() + 1]
            if after in ("=", ":") or (m.group().endswith("=") and after and not after.isspace() and after not in "\"'`,;)]}"):
                continue  # a key name (distributionSha256Sum=...), not a value; real padding ends the value
            if self.looks_like_placeholder(value, "all"):
                continue
            start = m.start() + m.group().index(value)
            out.append(Span(start, start + n, "SECRET", e["confidence"], f"{SOURCE}:entropy"))
        return out

    def _card_spans(self, text: str) -> list[Span]:
        out = []
        for m in _CARD.finditer(text):
            digits = re.sub(r"[ -]", "", m.group())
            if 13 <= len(digits) <= 19 and _CARD_IIN.match(digits) and luhn_valid(digits):
                out.append(Span(m.start(), m.end(), "ACCOUNT", self.luhn["confidence"], f"{SOURCE}:luhn"))
        return out

    def _iban_spans(self, text: str) -> list[Span]:
        out = []
        for m in _IBAN.finditer(text):
            iban = m.group(1).replace(" ", "")
            if 15 <= len(iban) <= 34 and iban[:2] in _IBAN_COUNTRIES and iban_valid(iban):
                out.append(Span(m.start(1), m.end(1), "ACCOUNT", self.iban["confidence"], f"{SOURCE}:iban"))
        return out
