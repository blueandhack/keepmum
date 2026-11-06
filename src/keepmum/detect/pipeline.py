"""The pipeline: L1 rules and L2 Privacy Filter, with deterministic refinement of model spans.

There is no LLM judge in v1: Phase 0 measured two (the Apple on-device model and Qwen3-4B) and
neither cleared anything the refinement rules did not already remove (bench/results/MODELS.md).
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from collections import Counter, OrderedDict

from . import Span
from .merge import merge
from .rules import RulesDetector, shannon_entropy

_PIECE = re.compile(r"\S+")
_EDGE = "/=:\"'`,;()[]{}<>"
# Used with Pattern.match(text, pos), which anchors at pos (a leading ^ would not).
_LABEL = re.compile(r"[A-Za-z_][\w.-]*=")
_DIGEST = re.compile(r"(?i)(?:[\w.-]+@)?sha(?:1|256|384|512):")
_URL = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://(?:[^\s:/@]*:([^\s@/]+)@)?[^\s/]*(\S*)")
_URL_SEGMENT = re.compile(r"[A-Za-z0-9_-]{16,}")
# Identifier shapes: UUIDs, hex (hashes, request and trace ids), digits.
_IDENTIFIER = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{7,}|[\d.:_-]+")
_HEX_RUN = frozenset("0123456789abcdefABCDEF-")
_KEYWORD_BEFORE = re.compile(r"(?i)\b(?:api[ _-]?key|key|token|secret|passw(?:or)?d|pwd|passphrase|auth|credentials?|bearer)\b")

# Personal-data refinement. Models tag log noise as personal data: request ids and UUIDs as
# account numbers, log timestamps as dates, API paths as URLs.
_ACCOUNT_SHAPE = re.compile(r"(?:\d[ -]?){8,19}|[A-Z]{2}\d{2}(?: ?[A-Z0-9]){10,30}")  # card/bank digits, IBAN
_ACCOUNT_KEYWORD = re.compile(r"(?i)\b(?:account|acct|iban|card|routing|bank|ssn|social security|tax id)\b")
# A number followed by a unit is a count or a size ("14987873 tokens left", "66060825 bytes").
_UNIT_AFTER = re.compile(r"(?i)\s?(?:tokens?|bytes?|[KMGT]i?B|B|ms|s|sec(?:onds?)?|chars?|characters?|lines?|rows?|records?"
                         r"|items?|requests?|users?|files?|words?|times|calls?|steps?|px|%)\b")
_TIME_OF_DAY = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?")
_RESERVED_EMAIL = re.compile(r"(?i)@(?:[\w-]+\.)*(?:example\.(?:com|net|org)|test|invalid|localhost)$")  # RFC 2606
# Not a person: no-reply senders, and the role mailboxes of RFC 2142.
_ROLE_EMAIL = re.compile(r"(?i)^(?:no-?reply|do-?not-?reply|notifications?|postmaster|mailer-daemon|hostmaster|webmaster"
                         r"|abuse|noc|security|info|marketing|sales|support|help|billing)@")
_FICTIONAL_PHONE = re.compile(r"555[-. ]?01\d\d$")  # NANP reserves 555-0100..0199 for fiction
_IDENTIFIER_WORD = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")  # ops-admin, setup.sh, etl
_BIRTH_BEFORE = re.compile(r"(?i)(?<![a-z])(?:born|birth|birthday|dob|d\.o\.b)(?![a-z])")  # matches date_of_birth too
_PII_EDGE = "/\\:;,.'\"`()[]{}<>|="

log = logging.getLogger("keepmum")

# Texts whose raw model output is kept: as many as the redactor caches.
MODEL_CACHE = 4096


class Pipeline:
    def __init__(self, rules: RulesDetector | None, pf=None, pii: bool = False, refine: bool = True,
                 refine_pii: bool = True):
        self.rules = rules
        self.pf = pf
        self.pii = pii
        # Turned off only to measure what refinement does (benchmark flags :raw and :rawpii).
        self.refine = refine
        self.refine_pii_spans = refine_pii
        self.allowlist = rules.allowlist if rules else None
        # Detection funnel for the stats feed: counts only, for newly scanned text.
        self.funnel: Counter[str] = Counter()
        self.by_type: Counter[tuple[str, str]] = Counter()  # (type, found|allowed|passed|masked)
        self.by_source: Counter[str] = Counter()  # rules, model, user
        self.by_rule: Counter[str] = Counter()  # rules:<id> or user:<name>
        self.pass_types: frozenset[str] = frozenset()  # types never masked ([detect] pass)
        self.generation = 0
        # The model's raw spans per text. Settings only filter them, so they outlive a settings
        # change: the redactor scans the resent conversation again, but only the rules rerun.
        self._model_cache: OrderedDict[bytes, tuple[Span, ...]] = OrderedDict()
        self.model_error: str | None = None  # why the model asked for (detect.model) is not running
        self.name = self._label()

    def _label(self) -> str:
        parts = [p for p, on in (("rules", self.rules), (getattr(self.pf, "name", "pf"), self.pf)) if on]
        flags = [f for f, on in (("pii", self.pii), ("raw", not self.refine), ("rawpii", not self.refine_pii_spans)) if on]
        return "+".join(parts) + "".join(f":{f}" for f in flags)

    def set_model(self, pf) -> None:
        """Turn the model on (a started PfServe) or off (None) while running. Bumps `generation`,
        so text scanned without the model is scanned again with it."""
        self.pf = pf
        self._model_cache.clear()
        self.name = self._label()
        self.generation += 1

    def detect(self, text: str) -> list[Span]:
        spans: list[Span] = []
        if self.rules:
            found = self.rules.detect(text, personal=self.pii, allowlist=False)
            self.funnel["candidates_rules"] += len(found)
            spans += found
        self.funnel["candidates_model"] += 0
        spans += self.model_spans(text)
        if self.pii and self.refine_pii_spans:
            # The rules' personal data gets the same noise checks as the model's: reserved
            # addresses, fictional numbers. Your own rules are kept as written.
            spans = [t for s in spans if (t := self.trim_pii(text, s)) is not None
                     and (t.type == "SECRET" or t.source.startswith("user:") or self.keep_pii(text, t))]
        if not self.pii:  # your own rules apply whatever their type
            spans = [s for s in spans if s.type == "SECRET" or s.source.startswith("user:")]
        # A file name or path that only a guess (entropy, the model) took for a secret is not one:
        # a token rule that knows the format still masks a key, wherever it is.
        from .rules import GUESSES, is_file_name

        # The model also calls some dated file names an EMAIL: without an @, nothing is one.
        spans = [s for s in spans if not (s.source in GUESSES and (
            s.type == "SECRET" and is_file_name(text, s.start, s.end)
            or s.type == "EMAIL" and "@" not in text[s.start:s.end]))]
        refined = len(spans)
        # Counted per value, after overlapping hits merge: three rules on one token are one secret.
        for s in merge(spans):
            self.by_type[(s.type, "found")] += 1
        if self.allowlist:
            kept = self.allowlist.filter(text, spans)
            for s in merge([s for s in spans if s not in set(kept)]):
                if not any(k.start < s.end and s.start < k.end for k in kept):
                    self.by_type[(s.type, "allowed")] += 1
            spans = kept
        allowed = len(spans)
        if self.pass_types:
            for s in merge([s for s in spans if s.type in self.pass_types]):
                self.by_type[(s.type, "passed")] += 1
            spans = [s for s in spans if s.type not in self.pass_types]
        merged = merge(spans)
        for s in merged:
            self.by_type[(s.type, "masked")] += 1
            # merge joins sources with "|": credit the user's rule first, then rules, then the model.
            parts = s.source.split("|")
            first = next((x for x in parts if x.startswith("user:")), None) or next((x for x in parts if x.startswith("rules")), None)
            self.by_source["user" if first and first.startswith("user:") else "rules" if first else "model"] += 1
            if first:
                self.by_rule[first] += 1
        # Stages: raw candidates -> after refinement -> after the allowlist -> masked (overlaps merged).
        self.funnel["after_refinement"] += refined
        self.funnel["after_allowlist"] += allowed
        self.funnel["masked"] += len(merged)
        return merged

    # --- Settings from the config, applied while running -------------------------------

    def apply_settings(self, pass_types=(), mask_rules=(), allow_strings=(), allow_patterns=(), off_rules=(),
                       pii: bool | None = None) -> None:
        """Types never masked, your own rules, your allowlist, built-in rules turned off. Bumps
        `generation` so cached detections (the redactor's) are dropped."""
        if pii is not None:
            self.pii = pii
        self.pass_types = frozenset(pass_types)
        if self.rules:
            self.rules.set_user_rules(list(mask_rules))
            self.rules.allowlist.set_user(allow_strings, allow_patterns)
            self.rules.off = frozenset(off_rules)
        self.name = self._label()
        self.generation += 1

    def lets_through(self, value: str, sources: frozenset[str], kind: str = "SECRET") -> bool:
        """A value masked earlier that the settings now let through: it has an exception, it is
        personal data and personal data is now off (unless your own rule found it), or every
        detector that found it is turned off ([rules] off, or the model) or was your own rule and is gone."""
        if self.allowlist and self.allowlist.allows(value):
            return True
        from .rules import GUESSES, is_file_name

        if kind == "SECRET" and sources and set(sources) <= GUESSES | {"known"} and is_file_name(value, 0, len(value)):
            return True  # a file name masked before keepmum knew better
        mine = {f"user:{r.id}" for r in self.rules.user_rules} if self.rules else set()
        if not self.pii and kind != "SECRET" and not any(x in mine for x in sources):
            return True
        if not self.rules or not sources:
            return False
        off = {f"rules:{r}" for r in self.rules.off}
        return all(x in off or (x.startswith("user:") and x not in mine)
                   or (self.pf is None and not x.startswith(("rules:", "user:"))) for x in sources)

    def report(self) -> dict:
        """Counts for the stats feed: the funnel, then per type and per detector. Never values."""
        from . import TYPES

        types = {t: {stage: self.by_type[(t, stage)] for stage in ("found", "allowed", "passed", "masked")} for t in TYPES}
        from .rules import builtin_rules

        top = [{"rule": k.split(":", 1)[1], "user": k.startswith("user:"), "n": n,
                "type": None if k.startswith("user:") else builtin_rules().get(k.split(":", 1)[1])}
               for k, n in self.by_rule.most_common(8)]
        out = {**self.funnel, "types": types, "sources": dict(self.by_source), "rules": top,
               "pass": sorted(self.pass_types), "off": sorted(self.rules.off) if self.rules else [],
               "model": self.pf is not None}
        if self.model_error:
            out["model_error"] = self.model_error
        return out

    def model_spans(self, text: str) -> list[Span]:
        """The model's spans: secrets refined, personal data only when enabled."""
        out: list[Span] = []
        pf = self.pf
        if not pf:
            return out
        for s in self._model_detect(pf, text):
            if s.type == "ACCOUNT" and self.refine and self.credential_not_account(text, s):
                s = Span(s.start, s.end, "SECRET", s.confidence, s.source)
            if s.type != "SECRET" and not self.pii:
                continue
            self.funnel["candidates_model"] += 1
            out += self.refine_secret(text, s) if self.refine and s.type == "SECRET" else [s]
        return out

    def _model_detect(self, pf, text: str) -> tuple[Span, ...]:
        key = hashlib.sha256(text.encode("utf-8", "surrogatepass")).digest()
        hit = self._model_cache.get(key)
        if hit is not None:
            self._model_cache.move_to_end(key)
            return hit
        spans = tuple(pf.detect(text))  # a failure raises: nothing is cached, the request is blocked
        self._model_cache[key] = spans
        if len(self._model_cache) > MODEL_CACHE:
            self._model_cache.popitem(last=False)
        return spans

    @staticmethod
    def credential_not_account(text: str, span: Span) -> bool:
        """The model sometimes labels an API key in prose an account number ("the Datadog key for
        staging is 3f9a…"). Not shaped like card or bank digits, with a digit (so not a name such as
        `X-API-Key`), after a secret keyword: a secret."""
        value = text[span.start : span.end]
        before = text[max(0, span.start - 40) : span.start]
        return (any(c.isdigit() for c in value) and not _ACCOUNT_SHAPE.fullmatch(value)
                and bool(_KEYWORD_BEFORE.search(before)))

    def refine_secret(self, text: str, span: Span) -> list[Span]:
        """Tighten a model SECRET span. The model tends to run spans across neighboring
        `key=value` fields in logs and to tag UUIDs and hashes as secrets.

        Secrets do not contain whitespace (private keys are L1's job), so the span splits
        into pieces. Each piece loses edge punctuation and a leading `label=`. Pieces shaped
        like identifiers survive only when a secret keyword precedes them in prose or an
        assignment (not as a URL path segment).
        """
        out = []
        for m in _PIECE.finditer(text, span.start, span.end):
            a, b = m.span()
            while a < b and text[a] in _EDGE:
                a += 1
            while b > a and text[b - 1] in _EDGE:
                b -= 1
            label = _LABEL.match(text, a, b)
            if label:
                a = label.end()
            digest = _DIGEST.match(text, a, b)
            if digest:
                a = digest.end()
            url = _URL.match(text, a, b)
            if url:
                # In a URL the secret is the password, or a token-like path segment (webhooks).
                if url.group(1):
                    a, b = url.span(1)
                elif not any(shannon_entropy(seg) > 3.5 for seg in _URL_SEGMENT.findall(url.group(2))):
                    continue
            value = text[a:b]
            if len(value) < 8:
                continue
            if self.rules and self.rules.looks_like_placeholder(value):
                continue
            # The model often starts a span partway into an id (".../accounts/e|65d2c19-..."):
            # judge the whole run of hex digits and hyphens around the piece.
            lo, hi = a, b
            while lo > 0 and text[lo - 1] in _HEX_RUN:
                lo -= 1
            while hi < len(text) and text[hi] in _HEX_RUN:
                hi += 1
            run = text[lo:hi]
            if _IDENTIFIER.fullmatch(value) or (run != value and _IDENTIFIER.fullmatch(run) and len(run.strip("-")) >= 16):
                before = text[max(0, lo - 40) : lo]
                if before.endswith("/") or not _KEYWORD_BEFORE.search(before):
                    continue
            out.append(Span(a, b, span.type, span.confidence, span.source))
        return out

    @staticmethod
    def trim_pii(text: str, span: Span) -> Span | None:
        """Model personal-data spans often carry punctuation at the edges ("/alex" in a path)."""
        if span.type == "SECRET" or span.source.startswith("rules"):
            return span
        a, b = span.start, span.end
        while a < b and text[a] in _PII_EDGE:
            a += 1
        while b > a and text[b - 1] in _PII_EDGE:
            b -= 1
        return Span(a, b, span.type, span.confidence, span.source) if a < b else None

    @staticmethod
    def keep_pii(text: str, span: Span) -> bool:
        """Drop model personal-data spans that are log noise or reserved example values."""
        value = text[span.start : span.end]
        before = text[max(0, span.start - 40) : span.start]
        if span.type == "ACCOUNT":
            after = text[span.end : span.end + 16]
            if _UNIT_AFTER.match(after) or text[span.start - 1 : span.start] == "." or after[:1] == "." and after[1:2].isdigit():
                return False  # a count, a size, or part of a decimal number
            digits = sum(c.isdigit() for c in value)
            keyword = bool(_ACCOUNT_KEYWORD.search(before))
            # Short runs of digits are everywhere (sizes, ids, counters): only after an account word.
            return keyword or bool(_ACCOUNT_SHAPE.fullmatch(value)) and (digits >= 10 or not value.isdigit())
        if span.type == "DATE":
            # A personal date is a date of birth; today's date or a release date is not.
            return not _TIME_OF_DAY.search(value) and bool(_BIRTH_BEFORE.search(text[max(0, span.start - 60) : span.start]))
        if span.type == "URL":
            return "://" in value  # an API path is not a personal URL
        if span.type == "EMAIL":
            return not (_RESERVED_EMAIL.search(value) or _ROLE_EMAIL.match(value))
        if span.type == "PHONE":
            return not _FICTIONAL_PHONE.search(value)
        if span.type == "PERSON":
            return not _IDENTIFIER_WORD.fullmatch(value)  # a name has a capital or a space
        return True


class ModelUnavailable(RuntimeError):
    pass


def load_model():
    """pf-serve with the pinned model, started. Fails loudly, never silently weaker."""
    from .pfserve import PfServe, PfServeError, find_binary, find_model

    off = "or run with the rules only: `keepmum model off`"
    binary, model = find_binary(), find_model()
    if binary is None:
        raise ModelUnavailable(f"pf-serve is not installed: build it with native/pf-serve/build.sh, {off}.")
    if model is None:
        raise ModelUnavailable(f"the Privacy Filter model is not downloaded: run `keepmum setup`, {off}.")
    try:
        return PfServe(binary, model)
    except (PfServeError, OSError) as e:
        raise ModelUnavailable(f"the Privacy Filter model did not load ({e}); {off}.") from e


def default_pipeline(pii: bool = False, rules_only: bool = False, allow_strings=(), allow_patterns=(),
                     pass_types=(), mask_rules=(), off_rules=()) -> Pipeline:
    """The v1 pipeline: the rules, then the model unless `rules_only` (detect.model = false)."""
    p = Pipeline(RulesDetector(), None if rules_only else load_model(), pii=pii)
    p.apply_settings(pass_types, mask_rules, allow_strings, allow_patterns, off_rules)
    return p


class ModelSwitch:
    """`detect.model`, applied while running. pf-serve starts and stops off the request path, and
    the swap waits for the scan in progress (`lock`, the proxy's scan lock), so a request sees the
    model or not, never half of it. Until the model has loaded, requests are scanned as before."""

    def __init__(self, pipeline: Pipeline, lock: threading.Lock, loader=None):
        self.pipeline, self.lock, self.loader = pipeline, lock, loader or load_model
        self.wanted = pipeline.pf is not None
        self._busy = threading.Lock()
        self.thread: threading.Thread | None = None

    def want(self, on: bool) -> None:
        self.wanted = on
        if on != (self.pipeline.pf is not None) or (not on and self.pipeline.model_error):
            self.thread = threading.Thread(target=self._apply, name="keepmum-model", daemon=True)
            self.thread.start()

    def _apply(self) -> None:
        with self._busy:
            while self.wanted != (self.pipeline.pf is not None):
                if self.wanted:
                    try:
                        pf = self.loader()
                    except ModelUnavailable as e:
                        self.pipeline.model_error = str(e)
                        log.error("model on, but %s Scanning with the rules only.", e)
                        return
                    with self.lock:
                        self.pipeline.model_error = None
                        self.pipeline.set_model(pf)
                    log.info("model on: scanning with %s", self.pipeline.name)
                else:
                    with self.lock:
                        old = self.pipeline.pf
                        self.pipeline.set_model(None)
                    old.close()
                    log.info("model off: scanning with the rules only")
            if not self.wanted:
                self.pipeline.model_error = None
