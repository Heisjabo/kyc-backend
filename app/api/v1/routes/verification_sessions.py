"""Opening a verification, and following it.

Two audiences, two kinds of access:

  * **The client**, holding an API key, opens verifications and reads their
    status. Everything they touch is scoped to their account.
  * **The person being verified**, holding only the session link, sees a thin
    view of their own session: where they are and what is next. No reasons, no
    extracted fields, nothing about the client. Anybody with the link holds it,
    which is what lets them carry on from their phone.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import Caller, account_from_api_key, current_caller
from app.api.v1.routes import console_verifications as views
from app.db.models import (
    Account,
    FileKind,
    StepStatus,
    Verification,
    VerificationFile,
    VerificationStep,
)
from app.db.session import get_session
from app.schemas.console import ConsoleVerificationList
from app.schemas.verification import (
    DetailStep,
    VerificationDetailResponse,
    CreateVerificationRequest,
    SessionResponse,
    StepResponse,
    VerificationResponse,
)
from app.reports import verification as report
from app.services import due_diligence as forms
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
    # A form_id is how Shufti's clients ask for the questionnaire, so sending one
    # is enough: the due diligence step is added for them, ahead of AML.
    if body.form_id and VerificationStep.due_diligence.value not in body.services:
        at = body.services.index('aml') if 'aml' in body.services else len(body.services)
        body.services.insert(at, VerificationStep.due_diligence.value)
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

    if VerificationStep.due_diligence.value in body.services:
        if not body.form_id:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "form_id is required when the due_diligence service is requested",
            )
        form = forms.by_public_id(db, body.form_id)
        # A form that does not exist and one this client was not given read the
        # same, so a client cannot find out which form ids exist.
        if form is None or form.status != "active" or not forms.usable_by(db, form, account.id):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"No active due diligence form {body.form_id!r} on this account",
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


# Who reads a verification by reference: the client it belongs to (its API key or
# its dashboard session), or Pangolin staff naming the client, since a reference
# is only unique within one client.
_CLIENT_ID = Query(default=None, description="Pangolin staff only: the client the reference belongs to")
PANGOLIN = "Pangolin compliance"


def _find(db: Session, caller: Caller, reference: str, client_id: int | None) -> Verification:
    account_id = caller.scope_for(client_id)
    if account_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Give client_id: a reference is only unique within one client")
    verification = service.by_reference(db, account_id, reference)
    if verification is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No verification with that reference")
    return verification


@router.get("/verifications", response_model=ConsoleVerificationList)
def list_verifications(
    status_: str | None = Query(default=None, alias="status", description="pending, in_progress, passed, failed, needs_review or expired"),
    q: str | None = Query(default=None, max_length=120, description="Reference, applicant name, email or customer id"),
    created_from: date | None = Query(default=None, description="Created on or after this day (UTC)"),
    created_to: date | None = Query(default=None, description="Created on or before this day (UTC)"),
    client_id: int | None = Query(default=None, description="Pangolin staff only: one client's verifications"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    caller: Caller = Depends(current_caller),
    db: Session = Depends(get_session),
) -> ConsoleVerificationList:
    """Verifications newest first, with a count per status. A client gets its own;
    Pangolin staff get every client's."""
    return views.verification_list(
        db,
        status_=status_,
        client_id=caller.scope_for(client_id),
        q=q,
        created_from=created_from,
        created_to=created_to,
        page=page,
        page_size=page_size,
    )


@router.get("/verifications/{reference}", response_model=VerificationDetailResponse)
def read_verification(
    reference: str,
    client_id: int | None = _CLIENT_ID,
    caller: Caller = Depends(current_caller),
    db: Session = Depends(get_session),
) -> VerificationDetailResponse:
    """How one customer got on, in full: every field this endpoint has always
    returned, with what was read from the document, the checks each step ran and
    every file captured."""
    verification = service.expire_if_due(db, _find(db, caller, reference, client_id))
    base = _as_response(verification)
    full = views.verification_detail(db, verification)
    extracted = {step.step: step.extracted for step in base.steps}
    reviews = full.reviews or []
    if not caller.staff:
        # What was decided is the client's to see; which staff member decided it is not.
        for review in reviews:
            review.by = PANGOLIN
    return VerificationDetailResponse(
        **base.model_dump(exclude={"steps"}),
        id=full.id,
        document_type=full.document_type,
        client=full.client,
        applicant=full.applicant,
        started_at=full.started_at,
        steps=[DetailStep(**step.model_dump(), extracted=extracted.get(step.step, {})) for step in full.steps],
        document=full.document,
        files=full.files,
        evidence=full.evidence,
        reviews=reviews,
    )


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
    client_id: int | None = _CLIENT_ID,
    caller: Caller = Depends(current_caller),
    db: Session = Depends(get_session),
):
    """Fetch the raw image for a verified document or selfie."""
    verification = _find(db, caller, reference, client_id)

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


@router.get(
    "/verifications/{reference}/report",
    response_class=Response,
    responses={
        200: {"content": {"application/pdf": {}}, "description": "The verification report as a PDF"},
        404: {"description": "No verification with that reference"},
    },
)
def read_verification_report(
    reference: str,
    client_id: int | None = _CLIENT_ID,
    caller: Caller = Depends(current_caller),
    db: Session = Depends(get_session),
) -> Response:
    """The verification report, as a PDF: what Shufti calls the verification proof.

    Built from what was recorded at the moment it is asked for, so it works for
    any status: a declined or unfinished verification reads that way, with its
    reasons and the steps nobody reached. Asking for it writes nothing.
    """
    verification = _find(db, caller, reference, client_id)
    return Response(
        content=report.build(db, verification),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{report.filename(verification)}"'},
    )


@router.get("/verifications/{reference}/files")
def list_verification_files(
    reference: str,
    client_id: int | None = _CLIENT_ID,
    caller: Caller = Depends(current_caller),
    db: Session = Depends(get_session),
):
    """List all available evidence files for a verification."""
    verification = _find(db, caller, reference, client_id)

    files = db.scalars(
        select(VerificationFile)
        .where(VerificationFile.verification_id == verification.id)
        .order_by(VerificationFile.id.asc())
    ).all()

    return {
        f.kind.value: f"/v1/verifications/{reference}/files/{f.kind.value}"
        for f in files
    }


@router.get("/verifications/{reference}/evidence/{file_id}")
def read_evidence(
    reference: str,
    file_id: int,
    client_id: int | None = _CLIENT_ID,
    caller: Caller = Depends(current_caller),
    db: Session = Depends(get_session),
) -> Response:
    """One captured file by its id, as listed in the verification's `evidence`:
    retakes included, where files/{kind} gives only the latest of a kind."""
    return views.evidence_response(db, _find(db, caller, reference, client_id).id, file_id)


@router.get("/verifications/{reference}/evidence/{file_id}/thumbnail")
def read_evidence_thumbnail(
    reference: str,
    file_id: int,
    client_id: int | None = _CLIENT_ID,
    caller: Caller = Depends(current_caller),
    db: Session = Depends(get_session),
) -> Response:
    """A still JPEG from a captured video, for showing it before it is played."""
    return views.thumbnail_response(db, _find(db, caller, reference, client_id).id, file_id)


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

    # A step flagged for review is done as far as the person is concerned.
    done = [name for name in verification.services
            if latest.get(VerificationStep(name)) and latest[VerificationStep(name)].status in (StepStatus.passed, StepStatus.review)]
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
