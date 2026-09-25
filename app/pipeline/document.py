"""Document scan pipeline.

decode -> quality -> OCR (all text) -> is it a document? -> MRZ if present -> fields
-> document checks -> applicant checks -> decision

Works for any passport, national ID or driving licence. The MRZ, when a document
has one, is used as the stronger source; otherwise fields come from the printed text.
"""

import re
import time
import unicodedata
from dataclasses import asdict
from datetime import date

from rapidfuzz import fuzz

from app.engines.fields import VisualFields, extract_visual_fields, to_alpha3
from app.engines.image import InvalidImageError, decode_image
from app.engines.mrz import MrzResult, parse_mrz_text, read_mrz
from app.engines.ocr import read_document
from app.engines.quality import assess_quality
from app.rules import document as rules
from app.schemas.document import (
    Applicant,
    CheckResult,
    CheckStatus,
    Decision,
    DocumentCheckResponse,
    DocumentType,
    ExtractedFields,
)

_MRZ_TYPE_CODES = {
    DocumentType.passport: ("P",),
    DocumentType.id_card: ("I", "A", "C"),
}


def scan_document(
    document_type: DocumentType,
    front: bytes,
    back: bytes | None = None,
    applicant: Applicant | None = None,
    today: date | None = None,
    require_adult: bool = True,
) -> DocumentCheckResponse:
    """Read a document and judge it.

    `require_adult` is the one thing a child's check changes: a birth certificate
    belongs to someone under 18 by definition, so the age rule is not applied. No
    other check is relaxed, and the result says plainly that age was not required.
    """
    started = time.perf_counter()
    today = today or date.today()
    applicant = applicant or Applicant()
    checks: list[CheckResult] = []
    extracted: ExtractedFields | None = None

    def finish() -> DocumentCheckResponse:
        return DocumentCheckResponse(
            decision=_decide(checks),
            reasons=_unique(reason for check in checks for reason in check.reasons),
            warnings=_unique(warning for check in checks for warning in check.warnings),
            document_type=document_type,
            extracted=extracted,
            checks=checks,
            processing_ms=int((time.perf_counter() - started) * 1000),
        )

    # 1. Decode every side we received.
    images = {}
    for side, data in (("front", front), ("back", back)):
        if data is None:
            continue
        try:
            images[side] = decode_image(data)
        except InvalidImageError:
            checks.append(CheckResult(name=f"image_{side}", status=CheckStatus.failed, reasons=["IMAGE_UNREADABLE"]))
            return finish()

    # 2. Image quality and 3. OCR of all text, per side.
    qualities = {side: assess_quality(image) for side, image in images.items()}
    ocr_lines = {side: [line.text for line in read_document(image)] for side, image in images.items()}
    all_lines = [line for side in images for line in ocr_lines[side]]

    # 4. Is this a document at all? If not, quality problems are the likely reason.
    visual = extract_visual_fields(all_lines, today)
    has_mrz_lines = any("<<" in line for line in all_lines)
    detected = _looks_like_document(visual, has_mrz_lines, all_lines)
    checks.append(
        CheckResult(
            name="document_detection",
            status=CheckStatus.passed if detected else CheckStatus.failed,
            reasons=[] if detected else ["DOCUMENT_NOT_DETECTED"],
            details={
                "text_lines": len(all_lines),
                "document_types_found": visual.document_types,
                "country_found": visual.country,
                "mrz_present": has_mrz_lines,
            },
        )
    )
    for side, quality in qualities.items():
        checks.append(
            CheckResult(
                name=f"quality_{side}",
                # When the document was read fine, quality issues are only warnings.
                status=CheckStatus.passed if detected or not quality.issues else CheckStatus.failed,
                reasons=[] if detected else quality.issues,
                warnings=quality.issues if detected else [],
                details={k: v for k, v in asdict(quality).items() if k != "issues"},
            )
        )
    if not detected:
        return finish()

    # 5. MRZ: first from the OCR text, then Tesseract as a second reader.
    mrz = _find_mrz(document_type, images, ocr_lines)
    if mrz is not None:
        checks.append(_mrz_check(mrz))
    elif document_type == DocumentType.passport:
        # Every passport has an MRZ; not reading it lowers confidence, so a person looks.
        checks.append(CheckResult(name="mrz", status=CheckStatus.review, reasons=["MRZ_NOT_READABLE"]))

    use_mrz = mrz is not None and mrz.valid
    extracted = _fields_from_mrz(mrz, visual, today) if use_mrz else _fields_from_visual(visual)
    if use_mrz:
        consistency = _mrz_visual_consistency(extracted, visual)
        if consistency:
            checks.append(consistency)

    # 6. The document itself, then 7. does it belong to the applicant.
    checks.append(_document_type_check(document_type, extracted if use_mrz else None, visual))
    checks.append(_validity_check(extracted, today, require_adult, document_type))
    if applicant.issuing_country:
        checks.append(_country_check(applicant.issuing_country, extracted))

    if applicant.first_name or applicant.last_name:
        checks.append(_name_check(applicant, extracted, all_lines, use_mrz))
    elif not use_mrz:
        # Without an MRZ, the printed fields are read less reliably, so the document is
        # only accepted when it matches details the applicant gave us.
        checks.append(CheckResult(name="name_match", status=CheckStatus.review, reasons=["APPLICANT_NAME_REQUIRED"]))
    else:
        checks.append(CheckResult(name="name_match", status=CheckStatus.skipped, warnings=["OWNER_NOT_VERIFIED"]))

    if applicant.date_of_birth:
        checks.append(_dob_check(applicant.date_of_birth, extracted, visual, use_mrz))

    return finish()


