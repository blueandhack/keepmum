"""Replace sensitive spans with placeholders, in plain text and in Messages API payloads."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import re
from collections import Counter, OrderedDict

from .detect import Span
from .detect.merge import GUESSING, guessed, narrow  # noqa: F401 (GUESSING: tests)
from .detect.merge import merge
from .encode import json_strings
from .tools import call_name, item_name, server_of
from .vault import PLACEHOLDER, Vault

# Signed or binary blocks are resent exactly as the API produced them.
PASS_THROUGH = frozenset({"thinking", "redacted_thinking", "image"})
# For block types keepmum does not know, only these fields are scanned. Ids, names, signatures
# and every other protocol field stay as they are.
CONTENT_KEYS = ("text", "content", "input", "title", "context", "source", "output", "error")

# Appended to every request's system prompt, always the same text, so the prompt cache stays
# valid. Without it Claude sees KEEPMUM_SECRET_... and concludes the file holds a fake value.
PLACEHOLDER_HINT = (
    "Note from keepmum, a local privacy proxy on this machine: values of the form "
    "KEEPMUM_<TYPE>_<8 hex digits> (KEEPMUM_SECRET_xxxxxxxx, KEEPMUM_PERSON_xxxxxxxx, KEEPMUM_EMAIL_xxxxxxxx, "
    "and so on) are placeholders for real values that were redacted before this request left "
    "the user's computer. Treat each one as the real value: copy it exactly when a value is "
    "needed, and do not call it fake, masked or a placeholder. Real values are restored locally "
    "in your replies and tool calls."
)


# Responses API items that are encrypted or signed, or carry no user content.
RESPONSES_PASS_THROUGH = frozenset({"reasoning", "compaction", "item_reference", "web_search_call", "image_generation_call"})
RESPONSES_HINT = {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": PLACEHOLDER_HINT}]}
# Images and files in a tool's output (Codex's view_image, an MCP screenshot) pass through, as they
# do in a message: a rule can match a stretch of base64, and a placeholder in it breaks the data URL.
RESPONSES_BINARY_PARTS = frozenset({"input_image", "input_file"})

# Below this length the first and last characters give too much of a value away: show none.
PREVIEW_MIN = 8


def preview(value: str) -> str:
    """The first and last character of a masked value, for the Logs page: `g…w`. Shorter
    values show nothing but dots."""
    return f"{value[0]}…{value[-1]}" if len(value) >= PREVIEW_MIN else "•••"


# Tool results the injection check skips: they hold what the user chose or typed, not what a tool
# fetched (Claude Code's AskUserQuestion returns the user's answers).
NOT_FETCHED = frozenset({"AskUserQuestion"})
# Tool results an agent writes itself, not a tool's output: Claude Code's notice that it refused a
# write. The injection check scored "Claude requested permissions to edit … which is a sensitive
# file." as an attack (k8s-01, Nov 18). The path is one word, so nothing else can ride in it.
AGENT_NOTICE = re.compile(r"Claude requested permissions to (?:edit|write to|write|read|use) \S{1,400}"
                          r"(?: which is a sensitive file\.|, but you haven't granted it yet\.)"
                          # Claude Code 2.1's notice for a compound command it holds for approval, the part on one
                          # line: scored 0.9997 (k8s-01 on Claude Code, Nov 23).
                          r"|This (?:Bash )?command contains multiple operations\. The following parts? requires? approval: [^\n]{1,400}"
                          # Its notice for a command with shell operators, a fixed text: scored 0.937 (doc-02 on
                          # Claude Code, Nov 25).
                          r"|This command uses shell operators that require approval for safety\.?"
                          # A file system's error as Node prints it, from pi's and opencode's own tools:
                          # `EISDIR: illegal operation on a directory, read` scored 0.489 (wf-01 on pi, Nov 19).
                          r"|E[A-Z]{3,9}: [a-z][a-z ]{4,60}(?:, [a-z]{2,12})?(?: '\S{1,400}')?")


# Characters of a secret in a row that make a decoded dump, or a cut-off run, hold it (Redactor._holds_a_piece,
# Redactor._known_pieces).
SECRET_PIECE = 12
# An email's local part that names no one: a role or a mailbox, not a person (`support@acme.com`).
ROLE_MAILBOXES = frozenset({
    "info", "support", "admin", "contact", "sales", "hello", "team", "help", "noreply", "no-reply", "billing",
    "office", "mail", "security", "abuse", "postmaster", "webmaster", "root", "user", "test", "careers", "jobs",
    "press", "privacy", "legal", "accounts", "service", "orders", "notifications", "alerts", "feedback",
})
EMAIL_PART = 4  # the shortest local part masked on its own
_LOCAL_TOKEN = re.compile(r"(?<![\w.%+-])[A-Za-z0-9._%+-]+")  # a token an email's local part could be
# A line's number in front of it, as grep -n, cat -n and agents' read tools print one: `12:`, `     3→`, `3: `.
_LINE_NUMBER = re.compile(r"^\s*\d+(?:[:\t→|]\s?| {1,2})")
_KEYLIKE_TOKEN = re.compile(r"(?<![A-Za-z0-9+/_.-])[A-Za-z0-9+/_.-]{12,}(?![A-Za-z0-9+/_.-])")

# Characters of text kept on each side of a masked value for the Logs page's context.
CONTEXT = 80


# Characters of a flagged text kept for the Logs page's check details.
CHECK_TEXT = 4000


def _marks(text: str, spans: list[Span]) -> list[tuple[int, int, str]]:
    return sorted([(s.start, s.end, s.type) for s in spans]
                  + [(m.start(), m.end(), m.group(1)) for m in PLACEHOLDER.finditer(text)])


def _render(text: str, marks, a: int, b: int) -> str:
    """text[a:b] with each masked value and placeholder in it shown whole as its type."""
    out, pos = [], a
    for s, e, kind in marks:
        if e <= a or s >= b or e <= pos:
            continue
        if s > pos:
            out.append(text[pos:s])
        out.append(f"‹{kind}›")
        pos = e
    if pos < b:
        out.append(text[pos:b])
    return "".join(out)


def context(text: str, spans: list[Span], start: int, end: int) -> tuple[str, str]:
    """The text before and after text[start:end], CONTEXT characters each way, as the provider
    saw it but with every masked value in it, and every placeholder, shown as its type: `‹SECRET›`.
    A value or placeholder cut by the window is shown whole as its type, never in part."""
    marks = _marks(text, spans)
    a, b = max(0, start - CONTEXT), min(len(text), end + CONTEXT)
    return ("…" if a > 0 else "") + _render(text, marks, a, start), _render(text, marks, end, b) + ("…" if b < len(text) else "")


def as_types(text: str, spans: list[Span] = (), limit: int = CHECK_TEXT) -> str:
    """A whole text for the Logs page, every masked value and placeholder in it shown as its type
    (`‹SECRET›`), never in part; at most `limit` characters of it, and how many more there were."""
    end = len(text) if len(text) <= limit else limit
    shown = _render(text, _marks(text, list(spans)), 0, end)
    return shown if end == len(text) else shown + f"\n… {len(text) - end:,} more characters"


def finders(sources) -> list[str]:
    """Every detector that found a value, for the Logs page, yours first, then built-in rules, then
    the model: `yours:<name>`, `rule:<id>`, `model`."""
    out = []
    for x in sorted(set(sources)):
        if x.startswith("rules:"):
            out.append("rule:" + x[len("rules:"):])
        elif x.startswith("user:"):
            out.append("yours:" + x[len("user:"):])
        elif x and x != "known" and "model" not in out:
            out.append("model")
    return sorted(out, key=lambda x: ("yours:", "rule:", "model").index(x.split(":")[0] + (":" if ":" in x else "")))


def detector_of(source: str) -> tuple[str, str]:
    """(detector, rule) for a span's source, preferring your rule, then a built-in rule, then the
    model: rules:github-token|pf-serve -> ("rules", "github-token")."""
    parts = source.split("|")
    for prefix, name in (("user:", "user"), ("rules:", "rules")):
        hit = next((x for x in parts if x.startswith(prefix)), None)
        if hit:
            return name, hit[len(prefix):]
    if "known" in parts and len(parts) == 1:
        return "known", ""
    return "model", ""



def _caller(item: dict) -> str:
    """The tool a Responses call's output comes from (script_caller). A custom tool call (Codex's
    exec) holds its script in `input`; a function call (opencode's execute) in `arguments`, a JSON
    document in a string."""
    value = item.get("input")
    if value is None and isinstance(item.get("arguments"), str):
        try:
            value = json.loads(item["arguments"])
        except ValueError:
            value = None
    return script_caller(item_name(item), value)


def script_caller(name: str, value) -> str:
    """The tool a call's output is credited to. A code-mode script (Codex's exec, opencode's
    execute) that calls only one MCP server's tools returns that server's results, so its output is
    that server's: a value it found can go back to it (codemode.py)."""
    from .codemode import CODE_TOOLS, MCP_RESOURCE_CALLS, parse, script_of

    code = script_of(value) if name in CODE_TOOLS else None
    if code is None:
        return name
    script = parse(code)
    servers = {server_of(n) for n in script.calls}
    if script.dynamic or len(servers) != 1 or script.calls & MCP_RESOURCE_CALLS or not next(iter(servers)).startswith("mcp__"):
        return name
    return f"{next(iter(servers))}__exec"

