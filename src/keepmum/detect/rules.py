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
# What may follow an escaped line break for the value to end there: the end, punctuation, the next name, or
# a heredoc's last line (`DB_PASSWORD=…\nEOF`: val-02 on agy, Nov 19).
_AFTER_VALUE = re.compile(r"""$|[\s,;)\]}>"'`\\+#*\[{<-]|[A-Za-z_][\w.-]*[ \t]*[=:]|[A-Z][A-Z0-9_]+(?:$|[\s\\"'`])""")
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
        if value != value.strip():
            return None  # a secret has no space at its edge: "…`:` will securely use the variable. The `" (rotate-01 on agy)
        if value[:1] in ",+)]};":
            # The quote closed a label and code follows: `print('  ACCESS_KEY_USER:',a.get('user'))`
            # (cloud-01 on opencode, Nov 21). A secret does not start with an operator.
            return None
        if any(c.isspace() for c in value):
            words = value.split()
            if not (_UPPER_KEY.fullmatch(key) or (len(words) <= 4 and char_classes("".join(words)) >= 3)):
                return None
            # A command or a phrase, not a password: "Store it as a secret: `gh secret set FLY_API_TOKEN`"
            # (deploy-01 on Claude Code, Nov 18). A password with spaces does not start with a plain word.
            if not _UPPER_KEY.fullmatch(key) and words[0].isalpha() and words[0].islower():
                return None
        return close
    if text[start:start + 1] in ("|", ")", "]", "}"):
        # An alternation or a closing bracket follows the key, not a value: `grep -E 'token:|secret'`, an
        # agent checking its own doc for leaks (cloud-01 on Claude Code, Nov 22). A secret starts with neither.
        return None
    if _NESTED.match(text, start):
        # An object or list nested under the key, its own first key quoted: `'secretRef': {'name': 'orders-env'}`,
        # a Deployment printed by Python (k8s-01 on Claude Code, Nov 23). An LDAP hash `{SSHA}…` still counts.
        return None
    if text[start:start + 1] == "/" and (start + 1 >= n or text[start + 1] in "{,;)|" or text[start + 1].isspace()):
        # The `/` closing a pattern the key was in: awk's `/_authToken=/{sub(/=.*/, …)}` (git-01 on pi, Nov 23).
        return None
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
        if c == "<" and text.startswith("</", i):  # a closing tag ends it: a .docx's XML (doc-03, Nov 18)
            break
        i += 1
    if _EMPTY_ASSIGNMENT.fullmatch(text[start:i]):
        # Another name set to nothing, printed by a script: `aws_iam_access_key.ci: ARN=None`
        # (cloud-01 on Codex, Nov 24). No value is in it.
        return None
    if _VARIABLE.fullmatch(text[start:i].replace("\\", "").rstrip("$")):
        # A variable's name, escaped for a pattern: `re.search(r'_authToken=\$\{NPM_TOKEN\}$', npmrc)`
        # checks that .npmrc names the variable (git-01 on opencode, Nov 23). The reference, not a value.
        return None
    return i


