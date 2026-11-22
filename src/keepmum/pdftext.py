"""The text of a PDF, read on this Mac, so a secret printed in it can be found before it is sent.

Agents attach a PDF they read whole: Claude Code and opencode as a `document` block, Codex as an
`input_file`, Gemini clients as `inline_data`. keepmum reads text, so a key in a vendor's onboarding
PDF went to the provider (doc-02, Nov 18). This reads what a PDF draws as text: its content streams
(FlateDecode opened), the strings shown by Tj, TJ, ' and ", literal or hex, mapped through the
fonts' ToUnicode tables where they have them. No layout, no images, no OCR: a PDF whose text is
pictures, or whose fonts map nothing, reads as little or nothing, and goes as it is.

Standard library only; bounded in size and time by the caps below.
"""

from __future__ import annotations

import base64
import binascii
import re
import zlib

MAX_PDF = 25_000_000  # bytes of PDF read; a larger one goes as it is
MAX_TEXT = 400_000  # characters of text kept
MAX_STREAM = 20_000_000  # bytes a stream may inflate to

_STREAM = re.compile(rb"<<(?P<dict>(?:[^<>]|<<(?:[^<>]|<<[^<>]*>>)*>>|<[0-9A-Fa-f\s]*>)*)>>\s*stream\r?\n", re.S)
# A string's escape takes any next byte, a newline too: a backslash at a line's end continues it.
_TOKEN = re.compile(rb"\((?:\\[\s\S]|[^\\()]|\((?:\\[\s\S]|[^\\()])*\))*\)|<[0-9A-Fa-f\s]*>|\[|\]|/[^\s/<>\[\]()]+|[-+]?\d*\.\d+|[-+]?\d+|[A-Za-z'\"*]+")
_ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f", b"(": b"(", b")": b")", b"\\": b"\\"}
_BFCHAR = re.compile(rb"beginbfchar(.*?)endbfchar", re.S)
_BFRANGE = re.compile(rb"beginbfrange(.*?)endbfrange", re.S)
_HEX = re.compile(rb"<([0-9A-Fa-f]+)>")


def is_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-"


def _streams(pdf: bytes):
    """(dictionary, decoded bytes) of each stream: FlateDecode opened, other filters skipped."""
    for m in _STREAM.finditer(pdf):
        start = m.end()
        end = pdf.find(b"endstream", start)
        if end < 0:
            break
        raw = pdf[start:end].rstrip(b"\r\n")
        filters = re.findall(rb"/(\w+Decode)", m.group("dict"))
        if filters == [b"FlateDecode"]:
            try:
                d = zlib.decompressobj()
                raw = d.decompress(raw, MAX_STREAM)
            except zlib.error:
                continue
        elif filters:
            continue
        yield m.group("dict"), raw


def _literal(s: bytes) -> bytes:
    out, i = bytearray(), 1
    while i < len(s) - 1:
        c = s[i:i + 1]
        if c == b"\\":
            nxt = s[i + 1:i + 2]
            if nxt in _ESCAPES:
                out += _ESCAPES[nxt]
                i += 2
                continue
            oct_ = re.match(rb"[0-7]{1,3}", s[i + 1:i + 4])
            if oct_:
                out.append(int(oct_.group(), 8) & 0xFF)
                i += 1 + len(oct_.group())
                continue
            i += 2 if nxt in (b"\n", b"\r") else 1
            continue
        out += c
        i += 1
    return bytes(out)