def _unique(items) -> list[str]:
    return list(dict.fromkeys(items))


def _decide(checks: list[CheckResult]) -> Decision:
    if any(c.name == "document_detection" and c.status == CheckStatus.failed for c in checks):
        return Decision.retake
    if any(c.status == CheckStatus.failed for c in checks):
        return Decision.failed
    if any(c.status == CheckStatus.review for c in checks):
        return Decision.review
    return Decision.passed


def _looks_like_document(visual: VisualFields, has_mrz_lines: bool, lines: list[str]) -> bool:
    # An identity document names what it is, carries an MRZ, or names a country
    # alongside several lines of printed fields.
    if visual.document_types or has_mrz_lines:
        return True
    return visual.country is not None and len(lines) >= 4


def _find_mrz(document_type: DocumentType, images: dict, ocr_lines: dict) -> MrzResult | None:
    sides = ["back", "front"] if document_type == DocumentType.id_card else ["front", "back"]
    sides = [side for side in sides if side in images]

    best = None
    for side in sides:
        result = parse_mrz_text("\n".join(ocr_lines[side]))
        if result and result.valid:
            return result
        best = best or result

    # Only fall back to the slower second reader when an MRZ is expected or was seen.
    mrz_seen = any("<<" in line for side in sides for line in ocr_lines[side])
    if document_type == DocumentType.passport or mrz_seen:
        for side in sides:
            result = read_mrz(images[side])
            if result and result.valid:
                return result
            best = best or result
    return best


def _mrz_check(mrz: MrzResult) -> CheckResult:
    return CheckResult(
        name="mrz",
        # A failed checksum is either an OCR misread or a tampered document, and
        # code can't tell which, so a person decides.
        status=CheckStatus.passed if mrz.valid else CheckStatus.review,
        reasons=[] if mrz.valid else ["MRZ_CHECKSUM_FAILED"],
        details={
            "format": mrz.format,
            "lines": mrz.lines,
            "checksums": mrz.checksums,
            "failed_checks": mrz.failed_checks,
        },
    )


def _fields_from_mrz(mrz: MrzResult, visual: VisualFields, today: date) -> ExtractedFields:
    f = mrz.fields
    return ExtractedFields(
        source="mrz",
        document_type_code=f.get("document_type"),
        document_number=f.get("document_number"),
        issuing_country=f.get("country"),
        nationality=f.get("nationality"),
        surname=f.get("surname"),
        given_names=f.get("name"),
        full_name=" ".join(filter(None, [f.get("surname"), f.get("name")])),
        date_of_birth=_parse_mrz_date(f.get("birth_date"), today, future_allowed=False),
        issue_date=visual.issue_date,
        expiry_date=_parse_mrz_date(f.get("expiry_date"), today, future_allowed=True),
        sex=f.get("sex"),
    )


