"""Address proof pipeline.

read (a photograph, or a PDF's own text, or a rendered PDF) -> quality -> is it the
kind of proof they said -> is the address on it, as it was written -> does it
belong to the same person -> is it recent enough -> decision

The address comparison is direct. Whatever the client sent has to appear on the
document; case, spacing and punctuation are ignored and nothing else is. See
app/engines/addresses.py.

The checks are named as Shufti names them in its verification report, so a client
holding one of their reports can map ours onto it line for line:

  Address Document Proof                     -> address_document_proof
  Address on Document Match                  -> address_on_document_match
  Match Address Proofs With Document Proofs  -> match_address_proofs_with_document_proofs
  Document Expiration                        -> address_document_expiration
  Address Document Authenticity              -> address_document_authenticity

Authenticity is reported as skipped rather than passed. Shufti runs a tamper check
there; we have no model for it, and a blank check that reads as a pass in a report
somebody relies on is worse than one that says plainly it was not run.
"""

import time
from dataclasses import asdict
from datetime import date

from app.core.config import settings
from app.engines.addresses import address_on_document, document_text, is_po_box
from app.engines.fields import find_dates
from app.engines.image import InvalidImageError, decode_image
from app.engines.ocr import read_document
from app.engines import provenance
from app.engines.pdf import PdfUnreadable, is_pdf, render_pages, text_lines
from app.engines.quality import assess_quality
from app.pipeline.ai_image import check_ai_generated
from app.pipeline.document import names_on_document
from app.rules import address as rules
from app.schemas.address import AddressCheckResponse, AddressExtracted, ProofType
from app.schemas.document import CheckResult, CheckStatus, Decision


def scan_address_proof(
    proof_type: ProofType,
    claimed_address: str,
    front: bytes,
    back: bytes | None = None,
    names: list[str | None] | None = None,
    today: date | None = None,
) -> AddressCheckResponse:
    """Judge one proof of address against the address the client gave us.

    `names` are the names the document step *proved*, not what anybody typed. A
    bill in somebody else's name proves where that person lives.
    """
    started = time.perf_counter()
    today = today or date.today()
    checks: list[CheckResult] = []
    extracted = AddressExtracted(document_type=proof_type, full_address=claimed_address)

    def finish() -> AddressCheckResponse:
        return AddressCheckResponse(
            decision=_decide(checks),
            reasons=_unique(reason for check in checks for reason in check.reasons),
            warnings=_unique(warning for check in checks for warning in check.warnings),
            extracted=extracted,
            checks=checks,
            processing_ms=int((time.perf_counter() - started) * 1000),
        )

    # A PO box is refused before anything is read: it is not a place anybody
    # lives, and our Shufti request says po_box: false for the same reason.
    if is_po_box(claimed_address):
        checks.append(
            CheckResult(
                name="address_on_document_match",
                status=CheckStatus.failed,
                reasons=["PO_BOX_NOT_ACCEPTED"],
                details={"claimed": claimed_address},
            )
        )
        return finish()

    try:
        lines, qualities, sides, pictures, on_the_page = _read_proof(front, back)
    except UnreadableProof as unreadable:
        checks.append(
            CheckResult(
                name="address_document_proof",
                status=CheckStatus.failed,
                reasons=[unreadable.reason],
            )
        )
        return finish()

    # Is there a document here at all? A bill is a page of print, so the test is
    # whether enough text came back to read an address off.
    readable = len(lines) >= rules.MIN_TEXT_LINES
    checks.append(
        CheckResult(
            name="address_document_proof",
            status=CheckStatus.passed if readable else CheckStatus.failed,
            reasons=[] if readable else ["PROOF_NOT_READABLE"],
            details={"text_lines": len(lines), "read": sides},
        )
    )
    for side, quality in qualities.items():
        checks.append(
            CheckResult(
                name=f"quality_{side}",
                status=CheckStatus.passed if readable or not quality.issues else CheckStatus.failed,
                reasons=[] if readable else quality.issues,
                warnings=quality.issues if readable else [],
                details={key: value for key, value in asdict(quality).items() if key != "issues"},
            )
        )
    if not readable:
        return finish()

    # Was the proof itself generated? A bill is text on a plain background, which is
    # the easiest thing in this flow to produce with a generator. A PDF is scored on
    # its rendered pages whether or not its text could be read directly.
    checks.append(check_ai_generated(pictures, subject="address"))
    checks.append(_type_check(proof_type, lines))
    checks.append(_address_check(claimed_address, lines, extracted))
    checks.append(_text_layer_agrees(claimed_address, lines, on_the_page, today))
    checks.append(_owner_check(names or [], lines))
    if proof_type == ProofType.driving_license:
        checks.append(_licence_validity_check(lines, today, extracted))
    else:
        checks.append(_recency_check(lines, today, extracted))
    # The file's own history: the editor that made it, saves after it was issued. Evidence,
    # not proof; a clean result proves nothing, so this only ever adds a reason.
    traces = {}
    for data in (front, back):
        if data:
            traces.update(provenance.signals(data))
    checks.append(
        CheckResult(
            name="address_document_provenance",
            status=CheckStatus.review if traces else CheckStatus.passed,
            reasons=["POSSIBLE_TAMPERING"] if traces else [],
            details={"signals": traces},
        )
    )
    # There is no way to prove a proof is genuine. By default a person looks at every proof
    # that would otherwise pass; see ADDRESS_PROOFS_NEED_REVIEW.
    needs_review = settings.ADDRESS_PROOFS_NEED_REVIEW
    checks.append(
        CheckResult(
            name="address_document_authenticity",
            status=CheckStatus.review if needs_review else CheckStatus.skipped,
            reasons=["AUTHENTICITY_NOT_VERIFIED"] if needs_review else [],
            warnings=["AUTHENTICITY_NOT_CHECKED"],
        )
    )
    return finish()


