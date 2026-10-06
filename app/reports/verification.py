"""The verification report: one PDF that shows how a verification went.

Laid out the way Shufti lays out theirs, because that is the report the people
reading ours already know: the outcome and the portrait first, then who and when,
then one block per service with its checks, what the client sent and what the
documents gave up, then the settings the verification was opened with, the
callbacks we sent, the two faces side by side and the device it was done on.

It is built for any status, not only a pass. A step nobody reached reads as not
performed, a failed one names its reasons, and every attempt is listed, so the
same report serves as evidence for a decline as for an acceptance.

Built from rows only. Nothing here writes: asking for the report of a session that
has run out shows it as expired without moving it there, the way the console
reads it.
"""

import io
import re
from pathlib import Path
from dataclasses import dataclass, field
from datetime import UTC, datetime
from xml.sax.saxutils import escape

import pycountry
from PIL import Image as PILImage
from PIL import ImageOps
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.platypus import (
    Flowable,
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    CallbackDelivery,
    DeviceSession,
    DocumentScan,
    FaceCheck,
    FileKind,
    Step,
    StepStatus,
    Verification,
    VerificationFile,
    VerificationStatus,
    VerificationStep,
)
from app.schemas.address import ProofType
from app.schemas.document import DocumentType
from app.services import verifications as service
from app.storage import evidence

# The product the report is issued by.
PRODUCT = "Pangolin KYC"

# Lufga, the typeface tuesday-backend sets its statements and contract notes in.
# The TTFs live beside this module, so the report looks the same on any machine.
_FONTS = Path(__file__).parent / "fonts"
for _name, _file in (("Lufga", "Lufga-Regular.ttf"), ("Lufga-Medium", "Lufga-Medium.ttf"),
                     ("Lufga-Bold", "Lufga-Bold.ttf")):
    if _name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(_name, str(_FONTS / _file)))
pdfmetrics.registerFontFamily("Lufga", normal="Lufga", bold="Lufga-Bold",
                              italic="Lufga", boldItalic="Lufga-Bold")
FONT, FONT_MEDIUM, FONT_BOLD = "Lufga", "Lufga-Medium", "Lufga-Bold"

# Colours

# Tuesday's brand-blue, as cdashboard's tailwind config defines it.
BRAND_BLUE = colors.HexColor("#1456F5")
INK = colors.HexColor("#1F2937")
MUTED = colors.HexColor("#6B7280")
BORDER = colors.HexColor("#E3E8EF")
TINT = colors.HexColor("#F5F8FC")
GREEN = colors.HexColor("#2E7D5B")
RED = colors.HexColor("#C0392B")
AMBER = colors.HexColor("#D98E04")
# The header bar: a dark navy like the one on Shufti's proof, with the wordmark's
# "KYC" in a lighter blue that still reads against it.
HEADER = colors.HexColor("#0B1A33")
HEADER_BLUE = colors.HexColor("#4C82FF")

PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN = 34
CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN

# The headline for each status: the sentence, the badge and its colour.
_OUTCOME = {
    VerificationStatus.passed: ("Verification has been successfully completed and accepted", "Accepted", GREEN),
    VerificationStatus.failed: ("Verification has been completed and declined", "Declined", RED),
    VerificationStatus.needs_review: ("Verification has been completed and is waiting for manual review", "Review", AMBER),
    VerificationStatus.in_progress: ("Verification has been started and is not finished yet", "In Progress", AMBER),
    VerificationStatus.pending: ("Verification has been created and has not been started", "Pending", AMBER),
    VerificationStatus.expired: ("Verification link expired before the verification was completed", "Expired", RED),
}

_SERVICE_NAMES = {
    "face": "Facial Biometrics",
    "document": "Document Verification",
    "address": "Address Verification",
    "due_diligence": "Due Diligence Forms",
    "aml": "AML Screening",
    "consent": "Consent",
}

# Which of a step's own checks stand behind each line of the report. A line made
# of several checks reads as the worst of them.
_DOCUMENT_LINES = [
    ("Document Originality", ["document_detection"]),
    ("Visual Integrity", ["quality_front", "quality_back", "ai_generated"]),
    ("Document Expiration", ["validity"]),
    ("Document Type Consistency", ["document_type"]),
    ("Document Issuing Country", ["issuing_country"]),
    ("Selfie and Face on Document Match", None),  # from the face step
    ("Name Match on Document", ["name_match"]),
    ("DOB on Document Match", ["date_of_birth_match"]),
    ("Document Sides Consistency", ["back_side"]),
]

_ADDRESS_LINES = [
    ("Address Document Authenticity", ["address_document_authenticity"]),
    ("Document Expiration", ["address_document_expiration"]),
    ("Match Address Proofs With Document Proofs", ["match_address_proofs_with_document_proofs"]),
    ("Address on Document Match", ["address_on_document_match"]),
    ("Address Document Proof", ["address_document_proof"]),
]

# Marks: what a check came to.
PASS, FAIL, REVIEW, SKIPPED, NOT_PERFORMED = "pass", "fail", "review", "skipped", None

_IMAGE_MAX_SIDE = 900


# What the report is built from