def _gemini_results(contents) -> dict[int, str]:
    """id() of each functionResponse part in contents -> the tool its call names (tools.call_name)."""
    by_id: dict[str, str] = {}
    pending: list[tuple[str, str]] = []  # (the name as called, the name decided), oldest first
    out: dict[int, str] = {}
    for c in contents if isinstance(contents, list) else []:
        for part in (c.get("parts") or []) if isinstance(c, dict) else []:
            if not isinstance(part, dict):
                continue
            call = part.get("functionCall") or part.get("function_call")
            if isinstance(call, dict) and isinstance(call.get("name"), str):
                decided = call_name(call["name"], call.get("args"))
                if isinstance(call.get("id"), str):
                    by_id[call["id"]] = decided
                pending.append((call["name"], decided))
            resp = part.get("functionResponse") or part.get("function_response")
            if isinstance(resp, dict):
                name = resp.get("name")
                match = next((i for i, (n, _) in enumerate(pending) if n == name), None)
                if isinstance(resp.get("id"), str) and resp["id"] in by_id:
                    out[id(part)] = by_id[resp["id"]]
                    match = next((i for i, (_, d) in enumerate(pending) if d == by_id[resp["id"]]), match)
                elif match is not None:
                    out[id(part)] = pending[match][1]
                if match is not None:
                    pending.pop(match)
    return out


# A value a request shows as a checksum (go.sum's `h1:…`, npm's `sha512-…`, an `integrity`, Cargo's `checksum`)
# is one wherever else it is. An agent checking the table it wrote quotes the hashes bare (`for s in '68Rb…='
# …; do grep -cF "$s" go.sum`, `printf '%s\n' '6xtPQ…==' | cut -c1-16`), and a guess took them for keys; the
# known-value pass then masked them in the lockfile read earlier too (lock-01, code-02 on Claude Code, Nov 22).
_CHECKSUM = re.compile(r"(?i)(?:\bh1:|\bsha(?:1|224|256|384|512)[-:]|\bintegrity\\?[\"']?\s*[:=]?\s*\\?[\"']?(?:sha\d+-)?"
                       r"|\bchecksum\\?[\"']?\s*[:=]\s*\\?[\"']?)([A-Za-z0-9+/_-]{16,})={0,2}")
CHECKSUMS = 4096  # checksum values kept, the newest
# A checksum's label at the start of a guessed span (`sha512-oUsD…`, `h1:68Rb…`), and anything before it; or
# the end of one the span began inside (`256:2f7e…` of `slim@sha256:2f7e…`: deploy-01 on Claude Code, Nov 25).
_CHECKSUM_LABEL = re.compile(r"(?i)^(?:.*?(?:h1:|sha(?:1|224|256|384|512)[-:])|(?:1|224|256|384|512)[-:])")
# What only guessed a value (the model, entropy) or knew it from such a guess before: not a format rule's find.
_GUESSED = frozenset({"rules:entropy", "pf-serve", "known"})
_ONLY_PLACEHOLDERS = re.compile(r"\s*(?:KEEPMUM_[A-Z]+_[0-9a-f]{8,12}\s*)+")


_LABEL = re.compile(r"[A-Za-z_][\w.-]*:")  # `LOG_LEVEL:` before a value a script printed


def encodes_nothing(value: str) -> bool:
    """`value`, a label before it or not, is base64 of nothing secret: of keepmum's own placeholders
    only (a model that encoded the placeholder it was given, for a Kubernetes Secret: k8s-01 on
    Codex, Nov 23), or of a plain word (`LOG_LEVEL:aW5mbw`, `info` encoded: k8s-01 on opencode, Nov
    23), which a guess would not take for a secret unencoded either (rules.public_shape)."""
    import base64
    import binascii

    if m := _LABEL.match(value):
        value = value[m.end():]
    if len(value) < 6:
        return False
    for altchars in (None, b"-_"):
        try:
            text = base64.b64decode(value + "=" * (-len(value) % 4), altchars=altchars, validate=True).decode("ascii")
        except (ValueError, binascii.Error, UnicodeDecodeError):
            continue
        if _ONLY_PLACEHOLDERS.fullmatch(text) or text.isalpha() and len(text) <= 16 and sum(c.isupper() for c in text) <= 2:
            return True
    return False