def _cmaps(streams: list[tuple[bytes, bytes]]) -> tuple[dict[bytes, str], int]:
    """Every ToUnicode table in the PDF, merged: code bytes -> text, and the width of its codes."""
    table: dict[bytes, str] = {}
    widths = []

    def text(hexstr: bytes) -> str:
        with_bytes = binascii.unhexlify(hexstr if len(hexstr) % 2 == 0 else hexstr + b"0")
        return with_bytes.decode("utf-16-be", "replace")

    for _, data in streams:
        if b"beginbfchar" not in data and b"beginbfrange" not in data:
            continue
        for block in _BFCHAR.findall(data):
            pairs = _HEX.findall(block)
            for src, dst in zip(pairs[::2], pairs[1::2]):
                table[binascii.unhexlify(src)] = text(dst)
                widths.append(len(src) // 2)
        for block in _BFRANGE.findall(data):
            for line in block.splitlines():
                hexes = _HEX.findall(line)
                if len(hexes) < 3 or b"[" in line:
                    continue
                lo, hi, dst = int(hexes[0], 16), int(hexes[1], 16), int(hexes[2], 16)
                width = len(hexes[0]) // 2
                widths.append(width)
                for k, code in enumerate(range(lo, min(hi, lo + 65535) + 1)):
                    table[code.to_bytes(width, "big")] = chr(min(dst + k, 0x10FFFF))
    width = max(set(widths), key=widths.count) if widths else 1
    return table, width


def _decode(raw: bytes, table: dict[bytes, str], width: int, hexed: bool) -> str:
    if table and (hexed or width == 1):
        out = []
        for i in range(0, len(raw) - width + 1, width):
            code = raw[i:i + width]
            out.append(table.get(code, code.decode("latin-1") if width == 1 else ""))
        if any(out):
            return "".join(out)
    return raw.decode("latin-1")


def text_of(pdf: bytes) -> str:
    """The PDF's text, line by line, as far as it can be read; "" when none can."""
    if not is_pdf(pdf) or len(pdf) > MAX_PDF:
        return ""
    streams = list(_streams(pdf))
    table, width = _cmaps(streams)
    lines, line, size = [], [], 0
    for _, data in streams:
        if b"BT" not in data or b"beginbfchar" in data or b"beginbfrange" in data:
            continue
        operands: list = []
        for tok in _TOKEN.findall(data):
            if tok[:1] == b"(":
                operands.append(_decode(_literal(tok), table, width, False))
            elif tok[:1] == b"<":
                hexstr = re.sub(rb"\s", b"", tok[1:-1])
                operands.append(_decode(binascii.unhexlify(hexstr if len(hexstr) % 2 == 0 else hexstr + b"0"), table, width, True))
            elif tok in (b"[", b"]"):
                operands.append(tok)
            elif tok[:1].isdigit() or tok[:1] in b"-+.":
                operands.append(float(tok))
            elif tok in (b"Tj", b"'", b'"'):
                line += [o for o in operands if isinstance(o, str)][-1:]
                if tok != b"Tj":
                    lines.append("".join(line))
                    line = []
                operands = []
            elif tok == b"TJ":
                for o in operands:
                    if isinstance(o, str):
                        line.append(o)
                    elif isinstance(o, float) and o < -200:  # a gap as wide as a space
                        line.append(" ")
                operands = []
            elif tok in (b"Td", b"TD") and len(operands) >= 2 and operands[-1] == 0:
                operands = []  # a move along the line (Chrome places runs this way): the same line
            elif tok in (b"T*", b"Td", b"TD", b"ET", b"Tm"):
                if line:
                    lines.append("".join(line))
                    line = []
                operands = []
            else:
                operands = [] if tok[:1] != b"/" else operands
            size = sum(len(x) for x in lines)
            if size > MAX_TEXT:
                break
        if line:
            lines.append("".join(line))
            line = []
        if size > MAX_TEXT:
            break
    text = "\n".join(x.rstrip() for x in lines if x.strip())
    return text[:MAX_TEXT]


def from_base64(data: str) -> bytes | None:
    """The bytes of base64 text (a `data:` URL's too), or None."""
    if data.startswith("data:"):
        data = data.partition(",")[2]
    try:
        return base64.b64decode(data, validate=False)
    except (binascii.Error, ValueError):
        return None