@dataclass
class ReportData:
    verification: Verification
    status: VerificationStatus
    steps: dict[VerificationStep, Step]
    attempts: list[Step]
    scan: DocumentScan | None
    face: FaceCheck | None
    device: DeviceSession | None
    callbacks: list[CallbackDelivery]
    images: dict[FileKind, bytes] = field(default_factory=dict)
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def gather(db: Session, verification: Verification) -> ReportData:
    """Everything the report shows, read and nothing written."""
    attempts = list(
        db.scalars(
            select(Step).where(Step.verification_id == verification.id).order_by(Step.step, Step.attempt)
        ).all()
    )
    latest: dict[VerificationStep, Step] = {}
    for step in attempts:
        if step.step not in latest or step.attempt > latest[step.step].attempt:
            latest[step.step] = step

    status = verification.status
    if status in (VerificationStatus.pending, VerificationStatus.in_progress) and service.is_expired(verification):
        status = VerificationStatus.expired

    def newest(model):
        return db.scalar(
            select(model).where(model.verification_id == verification.id).order_by(model.id.desc()).limit(1)
        )

    callbacks = list(
        db.scalars(
            select(CallbackDelivery)
            .where(CallbackDelivery.verification_id == verification.id)
            .order_by(CallbackDelivery.sent_at)
        ).all()
    )

    data = ReportData(
        verification=verification,
        status=status,
        steps=latest,
        attempts=attempts,
        scan=newest(DocumentScan),
        face=newest(FaceCheck),
        device=newest(DeviceSession),
        callbacks=callbacks,
    )

    # The newest file of each kind. The address proof can come in two parts; the
    # first part is the one shown.
    files = db.scalars(
        select(VerificationFile)
        .where(VerificationFile.verification_id == verification.id)
        .order_by(VerificationFile.id)
    ).all()
    chosen: dict[FileKind, VerificationFile] = {}
    for file in files:
        if file.kind == FileKind.address_proof and FileKind.address_proof in chosen:
            if chosen[FileKind.address_proof].step_id == file.step_id:
                continue
        chosen[file.kind] = file
    for kind in (FileKind.document_front, FileKind.document_back, FileKind.document_portrait,
                 FileKind.portrait, FileKind.address_proof):
        if kind in chosen:
            image = _image_bytes(chosen[kind])
            if image:
                data.images[kind] = image
    return data


def _image_bytes(file: VerificationFile) -> bytes | None:
    """The file as a JPEG small enough to embed, or None when it is gone or unreadable.

    A PDF proof of address is shown by its first page.
    """
    raw = evidence.read(file.storage_key)
    if not raw:
        return None
    try:
        if file.content_type == "application/pdf" or raw[:5] == b"%PDF-":
            import pypdfium2 as pdfium

            document = pdfium.PdfDocument(raw)
            try:
                picture = document[0].render(scale=1.5).to_pil()
            finally:
                document.close()
        else:
            picture = ImageOps.exif_transpose(PILImage.open(io.BytesIO(raw)))
        picture = picture.convert("RGB")
        picture.thumbnail((_IMAGE_MAX_SIDE, _IMAGE_MAX_SIDE))
        out = io.BytesIO()
        picture.save(out, format="JPEG", quality=82)
        return out.getvalue()
    except Exception:
        return None


# Reading the rows


def _check_status(step: Step | None, names: list[str]) -> str | None:
    """The worst of the named checks on this step, or None when none of them ran."""
    if step is None:
        return NOT_PERFORMED
    found = [c.get("status") for c in (step.result or {}).get("checks", []) if c.get("name") in names]
    if not found:
        return NOT_PERFORMED
    for outcome in (FAIL, REVIEW, PASS):
        if outcome in found:
            return outcome
    return SKIPPED


def _check(step: Step | None, name: str) -> dict:
    for check in (step.result or {}).get("checks", []) if step else []:
        if check.get("name") == name:
            return check
    return {}


def _bool_mark(value: bool | None) -> str | None:
    if value is None:
        return NOT_PERFORMED
    return PASS if value else FAIL


def _text(value) -> str:
    if value is None or value == "" or value == []:
        return "N/A"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


# Words a code is made of that read as acronyms, not as words.
_ACRONYMS = {"ai": "AI", "aml": "AML", "dob": "DOB", "mrz": "MRZ", "id": "ID", "ip": "IP", "url": "URL"}


def _humanise(code: str | None) -> str:
    """IMAGE_BLURRY as "Image blurry", id_card as "ID card"."""
    if not code:
        return "N/A"
    words = code.replace("_", " ").strip().lower().split()
    words = [_ACRONYMS.get(word, word) for word in words]
    if words and words[0] not in _ACRONYMS.values():
        words[0] = words[0].capitalize()
    return " ".join(words)


def _ordinal(day: int) -> str:
    if 10 <= day % 100 <= 20:
        return f"{day}th"
    return f"{day}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th') }"


def _date_long(moment: datetime | None) -> str:
    if moment is None:
        return "N/A"
    moment = _utc(moment)
    return f"{moment:%B} {_ordinal(moment.day)}, {moment.year}"


def _time_long(moment: datetime | None) -> str:
    if moment is None:
        return "N/A"
    moment = _utc(moment)
    return f"{moment.strftime('%I:%M:%S %p').lstrip('0')} (UTC +00:00)"


def _stamp(moment: datetime | None) -> str:
    if moment is None:
        return "N/A"
    return _utc(moment).strftime("%Y-%m-%d %I:%M:%S %p").replace(" 0", " ", 1)


def _utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _language(code: str | None) -> str:
    if not code:
        return "N/A"
    found = pycountry.languages.get(alpha_2=code.lower()[:2])
    return found.name if found else code