# An object or list opening where a value would: `{'name':`, `[\"a\",`, its first key or item quoted.
_NESTED = re.compile(r"""[{\[]\s*(?P<q>\\*['"])[\w.-]*(?P=q)\s*[:,\]}]""")
# `NAME=None`, `name=null`: a name set to an empty literal, where a value would be.
_EMPTY_ASSIGNMENT = re.compile(r"[A-Za-z_][\w.-]{0,40}=(?:None|null|NULL|Null|nil|undefined|N/A|)")
# `$NAME` or `${NAME}`: a variable the value comes from, not the value.
_VARIABLE = re.compile(r"\$\{[A-Za-z_]\w*\}|\$[A-Z_][A-Z0-9_]*")  # not `$ecr3tPass`: a password may start with `$`


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
    key that happens to hold a slash (wJalrXUtnFEMI/K7MDENG/…, whose pieces mix case at random). A
    piece may be a hash, as in a link to a commit's file (`…/blob/537a48f3…/Misc/NEWS.d/…`, cut at
    the hash when pi wrapped the page: wf-03, Nov 24), with two named pieces beside it."""
    if "/" not in value:
        return False
    if _PATH_OF_WORDS.fullmatch(value):
        return True
    named = _HASH_PIECE.sub("h", value)
    return named != value and sum(1 for x in named.split("/") if x and x != "h") >= 2 and bool(_PATH_OF_WORDS.fullmatch(named))


_HASH_PIECE = re.compile(r"(?<![^/])[0-9a-f]{7,64}(?![^/])")  # a whole piece of lowercase hex


# An absolute path on this Mac (or a Linux box), from a root folder, as tools print them: a workspace,
# macOS's per-user temp folder (/var/folders/ng/f6k2…0000gn/T/), an agent's own folders by UUID
# (~/.gemini/antigravity/brain/<uuid>/). The model takes their random-looking pieces for secrets
# (wf-01 live, Nov 18). Not a URL's path: `https://host/Users/…` starts after a host, not at a root.
_SYSTEM_PATH = re.compile(r"(?:(?<=file://)|(?<![\w.~/:-]))(?:~|/(?:Users|private|var|tmp|home|opt|Library|Applications|"
                          r"Volumes|System|usr|etc|root|mnt|srv|workspaces?))(?:/[\w.@+~-]+)+/?")


def in_system_path(text: str, start: int, end: int) -> bool:
    """text[start:end] lies inside an absolute file-system path (_SYSTEM_PATH)."""
    if "/" not in text[max(0, start - 1):end + 1]:
        return False
    begin = max(0, text.rfind(" ", 0, start), text.rfind("\n", 0, start))  # the token the path is in
    for m in _SYSTEM_PATH.finditer(text, begin):
        if m.start() > start:
            return False
        if end <= m.end():
            return True
    return False


# A pin: something fixed to a hash so it can be checked, made to be copied. A Docker image by digest
# (python:3.12-slim@sha256:<64 hex>), an Action or a module pinned to a commit (actions/setup-node@<40
# hex>), a commit or digest on its own. The model takes them for secrets (deploy-01, code-02 live, Nov 18).
# Bare hex is a key as often as a hash (a wallet's private key, an AES key): it counts as a hash only
# with no secret word right before it, and the hex-key rules catch a key named as one.
_PIN = re.compile(r"(?:[\w./-]*(?::[\w.-]+)?@)?(?:sha(?:1|224|256|384|512):)?[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24}|[0-9a-fA-F]{56}"
                  r"|[0-9a-fA-F]{88})?")
_HASH_MARK = re.compile(r"@|sha(?:1|224|256|384|512):")
# A nonce, sent to every browser that loads the page: `nonce="…"`, CSP's `'nonce-…'` (fe-01, Nov 18).
_NONCE_BEFORE = re.compile(r"(?i)nonce(?:-|\s*=\s*\\*[\"']?)$")  # a quote escaped too: JSON text in a string


# A word that names what follows as a secret ("the wallet's private key is 4c0883…", "token: 9f…").
_SECRET_WORD_BEFORE = re.compile(r"(?i)\b(?:api[ _-]?key|key|token|secret|passw(?:or)?d|pwd|passphrase|auth|credentials?"
                                 r"|bearer|seed)\b")


def is_pin(value: str) -> bool:
    """value is a pin by hash (_PIN) that says which hash it is (`@`, `sha256:`)."""
    return bool(_PIN.fullmatch(value)) and bool(_HASH_MARK.search(value))


# A package, plugin or tool by name and where it comes from or which version: Codex lists its plugins as
# `figma@openai-curated-remote`, and the model took them for secrets (wf-01 on Codex, Nov 18); npm's
# `react@18.3.1`, Homebrew's `node@20`. Words and numbers only, the registry without a dot (so not an
# email): a token after an @ mixes letters and digits in one piece and is still masked.
_PACKAGE_REF = re.compile(r"@?[a-z][a-z-]*(?:/[a-z][a-z-]*)?@(?:[a-z]+(?:-[a-z]+)*|v?\d+(?:\.\d+)*(?:-[a-z]+(?:\.\d+)?)?)")


def is_package_ref(value: str) -> bool:
    """value names a package, plugin or tool and its registry or version (_PACKAGE_REF)."""
    return bool(_PACKAGE_REF.fullmatch(value))


# A constant's or a variable's name: STRIPE_SECRET_KEY as a YAML key in a Kubernetes Secret, which the
# model took for the secret (k8s-01, Nov 18); masked once, keepmum masked the name everywhere after.
_CONSTANT_NAME = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")


def is_constant_name(value: str) -> bool:
    """value is an UPPER_SNAKE name whose parts are words (_CONSTANT_NAME), not a key's characters."""
    return bool(_CONSTANT_NAME.fullmatch(value)) and all(
        part.isalpha() or part.isdigit() for part in value.split("_"))


