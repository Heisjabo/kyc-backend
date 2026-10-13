"""The client dashboard: one client account's own verifications, numbers, AML
screenings, API logs, API keys and due diligence forms.

Every endpoint is for the account signed in to the dashboard (a dashboard
session token, from POST /v1/auth/login), and every answer is about that account
only. They share their queries with the admin console's endpoints, which see
every client, by passing the account along; a verification, a file or a report
of another account is answered as missing, so ids cannot be walked across
accounts. Pangolin staff appear only as "Pangolin compliance", never by name.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import current_account
from app.api.v1.routes import api_keys as client_keys
from app.api.v1.routes import console_aml, console_api_keys, console_audit, console_metrics, console_verifications
from app.api.v1.routes.due_diligence_forms import response_list
from app.db.models import Account, ApiKey, AuditEvent, Step, StepStatus, Verification, VerificationStep
from app.db.session import get_session
from app.schemas.auth import ApiKeyResponse, CreateApiKeyRequest, CreatedApiKeyResponse
from app.schemas.console import (
    ConsoleAmlEntity,
    ConsoleAmlEntryList,
    ConsoleAnalyticsMetrics,
    ConsoleApiKeyList,
    ConsoleAuditEventList,
    ConsoleOverviewMetrics,
    ConsoleVerificationDetail,
    ConsoleVerificationList,
)
from app.schemas.due_diligence import DashboardForm, FormClient, ResponseList
from app.services import due_diligence as forms

router = APIRouter(prefix="/dashboard", tags=["client dashboard"])

PANGOLIN = "Pangolin compliance"


def _own(db: Session, account: Account, verification_id: int) -> Verification:
    verification = db.get(Verification, verification_id)
    if verification is None or verification.account_id != account.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such verification on this account")
    return verification


# Verifications


@router.get("/verifications", response_model=ConsoleVerificationList)
def list_verifications(
    status_: str | None = Query(default=None, alias="status", description="pending, in_progress, passed, failed, needs_review or expired"),
    q: str | None = Query(default=None, max_length=120, description="Reference, applicant name, email or customer id"),
    created_from: date | None = Query(default=None, description="Created on or after this day (UTC)"),
    created_to: date | None = Query(default=None, description="Created on or before this day (UTC)"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    account: Account = Depends(current_account),
    db: Session = Depends(get_session),
) -> ConsoleVerificationList:
    """This account's verifications, newest first, with a count per status."""
    return console_verifications.verification_list(
        db,
        status_=status_,
        client_id=account.id,
        q=q,
        created_from=created_from,
        created_to=created_to,
        page=page,
        page_size=page_size,
    )


@router.get("/verifications/{verification_id}", response_model=ConsoleVerificationDetail)
def read_verification(
    verification_id: int,
    account: Account = Depends(current_account),
    db: Session = Depends(get_session),
) -> ConsoleVerificationDetail:
    """One of this account's verifications in full: steps, checks, what was read,
    the files captured, and the decisions Pangolin's compliance team made on it."""
    detail = console_verifications.verification_detail(db, _own(db, account, verification_id))
    for review in detail.reviews or []:
        review.by = PANGOLIN
    return detail


@router.get(
    "/verifications/{verification_id}/report",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}, "description": "The verification report as a PDF"}},
)
def read_report(
    verification_id: int,
    account: Account = Depends(current_account),
    db: Session = Depends(get_session),
) -> Response:
    """The verification report, as a PDF."""
    return console_verifications.report_response(db, _own(db, account, verification_id))


@router.get("/verifications/{verification_id}/files/{file_id}")
def read_file(
    verification_id: int,
    file_id: int,
    account: Account = Depends(current_account),
    db: Session = Depends(get_session),
) -> Response:
    """One file captured on one of this account's verifications, as it was stored."""
    _own(db, account, verification_id)
    return console_verifications.evidence_response(db, verification_id, file_id)


@router.get("/verifications/{verification_id}/files/{file_id}/thumbnail")
def read_thumbnail(
    verification_id: int,
    file_id: int,
    account: Account = Depends(current_account),
    db: Session = Depends(get_session),
) -> Response:
    """A still from a video captured on one of this account's verifications."""
    _own(db, account, verification_id)
    return console_verifications.thumbnail_response(db, verification_id, file_id)


# Numbers


@router.get("/metrics/overview", response_model=ConsoleOverviewMetrics, response_model_by_alias=True)
def overview(
    from_: date | None = Query(default=None, alias="from", description="First day, YYYY-MM-DD (UTC)"),
    to: date | None = Query(default=None, description="Last day, YYYY-MM-DD (UTC), included"),
    account: Account = Depends(current_account),
    db: Session = Depends(get_session),
) -> ConsoleOverviewMetrics:
    """Volume, pass rate, completion time, outcomes and decline reasons of this account's verifications."""
    return console_metrics.overview_metrics(db, from_, to, account_id=account.id)


@router.get("/metrics/analytics", response_model=ConsoleAnalyticsMetrics, response_model_by_alias=True)
def analytics(
    from_: date | None = Query(default=None, alias="from", description="First day, YYYY-MM-DD (UTC)"),
    to: date | None = Query(default=None, description="Last day, YYYY-MM-DD (UTC), included"),
    account: Account = Depends(current_account),
    db: Session = Depends(get_session),
) -> ConsoleAnalyticsMetrics:
    """Completion, review and retake rates and the breakdowns, for this account's verifications."""
    return console_metrics.analytics_metrics(db, from_, to, account_id=account.id)


# AML


