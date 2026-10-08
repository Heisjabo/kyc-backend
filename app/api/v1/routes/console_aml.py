"""AML screening for Pangolin staff in the admin console.

Two things live here:

- **Searches.** Staff look a person up on the watchlists by hand, by name and
  optionally date of birth, nationality and ID number. It is the same yente
  query the AML step runs, and each search is recorded in audit_events
  (event type aml.search) with who ran it, from where, why, and what came back.
  The list of past searches is read from there.
- **Screenings.** What the AML step found on real verifications: the latest
  attempt of each verification's AML step that has a result, newest first.

Searching is an action on a person, so readonly and support staff may not run
one; anyone with Pangolin KYC may read the screenings and past searches.
"""

from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import console_product, console_role
from app.api.v1.routes.console_verifications import _applicant, _client, _effective, _latest_scans
from app.db.models import Account, Applicant, AuditEvent, Step, Verification, VerificationStep
from app.db.session import get_session
from app.schemas.console import (
    ConsoleAmlCandidate,
    ConsoleAmlQuery,
    ConsoleAmlResult,
    ConsoleAmlScreening,
    ConsoleAmlScreeningList,
    ConsoleAmlSearchList,
    ConsoleAmlSearchRequest,
    ConsoleAmlSearchResponse,
    ConsoleAmlSearchRow,
)
from app.screening.yente import Identity, ScreeningResult, screen
from app.services.network import client_ip

router = APIRouter(prefix="/console/aml", tags=["admin console"])

# The console roles that may run a search.
SEARCHERS = ("owner", "admin", "reviewer")

SEARCH_EVENT = "aml.search"
OUTCOMES = ("no_match", "possible_match", "strong_match", "unavailable")


def _values(properties: dict, key: str) -> list[str]:
    values = properties.get(key) or []
    return [str(v) for v in values if v not in (None, "")] if isinstance(values, list) else []


def _candidate(match: dict) -> ConsoleAmlCandidate:
    """One yente match, flattened to what a reviewer compares."""
    properties = match.get("properties") or {}
    names = _values(properties, "name")
    name = str(match.get("caption") or (names[0] if names else "Unnamed"))
    aliases = [n for n in names + _values(properties, "alias") if n != name]
    return ConsoleAmlCandidate(
        id=str(match.get("id") or ""),
        name=name,
        entity_type=str(match.get("schema") or ""),
        score=float(match.get("score") or 0),
        topics=_values(properties, "topics"),
        datasets=[str(d) for d in (match.get("datasets") or [])],
        aliases=list(dict.fromkeys(aliases)),
        birth_dates=_values(properties, "birthDate"),
        nationalities=_values(properties, "nationality"),
        countries=_values(properties, "country"),
        positions=_values(properties, "position"),
        first_seen=match.get("first_seen"),
        last_seen=match.get("last_seen"),
    )


def _result(stored: dict) -> ConsoleAmlResult:
    result = ScreeningResult.model_validate(stored)
    candidates = sorted((_candidate(m) for m in result.matches if isinstance(m, dict)), key=lambda c: -c.score)
    return ConsoleAmlResult(
        outcome=result.outcome,
        action=result.action,
        highest_score=result.highest_score,
        dataset=result.dataset,
        screened_at=result.screened_at,
        error=result.error,
        candidates=candidates,
    )


def _query(values: dict) -> ConsoleAmlQuery:
    return ConsoleAmlQuery(
        full_name=values.get("full_name") or "",
        date_of_birth=values.get("date_of_birth"),
        nationality=values.get("nationality"),
        id_number=values.get("id_number"),
    )


def _staff_name(account: Account) -> str:
    return f"{account.first_name} {account.last_name}".strip() or account.email


