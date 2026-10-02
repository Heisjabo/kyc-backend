"""Opening a verification, and following it.

Two audiences, two kinds of access:

  * **The client**, holding an API key, opens verifications and reads their
    status. Everything they touch is scoped to their account.
  * **The person being verified**, holding only the session link, sees a thin
    view of their own session: where they are and what is next. No reasons, no
    extracted fields, nothing about the client. Anybody with the link holds it,
    which is what lets them carry on from their phone.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import account_from_api_key
from app.db.models import (
    Account,
    FileKind,
    StepStatus,
    Verification,
    VerificationFile,
    VerificationStep,
)
from app.db.session import get_session
from app.schemas.verification import (
    CreateVerificationRequest,
    SessionResponse,
    StepResponse,
    VerificationResponse,
)
from app.services import verifications as service
from app.storage import evidence

router = APIRouter(tags=["verifications"])

_KNOWN_SERVICES = {step.value for step in VerificationStep}


def _as_response(verification: Verification) -> VerificationResponse:
    latest: dict = {}
    for step in verification.steps:
        current = latest.get(step.step)
        if current is None or step.attempt > current.attempt:
            latest[step.step] = step

    from app.core.config import settings
    session_url = (
        f"{settings.VERIFY_BASE_URL.strip().rstrip('/')}/verify/{verification.session_token}"
        if settings.VERIFY_BASE_URL
        else verification.session_url
    )

    return VerificationResponse(
        reference=verification.reference,
        customer_id=verification.applicant.customer_id if verification.applicant else None,
        status=verification.status.value,
        event=verification.event,
        services=verification.services,
        session_url=session_url,
        country=verification.country,
        language=verification.language,
        reasons=verification.reasons,
        warnings=verification.warnings,
        aml=latest[VerificationStep.aml].result or None if VerificationStep.aml in latest else None,
        steps=[
            StepResponse(
                step=step.step.value,
                attempt=step.attempt,
                status=step.status.value,
                reasons=step.reasons,
                warnings=step.warnings,
                extracted=step.extracted,
                started_at=step.started_at,
                completed_at=step.completed_at,
            )
            for step in sorted(latest.values(), key=lambda s: verification.services.index(s.step.value)
                               if s.step.value in verification.services else 99)
        ],
        expires_at=verification.expires_at,
        created_at=verification.created_at,
        completed_at=verification.completed_at,
    )


@router.post("/verifications", response_model=VerificationResponse, status_code=status.HTTP_201_CREATED)
def create_verification(
    body: CreateVerificationRequest,
    request: Request,
    account: Account = Depends(account_from_api_key),
    db: Session = Depends(get_session),
) -> VerificationResponse:
    """Open a verification and get back the link to send the person.

    The record exists from this moment, before they have done anything, so every
    step, image and callback that follows has something to hang off.
    """
    # Existing clients that send document + face receive the final AML check too.
    if 'document' in body.services and 'face' in body.services and 'aml' not in body.services:
        body.services.append('aml')
    if 'aml' in body.services:
        if (len(set(body.services)) != len(body.services)
                or any(name not in body.services for name in ('document', 'face'))
                or body.services.index('document') >= body.services.index('face')
                or body.services[-1] != 'aml'):
            raise HTTPException(422, 'AML requires document before face and must be the final service, without duplicates')
    unknown = [name for name in body.services if name not in _KNOWN_SERVICES]
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Unknown services: {', '.join(unknown)}. Known: {', '.join(sorted(_KNOWN_SERVICES))}",
        )
    if not body.services:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "At least one service is required")

    # The address step has nothing to check a document against unless the client
    # says where the person lives. Refused here rather than at the step, where the
    # person would be holding a bill and be told the request was wrong all along.
    if VerificationStep.address.value in body.services and not (body.applicant.address or "").strip():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "applicant.address is required when the address service is requested",
        )

    claimed = body.applicant.model_dump(mode="json", exclude_none=True)
    try:
        verification = service.create_verification(
            db,
            account.id,
            base_url=str(request.base_url),
            services=body.services,
            claimed=claimed,
            reference=body.reference,
            callback_url=body.callback_url,
            redirect_url=body.redirect_url,
            country=body.country,
            language=body.language,
            verification_mode=body.verification_mode,
            form_id=body.form_id,
            ttl_minutes=body.ttl,
            settings_echo=body.model_dump(mode="json", exclude={"applicant"}, exclude_none=True),
        )
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"reference {body.reference!r} has already been used on this account",
        )
    return _as_response(verification)


@router.get("/verifications/{reference}", response_model=VerificationResponse)
def read_verification(
    reference: str,
    account: Account = Depends(account_from_api_key),
    db: Session = Depends(get_session),
) -> VerificationResponse:
    """How one of this client's customers got on."""
    verification = service.by_reference(db, account.id, reference)
    if verification is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No verification with that reference")
    return _as_response(service.expire_if_due(db, verification))