@router.get("/aml/results", response_model=ConsoleAmlEntryList)
def aml_results(
    outcome: str | None = Query(default=None, description="no_match, possible_match, strong_match or unavailable"),
    q: str | None = Query(default=None, max_length=120, description="Name, reference or email"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    account: Account = Depends(current_account),
    db: Session = Depends(get_session),
) -> ConsoleAmlEntryList:
    """Screenings of this account's verifications that found someone or could not screen, newest first."""
    return console_aml.aml_results(
        db, source="verification", outcome=outcome, q=q, page=page, page_size=page_size, account_id=account.id
    )


@router.get("/aml/entities/{entity_id}", response_model=ConsoleAmlEntity)
def aml_entity(entity_id: str, account: Account = Depends(current_account)) -> ConsoleAmlEntity:
    """One watchlist entry in full, as listed publicly."""
    return console_aml.read_entity(entity_id, account)


# API logs


@router.get("/activity", response_model=ConsoleAuditEventList)
def activity(
    category: str | None = Query(default=None, max_length=40, description="request, api_key, review, dashboard and so on"),
    q: str | None = Query(default=None, max_length=120, description="Text anywhere in the event type or its details"),
    created_from: date | None = Query(default=None, description="On or after this day (UTC)"),
    created_to: date | None = Query(default=None, description="On or before this day (UTC)"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    account: Account = Depends(current_account),
    db: Session = Depends(get_session),
) -> ConsoleAuditEventList:
    """What this account did, signed in or with its API keys, and what happened on its verifications."""
    return console_audit.audit_events(
        db,
        category=category,
        actor=None,
        q=q,
        created_from=created_from,
        created_to=created_to,
        page=page,
        page_size=page_size,
        account_id=account.id,
    )


# API keys


@router.get("/api-keys", response_model=ConsoleApiKeyList)
def list_api_keys(
    q: str | None = Query(default=None, max_length=120, description="Key name or prefix"),
    state: str | None = Query(default=None, description="active, expiring, suspended, expired or revoked"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    account: Account = Depends(current_account),
    db: Session = Depends(get_session),
) -> ConsoleApiKeyList:
    """This account's API keys, newest first, with a count per state."""
    keys = console_api_keys.api_key_list(db, q=q, state=state, client_id=account.id, page=page, page_size=page_size)
    for row in keys.items:
        if row.last_action is not None:
            row.last_action.by = PANGOLIN
    return keys


def _record_key(db: Session, account: Account, action: str, key: ApiKeyResponse) -> None:
    db.add(
        AuditEvent(
            event_type=f"api_key.{action}",
            actor=str(account.id),
            payload={"api_key_id": key.id, "name": key.name, "prefix": key.prefix, "by": account.email},
        )
    )
    db.commit()


@router.post("/api-keys", response_model=CreatedApiKeyResponse, status_code=status.HTTP_201_CREATED)
def create_api_key(
    body: CreateApiKeyRequest,
    account: Account = Depends(current_account),
    db: Session = Depends(get_session),
) -> CreatedApiKeyResponse:
    """Make a key on this account. This answer is the only time the key itself is returned."""
    created = client_keys.create_key(body, account, db)
    _record_key(db, account, "created", created)
    return created


@router.delete("/api-keys/{key_id}", response_model=ApiKeyResponse)
def revoke_api_key(
    key_id: str,
    account: Account = Depends(current_account),
    db: Session = Depends(get_session),
) -> ApiKeyResponse:
    """Stop one of this account's keys working. It stays listed, revoked."""
    key = db.scalar(
        select(ApiKey).where(
            ApiKey.account_id == account.id,
            or_(ApiKey.prefix == key_id, ApiKey.id == (int(key_id) if key_id.isdigit() else -1)),
        )
    )
    was_revoked = key is not None and key.revoked_at is not None
    revoked = client_keys.revoke_key(key_id, account, db)
    if not was_revoked:
        _record_key(db, account, "revoked", revoked)
    return revoked


# Due diligence


@router.get("/due-diligence/forms", response_model=list[DashboardForm])
def due_diligence_forms(
    account: Account = Depends(current_account),
    db: Session = Depends(get_session),
) -> list[DashboardForm]:
    """The due diligence forms this account may attach to a verification."""
    given = forms.forms_for(db, account.id)
    counts = (
        dict(
            db.execute(
                select(Verification.form_id, func.count())
                .join(Step, Step.verification_id == Verification.id)
                .where(
                    Verification.account_id == account.id,
                    Verification.form_id.in_([f.public_id for f in given]),
                    Step.step == VerificationStep.due_diligence,
                    Step.status == StepStatus.passed,
                )
                .group_by(Verification.form_id)
            ).all()
        )
        if given
        else {}
    )
    me = FormClient(id=str(account.id), name=f"{account.first_name} {account.last_name}".strip(), email=account.email)
    return [
        DashboardForm(
            id=form.public_id,
            name=form.name,
            description=form.description,
            status=form.status,
            fields=forms.fields_of(form),
            clients=[me],
            responses=counts.get(form.public_id, 0),
            created_at=form.created_at,
            updated_at=form.updated_at,
        )
        for form in given
    ]


@router.get("/due-diligence/responses", response_model=ResponseList)
def due_diligence_responses(
    form_id: str | None = Query(default=None, description="Only this form's answers"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    account: Account = Depends(current_account),
    db: Session = Depends(get_session),
) -> ResponseList:
    """Answers given on this account's verifications, newest first."""
    return response_list(db, form_id=form_id, limit=limit, offset=offset, account_id=account.id)