def _country(value: str | None) -> str:
    if not value:
        return "N/A"
    found = None
    if len(value) == 2:
        found = pycountry.countries.get(alpha_2=value.upper())
    elif len(value) == 3:
        found = pycountry.countries.get(alpha_3=value.upper())
    return found.name if found else value


def _browser(user_agent: str | None) -> tuple[str, str]:
    """Browser and operating system, read off the user agent."""
    if not user_agent:
        return "N/A", "N/A"
    browser = "Unknown browser"
    for name, pattern in (("Edge", r"Edg/([\d.]+)"), ("Opera", r"OPR/([\d.]+)"),
                          ("Chrome", r"Chrome/([\d.]+)"), ("Firefox", r"Firefox/([\d.]+)"),
                          ("Safari", r"Version/([\d.]+).*Safari")):
        found = re.search(pattern, user_agent)
        if found:
            browser = f"{name} {found.group(1)}"
            break
    system = "Unknown OS"
    for pattern, name in ((r"Windows NT ([\d.]+)", "Windows NT {}"), (r"Android ([\d.]+)", "Android {}"),
                          (r"iPhone OS ([\d_]+)", "iOS {}"), (r"CPU OS ([\d_]+)", "iPadOS {}"),
                          (r"Mac OS X ([\d_.]+)", "macOS {}"), (r"Linux", "Linux")):
        found = re.search(pattern, user_agent)
        if found:
            system = name.format(*(g.replace("_", ".") for g in found.groups()))
            break
    return browser, system


def _first_device(data: ReportData) -> dict:
    """The device the verification started on: the first step that recorded one."""
    for step in sorted(data.attempts, key=lambda s: (s.started_at or s.created_at or data.generated_at)):
        if step.device:
            return step.device
    return {}


# Drawing


_BASE = ParagraphStyle("base", fontName=FONT, fontSize=8, leading=10.5, textColor=INK)
_SMALL = ParagraphStyle("small", parent=_BASE, fontSize=7, leading=9, textColor=MUTED)
_BOLD = ParagraphStyle("bold", parent=_BASE, fontName=FONT_BOLD)
_TITLE = ParagraphStyle("title", parent=_BASE, fontName=FONT_BOLD, fontSize=10.5, leading=13)
_SUB = ParagraphStyle("sub", parent=_BASE, fontName=FONT_BOLD, fontSize=9, leading=12)
_BADGE = ParagraphStyle("badge", parent=_BASE, fontName=FONT_BOLD, fontSize=9, textColor=colors.white,
                        alignment=TA_CENTER)
_CAPTION = ParagraphStyle("caption", parent=_BOLD, fontSize=8)
_PLACEHOLDER = ParagraphStyle("placeholder", parent=_SMALL, alignment=TA_CENTER)


def _p(text, style=_BASE) -> Paragraph:
    return Paragraph(escape(_text(text)), style)


def _rich(markup: str, style=_BASE) -> Paragraph:
    return Paragraph(markup, style)


class Mark(Flowable):
    """A small round mark: a tick, a cross, a warning or a dash."""

    SIZE = 9

    def __init__(self, outcome: str | None):
        super().__init__()
        self.outcome = outcome
        self.width = self.height = self.SIZE

    def draw(self):
        c = self.canv
        r = self.SIZE / 2
        # Green for a pass, red for a failure, yellow for everything still open:
        # in review, not performed yet, or not checked.
        colour = {PASS: GREEN, FAIL: RED}.get(self.outcome, AMBER)
        c.setFillColor(colour)
        c.circle(r, r, r, stroke=0, fill=1)
        c.setStrokeColor(colors.white)
        c.setLineWidth(1.2)
        c.setLineCap(1)
        if self.outcome == PASS:
            path = c.beginPath()
            path.moveTo(r * 0.5, r * 1.0)
            path.lineTo(r * 0.85, r * 0.62)
            path.lineTo(r * 1.5, r * 1.38)
            c.drawPath(path, stroke=1, fill=0)
        elif self.outcome == FAIL:
            c.line(r * 0.6, r * 0.6, r * 1.4, r * 1.4)
            c.line(r * 0.6, r * 1.4, r * 1.4, r * 0.6)
        elif self.outcome == REVIEW:
            c.line(r, r * 0.95, r, r * 1.5)
            c.circle(r, r * 0.55, 0.5, stroke=1, fill=0)
        else:
            c.line(r * 0.55, r, r * 1.45, r)


def _mark_label(outcome: str | None) -> str:
    return {PASS: "", FAIL: " (Failed)", REVIEW: " (Review)", SKIPPED: " (Not checked)"}.get(
        outcome, " (Not performed)"
    )


def _card(rows: list, title: str | None = None, title_note: str | None = None, tinted_title=True) -> Table:
    """A bordered, rounded box. One row per flowable, so a long card can break
    across pages between rows rather than refusing to fit."""
    cells = []
    if title:
        heading = f"<b>{escape(title)}</b>"
        if title_note:
            heading += f"&nbsp;&nbsp;<font color='#6B7280'>{escape(title_note)}</font>"
        cells.append([_rich(heading, _TITLE)])
    cells.extend([[row] for row in rows])
    table = Table(cells, colWidths=[CONTENT_WIDTH])
    style = [
        ("BOX", (0, 0), (-1, -1), 0.6, BORDER),
        ("ROUNDEDCORNERS", [6, 6, 6, 6]),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, 0), 9),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 10),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]
    if title and tinted_title:
        style += [("BACKGROUND", (0, 0), (-1, 0), TINT), ("BOTTOMPADDING", (0, 0), (-1, 0), 9)]
    table.setStyle(TableStyle(style))
    return table