def _fields_from_visual(visual: VisualFields) -> ExtractedFields:
    full_name = visual.full_name or " ".join(filter(None, [visual.surname, visual.given_names])) or None
    return ExtractedFields(
        source="visual",
        document_number=visual.document_number,
        issuing_country=visual.country,
        surname=visual.surname,
        given_names=visual.given_names,
        full_name=full_name,
        date_of_birth=visual.date_of_birth,
        issue_date=visual.issue_date,
        expiry_date=visual.expiry_date,
        sex=visual.sex,
    )


def _parse_mrz_date(value: str | None, today: date, future_allowed: bool) -> date | None:
    """MRZ dates are YYMMDD, so the century has to be inferred."""
    if not value or not re.fullmatch(r"\d{6}", value):
        return None
    yy, mm, dd = int(value[:2]), int(value[2:4]), int(value[4:])
    year = 2000 + yy
    # Birth dates can't be in the future; expiry dates are at most ~20 years out.
    limit = today.year + 20 if future_allowed else today.year
    if year > limit:
        year -= 100
    try:
        return date(year, mm, dd)
    except ValueError:
        return None


def _mrz_visual_consistency(extracted: ExtractedFields, visual: VisualFields) -> CheckResult | None:
    """The printed dates must agree with the MRZ. Editing one but not the other is a classic forgery."""
    if not visual.all_dates:
        return None
    mismatches = []
    if extracted.date_of_birth and extracted.date_of_birth not in visual.all_dates:
        mismatches.append("date_of_birth")
    if extracted.expiry_date and extracted.expiry_date not in visual.all_dates:
        mismatches.append("expiry_date")
    return CheckResult(
        name="mrz_visual_consistency",
        status=CheckStatus.review if mismatches else CheckStatus.passed,
        reasons=["MRZ_VISUAL_MISMATCH"] if mismatches else [],
        details={"mismatched_fields": mismatches},
    )


def _document_type_check(
    document_type: DocumentType, mrz_fields: ExtractedFields | None, visual: VisualFields
) -> CheckResult:
    if mrz_fields and document_type in _MRZ_TYPE_CODES:
        code = (mrz_fields.document_type_code or "")[:1]
        ok = code in _MRZ_TYPE_CODES[document_type]
        return CheckResult(
            name="document_type",
            status=CheckStatus.passed if ok else CheckStatus.review,
            reasons=[] if ok else ["DOCUMENT_TYPE_MISMATCH"],
            details={"source": "mrz", "mrz_code": mrz_fields.document_type_code},
        )

    found = visual.document_types
    if not found:
        return CheckResult(
            name="document_type",
            status=CheckStatus.passed,
            warnings=["DOCUMENT_TYPE_NOT_CONFIRMED"],
            details={"source": "visual", "found": []},
        )
    ok = document_type.value in found
    return CheckResult(
        name="document_type",
        status=CheckStatus.passed if ok else CheckStatus.review,
        reasons=[] if ok else ["DOCUMENT_TYPE_MISMATCH"],
        details={"source": "visual", "found": found},
    )


def _validity_check(
    extracted: ExtractedFields,
    today: date,
    require_adult: bool = True,
    document_type: DocumentType | None = None,
) -> CheckResult:
    reasons, warnings = [], []
    age = None
    # A birth certificate does not expire, so a missing expiry date is not worth
    # reporting on one. On documents that should carry one, it is.
    expires = document_type != DocumentType.birth_certificate
    if extracted.expiry_date is None:
        if expires:
            # Some documents have no expiry date either: older Rwandan IDs.
            warnings.append("EXPIRY_DATE_NOT_FOUND")
    elif extracted.expiry_date < today:
        reasons.append("DOCUMENT_EXPIRED")

    dob = extracted.date_of_birth
    if dob is not None:
        age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))

    if not require_adult:
        # A child's check. Age is still read and reported, it just cannot fail.
        warnings.append("AGE_CHECK_NOT_REQUIRED")
    elif dob is None:
        warnings.append("AGE_NOT_VERIFIED")
    elif age < rules.MIN_AGE_YEARS:
        reasons.append("UNDERAGE")

    return CheckResult(
        name="validity",
        status=CheckStatus.failed if reasons else CheckStatus.passed,
        reasons=reasons,
        warnings=warnings,
        details={"expiry_date": extracted.expiry_date, "age": age, "adult_required": require_adult},
    )


