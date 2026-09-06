"""Rasterise the ring mark to a PNG, deterministically, with the standard library.

macOS notifications take an icon as a **file path**, so the mark has to exist as
a raster somewhere the wheel carries. Converting `mark.svg` with `qlmanage` or
`rsvg-convert` would work on the machine that has one and nowhere else, and the
committed asset's provenance would be "whatever a thumbnailer produced that day".

This draws the same two circles from the same numbers instead: `design/mark.svg`
is a 24-unit viewBox with `r=9`, `stroke-width=2.6`, and an arc of `38.45` of the
`56.55` circumference starting at twelve o'clock. Run it and you get the same
bytes on any platform with a Python and no other dependency.

    python design/render_mark_png.py

`tests/test_notify.py` re-runs it and compares, so the committed PNG cannot drift
from the SVG it claims to come from.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

SIZE = 256
SS = 4  # supersample factor, box-downsampled for the antialiasing

# Straight from design/mark.svg's viewBox units.
VIEW = 24.0
CX = CY = 12.0
R = 9.0
STROKE = 2.6
ARC_LEN = 38.45
CIRCUMFERENCE = 56.55

# The dark-scheme palette, which reads on a light banner too. A PNG cannot carry
# `prefers-color-scheme`, so one fixed pair is the honest choice.
RING = (0x62, 0x6A, 0x6B)  # --txt-far
ARC = (0xF2, 0xB3, 0x3D)  # --s1, the session amber


def _coverage() -> list[list[tuple[int, int, int, int]]]:
    """One RGBA row per pixel row, supersampled then averaged."""
    big = SIZE * SS
    scale = big / VIEW
    cx, cy = CX * scale, CY * scale
    r, half = R * scale, STROKE * scale / 2
    fraction = ARC_LEN / CIRCUMFERENCE

    # Accumulate colour and alpha at supersample resolution, then box-filter.
    acc = [[(0, 0, 0, 0)] * SIZE for _ in range(SIZE)]
    sums = [[[0, 0, 0, 0] for _ in range(SIZE)] for _ in range(SIZE)]
    import math

    for sy in range(big):
        dy = sy + 0.5 - cy
        for sx in range(big):
            dx = sx + 0.5 - cx
            dist = math.hypot(dx, dy)
            if abs(dist - r) > half:
                continue
            # Angle clockwise from twelve o'clock, matching rotate(-90).
            angle = (math.degrees(math.atan2(dx, -dy))) % 360.0
            colour = ARC if angle <= fraction * 360.0 else RING
            cell = sums[sy // SS][sx // SS]
            cell[0] += colour[0]
            cell[1] += colour[1]
            cell[2] += colour[2]
            cell[3] += 255

    n = SS * SS
    for y in range(SIZE):
        for x in range(SIZE):
            r_, g_, b_, a_ = sums[y][x]
            hits = a_ // 255
            if not hits:
                continue
            acc[y][x] = (r_ // hits, g_ // hits, b_ // hits, a_ // n)
    return acc


def _png(rows: list[list[tuple[int, int, int, int]]]) -> bytes:
    raw = b"".join(
        b"\x00" + bytes(v for px in row for v in px)  # filter type 0, then RGBA
        for row in rows
    )

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def render() -> bytes:
    return _png(_coverage())


if __name__ == "__main__":
    out = Path(__file__).resolve().parents[1] / "src" / "quotalens" / "web" / "mark.png"
    out.write_bytes(render())
    print(f"wrote {out} ({out.stat().st_size} bytes)")