def _grid(cells: list, columns: int, widths: list[float] | None = None, padding=3) -> Table:
    """Lay cells out left to right, `columns` to a row."""
    rows = [cells[i:i + columns] for i in range(0, len(cells), columns)] or [[""]]
    for row in rows:
        row.extend([""] * (columns - len(row)))
    inner = CONTENT_WIDTH - 20
    table = Table(rows, colWidths=widths or [inner / columns] * columns)
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), padding),
        ("BOTTOMPADDING", (0, 0), (-1, -1), padding),
    ]))
    return table


def _checks(lines: list[tuple[str, str | None]]) -> Table:
    """Checks two to a row, each a mark and its name, the way Shufti lists them."""
    inner = CONTENT_WIDTH - 20
    cells = []
    for label, outcome in lines:
        cells += [Mark(outcome), _p(label + _mark_label(outcome))]
    rows = [cells[i:i + 4] for i in range(0, len(cells), 4)]
    for row in rows:
        row.extend([""] * (4 - len(row)))
    table = Table(rows, colWidths=[14, inner / 2 - 14, 14, inner / 2 - 14])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def _pairs(pairs: list[tuple[str, object]], columns=2, label_width=None) -> Table:
    """Label and value pairs, `columns` pairs to a row."""
    inner = CONTENT_WIDTH - 20
    each = inner / columns
    label_width = label_width or min(105, each * 0.42)
    cells = []
    for label, value in pairs:
        cells += [_p(label, _SMALL), _p(value, _BOLD)]
    rows = [cells[i:i + columns * 2] for i in range(0, len(cells), columns * 2)] or [["", ""]]
    for row in rows:
        row.extend([""] * (columns * 2 - len(row)))
    table = Table(rows, colWidths=[label_width, each - label_width] * columns)
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return table


def _band(text: str) -> Table:
    """A tinted sub-heading inside a card, like Shufti's 'Data Submitted Via API'."""
    table = Table([[_rich(f"<b>{escape(text)}</b>", _SUB)]], colWidths=[CONTENT_WIDTH - 20])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), TINT),
        ("ROUNDEDCORNERS", [4, 4, 4, 4]),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table


def _table(header: list[str], rows: list[list], widths: list[float]) -> Table:
    """A plain data table with a tinted header."""
    body = [[_p(h, _BOLD) for h in header]] + [
        [cell if isinstance(cell, Flowable) else _p(cell) for cell in row] for row in rows
    ]
    table = Table(body, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), TINT),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def _picture(data: bytes | None, max_width: float, max_height: float) -> Flowable:
    """An image scaled into the box, or a grey placeholder saying it is not there."""
    if data:
        reader = ImageReader(io.BytesIO(data))
        w, h = reader.getSize()
        scale = min(max_width / w, max_height / h)
        image = Image(io.BytesIO(data), width=w * scale, height=h * scale)
        image.hAlign = "LEFT"
        return image
    box = Table([[_p("Image not available", _PLACEHOLDER)]], colWidths=[max_width], rowHeights=[max_height * 0.6])
    box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), TINT),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROUNDEDCORNERS", [4, 4, 4, 4]),
    ]))
    return box


def _gallery(items: list[tuple[str, bytes | None]], max_height=130) -> Table:
    inner = CONTENT_WIDTH - 20
    each = inner / max(len(items), 2)
    pictures = [_picture(data, each - 12, max_height) for _, data in items]
    captions = [_p(caption, _CAPTION) for caption, _ in items]
    table = Table([pictures, captions], colWidths=[each] * len(items))
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    table.hAlign = "LEFT"
    return table


# The sections, top to bottom


def _headline(data: ReportData) -> list:
    v = data.verification
    sentence, badge_text, colour = _OUTCOME[data.status]
    outcome = {VerificationStatus.passed: PASS, VerificationStatus.failed: FAIL,
               VerificationStatus.expired: FAIL, VerificationStatus.needs_review: REVIEW}.get(data.status)

    badge = Table([[_p(badge_text, _BADGE)]], colWidths=[72], rowHeights=[20])
    badge.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colour),
        ("ROUNDEDCORNERS", [10, 10, 10, 10]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    left_width = CONTENT_WIDTH - 140 - 10
    status_row = Table([[Mark(outcome), _p(sentence, _BOLD), badge]],
                       colWidths=[14, left_width - 20 - 14 - 80, 80])
    status_row.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))

    device = _first_device(data)
    browser, system = _browser(device.get("user_agent"))
    place = ", ".join(p for p in ((data.device.country if data.device else None),
                                  (data.device.city if data.device else None)) if p) or "N/A"
    applicant = v.applicant
    email = (v.claimed or {}).get("email") or (applicant.email if applicant else None)

    inner = left_width - 20
    who = _grid([
        _rich(f"<b>Reference:</b>&nbsp;&nbsp;{escape(v.reference)}", _BASE),
        _rich(f"<b>Customer ID:</b>&nbsp;&nbsp;{escape(_text(applicant.customer_id if applicant else None))}", _BASE),
        _rich(f"<font color='#6B7280'>Email:</font>&nbsp;&nbsp;{escape(_text(email))}", _BASE),
        "",
    ], 2, widths=[inner / 2, inner / 2])
    when = _grid([
        _p(_date_long(v.created_at)), _p(_time_long(v.created_at)), _p(_language(v.language)),
    ], 3, widths=[inner / 3] * 3)
    browser_info = _grid([
        _rich("<b>Browser Info:</b>", _BASE), _p(device.get("ip")), _p(browser), _p(system), _p(place),
    ], 5, widths=[inner * w for w in (0.18, 0.17, 0.25, 0.2, 0.2)])

    reasons = [_rich(f"<font color='#C0392B'><b>Reason:</b></font>&nbsp;{escape(_humanise(r))} "
                     f"<font color='#6B7280'>({escape(r)})</font>", _BASE) for r in (v.reasons or [])]

    left = [_mini_card([status_row] + reasons, left_width), Spacer(1, 8),
            _mini_card([who, _rule(inner), when, _rule(inner), browser_info], left_width)]

    liveness = data.face.liveness_passed if data.face else None
    live_text = {True: "Liveness Passed", False: "Liveness Failed", None: "Liveness Not Performed"}[liveness]
    right = _mini_card([
        Table([[Mark(_bool_mark(liveness)), _p(live_text, _BOLD)]], colWidths=[14, 100],
              style=[("LEFTPADDING", (0, 0), (-1, -1), 0), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]),
        Spacer(1, 4),
        _picture(data.images.get(FileKind.portrait), 120, 120),
    ], 140)

    layout = Table([[left, right]], colWidths=[left_width + 10, 140])
    layout.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return [layout]


