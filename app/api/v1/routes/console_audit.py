"""The audit log, for Pangolin staff in the admin console.

Everything recorded in audit_events, newest first: console sign-ins, AML
searches, review decisions, API key controls, due diligence form changes and
every HTTP request the service answered, each with who did it and the details
stored at the time. Only owners and admins may read it, since it
holds the names people searched for and the addresses staff signed in from.
"""

from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import console_role
from app.db.models import Account, AuditEvent
from app.db.session import get_session
from app.schemas.console import ConsoleAuditEvent, ConsoleAuditEventList

router = APIRouter(prefix="/console/audit-events", tags=["admin console"])

AUDIT_READERS = ("owner", "admin")

# The category is the part of the event type before the first dot.
_CATEGORY = func.split_part(AuditEvent.event_type, ".", 1)


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


@router.get("", response_model=ConsoleAuditEventList)
def list_audit_events(
    category: str | None = Query(default=None, max_length=40, description="aml, api_key, console, due_diligence_form, review or request"),
    actor: str | None = Query(default=None, max_length=40, description="A console account id, or system"),
    q: str | None = Query(default=None, max_length=120, description="Text anywhere in the event type or its details"),
    created_from: date | None = Query(default=None, description="On or after this day (UTC)"),
    created_to: date | None = Query(default=None, description="On or before this day (UTC)"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    _: Account = Depends(console_role("kyc", AUDIT_READERS)),
    db: Session = Depends(get_session),
) -> ConsoleAuditEventList:
    """Every recorded event, newest first."""
    filters = []
    if actor:
        filters.append(AuditEvent.actor == actor)
    if created_from is not None:
        filters.append(AuditEvent.created_at >= _day_start(created_from))
    if created_to is not None:
        filters.append(AuditEvent.created_at < _day_start(created_to + timedelta(days=1)))
    if q and q.strip():
        like = f"%{q.strip()}%"
        filters.append(or_(AuditEvent.event_type.ilike(like), cast(AuditEvent.payload, String).ilike(like)))

    counted = db.execute(select(_CATEGORY.label("c"), func.count()).where(*filters).group_by("c")).all()
    category_counts = {row[0]: row[1] for row in counted}

    listed = select(AuditEvent).where(*filters)
    if category:
        listed = listed.where(_CATEGORY == category)
    total = db.scalar(select(func.count()).select_from(listed.subquery())) or 0
    events = db.scalars(
        listed.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()

    staff_ids = {int(e.actor) for e in events if e.actor.isdigit()}
    staff = {a.id: a for a in db.scalars(select(Account).where(Account.id.in_(staff_ids)))} if staff_ids else {}

    def who(event: AuditEvent) -> Account | None:
        return staff.get(int(event.actor)) if event.actor.isdigit() else None

    return ConsoleAuditEventList(
        items=[
            ConsoleAuditEvent(
                id=str(e.id),
                event_type=e.event_type,
                actor=e.actor,
                actor_name=(f"{a.first_name} {a.last_name}".strip() or None) if (a := who(e)) else None,
                actor_email=a.email if (a := who(e)) else e.payload.get("by"),
                verification_id=str(e.verification_id) if e.verification_id else None,
                payload=e.payload,
                created_at=e.created_at,
            )
            for e in events
        ],
        total=total,
        page=page,
        page_size=page_size,
        category_counts=category_counts,
    )
