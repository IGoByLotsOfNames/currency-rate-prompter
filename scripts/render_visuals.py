"""Render explanatory assets from the committed fixture/evidence. Pillow is dev-only."""

import json
from html import escape
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs/assets"
ASSETS.mkdir(parents=True, exist_ok=True)
NAVY, TEAL, INK, MUTED, PALE = "#112e43", "#00838a", "#17374c", "#526b7c", "#f1f6f9"


def font(size, bold=False):
    candidates = [
        Path("C:/Windows/Fonts") / ("segoeuib.ttf" if bold else "segoeui.ttf"),
        Path("/usr/share/fonts/truetype/dejavu")
        / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default(size=size)


class Canvas:
    def __init__(self, width, height, background=PALE):
        self.image = Image.new("RGB", (width, height), background)
        self.draw = ImageDraw.Draw(self.image)
        self.svg = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}"><rect width="100%" height="100%" fill="{background}"/>'
        ]

    def rect(self, box, fill, radius=16, outline=None):
        self.draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=2)
        x1, y1, x2, y2 = box
        self.svg.append(
            f'<rect x="{x1}" y="{y1}" width="{x2 - x1}" height="{y2 - y1}" rx="{radius}" fill="{fill}" stroke="{outline or fill}"/>'
        )

    def text(self, position, value, size=26, fill=INK, bold=False):
        x, y = position
        self.draw.text(position, value, font=font(size, bold), fill=fill)
        self.svg.append(
            f'<text x="{x}" y="{y + size}" font-family="Segoe UI,Arial,sans-serif" font-size="{size}" font-weight="{700 if bold else 400}" fill="{fill}">{escape(value)}</text>'
        )

    def line(self, points, fill=TEAL, width=3):
        self.draw.line(points, fill=fill, width=width)
        self.svg.append(
            f'<polyline points="{" ".join(f"{x},{y}" for x, y in points)}" fill="none" stroke="{fill}" stroke-width="{width}"/>'
        )

    def dot(self, x, y, radius=4, fill=TEAL):
        self.draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill)
        self.svg.append(f'<circle cx="{x}" cy="{y}" r="{radius}" fill="{fill}"/>')

    def arrow(self, start, end):
        self.line([start, end], TEAL, 4)
        x, y = end
        self.line([(x - 10, y - 8), end, (x - 10, y + 8)], TEAL, 4)

    def save(self, name):
        self.image.save(ASSETS / f"{name}.png", optimize=True)
        (ASSETS / f"{name}.svg").write_text("".join(self.svg) + "</svg>", encoding="utf-8")


data = json.loads((ROOT / "src/currency_prompter/data/demo-quotes.json").read_text())
result = json.loads((ROOT / "docs/evidence/replay-results.json").read_text())
c = Canvas(1280, 910)
c.rect((0, 0, 1280, 180), NAVY, 0)
c.text((52, 28), "CURRENCY RATE PROMPTER", 24, "#74d2d0", True)
c.text((52, 75), "A rate history you can replay.", 48, "white", True)
for left, value, title in [
    (52, result["first_replay"]["inserted"], "observations stored"),
    (450, result["first_replay"]["queued"], "alerts recorded locally"),
    (848, result["second_replay"]["queued"], "new alerts on replay"),
]:
    c.rect((left, 214, left + 380, 341), "white")
    c.text((left + 25, 224), str(value), 48, TEAL, True)
    c.text((left + 25, 288), title, 23, MUTED)
c.text((52, 375), "SGD / THB", 28, INK, True)
c.text((260, 380), "Synthetic fixture · 6-hour observations", 24, MUTED)
low, high = 24.90, 25.10


def y(value):
    return 720 - (value - low) / (high - low) * 250


for value in (24.90, 24.95, 25, 25.05, 25.10):
    c.line([(115, y(value)), (1200, y(value))], "#d7e2ea", 2)
    c.text((32, y(value) - 17), f"{value:.2f}", 22, MUTED)
points = [(115 + i / (len(data) - 1) * 1085, y(float(row["rate"]))) for i, row in enumerate(data)]
c.line(points, TEAL, 4)
for x, yy in points:
    c.dot(x, yy, 4)
c.text((112, 742), "1 Sep 2026", 22, MUTED)
c.text((1043, 742), "12 Sep 2026", 22, MUTED)
c.text((52, 805), "Same input, same decisions. No duplicate notifications.", 27, INK, True)
c.text((52, 850), "Synthetic demonstration only · no market forecast · no messages sent", 22, MUTED)
c.save("overview")

c = Canvas(1440, 780)
c.text((54, 34), "From observation to a durable decision", 44, INK, True)
c.text((54, 99), "The transaction is small. The recovery story is explicit.", 27, MUTED)
boxes = [
    ((54, 190, 343, 376), "1  INPUT", ["Offline quote replay", "or daily reference API"]),
    ((400, 190, 689, 376), "2  VALIDATE", ["Decimal + UTC fields", "Pair, source, freshness"]),
    ((746, 190, 1035, 376), "3  TRANSACT", ["Quote + rule decision", "State + alert outbox"]),
    ((1092, 190, 1381, 376), "4  DISPATCH", ["Stable event ID", "Local journal + ack"]),
]
for box, title, lines in boxes:
    c.rect(box, "white")
    c.text((box[0] + 24, box[1] + 22), title, 28, TEAL, True)
    for i, line in enumerate(lines):
        c.text((box[0] + 24, box[1] + 81 + i * 39), line, 24, INK)
for left in (343, 689, 1035):
    c.arrow((left + 7, 280), (left + 48, 280))
c.rect((54, 434, 934, 704), NAVY)
c.text((82, 458), "SQLite stores the explanation", 30, "white", True)
c.text(
    (82, 516), "96 rule decisions = 68 condition misses + 13 cooldowns + 15 alerts", 24, "#cbe2ec"
)
c.text((82, 568), "An interrupted transaction commits all four records or none.", 25, "#cbe2ec")
c.text(
    (82, 619),
    "A delivery retry reuses its event ID instead of adding a new receipt.",
    24,
    "#cbe2ec",
)
c.rect((966, 434, 1381, 704), "white")
c.text((993, 460), "INSPECT", 28, TEAL, True)
c.text((993, 518), "Static HTML / SVG report", 25, INK)
c.text((993, 566), "Read-only localhost API", 25, INK)
c.text((993, 614), "History and alert status", 25, INK)
c.text(
    (54, 735),
    "Architecture of the maintained implementation · scenario counts come from docs/evidence/replay-results.json",
    21,
    MUTED,
)
c.save("architecture")
print("Rendered overview and architecture as PNG + SVG.")
