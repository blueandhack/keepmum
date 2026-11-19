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
from .merge import merge, narrow
from .rules import RulesDetector, shannon_entropy

_PIECE = re.compile(r"\S+")
_EDGE = "/=:\"'`,;()[]{}<>\\"
# A CSV row's fields: the model ran a span over `5e1f0c9a-…,us` (nb-01, Nov 18). A secret has no comma.
_FIELD = re.compile(r"[^\s,]+")
# A line break escaped in JSON text ends a piece as a real one does (`None\nfor`: git-01, Nov 18).
_ESCAPED_BREAK = re.compile(r"\\[nrt]")
# A `name=` inside a piece: what follows is the value (`registry.npmjs.org/:_authToken=npm_…`: git-01).
_LINE_BREAK = re.compile(r"\n|\\n")
_PII_CUT = re.compile(r"\\+[nrt]|\n|,(?! )")
_INNER_LABEL = re.compile(r"[A-Za-z_][\w.-]*=(?=\S{8,}$)")


def _pieces(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """The pieces of a model span: split at whitespace, and at commas where the piece is a row of
    fields (two or more commas, or a comma after an id)."""
    out = []
    for m in _PIECE.finditer(text, start, end):
        if _ESCAPED_BREAK.search(m.group()):
            at = m.start()
            for brk in _ESCAPED_BREAK.finditer(text, m.start(), m.end()):
                if brk.start() > at:
                    out.append((at, brk.start()))
                at = brk.end()
            if at < m.end():
                out.append((at, m.end()))
            continue
        piece = m.group()
        if piece.count(",") >= 2 or ("," in piece and _IDENTIFIER.fullmatch(piece.split(",", 1)[0])):
            out += [f.span() for f in _FIELD.finditer(text, m.start(), m.end())]
        else:
            out.append(m.span())
    return out
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


# Places a name alone does not make an address: China's municipalities, provinces and largest cities.
_PLACES = frozenset("""北京 上海 天津 重庆 广州 深圳 杭州 南京 苏州 成都 武汉 西安 长沙 郑州 青岛 济南 沈阳 大连 厦门
福州 宁波 无锡 合肥 昆明 哈尔滨 长春 石家庄 太原 南昌 南宁 贵阳 兰州 海口 三亚 珠海 东莞 佛山 香港 澳门 台北
河北 山西 辽宁 吉林 黑龙江 江苏 浙江 安徽 福建 江西 山东 河南 湖北 湖南 广东 海南 四川 贵州 云南 陕西 甘肃
青海 台湾 内蒙古 广西 西藏 宁夏 新疆
Beijing Shanghai Tianjin Chongqing Guangzhou Shenzhen Hangzhou Nanjing Suzhou Chengdu Wuhan Xian Changsha
Zhengzhou Qingdao Xiamen Hong Kong Macau Taipei""".split())
_MONTH_OR_DIGITS = re.compile(r"(?i)\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b|[\d\s:,./-]+")


def _plausible_address(value: str) -> bool:
    """A model ADDRESS that is one: not a number or a listing's date (`128 Nov 18`: i18n-01 on Codex),
    and not a city's name alone (北京, Paris), which a street, a number or more words would make one."""
    words = [w for w in re.split(r"[\s,;/()0-9]+", value) if w]
    if words and all(w.rstrip("市") in _PLACES for w in words):
        return False  # a list of cities with their counts: `Beijing (3), Shanghai` (i18n-01)
    letters = _MONTH_OR_DIGITS.sub("", value)
    if len(letters) < 2 or not any(c.isdigit() for c in value) and len(value.strip()) <= 6 and " " not in value.strip():
        return False
    return value.rstrip("市") not in _PLACES


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
            spans = [t for s in spans for piece in self.split_pii(text, s) if (t := self.trim_pii(text, piece)) is not None
                     and (t.type == "SECRET" or t.source.startswith("user:") or self.keep_pii(text, t))]
        if not self.pii:  # your own rules apply whatever their type
            spans = [s for s in spans if s.type == "SECRET" or s.source.startswith("user:")]
        # What only a guess (entropy, the model) took for a secret is not one when it has a public
        # shape (rules.public_shape): a file name or path, a search tool's link, a path on this Mac,
        # a pin by hash, a page's nonce. A token rule that knows the format still masks a key, wherever it is.
        from .rules import GUESSES, public_shape

        # The model also calls some dated file names an EMAIL: without an @, nothing is one.
        spans = [s for s in spans if not (s.source in GUESSES and (
            s.type == "SECRET" and public_shape(text, s.start, s.end)
            or s.type == "EMAIL" and "@" not in text[s.start:s.end]))]
        spans = narrow(spans, text)
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
        from .rules import GUESSES, is_file_name, is_path, is_pin

        if kind == "SECRET" and sources and set(sources) <= GUESSES | {"known"} and (
                is_file_name(value, 0, len(value)) or is_path(value) or is_pin(value)):
            return True  # a file name or a pin masked before keepmum knew better
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
        from .rules import file_view

        # With personal data on, the model reads names in file names as words (rules.file_view).
        for s in self._model_detect(pf, file_view(text) if self.pii else text):
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
        for a, b in _pieces(text, span.start, span.end):
            while a < b and text[a] in _EDGE:
                a += 1
            while b > a:  # an edge, or an escape JSON text leaves after a value (`\"`, `\n`: nb-01, Nov 18)
                if text[b - 1] in _EDGE:
                    b -= 1
                elif b - a > 2 and text[b - 2] == "\\" and text[b - 1] in "nrt":
                    b -= 2
                else:
                    break
            label = _LABEL.match(text, a, b) or _INNER_LABEL.search(text, a, b)
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
            # A reference is not the value (os.environ["STRIPE_SECRET_KEY"], a header's name: rotate-01,
            # debug-01, Nov 18): every filter the rules apply to an assigned value, for the model too.
            if self.rules and self.rules.looks_like_placeholder(value, "all"):
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
                # The secret word must be on the id's own line: `Authorization: Bearer …` a line above
                # a request id made it a secret (debug-01 on Codex, Nov 18).
                before = _LINE_BREAK.split(text[max(0, lo - 40) : lo])[-1]
                if before.endswith("/") or not _KEYWORD_BEFORE.search(before):
                    continue
            out.append(Span(a, b, span.type, span.confidence, span.source))
        return out

    @staticmethod
    def split_pii(text: str, span: Span) -> list[Span]:
        """A model personal-data span cut into the fields it ran over: at a line break escaped in JSON
        text, and at a comma with no space after it (a CSV row). The model gave one ADDRESS over
        `张伟,+86 138 0013 8000,110101199003071234`, and one PERSON over two lines (i18n-01, Nov 18). An
        address's own `, ` keeps it whole."""
        if span.type == "SECRET" or not span.source.startswith("pf"):
            return [span]
        cuts = [m.span() for m in _PII_CUT.finditer(text, span.start, span.end)]
        if not cuts:
            return [span]
        out, at = [], span.start
        for a, b in cuts + [(span.end, span.end)]:
            if a > at:
                out.append(Span(at, a, span.type, span.confidence, span.source))
            at = b
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
        if span.type == "PHONE":  # a number starts at a digit, +, or (: not at the name before it (i18n-01)
            while a < b and not (text[a].isdigit() or text[a] in "+("):
                a += 1
        if span.type not in ("EMAIL", "URL") and a < b:
            # A name in a path or file name: not the folders before it, nor the rest of the file
            # name after it (clients/Margaret Thornbury_resume.md is Margaret Thornbury). Every type
            # but addresses with their own dots and underscores, so that no span the model also
            # gives (an ADDRESS over Thornbury_cover) merges the name back into the file name.
            from .rules import _EXT_AFTER

            value = text[a:b]
            if "/" in value:
                a += value.rindex("/") + 1
            # Cut at `_resume`, `_cover_letter`: an underscore before a lowercase word. Names are
            # capitalized, so Margaret_Thornbury stays whole.
            cut = next((i for i in range(a, b) if text[i] == "_" and text[i + 1:i + 2].islower()
                        or text[i] == "." and _EXT_AFTER.match(text, i)), b)
            b = cut
            while b > a and text[b - 1] in _PII_EDGE + " ":
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
            # A domain without a dot is no mail domain: Codex's plugin ids, gmail@openai-curated-remote (Nov 18).
            if "." not in value.rpartition("@")[2]:
                return False
            return not (_RESERVED_EMAIL.search(value) or _ROLE_EMAIL.match(value))
        if span.type == "PHONE":
            return not _FICTIONAL_PHONE.search(value)
        if span.type == "PERSON":
            # A name has a capital or a space; it is no dot-file (.git, .codex in a listing), no
            # handle or path, and no city (北京 in a table of cities: i18n-01, Nov 18).
            return not (_IDENTIFIER_WORD.fullmatch(value) or value[:1] in ".@" or "/" in value or "@" in value
                        or value.rstrip("市") in _PLACES
                        or value.isascii() and " " not in value.strip() and not any(c.isupper() for c in value))  # n-rw-r--r
        if span.type == "ADDRESS":
            return _plausible_address(value)
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
