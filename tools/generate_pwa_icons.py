"""Generate the original WhatsBot-Lite PWA mark (development only; needs Pillow)."""
from pathlib import Path
from PIL import Image, ImageDraw

out = Path(__file__).resolve().parents[1] / "web/static/icons"
out.mkdir(parents=True, exist_ok=True)
# Opaque full-bleed square; all important content stays in the maskable safe zone.
image = Image.new("RGB", (1024, 1024), "#008069")
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((270, 300, 754, 660), radius=100, fill="white")
draw.polygon([(320, 625), (320, 730), (450, 625)], fill="white")
for x in (385, 512, 639):
    draw.ellipse((x - 34, 450, x + 34, 518), fill="#008069")
for filename, size in [("icon-192.png",192), ("icon-512.png",512),
                       ("maskable-512.png",512), ("apple-touch-icon.png",180)]:
    image.resize((size,size), Image.Resampling.LANCZOS).save(out / filename)