def _b64_text(value: str) -> str:
    """value read as base64, standard or URL-safe, if it is that and decodes to text; else ""."""
    import base64
    import binascii

    if len(value) < 8:
        return ""
    for altchars in (None, b"-_"):
        try:
            return base64.b64decode(value + "=" * (-len(value) % 4), altchars=altchars, validate=True).decode("utf-8").strip()
        except (ValueError, binascii.Error, UnicodeDecodeError):
            continue
    return ""


def _decodes_into(line: str, own: str) -> bool:
    """line is base64 of text in `own`: the model's own string encoded by a command it ran
    (`printf 'info' | base64`: k8s-01 on Claude Code, Nov 24)."""
    import base64
    import binascii

    try:
        text = base64.b64decode(line + "=" * (-len(line) % 4), validate=True).decode("utf-8")
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return False
    return len(text.strip()) >= 3 and text.strip() in own


def _blanked(text: str, masked) -> str:
    """`text` as it went: each of the `masked` (start, end) ranges a NUL."""
    pos, out = 0, []
    for a, b in masked:
        out += [text[pos:a], "\0"]
        pos = b
    return "".join(out) + text[pos:]


class Redactor:
    def __init__(self, detector, vault: Vault, cache_size: int = 4096):
        self.detector = detector
        self.vault = vault
        self._cache: OrderedDict[bytes, tuple[Span, ...]] = OrderedDict()
        # Per text, beside the detector's spans: whether it has encoded blocks (decoded.encoded),
        # and where the known values in it are, as of a vault mark. Both ran on every text of every
        # request, the resent history too, and took about three quarters of keepmum's own time on
        # agent traffic. Positions only, never text: a value the vault lets go leaves no copy here,
        # and a block's decoded text is decoded again when the text comes back (few have any).
        self._derived: OrderedDict[bytes, list] = OrderedDict()  # key -> [has blocks, mark, [(start, end)]]
        self._cache_size = cache_size
        self._generation = getattr(detector, "generation", 0)
        self.counts: Counter[str] = Counter()  # spans masked per type since the last reset
        # Spans masked in text this redactor had not seen before, since the last reset: what a
        # request brought that is new. Claude Code resends the whole conversation every turn.
        self.fresh = 0
        # Those spans for the Logs page: type, detector, rule, length, and with `previews` the
        # first and last character; the value itself only with `values` ([log] values: the Mac
        # app's Show values, in memory only). Reset with `fresh`.
        self.findings: list[dict] = []
        self.previews = True
        self.context = True
        self.values = False
        # While masking a tool's result: the server it came from, noted on each value in the vault,
        # so that server may have its own values back (tools.py). Tool call ids -> tool names.
        self._origin: str | None = None
        self._calls: dict[str, str] = {}
        # Provider-run tools (provided.py): their calls' names by id, and the results already read,
        # so a result the agent resends every turn is scored once.
        self._server_calls: dict[str, str] = {}
        self._provided_seen: OrderedDict[bytes, None] = OrderedDict()
        # The text of every tool result in the request being masked, for the guard (guard.py):
        # (tool, text, new), where new means this redactor had not seen the text before. Reset
        # with `findings`. The text is the original, before masking, and never leaves the process.
        self.tool_results: list[tuple[str | None, str, bool]] = []
        self._tool: str | None = None
        self._in_result = False
        # The same for the harm check (harm.py): what the user typed, as (None, text, new), without
        # what agents add to a user turn. `_role` is the role of the message being masked.
        self.user_texts: list[tuple[None, str, bool]] = []
        # Each of those texts -> the whole string it came from, as the request holds it (a harm
        # hand-back replaces the typed part inside it). Reset with `user_texts`.
        self.typed_in: dict[str, str] = {}
        # Whether the request is one an agent makes aside the turn (harm.aside): Codex's task title.
        self.aside = False
        # With [log] context on: each of those texts as the Logs page may show it, every masked value
        # in it as its type (as_types). Per request, in memory only.
        self.shown: dict[str, str] = {}
        self._role: str | None = None
        # What the model wrote in this request (its text, its tool calls' inputs): a tool result made
        # only of it is no outside content for the injection check (Redactor._authored_by_model).
        self.authored: list[str] = []
        # The same, as it went: what was masked in it blanked (_provider_has).
        self.wrote: list[str] = []
        self._authoring = False
        # Values the conversation shows as checksums (_CHECKSUM): a guess at one of them is no secret.
        self._checksums: OrderedDict[str, None] = OrderedDict()
        self._checksum_starts: dict[str, str] = {}  # the first 16 characters of each -> it
        # This request's texts an earlier request sent, as they went, joined (_note_sent): what the
        # provider has seen. Per text, where it was masked when it last went (`_went`): positions only.
        self._sent = ""
        self._wrote_joined: tuple[list[str] | None, int, str] = (None, 0, "")
        self._went: OrderedDict[bytes, tuple[tuple[int, int], ...]] = OrderedDict()

    @staticmethod
    def _key(text: str) -> bytes:
        return hashlib.sha256(text.encode("utf-8", "surrogatepass")).digest()

    def _detect(self, text: str, key: bytes | None = None) -> tuple[tuple[Span, ...], bool]:
        """Detector spans, cached per text: the conversation history is resent every turn.
        Also says whether the text was seen before."""
        generation = getattr(self.detector, "generation", 0)
        if generation != self._generation:  # the settings changed: detect again
            self._cache.clear()
            self._derived.clear()
            self._generation = generation
        key = key or self._key(text)
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
            return hit, True
        spans = tuple(self.detector.detect(text))
        self._cache[key] = spans
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return spans, False

    def spans(self, text: str) -> list[Span]:
        """Detector spans plus every exact occurrence of a value masked before."""
        return self._spans(text)[0]

    def _spans(self, text: str, authored: bool = False) -> tuple[list[Span], bool]:
        """The spans to mask in `text`. Two kinds of guess are no secret (GUESSING):

        - one in text the model wrote (its words, its tool calls' input). A real value there came from a
          request: masked, it is masked again as known (`known`, the format rules); sent unmasked, the
          provider has it. A guess there only took the model's own commands for secrets: a grep for
          `token:|secret`, a key file's name, a hash quoted to compare, an awk pattern, a request id it
          named (cloud-01, srv-01, lock-01, code-02, git-01, debug-01, rotate-01: Nov 22-23);
        - one at a value a message already sent showed unmasked (`_sent`): the provider has it, and
          masked now it would change that message, and the prompt cache with it (debug-01, rotate-01).
        """
        key = self._key(text)
        found, seen = self._detect(text, key)
        derived = self._derived.get(key)
        blocks = None
        if derived is None:
            from .decoded import encoded

            blocks = encoded(text)
            derived = self._derived[key] = [bool(blocks), -1, []]
            if len(self._derived) > self._cache_size:
                self._derived.popitem(last=False)
        else:
            self._derived.move_to_end(key)
            if not derived[0]:
                blocks = []
        spans = list(found)
        known = self.vault.known_values()
        if known is not None:
            passed = getattr(self.detector, "pass_types", frozenset())
            released = getattr(self.detector, "lets_through", None)
            spans += [Span(a, b, kind, 1.0, "known") for a, b, value in self._known_in(text, derived, known)
                      if (kind := self.vault.kind_of(value)) not in passed
                      and not (released and released(value, self.vault.sources_of(value), kind))]
        spans += self._encoded(text, spans, known, blocks)
        spans += self._known_pieces(text, spans)
        spans += self._email_parts(text, spans)
        spans = merge(narrow(spans, text))
        spans = [s for s in spans if not (guessed(s) and encodes_nothing(text[s.start:s.end]))]
        if authored:
            spans = [s for s in spans if not guessed(s)]
        elif self._sent or self.wrote:
            spans = [s for s in spans if not (guessed(s) and (self._provider_has(v := text[s.start:s.end])
                                                              or self._provider_has(_b64_text(v))))]
        if self._checksums:
            spans = [s for s in spans if not (set(s.source.split("|")) <= _GUESSED and self._a_checksum(text[s.start:s.end]))]
        return spans, seen

    def _a_checksum(self, value: str) -> bool:
        """`value` is a checksum the conversation shows, or the start of one (a guess's span can stop
        short), with or without its label (`.yaml:sha512-oUsD…`: lock-01 on DeepSeek, Nov 22)."""
        value = _CHECKSUM_LABEL.sub("", value).rstrip("=")
        if len(value) < 16:
            return False
        if value in self._checksums:
            return True
        full = self._checksum_starts.get(value[:16])
        return full is not None and full.startswith(value)

    def _provider_has(self, value: str) -> bool:
        """`value` went to the provider as it is, not masked nor inside a value masked whole: in a text
        of this request an earlier request sent (`_sent`), or in what the model wrote (`wrote`): the
        label a script of its own prints (`startswith_KEEPMUM_in_b64_text`, k8s-01 on Claude Code,
        Nov 23). Both as they went: `STRIPE_SECRET_KEY:<its base64>` masked as one value sent no part
        of it, and a guess at the base64 alone, read later, was dropped as sent (k8s-01 on opencode,
        Nov 23: the base64 of the Stripe key left)."""
        if len(value) < 4 or self.vault.contains(value):
            return False
        if value in self._sent:
            return True
        wrote, n, joined = self._wrote_joined
        if wrote is not self.wrote or n != len(self.wrote):
            self._wrote_joined = (self.wrote, len(self.wrote), joined := "\0".join(self.wrote))
        return value in joined

    def _note_sent(self, body) -> None:
        """The texts of `body` an earlier request sent, as they went (what was masked in them blanked),
        joined, for `_sent_unmasked`. A text new in this request is not one, even after a walk of it."""
        sent = []

        def walk(v):
            if isinstance(v, str):
                if len(v) >= 8 and (masked := self._went.get(self._key(v))) is not None:
                    sent.append(_blanked(v, masked))
            elif isinstance(v, dict):
                for x in v.values():
                    walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)

        walk(body)
        self._sent = "\0".join(sent)

    def _note_checksums(self, body) -> None:
        """The values a request shows as checksums (_CHECKSUM), kept for the conversation."""
        try:
            raw = json.dumps(body, ensure_ascii=False)
        except (TypeError, ValueError):
            return
        if not any(k in raw for k in ("h1:", "sha", "integrity", "checksum", "SHA", "Integrity", "Checksum")):
            return
        for m in _CHECKSUM.finditer(raw):
            value = m.group(1)
            self._checksums[value] = None
            self._checksums.move_to_end(value)
            self._checksum_starts[value[:16]] = value
        while len(self._checksums) > CHECKSUMS:
            gone, _ = self._checksums.popitem(last=False)
            if self._checksum_starts.get(gone[:16]) == gone:
                del self._checksum_starts[gone[:16]]

    def _known_in(self, text: str, derived: list, known) -> list[tuple[int, int, str]]:
        """The known values in text, as (start, end, value): where they were the last time, with
        only the values learned since looked for, or all of them again when that was too long ago.
        The cache keeps positions; each value is read from the text in hand."""
        from .vault import occurrences

        mark, keeps = self.vault.mark(), self.vault.keeps
        # A value found before and let go since may have hidden a shorter one still kept ("Anna" in
        # "Anna Smith"): such a text is looked at again in full.
        gone = any(not keeps(text[a:b]) for a, b in derived[2])
        if derived[1] != mark or gone:
            new = self.vault.learned_since(derived[1]) if derived[1] >= 0 and not gone else None
            if new is None:
                derived[2] = [(m.start(), m.end()) for m in known.finditer(text)]
            else:
                derived[2] = derived[2] + [where for v in new for where in occurrences(v, text)]
            derived[1] = mark
        return [(a, b, text[a:b]) for a, b in derived[2] if keeps(text[a:b])]

    def _encoded(self, text: str, spans: list[Span], known, blocks=None) -> list[Span]:
        """Blocks a command printed encoded (a hex dump, `od -c`, plain hex, base64) whose decoded
        text holds a secret, or a value masked before: each is masked whole (decoded.py). `blocks`:
        decoded.encoded(text), when it was found before."""
        from .decoded import encoded
        from .detect import PRIORITY
        from .detect.rules import GUESSES

        out = []
        rules = getattr(self.detector, "rules", None) or self.detector
        for start, end, decoded in (encoded(text) if blocks is None else blocks):
            if any(s.start <= start and end <= s.end and not guessed(s) for s in spans):
                continue  # already masked, by an exact find: a guess may be dropped, or give way
            if self._provider_has(decoded.strip()):
                # The model's own text, encoded on this Mac: `printf 'postgres://orders:x@db' | base64`, a
                # test string it wrote (k8s-01 on Claude Code, Nov 24). The provider has it as it is.
                continue
            kinds = [s.type for s in rules.detect(decoded) if s.source not in GUESSES]
            if known is not None:
                kinds += [self.vault.kind_of(m.group()) for m in known.finditer(decoded)]
            if not kinds and self._holds_a_piece(decoded):
                kinds = ["SECRET"]
            if kinds:
                out.append(Span(start, end, min(kinds, key=PRIORITY.__getitem__), 0.9, "decoded"))
        return out

    def _authored_by_model(self, text: str) -> bool:
        """text, a tool result, is the model's own writing read back: every line of it, a line number
        and indentation aside, is in what the model wrote earlier in this request. The agent printing
        its own script's message, or grepping the README it just wrote, is no outside content, and
        the injection check scored both as attacks (k8s-01 on opencode, deploy-01 on Claude Code, Nov
        19). Where the model copied its text from was checked when it was read."""
        if not self.authored:
            return False
        lines = [_LINE_NUMBER.sub("", line).strip() for line in text.splitlines()]
        lines = [line for line in lines if len(line) >= 8]
        if not lines:
            return False
        own = "\n".join(self.authored)
        return all(line in own or _decodes_into(line, own) for line in lines)

    def _holds_a_piece(self, decoded: str) -> bool:
        """decoded holds SECRET_PIECE characters in a row of a secret keepmum masked before: a dump of
        a file cut short (`tail -c 200 .env | od -c | tail -3` printed a key's last 23 characters,
        crypto-01 on Claude Code, Nov 18). Indexed once per change of the vault."""
        pieces = self._secret_pieces()
        if not pieces:
            return False
        flat = "".join(decoded.split())
        return any(flat[i:i + SECRET_PIECE] in pieces for i in range(len(flat) - SECRET_PIECE + 1))

    def _secret_pieces(self) -> dict[str, str]:
        """Every SECRET_PIECE characters in a row of each secret the vault holds: piece -> the secret.
        Built again only when the vault changes."""
        version = (len(self.vault), self.vault.mark(), getattr(self.detector, "generation", None))
        if getattr(self, "_pieces_version", None) != version:
            pieces: dict[str, str] = {}
            passed = getattr(self.detector, "pass_types", frozenset())
            released = getattr(self.detector, "lets_through", None)
            for value in list(self.vault._values.values()):
                if not isinstance(value, str) or len(value) < SECRET_PIECE + 4 or self.vault.kind_of(value) != "SECRET":
                    continue
                # What the settings let through now is no piece to mask either (the known pass's rule).
                if "SECRET" in passed or released and released(value, self.vault.sources_of(value), "SECRET"):
                    continue
                if True:
                    flat = "".join(value.split())
                    for i in range(len(flat) - SECRET_PIECE + 1):
                        pieces.setdefault(flat[i:i + SECRET_PIECE], flat)
            self._pieces, self._pieces_version = pieces, version
        return self._pieces

    def _known_pieces(self, text: str, spans: list[Span]) -> list[Span]:
        """Pieces of a secret masked before, cut off by a command: `tail -c 200 .env` began in the
        middle of a key keepmum had masked whole a request earlier, and the rest of it went out (crypto-01
        on Claude Code, Nov 18). Each run of key-like characters that holds SECRET_PIECE of a secret's
        characters in a row is masked as far as it matches that secret."""
        pieces = self._secret_pieces()
        if not pieces:
            return []
        out = []
        # A piece counts only as a whole token (`=` and `:` end one): masked inside a longer one, its
        # placeholder would run into the characters after it (`…e6ab` + `fa64` reads as another id).
        for m in _KEYLIKE_TOKEN.finditer(text):
            a, b = m.span()
            run = m.group()
            if len(run) < SECRET_PIECE or "KEEPMUM_" in run or any(s.start < b and a < s.end for s in spans):
                continue
            value = pieces.get(run[:SECRET_PIECE])
            if value is not None and run in value:
                out.append(Span(a, b, "SECRET", 1.0, "known"))
        return out

    def _email_locals(self) -> frozenset[str]:
        """The local parts of the emails the vault holds, lowercase. Built again only when the vault changes."""
        version = (len(self.vault), self.vault.mark(), getattr(self.detector, "generation", None))
        if getattr(self, "_email_version", None) != version:
            passed = getattr(self.detector, "pass_types", frozenset())
            released = getattr(self.detector, "lets_through", None)
            parts = set()
            if "EMAIL" not in passed:
                for value in list(self.vault._values.values()):
                    if not isinstance(value, str) or "@" not in value or self.vault.kind_of(value) != "EMAIL":
                        continue
                    if released and released(value, self.vault.sources_of(value), "EMAIL"):
                        continue
                    local = value.rsplit("@", 1)[0].strip().lower()
                    if len(local) >= EMAIL_PART and local not in ROLE_MAILBOXES and "keepmum_" not in local:
                        parts.add(local)
            self._email_set, self._email_version = frozenset(parts), version
        return self._email_set

    def _email_parts(self, text: str, spans: list[Span]) -> list[Span]:
        """The local part of an email masked before, on its own, in any case: a script that split the
        address printed `['margaret.thornbury', 'gmail.com']`, and the name went out while the email
        stayed masked (data-01 on Codex with DeepSeek, Nov 23). Only a whole token counts. The domain
        is no person and stays; so does a role's mailbox (`support`)."""
        parts = self._email_locals()
        if not parts:
            return []
        out = []
        for m in _LOCAL_TOKEN.finditer(text):
            a, b = m.span()
            while b > a and text[b - 1] == ".":  # the end of a sentence
                b -= 1
            if b - a >= EMAIL_PART and text[a:b].lower() in parts and not any(s.start < b and a < s.end for s in spans):
                out.append(Span(a, b, "EMAIL", 1.0, "known"))
        return out

    def redact_text(self, text: str) -> str:
        if not text:
            return text
        authored = not self._in_result and (self._authoring or self._role in ("assistant", "model"))
        if authored:
            self.authored.append(text)
        spans, seen = self._spans(text, authored)
        key = self._key(text)
        self._went[key] = tuple((s.start, s.end) for s in spans)  # the walk that goes is the last
        if authored:
            self.wrote.append(_blanked(text, self._went[key]))
        self._went.move_to_end(key)
        if len(self._went) > self._cache_size:
            self._went.popitem(last=False)
        if self._in_result and self._tool not in NOT_FETCHED:
            if not AGENT_NOTICE.fullmatch(text.strip()) and not self._authored_by_model(text):
                self.tool_results.append((self._tool, text, not seen))
            if self.context:
                self.shown[text] = as_types(text, spans)
        elif self._role == "user":
            from .harm import aside, typed

            self.aside = self.aside or aside(text)
            if mine := typed(text):
                self.user_texts.append((None, mine, not seen))
                self.typed_in[mine] = text
                if self.context:
                    self.shown[mine] = typed(as_types(text, spans, limit=len(text)))[:CHECK_TEXT]
        if not seen:
            self.fresh += len(spans)
            for s in spans:
                value = text[s.start : s.end]
                detector, rule = detector_of(s.source)
                parts = s.source.split("|")
                # Seen earlier in the conversation: who found it then.
                earlier = "known" in parts
                by = finders(parts + (list(self.vault.sources_of(value)) if earlier else []))
                before, after = context(text, spans, s.start, s.end) if self.context else (None, None)
                self.findings.append({"type": s.type, "detector": detector, "rule": rule, "by": by, "earlier": earlier,
                                      "chars": len(value),
                                      "preview": preview(value) if self.previews else None,
                                      "before": before, "after": after,
                                      **({"value": value} if self.values else {})})
        if not spans:
            return text
        # A value inside a JSON string (Codex sends command output as JSON text) is kept as it reads
        # unescaped: `Tr0ub4dor\"&\\3x` there is `Tr0ub4dor"&\3x`, with the same placeholder.
        strings = json_strings(text) if any("\\" in text[s.start:s.end] for s in spans) else None
        out, pos = [], 0
        for s in spans:
            out.append(text[pos : s.start])
            value = text[s.start : s.end]
            if strings and any(a <= s.start and s.end <= b for a, b in strings):
                try:
                    value = json.loads(f'"{value}"')
                except ValueError:
                    pass
            out.append(self.vault.placeholder(s.type, value, s.source.split("|"), self._origin))
            self.counts[s.type] += 1
            pos = s.end
        out.append(text[pos:])
        return "".join(out)

    @contextlib.contextmanager
    def _as(self, role):
        before, self._role = self._role, role if isinstance(role, str) else None
        try:
            yield
        finally:
            self._role = before

    def _in_message(self, m: dict, fn):
        with self._as(m.get("role")):
            return fn()

    @contextlib.contextmanager
    def _result_of(self, tool: str | None, read: bool = True):
        """Mask a tool result, noting the tool's server on every value in it. `read`: the text is
        what the tool returned, which the guard checks (not a call's own input)."""
        before = self._origin, self._tool, self._in_result
        self._origin, self._tool, self._in_result = server_of(tool) if tool else None, tool, read
        try:
            yield
        finally:
            self._origin, self._tool, self._in_result = before

    # --- A request as a whole ------------------------------------------------------

    # What one walk of a request adds to: copied before a walk, put back before walking it again.
    _PER_REQUEST = ("counts", "fresh", "findings", "tool_results", "user_texts", "typed_in", "aside", "shown", "authored", "wrote")
    WALKS = 3  # at most: a walk learns from what the one before it masked (decoded blocks, pieces)

    def _state(self) -> dict:
        return {k: copy.copy(getattr(self, k)) for k in self._PER_REQUEST}

    def _whole(self, walk, body=None):
        """`walk` (a request's walk) masked as a whole: a value found anywhere in the request is
        masked everywhere in it, the messages before the one it was found in too. The walk masks text
        in order and learns values as it goes, so an earlier message went out with a secret found
        further down in the clear, and the next request (the same messages again) masked it: the
        provider got it once, and its prompt cache missed from that message on (a resumed session's
        first request, Nov 22: 18 of 435 messages changed between two identical requests). Walked
        again while the vault learns: the text is cached, so a walk again takes milliseconds. What
        the request brought that was new (findings, the checks' texts and whether each is new) is
        the first walk's, which met each text first; what was masked (counts, the Logs page's text)
        the last's, which is what goes."""
        if body is not None:
            self._note_checksums(body)
            self._note_sent(body)
        mark, before = self.vault.mark(), self._state()
        out = walk()
        if self.vault.mark() == mark:
            return out
        first = self._state()
        for _ in range(self.WALKS - 1):
            if self.vault.mark() == mark:
                break
            mark = self.vault.mark()
            for k, v in before.items():
                setattr(self, k, copy.copy(v))
            out = walk()
        for k in ("fresh", "findings", "tool_results", "user_texts", "typed_in", "aside", "authored"):
            setattr(self, k, first[k])
        return out

    # --- Messages API payloads ----------------------------------------------------

    def redact_request(self, body: dict) -> dict:
        """A copy of a /v1/messages (or count_tokens) body with system and messages masked.

        `tools`, `metadata` and every other top-level field pass through unchanged.
        """
        return self._whole(lambda: self._redact_request(body), body)

    def _redact_request(self, body: dict) -> dict:
        out = dict(body)
        self._calls = {b["id"]: script_caller(b["name"], b.get("input")) for m in out.get("messages") or [] if isinstance(m, dict)
                       for b in (m.get("content") if isinstance(m.get("content"), list) else [])
                       if isinstance(b, dict) and b.get("type") == "tool_use"
                       and isinstance(b.get("id"), str) and isinstance(b.get("name"), str)}
        if "system" in out:
            out["system"] = self._content(out["system"])
        out["system"] = self._with_hint(out.get("system"))
        if isinstance(out.get("messages"), list):
            out["messages"] = [
                {**m, "content": self._in_message(m, lambda m=m: self._content(m.get("content")))} if isinstance(m, dict) else m
                for m in out["messages"]
            ]
        return out

    # --- Gemini payloads (Gemini CLI: the Gemini API and Code Assist) -----------------------

    def redact_gemini_request(self, body: dict, hint: bool = False) -> dict:
        """A copy of a Gemini request with every content masked: `contents`, `systemInstruction`,
        an embedding's `content`, and the same inside the wrappers Gemini uses (`request` for Code
        Assist, `generateContentRequest` for countTokens, `requests` for batch embeddings).
        With `hint` (generateContent and streamGenerateContent only: countTokens and embeddings
        take no system instruction), the placeholder hint goes last in the system instruction."""
        return self._whole(lambda: self._redact_gemini_request(body, hint), body)

    def _redact_gemini_request(self, body: dict, hint: bool = False) -> dict:
        out = dict(body)
        # Each tool result, credited to the call it answers, as the call names its tool (Antigravity's
        # call_mcp_tool names an MCP server: tools.call_name). By id when the parts carry one;
        # otherwise in order, as the Gemini API pairs them: the earliest call of that name not yet
        # answered (agy's gateway mode sends no ids).
        self._gemini_results = _gemini_results(body.get("contents"))
        for key in ("request", "generateContentRequest", "generate_content_request"):
            if isinstance(out.get(key), dict):
                out[key] = self._redact_gemini_request(out[key], hint)
                hint = False  # the wrapped request carries it
        if isinstance(out.get("requests"), list):
            out["requests"] = [self._redact_gemini_request(r) if isinstance(r, dict) else r for r in out["requests"]]
        if isinstance(out.get("contents"), list):
            out["contents"] = [self._in_message(c, lambda c=c: self._gemini_content(c)) if isinstance(c, dict)
                               else self._gemini_content(c) for c in out["contents"]]
        elif isinstance(out.get("contents"), dict):
            out["contents"] = self._gemini_content(out["contents"])
        if isinstance(out.get("content"), dict):
            out["content"] = self._gemini_content(out["content"])
        key = "system_instruction" if "system_instruction" in out else "systemInstruction"
        if out.get(key) is not None:
            out[key] = self._gemini_content(out[key]) if isinstance(out[key], dict) else self._values(out[key])
        # `key` names the system instruction as this body spells it (REST takes either).
        if hint and "contents" in out:
            note = {"text": PLACEHOLDER_HINT}
            system = out.get(key)
            if isinstance(system, dict) and isinstance(system.get("parts"), list):
                out[key] = {**system, "parts": [*system["parts"], note]}
            elif isinstance(system, str):
                out[key] = {"parts": [{"text": system}, note]}
            elif system is None:
                out[key] = {"parts": [note]}
        return out

    def _gemini_content(self, content):
        if not isinstance(content, dict):
            return self._values(content) if isinstance(content, str) else content
        parts = content.get("parts")
        if isinstance(parts, list):
            return {**content, "parts": [self._gemini_part(p) for p in parts]}
        return content

    def _gemini_part(self, part):
        if isinstance(part, str):
            return self.redact_text(part)
        if not isinstance(part, dict):
            return part
        # A thought is model output, signed like a thinking block: it goes back as it came.
        if part.get("thought") is True:
            return part
        out = dict(part)
        if isinstance(out.get("text"), str):
            out["text"] = self.redact_text(out["text"])
        for key in ("functionCall", "function_call"):
            if isinstance(out.get(key), dict):
                call = out[key]
                out[key] = {**call, "args": self._values(call["args"])} if "args" in call else call
        for key in ("functionResponse", "function_response"):
            if isinstance(out.get(key), dict):
                resp = out[key]
                tool = getattr(self, "_gemini_results", {}).get(id(part)) \
                    or (resp.get("name") if isinstance(resp.get("name"), str) else None)
                with self._result_of(tool):
                    new = {**resp, **({"response": self._values(resp["response"])} if "response" in resp else {})}
                    # A result's own parts are parts: their text masked, an image (inline_data, the
                    # Antigravity CLI's read_file of a picture) or a file as it is. A placeholder in
                    # an image's base64 made Google refuse the request (wf-01, Nov 18).
                    if isinstance(resp.get("parts"), list):
                        # A result's own parts take files only: a PDF that goes as its text puts that
                        # text in the result's response (Code Assist refused a text part there: doc-02).
                        parts, texts = [], []
                        for p in resp["parts"]:
                            q = self._gemini_part(p)
                            was_file = isinstance(p, dict) and any(isinstance(p.get(k), dict) for k in ("inline_data", "inlineData"))
                            (texts if was_file and isinstance(q, dict) and set(q) == {"text"} else parts).append(q)
                        new["parts"] = parts
                        if texts:
                            response = new.get("response") if isinstance(new.get("response"), dict) else {}
                            new["response"] = {**response, "file_text": "\n\n".join(t["text"] for t in texts)}
                    elif "parts" in resp:
                        new["parts"] = self._values(resp["parts"])
                    out[key] = new
        for key, field in (("executableCode", "code"), ("executable_code", "code"),
                           ("codeExecutionResult", "output"), ("code_execution_result", "output")):
            if isinstance(out.get(key), dict) and isinstance(out[key].get(field), str):
                out[key] = {**out[key], field: self.redact_text(out[key][field])}
        # inlineData and fileData carry files and images: they pass through, but a PDF that holds a
        # value keepmum masks goes as its masked text.
        for key in ("inline_data", "inlineData"):
            blob = out.get(key)
            if isinstance(blob, dict) and (blob.get("mime_type") or blob.get("mimeType")) == "application/pdf":
                if (text := self.pdf_in_place(blob.get("data"))) is not None:
                    return {"text": text}
        return out

    # --- Responses API payloads (OpenAI: Codex, pi) ------------------------------------

    def redact_responses_request(self, body: dict) -> dict:
        """A copy of a /responses body with `instructions` and every `input` item masked.

        Reasoning items are encrypted and signed: they pass through, like thinking blocks. The
        placeholder hint goes first in `input` as a developer message, stable across turns.
        """
        return self._whole(lambda: self._redact_responses_request(body), body)

    def _redact_responses_request(self, body: dict) -> dict:
        out = dict(body)
        if isinstance(out.get("instructions"), str):
            out["instructions"] = self.redact_text(out["instructions"])
        items = out.get("input")
        if isinstance(items, str):
            items = [{"type": "message", "role": "user", "content": items}]
        if isinstance(items, list):
            self._calls = {i["call_id"]: _caller(i) for i in items if isinstance(i, dict)
                           and i.get("type") in ("function_call", "custom_tool_call")
                           and isinstance(i.get("call_id"), str) and isinstance(i.get("name"), str)}
            out["input"] = [RESPONSES_HINT, *(self._responses_item(i) for i in items)]
        return out

    def redact_search_request(self, body: dict) -> dict:
        """A copy of Codex's web search request (/alpha/search, Codex 0.169) masked: `input`, the
        conversation's last messages, as a /responses body's; `commands` (queries, pages to open,
        patterns to find) and `settings` (a location, allowed domains) value by value. Its id,
        model and limits pass."""
        return self._whole(lambda: self._redact_search_request(body), body)

    def _redact_search_request(self, body: dict) -> dict:
        out = dict(body)
        items = out.get("input")
        if isinstance(items, str):
            out["input"] = self.redact_text(items)
        elif isinstance(items, list):
            out["input"] = [self._responses_item(i) for i in items]
        for key in ("commands", "settings"):
            if key in out:
                out[key] = self._values(out[key])
        return out

    def _responses_item(self, item):
        if not isinstance(item, dict):
            return item
        kind = item.get("type", "message")
        if kind in RESPONSES_PASS_THROUGH:
            return item
        if kind == "message":
            with self._as(item.get("role")):
                return self._responses_message(item)
        return self._responses_call(item, kind)

    def _responses_message(self, item):
        content = item.get("content")
        if isinstance(content, str):
            return {**item, "content": self.redact_text(content)}
        if isinstance(content, list):
            def _part(c):
                if not isinstance(c, dict):
                    return c
                if isinstance(c.get("text"), str):
                    return {**c, "text": self.redact_text(c["text"])}
                if isinstance(c.get("refusal"), str):
                    return {**c, "refusal": self.redact_text(c["refusal"])}
                return self._responses_file(c)

            return {**item, "content": [_part(c) for c in content]}
        return item

    def _responses_call(self, item, kind):
        before, self._authoring = self._authoring, kind in ("function_call", "custom_tool_call", "local_shell_call")
        try:
            return self._responses_call_inner(item, kind)
        finally:
            self._authoring = before

    def _responses_call_inner(self, item, kind):
        out = {**item, "arguments": self._json_string(item["arguments"])} if isinstance(item.get("arguments"), str) else dict(item)
        if kind in ("function_call_output", "custom_tool_call_output"):
            tool = self._calls.get(item.get("call_id"))
        elif kind == "mcp_call" and item.get("server_label"):
            tool = f"mcp__{item['server_label']}__{item.get('name') or ''}"
        else:
            tool = None
        for key in ("output", "input", "content", "text", "action", "error"):
            if key in out:
                with self._result_of(tool if key == "output" else None, read=key in ("output", "content", "text", "error")):
                    if isinstance(out[key], list):
                        out[key] = [self._responses_file(p) if isinstance(p, dict) and p.get("type") in RESPONSES_BINARY_PARTS
                                    else self._values(p) for p in out[key]]
                    else:
                        out[key] = self._values(out[key])
        return out

    def _responses_file(self, part):
        """An input_file part: a PDF that holds a value keepmum masks goes as its masked text."""
        if isinstance(part, dict) and part.get("type") == "input_file" and isinstance(part.get("file_data"), str):
            data = part["file_data"]
            if data.startswith("data:application/pdf") or str(part.get("filename", "")).lower().endswith(".pdf"):
                if (text := self.pdf_in_place(data, part.get("filename"))) is not None:
                    return {"type": "input_text", "text": text}
        return part

    def _json_string(self, raw: str) -> str:
        """Tool-call arguments are a JSON document in a string: mask its values, keep its keys."""
        try:
            parsed = json.loads(raw)
        except ValueError:
            return self.redact_text(raw)
        masked = self._values(parsed)
        return raw if masked == parsed else json.dumps(masked, ensure_ascii=False)

    @staticmethod
    def _with_hint(system):
        """The system prompt with the placeholder hint as a last, separate text block."""
        hint = {"type": "text", "text": PLACEHOLDER_HINT}
        if isinstance(system, list):
            return [*system, hint]
        if isinstance(system, str) and system:
            return [{"type": "text", "text": system}, hint]
        return [hint]

    def pdf_in_place(self, data: str, name: str | None = None) -> str | None:
        """The text to send in place of a PDF attached whole (base64 or a data: URL), or None: send
        the PDF as it is. Its text is read on this Mac (pdftext) and goes through keepmum like any
        other: when it holds a value keepmum masks, the model gets that text, masked, under a note,
        not the file. A key printed in a vendor's PDF reached the provider before (doc-02, Nov 18).
        A PDF with nothing to mask, or none that can be read, goes as it is, its text still checked."""
        from .pdftext import from_base64, is_pdf, text_of

        if not isinstance(data, str) or len(data) < 64:
            return None
        key = hashlib.sha256(data.encode("ascii", "replace")).digest()
        cache = self.__dict__.setdefault("_pdfs", OrderedDict())
        if key in cache:
            cache.move_to_end(key)
            text = cache[key]
        else:
            raw = from_base64(data)
            text = text_of(raw) if raw and is_pdf(raw) else ""
            cache[key] = text
            if len(cache) > 32:
                cache.popitem(last=False)
        if not text:
            return None
        masked = self.redact_text(text)
        if masked == text:
            return None
        what = f"`{name}`" if name else "this PDF"
        return (f"[keepmum: {what} holds a value keepmum keeps on this Mac, so you get its text, with that "
                f"value masked, in place of the file. Layout and pictures are left out.]\n\n{masked}")

    def _content(self, content):
        if isinstance(content, str):
            return self.redact_text(content)
        if isinstance(content, list):
            return [self._block(b) for b in content]
        return content

    def _block(self, block):
        if not isinstance(block, dict):
            return self._content(block)
        kind = block.get("type")
        self._provider_run(block, kind)
        if kind in PASS_THROUGH:
            return block
        if kind == "text":
            return {**block, "text": self.redact_text(block.get("text", ""))}
        if kind == "tool_use":
            return {**block, "input": self._values(block.get("input"))}
        if kind == "tool_result":
            with self._result_of(self._calls.get(block.get("tool_use_id"))):
                return {**block, "content": self._content(block.get("content"))}
        if kind == "document":
            out = dict(block)
            for meta in ("title", "context"):
                if isinstance(out.get(meta), str):
                    out[meta] = self.redact_text(out[meta])
            source = out.get("source")
            if isinstance(source, dict) and source.get("type") == "text":
                out["source"] = {**source, "data": self.redact_text(source.get("data", ""))}
            elif isinstance(source, dict) and source.get("type") == "content":
                out["source"] = {**source, "content": self._content(source.get("content"))}
            if isinstance(source, dict) and source.get("type") == "base64" and source.get("media_type") == "application/pdf":
                if (text := self.pdf_in_place(source.get("data"), block.get("title"))) is not None:
                    return {"type": "text", "text": text}
            return out  # a PDF with nothing to mask, and other binary sources, pass through
        # Unknown block type: scan its content fields, leave protocol fields alone.
        return {k: (self._values(v) if k in CONTENT_KEYS else v) for k, v in block.items()}

    def _provider_run(self, block: dict, kind) -> None:
        """A result of a tool the provider ran (provided.py), resent with the conversation: for the
        injection check, as any tool's result, new the first time it is read."""
        from . import provided

        if kind in ("server_tool_use", "mcp_tool_use") and isinstance(block.get("id"), str):
            self._server_calls[block["id"]] = provided.call_name(block)
            return
        if provided.is_result(kind):
            name = self._server_calls.get(block.get("tool_use_id")) or kind.removesuffix("_tool_result")
            text = provided.result_text(block)
        elif kind == "text" and block.get("citations"):
            name, text = "web_search", provided.cited_text(block)
        else:
            return
        if not text:
            return
        key = self._key(text)
        new = key not in self._provided_seen
        self._provided_seen[key] = None
        self._provided_seen.move_to_end(key)
        if len(self._provided_seen) > self._cache_size:
            self._provided_seen.popitem(last=False)
        self.tool_results.append((provided.source(name), text, new))
        if self.context:
            self.shown[text] = as_types(text)

    def _values(self, value):
        """Every string value in a JSON tree, recursively. Keys are never changed."""
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            return {k: self._values(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._values(v) for v in value]
        return value
