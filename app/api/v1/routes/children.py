"""A child verified on the back of a verified parent.

A child never verifies themselves. An adult does, in the ordinary way, with
their own document and their own face. Their birth certificate is then a second
document that proves a *link* rather than an identity, and it is checked for
four things:

  1. it reads as a birth certificate,
  2. the parent's **verified** names appear on it,
  3. the child's names appear on it, and their date of birth matches,
  4. the country on it matches the country on the parent's document.

The parent's names are read off their own verification, never from this request.
A caller can say which parent record to use; they cannot say who the parent is.

Country is the one check that goes to a person rather than failing outright: a
child born while the family lived abroad has a genuine certificate from another
country, and refusing that outright costs a real customer.
"""

import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import account_from_api_key
from app.db.models import (
    Account,
    DocumentScan,
    FileKind,
    StepStatus,
    Verification,
    VerificationStatus,
    VerificationStep,
)
from app.db.session import get_session
from app.engines.fields import detect_country, to_alpha3
from app.engines.image import InvalidImageError, decode_image
from app.engines.ocr import read_document
from app.pipeline.document import names_on_document, scan_document
from app.rules import document as document_rules
from app.schemas.document import Applicant as ApplicantDetails
from app.schemas.document import CheckResult, CheckStatus, Decision, DocumentType
from app.schemas.verification import ChildVerificationResponse, VerifyChildRequest
from app.services import billing
from app.services import verifications as service
from app.storage import evidence
from app.db.models import VerificationFile

router = APIRouter(tags=["children"])


@router.post(
    "/verifications/{reference}/children",
    response_model=ChildVerificationResponse,
    status_code=status.HTTP_201_CREATED,
)
def verify_child(
    reference: str,
    request: Request,
    first_name: str = Form(...),
    last_name: str = Form(...),
    date_of_birth: str = Form(..., description="YYYY-MM-DD, checked against the certificate"),
    certificate: UploadFile = File(..., description="The child's birth certificate"),
    back: UploadFile | None = File(None, description="Back of the certificate, if it has one"),
    relationship: str | None = Form(None, description="parent, guardian"),
    account: Account = Depends(account_from_api_key),
    db: Session = Depends(get_session),
) -> ChildVerificationResponse:
    """Add a child to a parent who has already passed."""
    body = VerifyChildRequest(
        first_name=first_name,
        last_name=last_name,
        date_of_birth=date_of_birth,
        relationship=relationship,
    )

    parent = service.by_reference(db, account.id, reference)
    if parent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No verification with that reference")
    if parent.status != VerificationStatus.passed:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"The parent's verification is {parent.status.value}. A child can only be added to one that passed.",
        )
    if parent.applicant is None or not (parent.applicant.first_name or parent.applicant.last_name):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The parent's verification proved no name, so there is nothing to match on the certificate",
        )

    front_bytes = _read(certificate, "certificate")
    back_bytes = _read(back, "back") if back else None

    # A verification of its own, belonging to the child and pointing at the
    # parent's. It is a verification because that is what it is: its own
    # document, its own verdict, its own evidence.
    # A child's verification takes a unit of its own, like any other.
    try:
        child = service.create_verification(
            db,
            account.id,
            base_url=str(request.base_url),
            services=[VerificationStep.document.value],
            claimed=body.model_dump(mode="json", exclude_none=True),
            country=parent.applicant.country,
            language=parent.language,
        )
    except billing.OutOfCredit as refused:
        db.rollback()
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(refused))
    child.parent_verification_id = parent.id
    db.flush()

    step = service.open_step(db, child, VerificationStep.document)
    started = time.perf_counter()

    # The certificate is read as the child's document: their names and date of
    # birth are checked by the same pipeline an adult's document goes through,
    # minus the age rule, which a child by definition cannot pass.
    result = scan_document(
        document_type=DocumentType.birth_certificate,
        front=front_bytes,
        back=back_bytes,
        applicant=ApplicantDetails(
            first_name=body.first_name,
            last_name=body.last_name,
            date_of_birth=body.date_of_birth,
        ),
        require_adult=False,
    )

    lines = _lines(front_bytes) + (_lines(back_bytes) if back_bytes else [])
    checks = list(result.checks)
    checks.append(_parent_named(parent, lines))
    checks.append(_country_agrees(parent, lines))

    reasons = list(result.reasons)
    warnings = list(result.warnings)
    for check in checks[len(result.checks):]:
        reasons.extend(check.reasons)
        warnings.extend(check.warnings)

    decision = _decide(checks, result.decision)
    extracted = result.extracted.model_dump(mode="json") if result.extracted else {}

    db.add(
        DocumentScan(
            step_id=step.id,
            verification_id=child.id,
            applicant_id=child.applicant_id,
            document_type=DocumentType.birth_certificate,
            decision=decision,
            reasons=reasons,
            warnings=warnings,
            checks=[check.model_dump(mode="json") for check in checks],
            processing_ms=int((time.perf_counter() - started) * 1000),
            **{key: value for key, value in extracted.items() if key != "source"},
            source=extracted.get("source"),
        )
    )
    _keep(db, child, step.id, FileKind.document_front, front_bytes, certificate.content_type)
    if back_bytes:
        _keep(db, child, step.id, FileKind.document_back, back_bytes, back.content_type)

    service.finish_step(
        db,
        step,
        status={
            Decision.passed: StepStatus.passed,
            Decision.review: StepStatus.review,
        }.get(decision, StepStatus.failed),
        reasons=reasons,
        warnings=warnings,
        result={"checks": [check.model_dump(mode="json") for check in checks]},
        extracted=extracted,
    )
    child = service.settle(db, child)

    return ChildVerificationResponse(
        reference=child.reference,
        parent_reference=parent.reference,
        customer_id=child.applicant.customer_id if child.applicant else None,
        status=child.status.value,
        decision=decision.value,
        reasons=list(dict.fromkeys(reasons)),
        warnings=list(dict.fromkeys(warnings)),
        checks=[check.model_dump(mode="json") for check in checks],
        extracted=extracted,
        created_at=child.created_at,
    )


