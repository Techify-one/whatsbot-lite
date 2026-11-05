"""Generate the WhatsBot PWA icon set (development only; needs Pillow).

Usage:
    python tools/generate_pwa_icons.py            # Lite (default)
    python tools/generate_pwa_icons.py --variant pro

Each variant has its own colour and corner badge so the two apps are easy to tell
apart on a phone home screen. Outputs go to web/static/icons/.
"""
import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

VARIANTS = {
    # background, bubble dots/badge text colour, badge label
    "lite": {"bg": "#008069", "ink": "#008069", "badge": "LITE", "badge_bg": "#ffffff"},
    "pro": {"bg": "#0b3c5d", "ink": "#0b3c5d", "badge": "PRO", "badge_bg": "#f5b700"},
}
CANVAS = 1024


def _font(size: int) -> ImageFont.FreeTypeFont:
    for name in ("DejaVuSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def draw_icon(variant: dict, scale: float) -> Image.Image:
    """Opaque full-bleed square. `scale` shrinks the artwork toward the centre:
    1.0 for regular icons, ~0.78 for maskable so it survives any mask shape."""
    image = Image.new("RGB", (CANVAS, CANVAS), variant["bg"])
    layer = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    # Chat bubble, nudged up to leave room for the badge.
    draw.rounded_rectangle((270, 230, 754, 590), radius=100, fill="white")
    draw.polygon([(320, 555), (320, 660), (450, 555)], fill="white")
    for x in (385, 512, 639):
        draw.ellipse((x - 34, 380, x + 34, 448), fill=variant["ink"])
    # Badge pill.
    pill = (332, 705, 692, 825)
    draw.rounded_rectangle(pill, radius=60, fill=variant["badge_bg"])
    font = _font(84)
    box = draw.textbbox((0, 0), variant["badge"], font=font)
    tx = (pill[0] + pill[2]) / 2 - (box[0] + box[2]) / 2
    ty = (pill[1] + pill[3]) / 2 - (box[1] + box[3]) / 2
    draw.text((tx, ty), variant["badge"], font=font, fill=variant["ink"])
    if scale != 1.0:
        size = round(CANVAS * scale)
        layer = layer.resize((size, size), Image.Resampling.LANCZOS)
        framed = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
        offset = (CANVAS - size) // 2
        framed.paste(layer, (offset, offset))
        layer = framed
    image.paste(layer, (0, 0), layer)
    return image


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", choices=sorted(VARIANTS), default="lite")
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "web/static/icons")
    args = parser.parse_args()
    variant = VARIANTS[args.variant]
    args.out.mkdir(parents=True, exist_ok=True)
    regular = draw_icon(variant, 1.0)
    maskable = draw_icon(variant, 0.78)
    for filename, source, size in [
        ("icon-192.png", regular, 192),
        ("icon-512.png", regular, 512),
        ("maskable-512.png", maskable, 512),
        ("apple-touch-icon.png", regular, 180),
    ]:
        source.resize((size, size), Image.Resampling.LANCZOS).save(args.out / filename)
    print(f"{args.variant}: icons written to {args.out}")


if __name__ == "__main__":
    main()