# A checksum that says what it is: Subresource Integrity (`sha512-…`, npm and pnpm lockfiles) and
# Go's module hashes (`h1:…`). The model took them for secrets (lock-01, Nov 18).
_LABELED_DIGEST = re.compile(r"(?:sha(?:1|256|384|512)-|h1:)[A-Za-z0-9+/]+={0,2}")
# An object's id, made to be shown and quoted: Stripe's and its imitators' prefixed ids (ord_7Hc2:
# debug-01). Never a key's prefix (sk_, rk_, whsec_), which their own rules mask.
_OBJECT_ID = re.compile(r"(?:ord|cus|ch|pi|pm|in|il|ii|sub|si|prod|price|evt|req|acct|txn|re|po|src|card|seti|"
                        r"cs|usr|org|prj|inv|item|sku|cart|ship)_[A-Za-z0-9]{2,32}")


# A host's name, with its port: an RDS endpoint or an EKS server has a random-looking label, which the
# model took for a secret (cloud-01, Nov 18). Dotted labels ending in a known top-level domain.
_HOSTNAME = re.compile(r"(?i)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:com|net|org|io|dev|app|ai|cloud|co|"
                       r"internal|local|lan|corp|edu|gov|us|uk|de|fr|jp|cn|eu)(?::\d{1,5})?")


def is_hostname(value: str) -> bool:
    return bool(_HOSTNAME.fullmatch(value))


# A ticket's, an invoice's or an order's reference (OPS-2291, INV-2026-0412): made to be quoted
# (doc-01, doc-02 on Codex, Nov 18). Capitals, then numbers between hyphens.
_REFERENCE = re.compile(r"[A-Z]{2,10}(?:-\d{1,8})+")
# A key's prefix alone (`sk_live_`), words and separators only: what a key looks like, not one (doc-02).
_PREFIX_ONLY = re.compile(r"[A-Za-z]+(?:[_-][A-Za-z]+)*[_-]")


def is_reference(value: str) -> bool:
    return bool(_REFERENCE.fullmatch(value) or _PREFIX_ONLY.fullmatch(value))


_PATH_TOKEN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/~@+-")


def in_path_token(text: str, start: int, end: int) -> bool:
    """text[start:end] is part of a path of named pieces: the model began `ploy_ed25519` inside
    `keys/deploy_ed25519` (srv-01 on opencode, Nov 18). The whole token decides (is_path)."""
    a, b = start, end
    while a > 0 and text[a - 1] in _PATH_TOKEN_CHARS:
        a -= 1
    while b < len(text) and text[b] in _PATH_TOKEN_CHARS:
        b += 1
    token = text[a:b].strip("./-")
    return (a, b) != (start, end) and is_path(token)


# A dot-file's name: .gitignore, .npmrc, .env.example (cloud-01 on Claude Code: "show .gitignore", Nov 19).
_DOTFILE = re.compile(r"\.[a-z][a-z0-9_-]{1,30}(?:\.[a-z0-9_-]{1,12})*")


def is_dotfile(value: str) -> bool:
    return bool(_DOTFILE.fullmatch(value))


def is_markup(value: str) -> bool:
    """A tag's text: `</STEP_9>`, an agent's own `<…_OUTPUT>` (agy, doc-03)."""
    return "</" in value or "><" in value


def is_labeled_digest(value: str) -> bool:
    return bool(_LABELED_DIGEST.fullmatch(value))


_B64_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
# SHA256:… an SSH key's fingerprint (srv-01); "Integrity SHA-512: `…`" in a model's own table (lock-01, Nov 19).
_DIGEST_LABEL_BEFORE = re.compile(r"(?i)(?:sha-?(?:1|224|256|384|512)|h1|md5|integrity|checksum|digest)[\s:=`'\"-]{0,4}$")


