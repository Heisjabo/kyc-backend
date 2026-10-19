"""What a file's own metadata says about how it was made and whether it was changed.

This is evidence, not proof. A forger who knows to strip metadata or who exports from
a plain tool leaves nothing here, so finding nothing proves nothing. Finding something
is worth a person's attention: a bank statement does not normally carry the name of a
photo or PDF editor, and a statement edited after it was issued normally has a second
save in its file.
"""

from __future__ import annotations

import io
import re
from datetime import datetime, timedelta

from PIL import Image

# Names of tools people use to alter documents, as they appear in a file's Software, Creator
# or Producer fields. Matched as whole lowercase fragments.
EDITORS = (
    "photoshop", "gimp", "canva", "illustrator", "inkscape", "pixlr", "affinity", "paint.net",
    "sejda", "pdfescape", "ilovepdf", "smallpdf", "pdf-xchange editor", "foxit phantompdf",
    "acrobat pro", "pdfelement", "lightroom", "snapseed", "picsart", "fotor",
)
# Taken to mean "edited after it was made" when the gap is longer than this.
MODIFIED_AFTER = timedelta(hours=1)

_PDF_DATE = re.compile(r"D:(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?")


def _editor(*fields: str | None) -> str | None:
    text = " ".join(f for f in fields if f).lower()
    return next((name for name in EDITORS if name in text), None)


def _pdf_date(value: str | None) -> datetime | None:
    match = _PDF_DATE.match(value or "")
    if not match:
        return None
    parts = [int(g) if g else 0 for g in match.groups()]
    try:
        return datetime(parts[0], parts[1] or 1, parts[2] or 1, parts[3], parts[4], parts[5])
    except ValueError:
        return None


def pdf_signals(data: bytes) -> dict:
    """Signals from a PDF's information dictionary and its structure."""
    found: dict = {}
    try:
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(data)
        try:
            meta = document.get_metadata_dict()
        finally:
            document.close()
    except Exception:
        return found

    creator, producer = meta.get("Creator"), meta.get("Producer")
    editor = _editor(creator, producer, meta.get("Author"))
    if editor:
        found["EDITING_SOFTWARE_DETECTED"] = editor
    created, modified = _pdf_date(meta.get("CreationDate")), _pdf_date(meta.get("ModDate"))
    if created and modified and modified - created > MODIFIED_AFTER:
        found["MODIFIED_AFTER_CREATION"] = f"{created:%Y-%m-%d %H:%M} then {modified:%Y-%m-%d %H:%M}"
    # Each save that only appends leaves another end-of-file marker behind it.
    if data.count(b"%%EOF") > 1:
        found["SAVED_MORE_THAN_ONCE"] = f"{data.count(b'%%EOF')} saves"
    return found


def image_signals(data: bytes) -> dict:
    """Signals from a photograph's EXIF block."""
    found: dict = {}
    try:
        with Image.open(io.BytesIO(data)) as image:
            exif = image.getexif()
    except Exception:
        return found
    editor = _editor(exif.get(0x0131), exif.get(0x013B))   # Software, Artist
    if editor:
        found["EDITING_SOFTWARE_DETECTED"] = editor
    original, changed = exif.get(0x9003), exif.get(0x0132)   # DateTimeOriginal, DateTime
    if original and changed and original != changed:
        try:
            a = datetime.strptime(str(original), "%Y:%m:%d %H:%M:%S")
            b = datetime.strptime(str(changed), "%Y:%m:%d %H:%M:%S")
            if b - a > MODIFIED_AFTER:
                found["MODIFIED_AFTER_CREATION"] = f"{a:%Y-%m-%d %H:%M} then {b:%Y-%m-%d %H:%M}"
        except ValueError:
            pass
    return found


# What a generator writes into the file to say it made it. IPTC's DigitalSourceType values, which
# C2PA content credentials and XMP both carry: an image drawn by a model, or a real one with
# model-drawn parts added.
AI_SOURCE_TYPES = (
    b"compositeWithTrainedAlgorithmicMedia", b"trainedAlgorithmicMedia", b"compositeSynthetic",
)
# Tool names that appear in a credentials block, only used to say which tool signed it.
GENERATORS = (
    b"Google", b"OpenAI", b"DALL-E", b"ChatGPT", b"Adobe Firefly", b"Midjourney", b"Imagen",
    b"Gemini", b"Microsoft", b"Stability", b"Leonardo", b"Ideogram",
)


def content_credentials(data: bytes) -> dict:
    """Does the file carry C2PA content credentials, and do they say it was made by AI?

    Several generators (Google, OpenAI, Adobe, Microsoft) sign the images they make with
    credentials that say so. This looks for the credentials block and the standard words that
    declare a generated image. It does not verify the signature: a forger gains nothing by
    adding a claim that their own image is generated, and one who strips the credentials leaves
    nothing to find. So a hit is strong evidence and a miss proves nothing.
    """
    found: dict = {}
    if b"c2pa" in data and b"jumb" in data:
        found["CONTENT_CREDENTIALS_PRESENT"] = "C2PA manifest"
    declared = [t.decode() for t in AI_SOURCE_TYPES if t in data]
    if declared:
        tools = [g.decode() for g in GENERATORS if g in data] if "CONTENT_CREDENTIALS_PRESENT" in found else []
        found["AI_GENERATED_DECLARED"] = declared[0] + (f" ({', '.join(tools)})" if tools else "")
    return found


def signals(data: bytes) -> dict:
    history = pdf_signals(data) if data[:5] == b"%PDF-" or b"%PDF-" in data[:1024] else image_signals(data)
    return {**history, **content_credentials(data)}