class UnreadableProof(Exception):
    """Nothing could be opened, so there is no document to judge."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _read_proof(
    front: bytes, back: bytes | None
) -> tuple[list[str], dict, list[str], list[tuple[str, object]], list[str]]:
    """The text on the proof, the quality of each photograph, what was read, and the
    pictures themselves.

    A PDF is read from its own text layer when it has one, which a bank's own
    statement does, and rendered and OCRed when it does not, which a scan does not.
    Quality is only measured on photographs: blur and glare mean nothing on a page
    a computer drew, and refusing a perfectly clean PDF for lacking contrast would
    be nonsense.
    """
    lines: list[str] = []
    qualities: dict = {}
    read: list[str] = []
    # Every picture this proof turned out to be, for the generator check. A PDF read
    # from its own text layer contributes none: there is no image to score.
    pictures: list[tuple[str, object]] = []
    # What the rendered pages actually show, read by OCR, when the text came from a
    # PDF's text layer instead. Empty for anything else, since then `lines` already
    # is what the page shows.
    on_the_page: list[str] = []

    for side, data in (("front", front), ("back", back)):
        if data is None:
            continue

        if is_pdf(data):
            # Rendered whatever happens, because the generated-image check needs
            # pixels. A statement whose text can be read directly used to contribute
            # no picture at all, so a generated bill submitted as a digital PDF, the
            # easiest document in this flow to produce and the only one we take as a
            # PDF, was never examined. The text is still read from the text layer
            # when there is one: that is accurate, and this is only for scoring.
            try:
                pages = render_pages(data)
            except PdfUnreadable:
                raise UnreadableProof("PROOF_NOT_READABLE") from None
            for number, page in enumerate(pages, start=1):
                pictures.append((f"{side} page {number}", page))

            try:
                inside = text_lines(data)
            except PdfUnreadable:
                raise UnreadableProof("PROOF_NOT_READABLE") from None
            if len(inside) >= rules.MIN_TEXT_LINES:
                lines.extend(inside)
                # And read the pages as well, to compare with. A text layer is not
                # obliged to say what the page shows: on a scan it is whatever a
                # scanner app, or somebody else, put there.
                for page in pages:
                    on_the_page.extend(line.text for line in read_document(page))
                read.append(f"{side}: pdf text, {len(pages)} page(s) scored and read")
                continue

            # A scan wrapped in a PDF: the pages are all there is to read.
            if not pages:
                raise UnreadableProof("PROOF_NOT_READABLE")
            for page in pages:
                lines.extend(line.text for line in read_document(page))
            read.append(f"{side}: pdf rendered, {len(pages)} page(s)")
            continue

        try:
            image = decode_image(data)
        except InvalidImageError:
            raise UnreadableProof("IMAGE_UNREADABLE") from None
        qualities[side] = assess_quality(image)
        pictures.append((side, image))
        lines.extend(line.text for line in read_document(image))
        read.append(f"{side}: photograph")

    return lines, qualities, read, pictures, on_the_page


def _decide(checks: list[CheckResult]) -> Decision:
    if any(c.name == "address_document_proof" and c.status == CheckStatus.failed for c in checks):
        # Nothing readable arrived: that is a photograph problem, not a refusal.
        return Decision.retake
    if any(c.status == CheckStatus.failed for c in checks):
        return Decision.failed
    if any(c.status == CheckStatus.review for c in checks):
        return Decision.review
    return Decision.passed


def _unique(items) -> list[str]:
    return list(dict.fromkeys(items))


def _type_check(proof_type: ProofType, lines: list[str]) -> CheckResult:
    """Does the document read like the kind of proof they said it was?

    Sent to a person when it does not, rather than refused: a small local bill can
    name none of the words on any list and still be a genuine bill.
    """
    text = document_text(lines)
    found = [word for word in rules.TYPE_WORDS[proof_type.value] if word in text]
    return CheckResult(
        name="address_document_type",
        status=CheckStatus.passed if found else CheckStatus.review,
        reasons=[] if found else ["PROOF_TYPE_MISMATCH"],
        details={"declared": proof_type.value, "words_found": found[:8]},
    )


def _address_check(claimed: str, lines: list[str], extracted: AddressExtracted) -> CheckResult:
    """Does the document say the address we were given, as it was written?

    Direct: the address has to appear on the document. Case, spacing and
    punctuation are ignored, and the document's lines are joined first so an
    address printed across two lines still reads as one. Nothing else is
    forgiven, and there is no partial credit: "Kigali, Rwanda" is not matched by a
    bill printing "KG 333 St", nor by one printing "Kigali City".

    Pass or fail, never review. A check that asks a person to decide whenever two
    addresses are written differently is a check nobody runs.
    """
    found = address_on_document(claimed, lines)
    extracted.address_found = found

    return CheckResult(
        name="address_on_document_match",
        status=CheckStatus.passed if found else CheckStatus.failed,
        reasons=[] if found else ["ADDRESS_MISMATCH"],
        details={
            "claimed": claimed,
            "looked_for": document_text([claimed]),
            # What the document actually says, so a person can see how far off it
            # was without opening the file.
            "document_text": document_text(lines)[:400],
        },
    )


def _text_layer_agrees(claimed: str, lines: list[str], on_the_page: list[str], today: date) -> CheckResult:
    """Does a PDF's text layer say what its page shows?

    The two are independent. A PDF carries text and it carries a rendering, and
    nothing makes them agree: on a scan the text layer is whatever a scanner app
    wrote, or whatever somebody replaced it with. So a document can show a Nairobi
    address while its text layer says Kigali, and every check here reads the text,
    which is the accurate thing to read right up until it is a lie.

    This compares the two on the only things this step decides by: whether the
    claimed address is on the page as well as in the text, and whether the newest
    date agrees. A disagreement about the address goes to a person, because that is
    exactly the shape of the attack and the address check cannot be trusted on its
    own once the two differ. A disagreement about the date is a warning: OCR
    misreads dates on a rendering often enough that failing on it would be noise.

    Skipped for anything that is not a PDF read from its text layer, where `lines`
    already is what the page shows.
    """
    if not on_the_page:
        return CheckResult(name="pdf_text_matches_page", status=CheckStatus.skipped)

    in_text = address_on_document(claimed, lines)
    on_page = address_on_document(claimed, on_the_page)

    text_dates = [d for line in lines for d in find_dates(line, today) if d <= today]
    page_dates = [d for line in on_the_page for d in find_dates(line, today) if d <= today]
    newest_text = max(text_dates) if text_dates else None
    newest_page = max(page_dates) if page_dates else None
    dates_agree = newest_text is None or newest_page is None or newest_text == newest_page

    details = {
        "address_in_text_layer": in_text,
        "address_on_rendered_page": on_page,
        "newest_date_in_text_layer": newest_text,
        "newest_date_on_rendered_page": newest_page,
    }

    if in_text and not on_page:
        return CheckResult(
            name="pdf_text_matches_page",
            status=CheckStatus.review,
            reasons=["PDF_TEXT_LAYER_DISAGREES"],
            details=details,
        )
    return CheckResult(
        name="pdf_text_matches_page",
        status=CheckStatus.passed,
        warnings=[] if dates_agree else ["PDF_DATE_DISAGREES"],
        details=details,
    )


def _owner_check(names: list[str | None], lines: list[str]) -> CheckResult:
    """Is the proof in the name of the person whose document we verified?

    Shufti calls this matching the address proof against the document proof. The
    names come from what the document step proved, so a bill in a landlord's or a
    spouse's name fails, which is the point: it proves where they live.
    """
    if not any(names):
        return CheckResult(
            name="match_address_proofs_with_document_proofs",
            status=CheckStatus.review,
            reasons=["OWNER_NOT_VERIFIED"],
            details={"looked_for": None},
        )

    found, missing = names_on_document(names, lines)
    ok = bool(found) and not missing
    return CheckResult(
        name="match_address_proofs_with_document_proofs",
        status=CheckStatus.passed if ok else CheckStatus.failed,
        reasons=[] if ok else ["NAME_NOT_ON_ADDRESS_PROOF"],
        details={
            "looked_for": " ".join(name for name in names if name),
            "matched": found,
            "missing": missing,
        },
    )


def _recency_check(lines: list[str], today: date, extracted: AddressExtracted) -> CheckResult:
    """Is the proof recent enough to say where somebody lives now?

    The newest date on the page that is not in the future, against the 90 days our
    Shufti request asks for as document_validity. No date at all goes to a person:
    plenty of genuine bills print one in a format nothing reads.
    """
    found = [value for line in lines for value in find_dates(line, today)]
    dates = [value for value in found if value <= today]
    if not dates:
        # Dated, but only in the future: that is not "no date", and it is not a normal bill.
        return CheckResult(
            name="address_document_expiration",
            status=CheckStatus.review,
            reasons=["PROOF_DATE_IN_FUTURE" if found else "PROOF_DATE_NOT_FOUND"],
        )

    issued = max(dates)
    age_days = (today - issued).days
    extracted.proof_date = issued
    extracted.proof_age_days = age_days

    fresh = age_days <= rules.MAX_PROOF_AGE_DAYS
    return CheckResult(
        name="address_document_expiration",
        status=CheckStatus.passed if fresh else CheckStatus.failed,
        reasons=[] if fresh else ["PROOF_TOO_OLD"],
        details={"proof_date": issued, "age_days": age_days, "allowed_days": rules.MAX_PROOF_AGE_DAYS},
    )


def _licence_validity_check(lines: list[str], today: date, extracted: AddressExtracted) -> CheckResult:
    """Is the driving licence still valid?

    A licence is issued years before it is used, so the 90 days a bill is held to
    would refuse every one. What matters is that it has not expired: the latest
    date printed on it is its expiry, and it has to be today or later. No date at
    all goes to a person, as it does for a bill.
    """
    dates = [value for line in lines for value in find_dates(line, today)]
    if not dates:
        return CheckResult(
            name="address_document_expiration",
            status=CheckStatus.review,
            reasons=["PROOF_DATE_NOT_FOUND"],
        )

    expires = max(dates)
    issued = [value for value in dates if value <= today]
    if issued:
        extracted.proof_date = max(issued)
        extracted.proof_age_days = (today - extracted.proof_date).days

    valid = expires >= today
    return CheckResult(
        name="address_document_expiration",
        status=CheckStatus.passed if valid else CheckStatus.failed,
        reasons=[] if valid else ["PROOF_EXPIRED"],
        details={"expiry_date": expires},
    )