def _mini_card(rows: list, width: float) -> Table:
    table = Table([[row] for row in rows], colWidths=[width])
    table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.6, BORDER),
        ("ROUNDEDCORNERS", [6, 6, 6, 6]),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, 0), 10),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 10),
    ]))
    return table


class _Rule(Flowable):
    def __init__(self, width):
        super().__init__()
        self.width, self.height = width, 6

    def draw(self):
        self.canv.setStrokeColor(BORDER)
        self.canv.setLineWidth(0.6)
        self.canv.line(0, 3, self.width, 3)


def _rule(width: float) -> Flowable:
    return _Rule(width)


def _services(data: ReportData) -> list:
    names = [_SERVICE_NAMES.get(name, _humanise(name)) for name in data.verification.services]
    cells = []
    for name, label in zip(data.verification.services, names):
        step = data.steps.get(VerificationStep(name)) if name in VerificationStep._value2member_map_ else None
        outcome = _step_outcome(step)
        cells.append(Table([[Mark(outcome), _p(label)]], colWidths=[14, 120],
                           style=[("LEFTPADDING", (0, 0), (-1, -1), 0), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    inner = CONTENT_WIDTH - 20
    body = Table([[_rich("<b>Used Verification Services :</b>", _SUB), _grid(cells, 3, [(inner - 140) / 3] * 3)]],
                 colWidths=[140, inner - 140])
    body.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("TOPPADDING", (0, 0), (0, 0), 4)]))
    return [_card([body])]


def _step_outcome(step: Step | None) -> str | None:
    if step is None:
        return NOT_PERFORMED
    return {StepStatus.passed: PASS, StepStatus.failed: FAIL, StepStatus.review: REVIEW,
            StepStatus.skipped: SKIPPED}.get(step.status, NOT_PERFORMED)


def _facial(data: ReportData) -> list:
    face = data.face
    deepfake = NOT_PERFORMED
    if face is not None:
        deepfake = _bool_mark(face.deepfake_passed) if face.deepfake_checked else SKIPPED
    lines = [("Face Liveness", _bool_mark(face.liveness_passed) if face else NOT_PERFORMED),
             ("Deepfake detection", deepfake)]
    return [_card([_checks(lines)], "Facial Biometrics Verification")]


def _document(data: ReportData) -> list:
    step = data.steps.get(VerificationStep.document)
    claimed = data.verification.claimed or {}
    extracted = (step.extracted if step else None) or {}
    face_match = _bool_mark(data.face.matched) if data.face and data.face.matched is not None else NOT_PERFORMED

    lines = []
    for label, names in _DOCUMENT_LINES:
        lines.append((label, face_match if names is None else _check_status(step, names)))

    dob_note = {PASS: " (Matched)", FAIL: " (Not matched)", REVIEW: " (Review)"}.get(
        _check(step, "date_of_birth_match").get("status"), "")
    document_type = extracted.get("document_type")
    breakdown = "MRZ Data Breakdown" if extracted.get("source") == "mrz" else "Document Data Breakdown"
    rows = [
        _checks(lines),
        Spacer(1, 4), _band("Data Submitted Via API"), _pairs([
            ("First Name", claimed.get("first_name")),
            ("Last Name", claimed.get("last_name")),
        ]),
        Spacer(1, 4), _band("Extracted Data"), _pairs([
            ("DOB", extracted.get("date_of_birth")),
            ("Document Type", _humanise(document_type) if document_type else None),
        ]),
        Spacer(1, 4), _band(breakdown), _pairs([
            ("Country Code", extracted.get("issuing_country")),
            ("Document Number", extracted.get("document_number")),
            ("Nationality", _country(extracted.get("nationality")) if extracted.get("nationality") else None),
            ("Date Of Birth", (str(extracted.get("date_of_birth")) + dob_note) if extracted.get("date_of_birth") else None),
            ("Gender", extracted.get("sex")),
            ("Expiry Date", extracted.get("expiry_date")),
        ]),
        Spacer(1, 6), _gallery([("Document - Front Side", data.images.get(FileKind.document_front))], 170),
    ]
    return [_card(rows, "Document Verification")]


