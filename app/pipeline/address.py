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

from app.engines.addresses import address_on_document, document_text, is_po_box
from app.engines.fields import find_dates
from app.engines.image import InvalidImageError, decode_image
from app.engines.ocr import read_document
from app.engines.pdf import is_pdf, render_pages, text_lines
from app.engines.quality import assess_quality
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
        lines, qualities, sides = _read_proof(front, back)
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

    checks.append(_type_check(proof_type, lines))
    checks.append(_address_check(claimed_address, lines, extracted))
    checks.append(_owner_check(names or [], lines))
    checks.append(_recency_check(lines, today, extracted))
    checks.append(
        CheckResult(
            name="address_document_authenticity",
            status=CheckStatus.skipped,
            warnings=["AUTHENTICITY_NOT_CHECKED"],
        )
    )
    return finish()


class UnreadableProof(Exception):
    """Nothing could be opened, so there is no document to judge."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _read_proof(front: bytes, back: bytes | None) -> tuple[list[str], dict, list[str]]:
    """The text on the proof, the quality of each photograph, and what was read.

    A PDF is read from its own text layer when it has one, which a bank's own
    statement does, and rendered and OCRed when it does not, which a scan does not.
    Quality is only measured on photographs: blur and glare mean nothing on a page
    a computer drew, and refusing a perfectly clean PDF for lacking contrast would
    be nonsense.
    """
    lines: list[str] = []
    qualities: dict = {}
    read: list[str] = []

    for side, data in (("front", front), ("back", back)):
        if data is None:
            continue

        if is_pdf(data):
            inside = text_lines(data)
            if len(inside) >= rules.MIN_TEXT_LINES:
                lines.extend(inside)
                read.append(f"{side}: pdf text")
                continue
            # A scan wrapped in a PDF: every page becomes a picture to read.
            pages = render_pages(data)
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
        lines.extend(line.text for line in read_document(image))
        read.append(f"{side}: photograph")

    return lines, qualities, read


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
    dates = [value for line in lines for value in find_dates(line, today) if value <= today]
    if not dates:
        return CheckResult(
            name="address_document_expiration",
            status=CheckStatus.review,
            reasons=["PROOF_DATE_NOT_FOUND"],
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
