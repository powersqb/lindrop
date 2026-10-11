"""Regenerate the full-size portable icon using only Python's standard library.

Run: python scripts/Generate-Icon.py. Shape coordinates use a 256-unit canvas.
PNG images use four-sample antialiasing; the ICO embeds common Windows sizes.
No runtime dependency, font, online artwork or image-generation service needed.
"""

import math
import struct
import zlib
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "lan_drop" / "assets"
ARROWS = [
    [(45, 80), (158, 80), (158, 57), (212, 99), (158, 141), (158, 118), (45, 118)],
    [(211, 176), (98, 176), (98, 199), (44, 157), (98, 115), (98, 138), (211, 138)],
]


def polygon(x, y, points):
    inside = False
    previous = points[-1]
    for current in points:
        a, b = previous, current
        if (a[1] > y) != (b[1] > y) and x < (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]:
            inside = not inside
        previous = current
    return inside


def color(x, y):
    # A 6-unit outer margin fills ~95% of the icon's canvas.
    dx, dy = max(50 - x, 0, x - 206), max(50 - y, 0, y - 206)
    if math.hypot(dx, dy) > 44:
        return (0, 0, 0, 0)
    if any(polygon(x, y, points) for points in ARROWS):
        return (255, 255, 255, 255)
    return (38, 104, 78, 255)


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png(size):
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            samples = [
                color((x + u) * 256 / size, (y + v) * 256 / size)
                for u, v in [(0.25, 0.25), (0.75, 0.25), (0.25, 0.75), (0.75, 0.75)]
            ]
            alpha = sum(c[3] for c in samples)
            rows.extend(
                [round(sum(c[i] * c[3] for c in samples) / alpha) if alpha else 0 for i in range(3)]
            )
            rows.append(round(alpha / 4))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows, 9))
        + chunk(b"IEND", b"")
    )


def main():
    ASSETS.mkdir(exist_ok=True)
    sizes = [16, 20, 24, 32, 48, 64, 128, 256]
    images = [png(size) for size in sizes]
    for size, image in zip(sizes, images, strict=True):
        (ASSETS / f"icon-{size}.png").write_bytes(image)
    offset = 6 + len(images) * 16
    directory = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    for size, image in zip(sizes, images, strict=True):
        directory.extend(
            struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(image), offset)
        )
        offset += len(image)
    (ASSETS / "lindrop.ico").write_bytes(bytes(directory) + b"".join(images))


if __name__ == "__main__":
    main()