def in_labeled_digest(text: str, start: int, end: int) -> bool:
    """text[start:end] is part of a labeled checksum: the model masked the base64 alone, or a piece of
    it, without its `h1:` or `sha512-` (lock-01, Nov 18)."""
    a = start
    while a > 0 and text[a - 1] in _B64_CHARS:
        a -= 1
    return bool(_DIGEST_LABEL_BEFORE.search(text, max(0, a - 16), a))


_HOST_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-:")


def in_hostname(text: str, start: int, end: int) -> bool:
    """text[start:end] is part of a host's name: the model stopped an RDS endpoint at `.rds` (cloud-01)."""
    a, b = start, end
    while a > 0 and text[a - 1] in _HOST_CHARS:
        a -= 1
    while b < len(text) and text[b] in _HOST_CHARS:
        b += 1
    return "." in text[start:end] and is_hostname(text[a:b].strip(".:-"))


# A pattern, not a value: a character class with a range, an alternation, an escape class
# (`STRIPE_KEY|STRIPE_SECRET_KEY|sk_(live|test)_`, `?-?[A-Za-z0-9+/=]+`: an agent's grep, Nov 18;
# `openai|langchain_openai|google.generativeai|…`, in Claude Code's own instructions; `KEEPMUM|AKIA|token:|secret`,
# cloud-01 on Claude Code, Nov 22), or a name anchored as a pattern (`grep 'deploy_ed25519$'`: srv-01 on Codex).
_REGEX_SHAPE = re.compile(r"\[[^\]]*\w-\w[^\]]*\]|\([^()]*\|[^()]*\)|\\[dwsbS]|^[\w.():|-]+\|[\w.():|-]+$|\\\|"  # `\|`: grep's alternation
                          r"|^\^?[a-z][a-z0-9]*(?:[_.-][a-z0-9]+)+\$$|^\^[a-z][a-z0-9]*(?:[_.-][a-z0-9]+)+$")


def is_regex(value: str) -> bool:
    return bool(_REGEX_SHAPE.search(value))


def is_object_id(value: str) -> bool:
    return bool(_OBJECT_ID.fullmatch(value))


def is_bare_hash(text: str, start: int, end: int) -> bool:
    """text[start:end] is hex of a hash's length (a commit, a digest, a transaction or block hash
    written 0x…) with no secret word in the 40 characters before it: hex named a key is one (Nov 18)."""
    value = text[start:end]
    if value[:2] in ("0x", "0X"):
        value = value[2:]
    elif value[:1] == "#":  # a link's fragment: yarn.lock's `….tgz#<sha1>`, with the `#` (lock-01 on Claude Code, Nov 22)
        value = value[1:]
    return (bool(_PIN.fullmatch(value)) and bool(_HEX_ONLY.fullmatch(value))
            and not _SECRET_WORD_BEFORE.search(text, max(0, start - 40), start))


def after_nonce(text: str, start: int, end: int) -> bool:
    """text[start:end] is a nonce: it starts with `nonce-`, or a nonce marker comes right before it."""
    value = text[start:end].lower()
    return (value.startswith("nonce-") or bool(_NONCE_BEFORE.search(text, max(0, start - 12), start))
            or value.startswith("-") and text[max(0, start - 5):start].lower() == "nonce")  # `nonce` | `-q7Xv…` (fe-01)


def public_shape(text: str, start: int, end: int) -> bool:
    """A guess (entropy, the model) at text[start:end] is not a secret: it is a file name or path, a
    link a provider's search tool issued, a path on this Mac, a pin by hash, a commit or digest
    not named a secret, a package or plugin id, a constant's name, a labeled
    checksum, an object's id, a lowercase word, a file in base64, or a page's nonce. A token
    rule that knows a key's format still masks it wherever it is."""
    value = text[start:end]
    return (is_file_name(text, start, end) or is_path(value) or in_issued_link(text, start, end)
            or in_system_path(text, start + _tag_end(value), end) or in_encoded_file(text, start, end) or is_pin(value) or is_bare_hash(text, start, end) or is_package_ref(value)
            or is_constant_name(value) or is_labeled_digest(value) or is_object_id(value) or is_hostname(value)
            or in_labeled_digest(text, start, end) or in_hostname(text, start, end) or is_regex(value)
            or is_reference(value) or is_markup(value) or in_path_token(text, start, end) or is_dotfile(value)
            # A word: `contexts`, a YAML key; `HttpOnly` (cloud-01, debug-01). Not a token made of letters,
            # which has capitals all through it.
            or value.isalpha() and len(value) <= 16 and sum(c.isupper() for c in value) <= 2
            or after_nonce(text, start, end))


