"""Every verification, for Pangolin staff in the admin console.

The client API shows a client its own verifications by reference. Staff need all
of them, across every client, so these endpoints are keyed by id (a reference is
only unique within one client) and open to console accounts with access to
Pangolin KYC.

A session whose link has run out is reported as expired even before anyone has
looked at it, by working the status out in the query rather than waiting for the
row to be updated. Listing writes nothing.
"""

import tempfile
from datetime import date, datetime, time, timedelta, timezone

import cv2
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import String, and_, case, cast, func, literal, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import console_product
from app.db.models import (
    Account,
    Applicant,
    DocumentScan,
    Verification,
    VerificationFile,
    VerificationStatus,
)
from app.db.session import get_session
from app.schemas.console import (
    ConsoleApplicant,
    ConsoleCheck,
    ConsoleClient,
    ConsoleDocument,
    ConsoleFile,
    ConsoleStep,
    ConsoleVerificationDetail,
    ConsoleVerificationList,
    ConsoleVerificationSummary,
)
from app.services import verifications as service
from app.storage import evidence as evidence_store

router = APIRouter(prefix="/console/verifications", tags=["admin console"])

_OPEN = (VerificationStatus.pending, VerificationStatus.in_progress)
_STATUSES = [s.value for s in VerificationStatus]

# The status as a client would see it now: an open session past its expiry is expired.
_EFFECTIVE_STATUS = case(
    (
        and_(
            Verification.status.in_(_OPEN),
            Verification.expires_at.is_not(None),
            Verification.expires_at < func.now(),
        ),
        literal(VerificationStatus.expired.value),
    ),
    else_=cast(Verification.status, String),
)


def _effective(verification: Verification) -> str:
    if verification.status in _OPEN and service.is_expired(verification):
        return VerificationStatus.expired.value
    return verification.status.value


def _client(account: Account) -> ConsoleClient:
    return ConsoleClient(
        id=str(account.id),
        name=f"{account.first_name} {account.last_name}".strip(),
        email=account.email,
    )


def _applicant(verification: Verification, scan: DocumentScan | None) -> ConsoleApplicant:
    applicant = verification.applicant
    claimed = verification.claimed or {}
    first = (applicant.first_name if applicant else None) or claimed.get("first_name")
    last = (applicant.last_name if applicant else None) or claimed.get("last_name")
    name = " ".join(part for part in (first, last) if part) or (scan.full_name if scan else None)
    return ConsoleApplicant(
        customer_id=applicant.customer_id if applicant else None,
        name=name,
        email=(applicant.email if applicant else None) or claimed.get("email"),
        country=(applicant.country if applicant else None) or verification.country,
    )


def _summary(verification: Verification, scan: DocumentScan | None) -> ConsoleVerificationSummary:
    return ConsoleVerificationSummary(
        id=str(verification.id),
        reference=verification.reference,
        status=_effective(verification),
        event=verification.event,
        services=verification.services,
        document_type=scan.document_type.value if scan else None,
        reasons=verification.reasons,
        client=_client(verification.account),
        applicant=_applicant(verification, scan),
        created_at=verification.created_at,
        completed_at=verification.completed_at,
        expires_at=verification.expires_at,
    )


def _latest_scans(db: Session, verification_ids: list[int]) -> dict[int, DocumentScan]:
    """The most recent document scan of each verification, if it has one."""
    if not verification_ids:
        return {}
    scans = db.scalars(
        select(DocumentScan)
        .where(DocumentScan.verification_id.in_(verification_ids))
        .order_by(DocumentScan.verification_id, DocumentScan.created_at.desc())
    )
    latest: dict[int, DocumentScan] = {}
    for scan in scans:
        latest.setdefault(scan.verification_id, scan)
    return latest