def _address(data: ReportData) -> list:
    step = data.steps.get(VerificationStep.address)
    extracted = (step.extracted if step else None) or {}
    document_type = extracted.get("document_type")
    lines = [(label, _check_status(step, names)) for label, names in _ADDRESS_LINES]
    rows = [
        _checks(lines),
        Spacer(1, 4), _band("Data Submitted Via API"),
        _pairs([("Full Address", (data.verification.claimed or {}).get("address"))], columns=1),
        Spacer(1, 4), _band("Extracted Data"),
        _pairs([("Document Type", _humanise(document_type) if document_type else None)]),
        Spacer(1, 6), _gallery([("Address - Front Side", data.images.get(FileKind.address_proof))], 170),
    ]
    return [_card(rows, "Address Verification")]


# The names Shufti's report gives each kind of question.
_QUESTION_TYPES = {
    "text": "Text", "textarea": "Text", "number": "Number", "amount": "Text", "date": "Date",
    "select": "Single Select", "multi_select": "Multi Select", "country": "Countries List",
    "yes_no": "Yes/No", "file": "File",
}


def _answer(kind: str, value) -> str:
    """A file answer by its name (the file itself is in the verification's
    evidence), and a country by its name, as Shufti's report shows them."""
    if isinstance(value, dict) and "file_id" in value:
        return value.get("name") or "File attached"
    if kind == "country" and isinstance(value, str):
        return _country(value)
    return _text(value)


def _due_diligence(data: ReportData) -> list:
    step = data.steps.get(VerificationStep.due_diligence)
    answers = (step.extracted if step else None) or {}
    # The questions as they were when this person answered them, kept on the step.
    snapshot = ((step.result if step else None) or {}).get("form") or {}
    questions = snapshot.get("fields") or [
        {"key": key, "label": _humanise(str(key)), "type": "multi_select" if isinstance(value, list) else "text"}
        for key, value in answers.items()
    ]
    name = snapshot.get("name")
    rows = [_band("DDF"), _pairs([("Form", f"{name} ({data.verification.form_id})" if name else data.verification.form_id)], columns=1)]
    if answers:
        inner = CONTENT_WIDTH - 20
        rows.append(_table(
            ["Sr no", "Question(s)", "Type", "Answer", "Status"],
            [[str(i), question["label"], _QUESTION_TYPES.get(question["type"], _humanise(question["type"])),
              _answer(question["type"], answers.get(question["key"])), ""]
             for i, question in enumerate(questions, start=1)],
            [36, inner * 0.45 - 36, inner * 0.13, inner * 0.3, inner * 0.12],
        ))
    else:
        rows.append(_p("No answers have been recorded for this form.", _SMALL))
    return [_card(rows, "Due Diligence Forms")]


def _aml(data: ReportData) -> list:
    """Not on Shufti's report, which screens separately; ours is a step of the
    verification, so it is shown like the other services."""
    step = data.steps.get(VerificationStep.aml)
    result = (step.result if step else None) or {}
    score = result.get("highest_score")
    rows = [_pairs([
        ("Outcome", _humanise(result.get("outcome")) if result.get("outcome") else None),
        ("Action", _humanise(result.get("action")) if result.get("action") else None),
        ("Dataset", result.get("dataset")),
        ("Highest Score", f"{float(score):.2f}" if score is not None else None),
        ("Screened At", _stamp(datetime.fromisoformat(result["screened_at"])) if result.get("screened_at") else None),
        ("Matches", len(result.get("matches") or [])),
    ])]
    matches = result.get("matches") or []
    if matches:
        inner = CONTENT_WIDTH - 20
        rows.append(_table(
            ["Name", "Score", "Type", "Datasets"],
            [[m.get("caption") or ((m.get("properties") or {}).get("name") or ["N/A"])[0],
              f"{float(m.get('score', 0)):.2f}", m.get("schema"), _text(m.get("datasets"))] for m in matches[:20]],
            [inner * 0.4, inner * 0.1, inner * 0.15, inner * 0.35],
        ))
    return [_card(rows, "AML Screening")]


def _online_offline(mode: str | None) -> tuple[str, str]:
    """Shufti's verification_mode, which ours copies: "any" allows both a live
    capture (online) and an uploaded file (offline)."""
    mode = (mode or "any").lower()
    return ("No" if mode == "image_only" else "Yes", "No" if mode == "video_only" else "Yes")