_TAG_END = re.compile(r"[\w-]*>(?=[/~])")


def _tag_end(value: str) -> int:
    """The length of a tag's end the model took in front of a path (`path>/private/var/…`: Codex lists
    its sandbox's folders as `<path>…</path>`, lock-01, Nov 19)."""
    m = _TAG_END.match(value)
    return m.end() if m else 0


def in_issued_link(text: str, start: int, end: int) -> bool:
    """text[start:end] lies inside a link a provider's own search tool issued (_ISSUED_LINK)."""
    if "grounding-api-redirect/" not in text:
        return False
    return any(m.start() <= start and end <= m.end() for m in _ISSUED_LINK.finditer(text))


def in_encoded_file(text: str, start: int, end: int) -> bool:
    """text[start:end] lies inside a file in base64 that starts with the file's own signature (_ENCODED_FILE)."""
    if not _FILE_MAGIC_HINT.search(text):
        return False
    return any(m.start() <= start and end <= m.end() for m in _ENCODED_FILE.finditer(text))


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
# A lowercase word that more words follow is prose; one an operator follows is a variable in code
# (`secret = directory / 'secret.yaml'`, k8s-01 on Codex, Nov 18).
_PROSE_NEXT = re.compile(r" (?:[a-z]|[-+*/%|&] )")
_ENTROPY_CANDIDATE = re.compile(r"[A-Za-z0-9+/_-]{20,}={0,2}")
_DIGEST_PREFIX = re.compile(r"(?i)(?:sha(?:1|224|256|384|512)|md5|nonce)[-:]")  # CSP: 'sha256-…', 'nonce-…'
_HEX = re.compile(r"[0-9a-fA-F]+")
_HEX_ONLY = _HEX
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# 13-19 digits, optionally grouped by single spaces or hyphens.
# Public keys and certificates are long base64 by design; entropy hits inside them are noise.
_PUBLIC_PEM = re.compile(r"-----BEGIN (?:PUBLIC KEY|RSA PUBLIC KEY|CERTIFICATE|X509 CRL|SSH2 PUBLIC KEY|PGP PUBLIC KEY BLOCK)-----[\s\S]*?-----END [A-Z0-9 ]+-----")
# Links a provider's own search tool hands the model: Google's grounding redirects (the Antigravity
# CLI's search_web, Gemini CLI's google_web_search). Their opaque handle is Google's own, made for the
# model; masked, the model loses its sources and Google learns nothing (wf-03, Nov 18). Only these
# hosts: a token in another URL (a Slack webhook, a presigned link) is still a guess worth masking.
# A file in base64, from its signature: PNG, JPEG, GIF, WebP or WAV (RIFF), PDF, zip (and docx, xlsx).
# Its lines may be wrapped, by new lines or, in JSON text, by escaped ones (GitHub's contents API wraps
# at 60). A secret never starts with a file's magic bytes; the model flagged the last line of a PNG
# Codex fetched (wf-02 on Codex, Nov 18).
# A certificate or a public key, PEM in base64 (a kubeconfig's certificate-authority-data: cloud-01).
_FILE_MAGIC = r"(?:iVBORw0KGgo|/9j/|R0lGOD[lh]h|UklGR|JVBERi0|UEsDB|LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0t|LS0tLS1CRUdJTiBQVUJMSUMgS0VZLS0tLS)"
_FILE_MAGIC_HINT = re.compile(_FILE_MAGIC)
# Codex cuts a long output's middle out, in place: `…1234 tokens truncated…`. A file's base64 runs on across it
# (a notebook's chart, nb-01 on Codex, Nov 23: the model flagged the run on each side of the cut).
_CUT = r"…\d+ tokens truncated…"
_ENCODED_FILE = re.compile(r"(?<![A-Za-z0-9+/])" + _FILE_MAGIC + r"(?:[A-Za-z0-9+/]|\\{1,3}[nr]|\r?\n|" + _CUT + r")*={0,2}")  # padding ends it
_ISSUED_LINK = re.compile(r"https?://vertexaisearch\.cloud\.google\.com/grounding-api-redirect/[A-Za-z0-9_=-]+")
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