@router.get("", response_model=ConsoleVerificationList)
def list_verifications(
    status_: str | None = Query(default=None, alias="status", description="One of " + ", ".join(_STATUSES)),
    client_id: int | None = Query(default=None, description="Only this client account's verifications"),
    q: str | None = Query(default=None, max_length=120, description="Reference, applicant name, email or customer id"),
    created_from: date | None = Query(default=None, description="Created on or after this day (UTC)"),
    created_to: date | None = Query(default=None, description="Created on or before this day (UTC)"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    _: Account = Depends(console_product("kyc")),
    db: Session = Depends(get_session),
) -> ConsoleVerificationList:
    """Every client's verifications, newest first."""
    if status_ is not None and status_ not in _STATUSES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Status must be one of " + ", ".join(_STATUSES))

    filters = []
    if client_id is not None:
        filters.append(Verification.account_id == client_id)
    if created_from is not None:
        filters.append(Verification.created_at >= datetime.combine(created_from, time.min, tzinfo=timezone.utc))
    if created_to is not None:
        filters.append(Verification.created_at < datetime.combine(created_to + timedelta(days=1), time.min, tzinfo=timezone.utc))
    if q and q.strip():
        like = f"%{q.strip()}%"
        filters.append(
            or_(
                Verification.reference.ilike(like),
                Applicant.customer_id.ilike(like),
                Applicant.email.ilike(like),
                func.concat_ws(" ", Applicant.first_name, Applicant.last_name).ilike(like),
                Verification.claimed["email"].astext.ilike(like),
                func.concat_ws(" ", Verification.claimed["first_name"].astext, Verification.claimed["last_name"].astext).ilike(like),
            )
        )

    base = select(Verification).outerjoin(Applicant, Verification.applicant_id == Applicant.id).where(*filters)

    # Counts per status under every filter but the status itself.
    counted = db.execute(
        select(_EFFECTIVE_STATUS.label("s"), func.count())
        .select_from(Verification)
        .outerjoin(Applicant, Verification.applicant_id == Applicant.id)
        .where(*filters)
        .group_by("s")
    ).all()
    status_counts = {s: 0 for s in _STATUSES} | {row[0]: row[1] for row in counted}

    listed = base.where(_EFFECTIVE_STATUS == status_) if status_ else base
    total = db.scalar(select(func.count()).select_from(listed.subquery())) or 0
    rows = db.scalars(
        listed.options(selectinload(Verification.applicant), selectinload(Verification.account))
        .order_by(Verification.created_at.desc(), Verification.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    scans = _latest_scans(db, [v.id for v in rows])
    return ConsoleVerificationList(
        items=[_summary(v, scans.get(v.id)) for v in rows],
        total=total,
        page=page,
        page_size=page_size,
        status_counts=status_counts,
    )


@router.get("/{verification_id}", response_model=ConsoleVerificationDetail)
def read_verification(
    verification_id: int,
    _: Account = Depends(console_product("kyc")),
    db: Session = Depends(get_session),
) -> ConsoleVerificationDetail:
    """One verification in full: its steps, their checks, what was read, and
    every file captured along the way."""
    verification = db.get(Verification, verification_id)
    if verification is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No verification with that id")
    # The same as the client API does when a verification is looked at.
    verification = service.expire_if_due(db, verification)

    latest: dict = {}
    for step in verification.steps:
        current = latest.get(step.step)
        if current is None or step.attempt > current.attempt:
            latest[step.step] = step
    order = verification.services
    steps = sorted(latest.values(), key=lambda s: order.index(s.step.value) if s.step.value in order else 99)

    scan = _latest_scans(db, [verification.id]).get(verification.id)
    stored = db.scalars(
        select(VerificationFile)
        .where(VerificationFile.verification_id == verification.id)
        .options(selectinload(VerificationFile.step))
        .order_by(VerificationFile.id.asc())
    ).all()

    summary = _summary(verification, scan)
    return ConsoleVerificationDetail(
        **summary.model_dump(),
        language=verification.language,
        warnings=verification.warnings,
        started_at=verification.started_at,
        steps=[
            ConsoleStep(
                step=step.step.value,
                attempt=step.attempt,
                status=step.status.value,
                reasons=step.reasons,
                warnings=step.warnings,
                checks=[
                    ConsoleCheck(
                        name=check.get("name", ""),
                        status=str(check.get("status", "")),
                        reasons=check.get("reasons") or [],
                        warnings=check.get("warnings") or [],
                    )
                    for check in (step.result or {}).get("checks", [])
                    if isinstance(check, dict)
                ],
                match_score=(step.result or {}).get("match_score"),
                match_threshold=(step.result or {}).get("threshold"),
                started_at=step.started_at,
                completed_at=step.completed_at,
            )
            for step in steps
        ],
        document=(
            ConsoleDocument(
                document_type=scan.document_type.value,
                decision=scan.decision.value,
                full_name=scan.full_name,
                date_of_birth=scan.date_of_birth,
                document_number_last4=scan.document_number[-4:] if scan.document_number else None,
                issuing_country=scan.issuing_country,
                nationality=scan.nationality,
                expiry_date=scan.expiry_date,
            )
            if scan
            else None
        ),
        files=sorted({f.kind.value for f in stored}),
        evidence=[
            ConsoleFile(
                id=str(f.id),
                kind=f.kind.value,
                step=f.step.step.value if f.step else None,
                attempt=f.step.attempt if f.step else None,
                content_type=f.content_type,
                byte_size=f.byte_size,
                captured_at=f.captured_at,
                created_at=f.created_at,
            )
            for f in stored
        ],
    )


def _evidence_file(db: Session, verification_id: int, file_id: int) -> VerificationFile:
    """A file of this verification, or 404. A file id from another verification is
    treated as missing, so ids cannot be walked across verifications."""
    file = db.get(VerificationFile, file_id)
    if file is None or file.verification_id != verification_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such file on this verification")
    return file


# Staff look at the same capture several times while deciding; a short private
# cache saves fetching it again, and nothing in between may keep a copy.
_PRIVATE_CACHE = {"Cache-Control": "private, max-age=300"}


@router.get("/{verification_id}/files/{file_id}")
def read_evidence_file(
    verification_id: int,
    file_id: int,
    _: Account = Depends(console_product("kyc")),
    db: Session = Depends(get_session),
) -> Response:
    """One captured file, as it was stored: an image, a proof of address or a video."""
    file = _evidence_file(db, verification_id, file_id)
    data = evidence_store.read(file.storage_key)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That file is no longer stored")
    return Response(content=data, media_type=file.content_type, headers=_PRIVATE_CACHE)


_VIDEO_SUFFIX = {"video/webm": ".webm", "video/mp4": ".mp4", "video/quicktime": ".mov"}


def _video_still(data: bytes, content_type: str, max_width: int = 480) -> bytes | None:
    """A JPEG frame from about a third of the way into a video, or None if it cannot
    be read. A third of the way in is past the first, often blank, frames and
    usually shows the person mid-challenge.

    OpenCV reads videos from a path, so the bytes go to a temporary file first.
    Browsers' WebM recordings often do not say how many frames they have, so the
    frames are counted by reading them when the container does not say.
    """
    suffix = _VIDEO_SUFFIX.get(content_type.split(";")[0].strip(), ".bin")
    with tempfile.NamedTemporaryFile(suffix=suffix) as handle:
        handle.write(data)
        handle.flush()
        capture = cv2.VideoCapture(handle.name)
        try:
            if not capture.isOpened():
                return None
            frames = []
            total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            if total > 0:
                capture.set(cv2.CAP_PROP_POS_FRAMES, total // 3)
                ok, frame = capture.read()
                frame = frame if ok else None
            else:
                while len(frames) < 600:
                    ok, read = capture.read()
                    if not ok:
                        break
                    frames.append(read)
                frame = frames[len(frames) // 3] if frames else None
        finally:
            capture.release()

    if frame is None:
        return None
    height, width = frame.shape[:2]
    if width > max_width:
        frame = cv2.resize(frame, (max_width, int(height * max_width / width)), interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 82])
    return encoded.tobytes() if ok else None


@router.get("/{verification_id}/files/{file_id}/thumbnail")
def read_evidence_thumbnail(
    verification_id: int,
    file_id: int,
    _: Account = Depends(console_product("kyc")),
    db: Session = Depends(get_session),
) -> Response:
    """A still JPEG from a captured video, for showing it before it is played."""
    file = _evidence_file(db, verification_id, file_id)
    if not file.content_type.startswith("video/"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Only videos have a thumbnail")
    data = evidence_store.read(file.storage_key)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That file is no longer stored")
    still = _video_still(data, file.content_type)
    if still is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No still could be taken from this video")
    return Response(content=still, media_type="image/jpeg", headers=_PRIVATE_CACHE)