def _parent_named(parent: Verification, lines: list[str]) -> CheckResult:
    """Is the verified adult named on this certificate?

    Either parent's side counts: a certificate names a mother and a father, and
    which of them is standing in front of the camera is not our business. A
    guardian who is on neither side cannot be linked this way, and that is a
    failure rather than a question for a person, because there is nothing on the
    document for anybody to look at.
    """
    names = [parent.applicant.first_name, parent.applicant.last_name]
    found, missing = names_on_document(names, lines)
    ok = bool(found) and not missing
    return CheckResult(
        name="parent_named_on_certificate",
        status=CheckStatus.passed if ok else CheckStatus.failed,
        reasons=[] if ok else ["PARENT_NOT_ON_CERTIFICATE"],
        details={"looked_for": " ".join(n for n in names if n), "matched": found, "missing": missing},
    )


def _country_agrees(parent: Verification, lines: list[str]) -> CheckResult:
    """Does the certificate come from the same country as the parent's document?

    A mismatch goes to a person rather than failing: a child born while the
    family lived abroad has a genuine certificate from somewhere else, and that
    is common enough in this part of the world to matter.
    """
    on_certificate = to_alpha3(detect_country(lines))
    parent_country = to_alpha3(parent.applicant.country)

    if on_certificate is None:
        return CheckResult(
            name="certificate_country",
            status=CheckStatus.review,
            reasons=["CERTIFICATE_COUNTRY_NOT_DETECTED"],
            details={"parent_country": parent_country},
        )
    if parent_country is None:
        return CheckResult(
            name="certificate_country",
            status=CheckStatus.review,
            warnings=["PARENT_COUNTRY_UNKNOWN"],
            details={"certificate_country": on_certificate},
        )

    same = on_certificate == parent_country
    return CheckResult(
        name="certificate_country",
        status=CheckStatus.passed if same else CheckStatus.review,
        reasons=[] if same else ["CERTIFICATE_COUNTRY_MISMATCH"],
        details={"certificate_country": on_certificate, "parent_country": parent_country},
    )


def _decide(checks: list[CheckResult], document_decision: Decision) -> Decision:
    if document_decision == Decision.retake:
        return Decision.retake
    if any(check.status == CheckStatus.failed for check in checks):
        return Decision.failed
    if any(check.status == CheckStatus.review for check in checks):
        return Decision.review
    return Decision.passed


def _lines(data: bytes) -> list[str]:
    try:
        return [line.text for line in read_document(decode_image(data))]
    except InvalidImageError:
        return []


def _read(upload: UploadFile, field: str) -> bytes:
    if upload.content_type not in document_rules.ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"{field} must be one of: {', '.join(sorted(document_rules.ALLOWED_CONTENT_TYPES))}",
        )
    data = upload.file.read(document_rules.MAX_UPLOAD_BYTES + 1)
    if len(data) > document_rules.MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"{field} is larger than 10 MB")
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{field} is empty")
    return data


def _keep(db: Session, verification: Verification, step_id: int, kind: FileKind,
          data: bytes, content_type: str) -> None:
    stored = evidence.keep(verification.id, data, kind.value, content_type)
    if stored is None:
        return
    db.add(
        VerificationFile(
            verification_id=verification.id,
            step_id=step_id,
            kind=kind,
            storage_key=stored.storage_key,
            content_type=stored.content_type,
            byte_size=stored.byte_size,
            sha256=stored.sha256,
        )
    )
