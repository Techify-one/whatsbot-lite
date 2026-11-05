"""Generate the PWA / favicon icon set from the supplied artwork (development only; needs Pillow).

Usage:
    python tools/generate_pwa_icons.py [--src tools/icon-src/icon.png] [--out web/static/icons]

The source is the finished app icon (rounded green square with the "W" bubble and the
LITE/PRO badge, transparent corners). This script does NOT redraw anything: it only
cleans the faint halo pixels, crops to the artwork, squares it and resizes.

Outputs (web/static/icons/):
    icon-192.png, icon-512.png   "any" icons, artwork edge to edge, transparent corners
    maskable-512.png             Android adaptive icon: opaque gradient background sampled from
                                 the artwork + artwork at MASKABLE_SCALE so the badge stays
                                 inside the 80% safe circle
    apple-touch-icon.png (180)   iOS has no alpha (transparent becomes black): opaque background
    favicon-32.png, favicon-48.png, favicon.ico   browser-tab icons (transparent corners)
"""
import argparse
from pathlib import Path

from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parents[1]
ALPHA_FLOOR = 16          # alpha below this is rendering halo, not artwork
MASKABLE_SCALE = 0.70     # badge corner sits ~0.55 from centre; 0.55*0.70 < 0.40 safe radius
SUPERSAMPLE = 1024


def load_artwork(src: Path) -> Image.Image:
    """Return the artwork cropped to its opaque bounding box and padded to a square."""
    image = Image.open(src).convert("RGBA")
    alpha = image.getchannel("A").point(lambda v: 0 if v < ALPHA_FLOOR else v)
    image.putalpha(alpha)
    box = alpha.point(lambda v: 255 if v else 0).getbbox()
    if not box:
        raise SystemExit(f"{src}: image is fully transparent")
    image = image.crop(box)
    side = max(image.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(image, ((side - image.width) // 2, (side - image.height) // 2), image)
    return square.resize((SUPERSAMPLE, SUPERSAMPLE), Image.Resampling.LANCZOS)


def _sample(art: Image.Image, fx: float, fy: float) -> tuple[int, int, int]:
    """Average colour of a small opaque patch of the artwork around (fx, fy)."""
    size = art.width
    cx, cy, r = int(fx * size), int(fy * size), size // 60
    patch = art.crop((cx - r, cy - r, cx + r, cy + r)).convert("RGB").resize((1, 1), Image.Resampling.BOX)
    return patch.getpixel((0, 0))


def gradient_background(art: Image.Image, size: int) -> Image.Image:
    """Opaque diagonal gradient from the artwork's own top-left to bottom-right greens."""
    start, end = _sample(art, 0.14, 0.14), _sample(art, 0.86, 0.86)
    vertical = Image.linear_gradient("L").resize((size, size), Image.Resampling.BICUBIC)
    horizontal = vertical.transpose(Image.Transpose.ROTATE_90)  # black left -> white right
    ramp = ImageChops.add(horizontal, vertical, scale=2)  # 0 at top-left -> 255 at bottom-right
    return Image.composite(Image.new("RGB", (size, size), end), Image.new("RGB", (size, size), start), ramp)


def resized(art: Image.Image, size: int) -> Image.Image:
    return art.resize((size, size), Image.Resampling.LANCZOS)


def opaque(art: Image.Image, size: int, scale: float) -> Image.Image:
    base = gradient_background(art, size)
    inner = round(size * scale)
    layer = resized(art, inner)
    offset = (size - inner) // 2
    base.paste(layer, (offset, offset), layer)
    return base


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", type=Path, default=ROOT / "tools/icon-src/icon.png")
    parser.add_argument("--out", type=Path, default=ROOT / "web/static/icons")
    args = parser.parse_args()
    art = load_artwork(args.src)
    args.out.mkdir(parents=True, exist_ok=True)
    resized(art, 192).save(args.out / "icon-192.png", optimize=True)
    resized(art, 512).save(args.out / "icon-512.png", optimize=True)
    opaque(art, 512, MASKABLE_SCALE).save(args.out / "maskable-512.png", optimize=True)
    opaque(art, 180, 1.0).save(args.out / "apple-touch-icon.png", optimize=True)
    resized(art, 32).save(args.out / "favicon-32.png", optimize=True)
    resized(art, 48).save(args.out / "favicon-48.png", optimize=True)
    resized(art, 256).save(args.out / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    print(f"icons written to {args.out}")


if __name__ == "__main__":
    main()
