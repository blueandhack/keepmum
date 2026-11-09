"""Secrets a command printed encoded: a hex dump, plain hex, `od -c`, base64.

keepmum finds a secret by its format, or by its text when it was masked before. A command that
prints a file encoded shows neither: `xxd .env` writes the token as hex, and its text column cuts
it into 16-character pieces, one per line (a live pi run on DeepSeek sent them out that way). So
encoded blocks are decoded, and the decoded text is scanned: if it holds a secret or a value
masked before, the whole block is masked, as one value.

What counts as a block, and what is decoded:

- a hex dump: lines that start with an offset, then hex bytes (`xxd`, `hexdump -C`, `od -t x1`),
  their text column included; consecutive lines are one block
- `od -c`: an offset, then one character (or escape) per 4-column field
- plain hex (`xxd -p`): a run of 32 or more hex digits, across consecutive lines
- base64: a run of 20 or more base64 characters, across consecutive lines (`base64` wraps)

Only text comes out of decoding: bytes that are not mostly printable UTF-8 (a hash, an image) are
left alone. The decoded text is scanned by the rules and the values masked before, never by the
guesses (entropy, the model): decoded noise must not mask a hash.
"""

from __future__ import annotations

import base64
import binascii
import re

# An offset starts a dump line, or follows a quote or a space there (`{"output": "00000000: …`).
_OFFSET = re.compile(r"(?:^|(?<=[\s\"'`]))([0-9a-fA-F]{6,16}):?(?=[ \t])")
_HEX_TOKEN = re.compile(r"(?:[0-9a-fA-F]{2}){1,4}")
_OD_C = re.compile(r"(?:^|(?<=[\s\"'`]))[0-7]{7}(?=  )")
_PLAIN_HEX = re.compile(r"(?<![0-9A-Za-z])(?:[0-9a-fA-F]{2}){16,}(?![0-9A-Za-z])")
_PLAIN_HEX_LINE = re.compile(r"(?:[0-9a-fA-F]{2}){8,}")
_B64_RUN = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/_-]{20,}={0,2}(?![A-Za-z0-9+/=_-])")
_B64_LINE = re.compile(r"[A-Za-z0-9+/_-]{16,}={0,2}")
_OD_ESCAPES = {"\\n": "\n", "\\t": "\t", "\\r": "\r", "\\0": "\0", "\\v": "\v", "\\f": "\f", "\\b": "\b", "\\a": "\a"}


def _lines(text: str):
    """(start, end, line) of each line, end before its new line (and its \\r: a command run in a
    terminal, as the Antigravity CLI runs them, ends its lines \\r\\n)."""
    pos = 0
    for line in text.split("\n"):
        body = line[:-1] if line.endswith("\r") else line
        yield pos, pos + len(body), body
        pos += len(line) + 1


_BARE_HEX = re.compile(r"[ \t]*(?:(?:[0-9a-fA-F]{2}){1,2}[ \t]+){3,}(?:[0-9a-fA-F]{2}){1,2}[ \t]*")


def _dump_bytes(line: str) -> bytes | None:
    """The bytes a hex-dump line shows, or None: not one. With an offset (`xxd`, `hexdump -C`,
    `od -t x1`), hex fields up to the text column; without one (`od -An -t x1`), a line of at least
    4 hex fields of one width and nothing else."""
    m = _OFFSET.search(line)
    if m and m.start() <= 40:
        rest = line[m.end():]
    elif _BARE_HEX.fullmatch(line) and len({len(t) for t in line.split()}) == 1:
        rest = line
    else:
        return None
    out = bytearray()
    for tok in rest.split():
        if tok.startswith("|") or not _HEX_TOKEN.fullmatch(tok) or len(out) >= 64:
            break  # the text column, or past what a dump line holds
        out += bytes.fromhex(tok)
    return bytes(out) if out else None


_OD_FIELD = re.compile(r" {4}| {1,3}(\\[0-7]{1,3}|\\[ntrvfab]|\S)")


def _od_c(line: str) -> str | None:
    """The characters an `od -c` line shows, or None: an offset (or none, `od -An -c`), then a
    field per character, right-aligned in 4 columns (4 spaces: a space); at least 4 without one."""
    m = _OD_C.search(line)
    if m and m.start() <= 40:
        rest, bare = line[m.end() + 1:], False
    elif line.startswith(" " * 8) and line.strip():
        rest, bare = line[8:], True  # where the 7-digit offset and its space would be
    else:
        return None
    out, i, fields = [], 0, 0
    while i < len(rest):
        f = _OD_FIELD.match(rest, i)
        if not f:
            return None
        tok = f.group(1)
        if tok is None:
            out.append(" ")
        elif tok in _OD_ESCAPES:
            out.append(_OD_ESCAPES[tok])
        elif tok.startswith("\\") and tok[1:].isdigit():
            out.append(chr(int(tok[1:], 8)))
        elif len(tok) == 1:
            out.append(tok)
        else:
            return None
        fields += tok is not None
        i = f.end()
    if bare and (fields < 4 or _dump_bytes(line) is not None):
        return None
    return "".join(out).rstrip(" ") or None


