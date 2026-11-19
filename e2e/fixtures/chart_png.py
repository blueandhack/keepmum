"""A small bar chart as a PNG, the same bytes every run, from the standard library only.

  python3 chart_png.py FILE MARKER   replace MARKER in FILE with the chart in base64 (a notebook's
                                     image/png output)
  python3 chart_png.py --sha256      the sha256 of the chart's bytes (for a case's check)
"""

import base64
import hashlib
import struct
import sys
import zlib

W, H = 160, 100
BARS = [(20, 70, (66, 133, 244)), (65, 40, (66, 133, 244)), (110, 85, (219, 68, 55))]  # x, height, colour


def chart() -> bytes:
    rows = []
    for y in range(H):
        row = bytearray([0])  # filter: none
        for x in range(W):
            colour = (255, 255, 255)
            if y == H - 5:
                colour = (0, 0, 0)  # the axis
            for bx, height, c in BARS:
                if bx <= x < bx + 30 and H - 5 - height <= y < H - 5:
                    colour = c
            grain = (x * 7919 + y * 104729) * 2654435761 >> 13 & 7  # antialiasing-like grain, fixed
            row += bytes(min(255, c + grain) if c < 248 else c - grain for c in colour)
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    ihdr = struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
            + chunk(b"IEND", b""))


if __name__ == "__main__":
    if sys.argv[1:] == ["--sha256"]:
        print(hashlib.sha256(chart()).hexdigest())
    else:
        path, marker = sys.argv[1], sys.argv[2]
        with open(path) as f:
            text = f.read()
        with open(path, "w") as f:
            f.write(text.replace(marker, base64.b64encode(chart()).decode()))
