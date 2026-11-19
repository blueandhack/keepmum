"""Union spans from all layers. When in doubt, redact: overlaps merge into the widest span."""

from __future__ import annotations

import re

from . import PRIORITY, Span


def merge(spans: list[Span]) -> list[Span]:
    """Merge overlapping spans; the result keeps the highest-priority type and confidence."""
    out: list[Span] = []
    for s in sorted(spans, key=lambda s: (s.start, -s.end)):
        if out and s.start < out[-1].end:
            cur = out[-1]
            sources = cur.source if s.source in cur.source.split("|") else f"{cur.source}|{s.source}"
            out[-1] = Span(
                cur.start,
                max(cur.end, s.end),
                min(cur.type, s.type, key=PRIORITY.__getitem__),
                max(cur.confidence, s.confidence),
                sources,
            )
        else:
            out.append(s)
    return out


def narrow(spans: list[Span], text: str) -> list[Span]:
    """Before merging, the overlaps that should not merge into the widest span: a generic match that
    only adds a line's `$` to a token a format rule found, and a URL flagged for the value in it."""
    return _narrow_urls(_guess_around_rule(_trailing_dollar(spans, text), text))


# What a guess may add around a value a format rule found exactly, and still be about that value:
# the user and colon before a password, the host, port and database after it.
_BEFORE = re.compile(r"(?:[\w.+%-]*:|s?[/|#]|['\"=])?")  # also `s/` of a sed expression (rotate-01)
_AFTER = re.compile(r"(?:@(?:[\w.-]+(?::\d+)?(?:/[\w./-]*)?)?|[/|#]\w*/?|['\"])?")  # the @ alone too (git-01), a sed's /…/


def _guess_around_rule(spans: list[Span], text: str) -> list[Span]:
    """A guess (the model, entropy) over a value a format rule found exactly, or over part of it,
    plus only the user and host around it, gives way to the rule's span. The model masked `<password>@db.internal:5432/orders`
    from a connection string whose password the connection-string rule had found (k8s-01, Nov 18):
    the placeholder stood for the host too, and the model could no longer say where the database is."""
    from .rules import GUESSES

    exact = [s for s in spans if s.source.startswith("rules:") and s.source not in GUESSES
             and "secret-assignment" not in s.source]
    if not exact:
        return spans
    return [s for s in spans if not (s.source in GUESSES and any(
        s.start < e.end and e.start < s.end and (e.start, e.end) != (s.start, s.end)  # overlapping, not the same
        and _BEFORE.fullmatch(text, min(s.start, e.start), e.start)
        and _AFTER.fullmatch(text, e.end, max(s.end, e.end)) for e in exact))]


def _trailing_dollar(spans: list[Span], text: str) -> list[Span]:
    """A generic `KEY=value` match that runs one `$` past a token a format rule found whole gives
    way to that token. `sed -n l` and `cat -A` end each line with `$`, and so does a grep anchor
    (`^GITHUB_TOKEN=ghp_…$`): masked with it, the model got one placeholder for the token and the
    `$`, and a copy of it restored both (a live pi run on DeepSeek, base-01)."""
    exact = [s for s in spans if s.source.startswith("rules:") and "secret-assignment" not in s.source]
    if not exact:
        return spans
    return [s for s in spans if not ("secret-assignment" in s.source and text[s.end - 1:s.end] == "$" and any(
        e.start == s.start and e.end == s.end - 1 for e in exact))]


def _narrow_urls(spans: list[Span]) -> list[Span]:
    """A URL the model flags for the value inside it gives way to that value's own span.

    `curl "https://html.duckduckgo.com/html/?q=Margaret Thornbury"` was masked whole, as one
    placeholder (a live pi run, web-01): the model could no longer read the command it ran, and the
    placeholder stood for a URL, not the name. The value inside is masked as it always is; the host
    and path around it were never what made the URL private.
    """
    inner = [s for s in spans if s.type != "URL"]
    if not inner or not any(s.type == "URL" for s in spans):
        return spans
    return [s for s in spans if not (s.type == "URL" and any(
        s.start <= i.start and i.end <= s.end and (i.end - i.start) < (s.end - s.start) for i in inner))]
