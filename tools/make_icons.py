#!/usr/bin/env python3
"""Draw the Zero-Drive icon (dark tile, orange USB stick) at any size."""
import sys
from PIL import Image, ImageDraw


def icon(n, o=(255, 168, 0, 255)):
    s = 4 * n                                   # draw big, downsample = smooth
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=s // 5, fill=(24, 27, 35, 255))
    u = s / 100
    d.rectangle([38 * u, 14 * u, 62 * u, 32 * u], outline=o, width=int(4 * u))   # plug
    d.rectangle([44 * u, 20 * u, 48 * u, 25 * u], fill=o)
    d.rectangle([52 * u, 20 * u, 56 * u, 25 * u], fill=o)
    d.rounded_rectangle([30 * u, 32 * u, 70 * u, 86 * u], radius=int(8 * u), fill=o)  # body
    d.ellipse([44 * u, 52 * u, 56 * u, 64 * u], fill=(24, 27, 35, 255))           # LED
    return im.resize((n, n), Image.LANCZOS)


if __name__ == "__main__":
    col = (255, 168, 0, 255)
    icon(100, col).save(sys.argv[1] if len(sys.argv) > 1 else "packaging/icon.png")
    icon(256, col).save(sys.argv[2] if len(sys.argv) > 2 else "packaging/store-icon.png")