# Runs of identical characters mark a placeholder when they are at least this share of the value
# (rules.toml, filters.repeated). 0: any run, as before Nov 17, 2026.
REPEATED_SHARE = 1 / 3


SEQUENTIAL_SHARE = 2 / 3
SEQUENTIAL_RUN = 6


class _Sequential:
    """Matches (search) a value made up of the alphabet or the digits in order, runs of
    SEQUENTIAL_RUN or more covering SEQUENTIAL_SHARE of it: AbCdEfGhIjKl…, abcdef123456,
    1234567890. A fake token an agent writes for a test (self-01, Nov 18); a real key has no such run.
    A real password that only holds one (Password0123456789!) is still a secret."""

    def search(self, value: str):
        low, total, run = value.lower(), 0, 1
        for i in range(1, len(low) + 1):
            if i < len(low) and low[i].isalnum() and low[i - 1].isalnum() and ord(low[i]) == ord(low[i - 1]) + 1:
                run += 1
                continue
            if run >= SEQUENTIAL_RUN:
                total += run
            run = 1
        return total > 0 and total >= SEQUENTIAL_SHARE * len(value)


class _PathReference:
    """The `reference` filter's paths: a value that starts like a path (`/`, `./`, `../`, `~/`) and is
    one of named pieces (is_path), a dot-file's leading dot aside. Not a key in base64 that happens
    to start with `/`: the filter let every such AWS secret key through (cloud-01, Nov 19)."""

    def search(self, value: str):
        if not value.startswith(("/", "./", "../", "~/")):
            return False
        flat = "/".join(piece.lstrip(".") for piece in value.lstrip("~").split("/"))
        return is_path(flat) or is_path("." + flat if flat.startswith("/") else flat)


class _Repeated:
    """The `repeated` filter: matches (search) when runs of one character, six or more, are at least
    REPEATED_SHARE of the value. A placeholder is mostly such runs; a real token has one by chance."""

    def __init__(self, runs: re.Pattern[str]):
        self.runs = runs

    def search(self, value: str):
        total = sum(m.end() - m.start() for m in self.runs.finditer(value))
        return total > 0 and total >= REPEATED_SHARE * len(value)


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
        self._placeholder_filters = [re.compile(f[k]) for k in ("stopwords", "placeholder", "public") if k in f]
        if "repeated" in f:  # not a plain search: how much of the value its runs are (_Repeated)
            self._placeholder_filters.append(_Repeated(re.compile(f["repeated"])))
        self._placeholder_filters.append(_Sequential())
        self._all_filters = self._placeholder_filters + [re.compile(f[k]) for k in ("reference", "numeric") if k in f] + [_PathReference()]
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
                    if text[max(0, m.start("key") - 3):m.start("key")] == "://":
                        continue  # a URL's user and password (https://x-access-token:ghp_…@github.com): the URL rule's (git-01)
                    start, opening = m.end(), m.group("open") or ""
                    if opening and closes_a_string(text, start - len(opening), opening):
                        continue
                    # A shell default, `${GITHUB_TOKEN:-<value>}`: the value is between `:-` and `}`
                    # (restart-01 on Claude Code, Nov 19).
                    shell_default = text[max(0, m.start("key") - 2):m.start("key")] == "${" and text[start:start + 1] in ("-", "=", "?", "+")
                    if shell_default:
                        start += 1
                    end = assignment_value(text, start, opening, m.group("key"))
                    if end is not None and shell_default and "}" in text[start:end]:
                        end = text.index("}", start, end)
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
        if "grounding-api-redirect/" in text:
            public += [m.span() for m in _ISSUED_LINK.finditer(text)]
        if _FILE_MAGIC_HINT.search(text):
            public += [m.span() for m in _ENCODED_FILE.finditer(text)]
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