_KIND_ALIASES = {
    "front": FileKind.document_front,
    "back": FileKind.document_back,
    "selfie": FileKind.portrait,
    "portrait": FileKind.portrait,
    "document_front": FileKind.document_front,
    "document_back": FileKind.document_back,
    "document_portrait": FileKind.document_portrait,
}


@router.get("/verifications/{reference}/files/{kind}")
def read_verification_file(
    reference: str,
    kind: str,
    account: Account = Depends(account_from_api_key),
    db: Session = Depends(get_session),
):
    """Fetch the raw image for a verified document or selfie."""
    verification = service.by_reference(db, account.id, reference)
    if verification is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No verification with that reference")

    file_kind = _KIND_ALIASES.get(kind.lower())
    if file_kind is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown file kind {kind!r}")

    file = db.scalar(
        select(VerificationFile)
        .where(
            VerificationFile.verification_id == verification.id,
            VerificationFile.kind == file_kind,
        )
        .order_by(VerificationFile.id.desc())
    )
    if file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No {kind} file recorded for this verification")

    data = evidence.read(file.storage_key)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That file is no longer stored")
    return Response(content=data, media_type=file.content_type)


@router.get("/verifications/{reference}/files")
def list_verification_files(
    reference: str,
    account: Account = Depends(account_from_api_key),
    db: Session = Depends(get_session),
):
    """List all available evidence files for a verification."""
    verification = service.by_reference(db, account.id, reference)
    if verification is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No verification with that reference")

    files = db.scalars(
        select(VerificationFile)
        .where(VerificationFile.verification_id == verification.id)
        .order_by(VerificationFile.id.asc())
    ).all()

    return {
        f.kind.value: f"/v1/verifications/{reference}/files/{f.kind.value}"
        for f in files
    }


@router.get("/sessions/{token}/document-portrait")
def session_document_portrait(token: str, db: Session = Depends(get_session)):
    """The face cut from the document this session already read.

    Served on the link rather than an API key, because it is shown to the person
    it belongs to, beside the photograph they are about to take.
    """
    verification = service.by_token(db, token)
    if verification is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That link is not valid")

    file = db.scalar(
        select(VerificationFile)
        .where(
            VerificationFile.verification_id == verification.id,
            VerificationFile.kind == FileKind.document_portrait,
        )
        .order_by(VerificationFile.id.desc())
    )
    if file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No portrait has been read from a document yet")

    data = evidence.read(file.storage_key)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That file is no longer stored")
    return Response(content=data, media_type=file.content_type)


@router.get("/sessions/{token}", response_model=SessionResponse)
def read_session(token: str, db: Session = Depends(get_session)) -> SessionResponse:
    """What the page in front of the person needs, and nothing more.

    The desktop polls this while the phone works, which is how a session handed
    over by QR code carries on where it left off.
    """
    verification = service.by_token(db, token)
    if verification is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That link is not valid")
    verification = service.expire_if_due(db, verification)

    latest: dict = {}
    for step in verification.steps:
        current = latest.get(step.step)
        if current is None or step.attempt > current.attempt:
            latest[step.step] = step

    done = [name for name in verification.services
            if latest.get(VerificationStep(name)) and latest[VerificationStep(name)].status == StepStatus.passed]
    outstanding = [name for name in verification.services if name not in done]

    return SessionResponse(
        reference=verification.reference,
        status=verification.status.value,
        services=verification.services,
        next_step=outstanding[0] if outstanding and verification.status.value != "needs_review" else None,
        completed_steps=done,
        expired=service.is_expired(verification),
        redirect_url=verification.redirect_url,
    )
