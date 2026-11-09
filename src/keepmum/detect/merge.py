"""Union spans from all layers. When in doubt, redact: overlaps merge into the widest span."""

from __future__ import annotations

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
    return _narrow_urls(_trailing_dollar(spans, text))


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