def _settings(data: ReportData) -> list:
    """Verification Settings, with Callback Details inside it, as Shufti has it."""
    v = data.verification
    online, offline = _online_offline(v.verification_mode)
    document = data.steps.get(VerificationStep.document)
    address = data.steps.get(VerificationStep.address)

    def selected(step):
        chosen = (step.extracted or {}).get("document_type") if step else None
        return _humanise(chosen) if chosen else None

    rows = [_band("General"), _pairs([
        ("Verification Mode", _humanise(v.verification_mode) if v.verification_mode else None),
        ("Verification Type", "Onsite"),
        ("Country Name", _country(v.country) if v.country else None),
        ("Offline Allowed", offline),
        ("Online Allowed", online),
        ("Decline On Single Step", "Yes"),
        ("Callback URL", v.callback_url),
        ("Redirect URL", v.redirect_url),
        ("Allow N/A", None),
        ("Show Results", None),
    ])]
    if "document" in v.services:
        rows += [Spacer(1, 4), _band("Document Verification"),
                 _pairs([("Supported Types", ", ".join(_humanise(t.value) for t in DocumentType))], columns=1,
                        label_width=120),
                 _pairs([
                     ("Fuzzy Match", "Yes"),
                     ("Fetch Enhanced Data", "No"),
                     ("Selected Type", selected(document)),
                     ("Online Allowed", online),
                     ("Offline Allowed", offline),
                 ])]
    if "address" in v.services:
        rows += [Spacer(1, 4), _band("Address Verification"),
                 _pairs([("Supported Types", ", ".join(_humanise(t.value) for t in ProofType))], columns=1,
                        label_width=120),
                 _pairs([
                     ("Fuzzy Match", "No"),
                     ("Address Fuzzy Match", "No"),
                     ("Selected Type", selected(address)),
                     ("Online Allowed", online),
                     ("Offline Allowed", offline),
                     ("Backside Proof Required", "No"),
                 ])]
    rows += [Spacer(1, 4), _band("Callback Details"), _callbacks(data)]
    return [_card(rows, "Verification Settings")]


def _callbacks(data: ReportData) -> Flowable:
    inner = CONTENT_WIDTH - 20
    if not data.verification.callback_url:
        return _p("No callback URL was set for this verification.", _SMALL)
    if not data.callbacks:
        return _p("No callback deliveries have been recorded.", _SMALL)
    grouped: dict[str, list[CallbackDelivery]] = {}
    for delivery in data.callbacks:
        grouped.setdefault(delivery.event, []).append(delivery)
    rows = []
    for event, deliveries in grouped.items():
        last = deliveries[-1]
        rows.append([event, str(max(d.attempt for d in deliveries)), _text(last.http_status), _stamp(last.sent_at)])
    return _table(["Event", "Attempts", "Callback Response Http Code", "Callback Send Timestamp"], rows,
                  [inner * 0.3, inner * 0.15, inner * 0.27, inner * 0.28])


def _face_match(data: ReportData) -> list:
    """The whole document beside the face that was captured, nothing cut from either."""
    face = data.face
    # The score is ArcFace's cosine similarity, -1 to 1, not a percentage: shown
    # as it is stored, beside the line it had to reach.
    note = f"{face.match_score:.4f}" if face and face.match_score is not None else "N/A"
    if face and face.match_threshold is not None:
        note += f"  (cosine similarity, pass at {face.match_threshold:.2f} or above)"
    pictures = [("Document Front Side", data.images.get(FileKind.document_front)),
                ("Face", data.images.get(FileKind.portrait))]
    return [KeepTogether(_card([Spacer(1, 4), _gallery(pictures, max_height=130)], "Face Match Score:", note))]


def _device(data: ReportData) -> list:
    """Shufti's Device and Network Intelligence, field for field and in its order.

    The lookup behind most of these is not wired up yet, so they read N/A until
    a device_sessions row carries them.
    """
    d = data.device
    first = _first_device(data)
    signals = (d.signals if d else None) or {}
    user_agent = (d.user_agent if d else None) or first.get("user_agent")
    _, system = _browser(user_agent)

    def flag(value):
        return None if value is None else ("Yes" if value else "No")

    def column(name):
        return getattr(d, name) if d else None

    pairs = [
        ("IP", column("ip") or first.get("ip")), ("Continent Name", signals.get("continent")),
        ("Country Name", column("country")), ("City", column("city")),
        ("Latitude", column("latitude")), ("Longitude", column("longitude")),
        ("ASN", column("asn")), ("ASN Name", column("asn_name")),
        ("IP Timezone", column("timezone")), ("Fingerprint", column("fingerprint")),
        ("Browser Timezone", signals.get("browser_timezone")), ("OS Name", system),
        ("User Agent", user_agent), ("Tor Usage Detected", flag(column("is_tor"))),
        ("Is Proxy", flag(column("is_proxy"))), ("Threat Level", column("threat_level")),
        ("IP Routing Type", signals.get("ip_routing_type")), ("Data Center Detected", flag(column("is_datacenter"))),
        ("Country Mismatch", flag(column("country_mismatch"))),
        ("IP Timezone Mismatch", flag(column("timezone_mismatch"))),
        ("Frequent IP Changes", flag(signals.get("frequent_ip_changes"))),
        ("Stable IP Detected", flag(signals.get("stable_ip"))),
        ("Jailbroken Or Rooted Device", flag(column("rooted_device"))),
        ("Emulated Device Detected", flag(column("emulated_device"))),
        ("Risk Level", column("risk_level")), ("Summary", signals.get("summary")),
        ("Fraud Score", column("fraud_score")),
    ]
    return [_card([_pairs(pairs)], "Device and Network Intelligence")]


# The pages