def _country_check(requested: str, extracted: ExtractedFields) -> CheckResult:
    # Callers may send "RW" (our backend's format), "RWA" or "Rwanda".
    expected = to_alpha3(requested)
    found = extracted.issuing_country
    if expected is None:
        return CheckResult(
            name="issuing_country",
            status=CheckStatus.passed,
            warnings=["COUNTRY_NOT_RECOGNISED"],
            details={"requested": requested, "document": found},
        )
    if found is None:
        return CheckResult(
            name="issuing_country",
            status=CheckStatus.review,
            reasons=["COUNTRY_NOT_DETECTED"],
            details={"requested": requested, "expected": expected, "document": None},
        )
    ok = expected == found
    return CheckResult(
        name="issuing_country",
        status=CheckStatus.passed if ok else CheckStatus.review,
        reasons=[] if ok else ["COUNTRY_MISMATCH"],
        details={"requested": requested, "expected": expected, "document": found},
    )


def _normalize_name(value: str | None) -> list[str]:
    if not value:
        return []
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    # Single letters (initials, or OCR noise in the MRZ name) are ignored.
    return [token for token in re.sub(r"[^A-Z]+", " ", ascii_value.upper()).split() if len(token) > 1]


def _name_check(applicant: Applicant, extracted: ExtractedFields, all_lines: list[str], use_mrz: bool) -> CheckResult:
    provided = _normalize_name(applicant.first_name) + _normalize_name(applicant.last_name)
    if use_mrz:
        on_document = _normalize_name(extracted.surname) + _normalize_name(extracted.given_names)
    else:
        # Without an MRZ, look for the names anywhere in the printed text. This works
        # on any layout, without knowing where each country prints the name.
        on_document = _normalize_name(" ".join(line for line in all_lines if "<<" not in line))

    # Every name the applicant gave must appear on the document, in any order
    # (first/last are often swapped). Middle names they left out are fine.
    # A prefix match covers names the MRZ truncated for lack of space.
    def token_found(token: str) -> bool:
        return any(
            fuzz.ratio(token, doc_token) >= rules.NAME_TOKEN_MATCH_SCORE
            or (use_mrz and len(doc_token) >= 4 and token.startswith(doc_token))
            for doc_token in on_document
        )

    matched = [token for token in provided if token_found(token)]
    ratio = len(matched) / len(provided) if provided else 0
    if ratio == 1:
        status, reasons = CheckStatus.passed, []
    elif ratio >= 0.5:
        status, reasons = CheckStatus.review, ["NAME_PARTIAL_MATCH"]
    else:
        status, reasons = CheckStatus.failed, ["NAME_MISMATCH"]
    return CheckResult(
        name="name_match",
        status=status,
        reasons=reasons,
        details={
            "source": "mrz" if use_mrz else "visual",
            "provided": " ".join(provided),
            "document": extracted.full_name,
            "unmatched": [token for token in provided if token not in matched],
        },
    )


def _dob_check(expected: date, extracted: ExtractedFields, visual: VisualFields, use_mrz: bool) -> CheckResult:
    if use_mrz:
        ok = expected == extracted.date_of_birth
        status, reasons = (CheckStatus.passed, []) if ok else (CheckStatus.failed, ["DOB_MISMATCH"])
    elif expected in visual.all_dates:
        status, reasons = CheckStatus.passed, []
    elif extracted.date_of_birth is not None:
        status, reasons = CheckStatus.failed, ["DOB_MISMATCH"]
    else:
        status, reasons = CheckStatus.review, ["DOB_NOT_FOUND"]
    return CheckResult(
        name="date_of_birth_match",
        status=status,
        reasons=reasons,
        details={"provided": expected, "document": extracted.date_of_birth},
    )