def _text(raw: bytes) -> str | None:
    """raw as text when it is text: valid UTF-8, nearly all of it printable."""
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if len(s) < 8:
        return None
    printable = sum(c.isprintable() or c in "\n\r\t" for c in s)
    return s if printable >= 0.95 * len(s) else None


def _b64(s: str) -> bytes | None:
    s = s.rstrip("=")
    try:
        if "-" in s or "_" in s:
            return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
        return base64.b64decode(s + "=" * (-len(s) % 4), validate=True)
    except (binascii.Error, ValueError):
        return None


def blocks(text: str) -> list[tuple[int, int, str]]:
    """(start, end, decoded text) of each encoded block in text that decodes to text."""
    if len(text) < 20:
        return []
    out: list[tuple[int, int, str]] = []
    lines = list(_lines(text))

    def runs(parse, other=None):
        """Consecutive lines `parse` reads, as (start, end, [parsed]); lines `other` reads (the
        character lines `od -t x1c` puts between its hex lines) belong to the run, adding nothing."""
        run: list = []
        for start, end, line in lines:
            got = parse(line)
            if got is not None:
                run.append((start, end, got))
                continue
            if run and other is not None and other(line) is not None:
                run.append((start, end, type(run[-1][2])()))
                continue
            if run:
                yield run[0][0], run[-1][1], [g for _, _, g in run]
                run = []
        if run:
            yield run[0][0], run[-1][1], [g for _, _, g in run]

    for start, end, parts in runs(_dump_bytes, _od_c):
        if (s := _text(b"".join(parts))) is not None:
            out.append((start, end, s))
    for start, end, parts in runs(_od_c, _dump_bytes):
        s = "".join(parts)
        if len(s) >= 8:
            out.append((start, end, s))
    taken = [(a, b) for a, b, _ in out]

    def free(a: int, b: int) -> bool:
        return not any(a < y and x < b for x, y in taken)

    # Plain hex and base64 that `xxd -p` and `base64` wrap over lines: whole lines of it are one block.
    for parse, decode in ((lambda l: l.strip() if _PLAIN_HEX_LINE.fullmatch(l.strip()) else None,
                           lambda s: bytes.fromhex(s) if len(s) % 2 == 0 else None),
                          (lambda l: l.strip() if _B64_LINE.fullmatch(l.strip()) else None, _b64)):
        for start, end, parts in runs(parse):
            if len(parts) > 1 and free(start, end) and (raw := decode("".join(parts))) and (s := _text(raw)):
                out.append((start, end, s))
                taken.append((start, end))
    # Inline: a run in a line of other text.
    for rx, decode in ((_PLAIN_HEX, bytes.fromhex), (_B64_RUN, _b64)):
        for m in rx.finditer(text):
            if free(m.start(), m.end()) and (raw := decode(m.group())) and (s := _text(raw)):
                out.append((m.start(), m.end(), s))
                taken.append((m.start(), m.end()))
    return out


def encoded(text: str, depth: int = 2) -> list[tuple[int, int, str]]:
    """blocks(text), and in text that is a JSON document (Codex's command output is JSON in a text
    field), the blocks in each of its strings, at the place their JSON-escaped form sits in text."""
    out: list[tuple[int, int, str]] = []
    if depth and text.lstrip()[:1] in "{[" and len(text) >= 40:
        import json

        try:
            doc = json.loads(text)
        except ValueError:
            return blocks(text)
        todo, strings = [doc], []
        while todo:
            x = todo.pop()
            if isinstance(x, dict):
                todo += x.values()
            elif isinstance(x, list):
                todo += x
            elif isinstance(x, str) and len(x) >= 20:
                strings.append(x)
        for s in strings:
            for a, b, d in encoded(s, depth - 1):
                form = json.dumps(s[a:b], ensure_ascii=False)[1:-1]
                at = text.find(form)
                if at >= 0 and not any(x <= at < y for x, y, _ in out):
                    out.append((at, at + len(form), d))
    # Read as it is too: a JSON document's own text, a dump that is not in one.
    out += [b for b in blocks(text) if not any(x < b[1] and b[0] < y for x, y, _ in out)]
    return out
