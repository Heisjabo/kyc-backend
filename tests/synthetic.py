"""Synthetic document images for tests. Clean renders, so they test the logic, not real-world OCR."""

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Courier New Bold.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
]

BACKGROUND = (228, 224, 214)


def mono_font(size: int) -> ImageFont.FreeTypeFont | None:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return None


def render_document(mrz_lines: list[str], header: str = "PASSPORT", width: int = 1400, height: int = 980) -> Image.Image:
    font = mono_font(34)
    image = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(image)

    draw.text((60, 50), header, fill=(40, 40, 90), font=mono_font(48))
    draw.rectangle((60, 150, 380, 560), outline=(90, 90, 90), width=3, fill=(200, 196, 188))
    for row, label in enumerate(["Surname", "Given names", "Nationality", "Date of birth", "Date of expiry"]):
        draw.text((440, 170 + row * 75), label, fill=(70, 70, 70), font=mono_font(28))

    line_height = 58
    top = height - 60 - line_height * len(mrz_lines)
    for index, line in enumerate(mrz_lines):
        draw.text((60, top + index * line_height), line, fill=(15, 15, 15), font=font)
    return image


def to_bytes(image: Image.Image, fmt: str = "PNG") -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


def blurred(image: Image.Image) -> Image.Image:
    return image.filter(ImageFilter.GaussianBlur(radius=8))


_SANS_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
_SANS_BOLD_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def _font(candidates: list[str], size: int) -> ImageFont.FreeTypeFont | None:
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return None


def sans_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | None:
    return _font(_SANS_BOLD_CANDIDATES if bold else _SANS_CANDIDATES, size)


def render_card(title: str, subtitle: str, rows: list[tuple[str, str]], width: int = 1280, height: int = 810) -> Image.Image:
    """A card-style document with printed label/value rows and a photo box, no MRZ."""
    image = Image.new("RGB", (width, height), (214, 228, 236))
    draw = ImageDraw.Draw(image)
    draw.text((330, 40), title, fill=(20, 40, 90), font=sans_font(44, bold=True))
    draw.text((330, 100), subtitle, fill=(20, 40, 90), font=sans_font(28))
    draw.rectangle((60, 200, 340, 560), fill=(160, 150, 140))
    for index, (label, value) in enumerate(rows):
        top = 180 + index * 100
        draw.text((400, top), label, fill=(60, 60, 60), font=sans_font(24))
        draw.text((400, top + 32), value, fill=(10, 10, 10), font=sans_font(34, bold=True))
    return image


def render_qr_like(size: int = 1000) -> Image.Image:
    """Black and white squares: an image with no text, like a QR code."""
    import random

    rng = random.Random(7)
    image = Image.new("RGB", (size, size), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    cell = size // 25
    for row in range(25):
        for col in range(25):
            if rng.random() < 0.5:
                draw.rectangle((col * cell, row * cell, (col + 1) * cell, (row + 1) * cell), fill=(0, 0, 0))
    return image


def render_text_page(lines: list[str], width: int = 1240, height: int = 1000) -> Image.Image:
    image = Image.new("RGB", (width, height), (245, 245, 240))
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        draw.text((60, 60 + index * 60), line, fill=(20, 20, 20), font=sans_font(32))
    return image
