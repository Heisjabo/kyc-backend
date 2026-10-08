"""AML screening for Pangolin staff in the admin console.

- **Searching.** Staff look a person up on the watchlists by hand, by name and
  optionally date of birth, nationality and ID number. It is the same yente
  query the AML step runs, and each search is recorded in audit_events
  (event type aml.search) with who ran it, from where, why, and the whole
  answer, so it can be opened again later.
- **The list.** One list of every console search and every verification whose
  AML step found something or could not screen, newest first. Verifications
  that came back with no match are left out: there is nothing to look at.

Searching is an action on a person, so readonly and support staff may not run
one; anyone with Pangolin KYC may read the list.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import console_product, console_role
from app.api.v1.routes.console_verifications import _applicant, _client, _effective, _latest_scans
from app.core.config import settings
from app.db.models import Account, Applicant, AuditEvent, Step, Verification, VerificationStep
from app.db.session import get_session
from app.schemas.console import (
    ConsoleAmlCandidate,
    ConsoleAmlEntry,
    ConsoleAmlEntryList,
    ConsoleAmlQuery,
    ConsoleAmlResult,
    ConsoleAmlSearchRequest,
    ConsoleAmlSearchResponse,
    ConsoleAmlTopMatch,
    ConsoleAmlVerificationRef,
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
            # The whole answer, so the search can be opened again as it was.
            "result": result.model_dump(),
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


def _top(candidates: list[ConsoleAmlCandidate], stored: list[dict]) -> ConsoleAmlTopMatch | None:
    if candidates:
        best = candidates[0]
        return ConsoleAmlTopMatch(name=best.name, score=best.score, topics=best.topics)
    if stored and isinstance(stored[0], dict) and stored[0].get("name"):
        return ConsoleAmlTopMatch(name=stored[0]["name"], score=float(stored[0].get("score") or 0), topics=stored[0].get("topics") or [])
    return None


def _search_entry(event: AuditEvent, staff: dict[int, Account]) -> ConsoleAmlEntry:
    payload = event.payload
    account = staff.get(int(event.actor)) if event.actor.isdigit() else None
    result = _result(payload["result"]) if isinstance(payload.get("result"), dict) else None
    return ConsoleAmlEntry(
        id=f"search:{event.id}",
        source="manual",
        at=event.created_at,
        person=_query(payload.get("query") or {}),
        outcome=payload.get("outcome") or "unavailable",
        highest_score=payload.get("highest_score"),
        match_count=int(payload.get("match_count") or 0),
        top_match=_top(result.candidates if result else [], payload.get("top_matches") or []),
        by=_staff_name(account) if account else payload.get("by") or event.actor,
        reason=payload.get("reason"),
        result=result,
    )


def _verification_entry(step: Step, document: Step | None, scan) -> ConsoleAmlEntry:
    verification = step.verification
    result = _result(step.result)
    fields = (document.extracted or {}) if document else {}
    name = fields.get("full_name") or " ".join(filter(None, [fields.get("given_names"), fields.get("surname")]))
    applicant = _applicant(verification, scan)
    return ConsoleAmlEntry(
        id=f"verification:{verification.id}",
        source="verification",
        at=step.completed_at or step.created_at,
        person=ConsoleAmlQuery(
            full_name=name or applicant.name or "Unnamed applicant",
            date_of_birth=fields.get("date_of_birth"),
            nationality=fields.get("nationality"),
            id_number=None,
        ),
        outcome=result.outcome,
        highest_score=result.highest_score,
        match_count=len(result.candidates),
        top_match=_top(result.candidates, []),
        verification=ConsoleAmlVerificationRef(
            id=str(verification.id),
            reference=verification.reference,
            status=_effective(verification),
            client=_client(verification.account),
        ),
        result=result,
    )


SOURCES = ("manual", "verification")


@router.get("/results", response_model=ConsoleAmlEntryList)
def list_results(
    source: str | None = Query(default=None, description="manual or verification"),
    outcome: str | None = Query(default=None, description="One of " + ", ".join(OUTCOMES)),
    q: str | None = Query(default=None, max_length=120, description="Name, reference, email or the staff member who searched"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    _: Account = Depends(console_product("kyc")),
    db: Session = Depends(get_session),
) -> ConsoleAmlEntryList:
    """Console searches and verification screenings that found something or could
    not screen, newest first, in one list."""
    if source is not None and source not in SOURCES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Source must be manual or verification")
    if outcome is not None and outcome not in OUTCOMES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Outcome must be one of " + ", ".join(OUTCOMES))
    like = f"%{q.strip()}%" if q and q.strip() else None

    # Searches run from the console, every outcome.
    search_outcome = AuditEvent.payload["outcome"].astext
    search_filters = [AuditEvent.event_type == SEARCH_EVENT]
    if like:
        search_filters.append(
            or_(
                AuditEvent.payload["query"]["full_name"].astext.ilike(like),
                AuditEvent.payload["by"].astext.ilike(like),
                AuditEvent.payload["reason"].astext.ilike(like),
            )
        )

    # Each verification's latest screened AML attempt, unless it found nobody.
    latest = (
        select(Step.verification_id, func.max(Step.attempt).label("attempt"))
        .where(Step.step == VerificationStep.aml, Step.result.has_key("outcome"))
        .group_by(Step.verification_id)
        .subquery()
    )
    step_outcome = Step.result["outcome"].astext
    step_at = func.coalesce(Step.completed_at, Step.created_at)
    step_filters = [step_outcome != "no_match"]
    if like:
        step_filters.append(
            or_(
                Verification.reference.ilike(like),
                Applicant.customer_id.ilike(like),
                Applicant.email.ilike(like),
                func.concat_ws(" ", Applicant.first_name, Applicant.last_name).ilike(like),
                Verification.claimed["email"].astext.ilike(like),
                func.concat_ws(" ", Verification.claimed["first_name"].astext, Verification.claimed["last_name"].astext).ilike(like),
            )
        )

    def screened(columns):
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
            .where(*step_filters)
        )

    def counts(rows) -> dict[str, int]:
        return {row[0]: row[1] for row in rows if row[0]}

    search_by_outcome = counts(
        db.execute(select(search_outcome.label("o"), func.count()).where(*search_filters).group_by("o")).all()
    )
    step_by_outcome = counts(
        db.execute(screened(select(step_outcome.label("o"), func.count()).select_from(Step)).group_by("o")).all()
    )
    use_searches, use_steps = source in (None, "manual"), source in (None, "verification")
    outcome_counts = {
        o: (search_by_outcome.get(o, 0) if use_searches else 0) + (step_by_outcome.get(o, 0) if use_steps else 0)
        for o in OUTCOMES
    }
    picked = (lambda by: sum(by.values())) if outcome is None else (lambda by: by.get(outcome, 0))
    source_counts = {"manual": picked(search_by_outcome), "verification": picked(step_by_outcome)}
    total = (source_counts["manual"] if use_searches else 0) + (source_counts["verification"] if use_steps else 0)

    # Newest first across both: take enough of each to fill this page, then merge.
    enough = page * page_size
    entries: list[ConsoleAmlEntry] = []
    if use_searches:
        listed = select(AuditEvent).where(*search_filters)
        if outcome:
            listed = listed.where(search_outcome == outcome)
        events = db.scalars(listed.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(enough)).all()
        staff_ids = {int(e.actor) for e in events if e.actor.isdigit()}
        staff = {a.id: a for a in db.scalars(select(Account).where(Account.id.in_(staff_ids)))} if staff_ids else {}
        entries += [_search_entry(e, staff) for e in events]
    if use_steps:
        listed = screened(select(Step))
        if outcome:
            listed = listed.where(step_outcome == outcome)
        steps = db.scalars(
            listed.options(
                selectinload(Step.verification).selectinload(Verification.applicant),
                selectinload(Step.verification).selectinload(Verification.account),
            )
            .order_by(step_at.desc(), Step.id.desc())
            .limit(enough)
        ).all()
        ids = [s.verification_id for s in steps]
        scans = _latest_scans(db, ids)
        documents: dict[int, Step] = {}
        if ids:
            for doc in db.scalars(
                select(Step).where(Step.step == VerificationStep.document, Step.verification_id.in_(ids)).order_by(Step.attempt)
            ):
                documents[doc.verification_id] = doc
        entries += [_verification_entry(s, documents.get(s.verification_id), scans.get(s.verification_id)) for s in steps]

    entries.sort(key=lambda e: e.at, reverse=True)
    return ConsoleAmlEntryList(
        items=entries[(page - 1) * page_size : enough],
        total=total,
        page=page,
        page_size=page_size,
        outcome_counts=outcome_counts,
        source_counts=source_counts,
        thresholds={"possible": settings.YENTE_POSSIBLE_THRESHOLD, "strong": settings.YENTE_STRONG_THRESHOLD},
    )