class _NumberedCanvas(pdfcanvas.Canvas):
    """Draws the header bar and footer on every page, with 'Page n of N'."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pages = []

    def showPage(self):
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._pages)
        for state in self._pages:
            self.__dict__.update(state)
            self._decorate(total)
            super().showPage()
        super().save()

    def _shield(self, x: float, y: float, size: float):
        """The Pangolin shield, the mark the admin console shows, drawn from its
        48 by 48 SVG with (x, y) as the top left corner."""
        k = size / 48

        def path(*parts):
            p = self.beginPath()
            for op, *pts in parts:
                xy = [(x + px * k, y - py * k) for px, py in zip(pts[::2], pts[1::2])]
                if op == "M":
                    p.moveTo(*xy[0])
                elif op == "L":
                    p.lineTo(*xy[0])
                else:
                    p.curveTo(*xy[0], *xy[1], *xy[2])
            p.close()
            return p

        def gradient(p, start, end):
            self.saveState()
            self.clipPath(p, stroke=0, fill=0)
            self.linearGradient(x, y, x + size, y - size, (start, end), extend=True)
            self.restoreState()

        def fill(p, colour, alpha=1.0):
            self.saveState()
            self.setFillColor(colour, alpha=alpha)
            self.drawPath(p, stroke=0, fill=1)
            self.restoreState()

        gradient(path(("M", 24, 3), ("L", 6, 11), ("L", 6, 22), ("C", 6, 33.2, 13.7, 43.6, 24, 46),
                      ("C", 34.3, 43.6, 42, 33.2, 42, 22), ("L", 42, 11)),
                 colors.HexColor("#1456F5"), colors.HexColor("#0B37B8"))
        gradient(path(("M", 24, 10), ("C", 18, 10, 13, 14.5, 12, 20.5), ("C", 14.8, 20, 18.2, 20.5, 21, 22.5),
                      ("C", 22, 17, 24, 12, 24, 10)),
                 colors.HexColor("#38BDF8"), colors.HexColor("#1456F5"))
        fill(path(("M", 24, 10), ("C", 30, 10, 35, 14.5, 36, 20.5), ("C", 33.2, 20, 29.8, 20.5, 27, 22.5),
                  ("C", 26, 17, 24, 12, 24, 10)), colors.white, 0.25)
        fill(path(("M", 24, 14), ("C", 20.5, 17.5, 18, 22, 18, 27), ("C", 21, 28.5, 24, 31, 24, 35),
                  ("C", 24, 31, 27, 28.5, 30, 27), ("C", 30, 22, 27.5, 17.5, 24, 14)), colors.white, 0.95)
        fill(path(("M", 24, 20), ("L", 21, 25.5), ("L", 27, 25.5)), BRAND_BLUE)
        fill(path(("M", 20, 30), ("C", 22.5, 31.5, 25.5, 31.5, 28, 30), ("C", 26.5, 33, 24, 36, 24, 36),
                  ("C", 24, 36, 21.5, 33, 20, 30)), colors.HexColor("#38BDF8"))

    def _decorate(self, total: int):
        self.setFillColor(HEADER)
        self.rect(0, PAGE_HEIGHT - 48, PAGE_WIDTH, 48, stroke=0, fill=1)
        self._shield(MARGIN, PAGE_HEIGHT - 11, 26)
        word = MARGIN + 26 + 8
        self.setFont(FONT_BOLD, 15)
        self.setFillColor(colors.white)
        self.drawString(word, PAGE_HEIGHT - 29, "Pangolin ")
        self.setFillColor(HEADER_BLUE)
        self.drawString(word + self.stringWidth("Pangolin ", FONT_BOLD, 15), PAGE_HEIGHT - 29, "KYC")
        self.setFillColor(colors.white)
        self.setFont(FONT_MEDIUM, 8)
        self.drawRightString(PAGE_WIDTH - MARGIN, PAGE_HEIGHT - 28, "Verification Proof")

        self.setStrokeColor(BORDER)
        self.setLineWidth(0.6)
        self.line(MARGIN, 38, PAGE_WIDTH - MARGIN, 38)
        self._shield(MARGIN, 33, 14)
        word = MARGIN + 14 + 5
        self.setFont(FONT_BOLD, 9)
        self.setFillColor(INK)
        self.drawString(word, 23, "Pangolin ")
        self.setFillColor(BRAND_BLUE)
        self.drawString(word + self.stringWidth("Pangolin ", FONT_BOLD, 9), 23, "KYC")
        self.setFillColor(MUTED)
        self.setFont(FONT, 7)
        self.drawRightString(PAGE_WIDTH - MARGIN, 23.5, f"Page {self._pageNumber} of {total}")


def render(data: ReportData) -> bytes:
    """Lay the report out and return the PDF."""
    v = data.verification
    story: list = []
    gap = Spacer(1, 10)

    # Page by page as Shufti lays it out: the summary and the face checks first,
    # then each other service on a page of its own, then the settings with the
    # callbacks, then the faces side by side with the device they came from.
    story += _headline(data) + [gap] + _services(data)
    if "face" in v.services:
        story += [gap] + _facial(data)
    for name in v.services:
        section = {"document": _document, "address": _address,
                   "due_diligence": _due_diligence, "aml": _aml}.get(name)
        if section:
            story += [PageBreak()] + section(data)
    story += [PageBreak()] + _settings(data)
    story += [PageBreak()]
    if "face" in v.services or "document" in v.services:
        story += _face_match(data) + [gap]
    story += _device(data)

    out = io.BytesIO()
    document = SimpleDocTemplate(
        out, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=64, bottomMargin=52,
        title=f"Verification Proof {v.reference}", author=PRODUCT,
        subject="Identity verification report", initialFontName=FONT,
    )
    document.build(story, canvasmaker=_NumberedCanvas)
    return out.getvalue()


def build(db: Session, verification: Verification) -> bytes:
    return render(gather(db, verification))


def filename(verification: Verification) -> str:
    """The same name tuesday-backend gives the Shufti proof it serves."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", verification.reference)
    return f"verification_{safe}.pdf"