@router.post("/search", response_model=ConsoleAmlSearchResponse)
def search(
    body: ConsoleAmlSearchRequest,
    request: Request,
    staff: Account = Depends(console_role("kyc", SEARCHERS)),
    db: Session = Depends(get_session),
) -> ConsoleAmlSearchResponse:
    """Look a person up on the watchlists. The search and its outcome are recorded
    in the audit log, whatever came back."""
    try:
        identity = Identity(
            full_name=body.full_name,
            date_of_birth=body.date_of_birth,
            nationality=body.nationality,
            id_number=(body.id_number or "").strip() or None,
        )
        identity.properties()  # refuses an unknown nationality code before searching
        found = screen(identity)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    result = _result(found.model_dump())
    query = {
        "full_name": identity.full_name,
        "date_of_birth": identity.date_of_birth.isoformat() if identity.date_of_birth else None,
        "nationality": identity.nationality.upper() if identity.nationality else None,
        "id_number": identity.id_number,
    }
    reason = (body.reason or "").strip() or None
    event = AuditEvent(
        event_type=SEARCH_EVENT,
        actor=str(staff.id),
        payload={
            "query": query,
            "reason": reason,
            "outcome": result.outcome,
            "highest_score": result.highest_score,
            "match_count": len(result.candidates),
            "top_matches": [
                {"id": c.id, "name": c.name, "score": c.score, "topics": c.topics} for c in result.candidates[:5]
            ],
            "dataset": result.dataset,
            "error": result.error,
            "by": staff.email,
            "ip": client_ip(request),
        },
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return ConsoleAmlSearchResponse(
        id=str(event.id), query=_query(query), reason=reason, result=result, by=_staff_name(staff), at=event.created_at
    )


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


@router.get("/searches", response_model=ConsoleAmlSearchList)
def list_searches(
    q: str | None = Query(default=None, max_length=120, description="Name searched for"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    _: Account = Depends(console_product("kyc")),
    db: Session = Depends(get_session),
) -> ConsoleAmlSearchList:
    """Searches run from the console, newest first."""
    filters = [AuditEvent.event_type == SEARCH_EVENT]
    if q and q.strip():
        filters.append(AuditEvent.payload["query"]["full_name"].astext.ilike(f"%{q.strip()}%"))
    listed = select(AuditEvent).where(*filters)
    total = db.scalar(select(func.count()).select_from(listed.subquery())) or 0
    events = db.scalars(
        listed.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()

    staff_ids = {int(e.actor) for e in events if e.actor.isdigit()}
    staff = {a.id: a for a in db.scalars(select(Account).where(Account.id.in_(staff_ids)))} if staff_ids else {}

    def by(event: AuditEvent) -> str:
        account = staff.get(int(event.actor)) if event.actor.isdigit() else None
        return _staff_name(account) if account else event.payload.get("by") or event.actor

    return ConsoleAmlSearchList(
        items=[
            ConsoleAmlSearchRow(
                id=str(e.id),
                query=_query(e.payload.get("query") or {}),
                reason=e.payload.get("reason"),
                outcome=e.payload.get("outcome") or "unavailable",
                highest_score=e.payload.get("highest_score"),
                match_count=int(e.payload.get("match_count") or 0),
                top_matches=e.payload.get("top_matches") or [],
                by=by(e),
                at=e.created_at,
            )
            for e in events
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/screenings", response_model=ConsoleAmlScreeningList)
def list_screenings(
    outcome: str | None = Query(default=None, description="One of " + ", ".join(OUTCOMES)),
    client_id: int | None = Query(default=None, description="Only this client account's verifications"),
    q: str | None = Query(default=None, max_length=120, description="Reference, applicant name, email or customer id"),
    screened_from: date | None = Query(default=None, description="Screened on or after this day (UTC)"),
    screened_to: date | None = Query(default=None, description="Screened on or before this day (UTC)"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    _: Account = Depends(console_product("kyc")),
    db: Session = Depends(get_session),
) -> ConsoleAmlScreeningList:
    """What the AML step found on real verifications: the latest screened attempt
    of each, newest first."""
    if outcome is not None and outcome not in OUTCOMES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Outcome must be one of " + ", ".join(OUTCOMES))

    latest = (
        select(Step.verification_id, func.max(Step.attempt).label("attempt"))
        .where(Step.step == VerificationStep.aml, Step.result.has_key("outcome"))
        .group_by(Step.verification_id)
        .subquery()
    )
    outcome_of = Step.result["outcome"].astext
    screened_at = func.coalesce(Step.completed_at, Step.created_at)

    filters = []
    if client_id is not None:
        filters.append(Verification.account_id == client_id)
    if screened_from is not None:
        filters.append(screened_at >= _day_start(screened_from))
    if screened_to is not None:
        filters.append(screened_at < _day_start(screened_to + timedelta(days=1)))
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

    def joined(columns):
        """Each verification's latest screened AML attempt, under the filters."""
        return (
            columns.join(
                latest,
                and_(
                    Step.verification_id == latest.c.verification_id,
                    Step.attempt == latest.c.attempt,
                    Step.step == VerificationStep.aml,
                ),
            )
            .join(Verification, Verification.id == Step.verification_id)
            .outerjoin(Applicant, Verification.applicant_id == Applicant.id)
            .where(*filters)
        )

    base = joined(select(Step))
    # Counts per outcome under every filter but the outcome itself.
    counted = db.execute(joined(select(outcome_of.label("o"), func.count()).select_from(Step)).group_by("o")).all()
    outcome_counts = {o: 0 for o in OUTCOMES} | {row[0]: row[1] for row in counted if row[0]}

    listed = base.where(outcome_of == outcome) if outcome else base
    total = db.scalar(select(func.count()).select_from(listed.subquery())) or 0
    steps = db.scalars(
        listed.options(
            selectinload(Step.verification).selectinload(Verification.applicant),
            selectinload(Step.verification).selectinload(Verification.account),
        )
        .order_by(screened_at.desc(), Step.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    ids = [s.verification_id for s in steps]
    scans = _latest_scans(db, ids)
    # The identity that was screened is what the document step read.
    documents: dict[int, Step] = {}
    if ids:
        for doc in db.scalars(
            select(Step).where(Step.step == VerificationStep.document, Step.verification_id.in_(ids)).order_by(Step.attempt)
        ):
            documents[doc.verification_id] = doc

    def screened(verification_id: int) -> ConsoleAmlQuery | None:
        doc = documents.get(verification_id)
        fields = (doc.extracted or {}) if doc else {}
        name = fields.get("full_name") or " ".join(filter(None, [fields.get("given_names"), fields.get("surname")]))
        if not name:
            return None
        return ConsoleAmlQuery(
            full_name=name,
            date_of_birth=fields.get("date_of_birth"),
            nationality=fields.get("nationality"),
            id_number=None,
        )

    return ConsoleAmlScreeningList(
        items=[
            ConsoleAmlScreening(
                verification_id=str(s.verification_id),
                reference=s.verification.reference,
                verification_status=_effective(s.verification),
                step_status=s.status.value,
                attempt=s.attempt,
                client=_client(s.verification.account),
                applicant=_applicant(s.verification, scans.get(s.verification_id)),
                screened=screened(s.verification_id),
                result=_result(s.result),
            )
            for s in steps
        ],
        total=total,
        page=page,
        page_size=page_size,
        outcome_counts=outcome_counts,
    )
