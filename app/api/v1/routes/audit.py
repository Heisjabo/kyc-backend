"""The audit log, for whoever is asking.

A client gets its API logs: what it did itself, signed in to the dashboard or
with one of its API keys, and what happened on its own verifications, where
Staff appear only as "Compliance team" making review decisions.
Staff get the whole log, as the console shows it, and only owners and
admins, since it holds the names people searched for and where staff signed in
from.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import Caller, current_caller
from app.api.v1.routes import console_audit
from app.db.session import get_session
from app.schemas.console import ConsoleAuditEventList

router = APIRouter(prefix="/audit-events", tags=["audit"])


@router.get("", response_model=ConsoleAuditEventList)
def list_events(
    category: str | None = Query(default=None, max_length=40, description="request, api_key, review, dashboard and so on"),
    actor: str | None = Query(default=None, max_length=40, description="Staff only: one account id, or system"),
    q: str | None = Query(default=None, max_length=120, description="Text anywhere in the event type or its details"),
    created_from: date | None = Query(default=None, description="On or after this day (UTC)"),
    created_to: date | None = Query(default=None, description="On or before this day (UTC)"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    caller: Caller = Depends(current_caller),
    db: Session = Depends(get_session),
) -> ConsoleAuditEventList:
    """Recorded events, newest first, with a count per category."""
    if caller.staff and caller.account.console_role not in console_audit.AUDIT_READERS:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your role cannot do this")
    return console_audit.audit_events(
        db,
        category=category,
        actor=actor if caller.staff else None,
        q=q,
        created_from=created_from,
        created_to=created_to,
        page=page,
        page_size=page_size,
        account_id=caller.scope,
    )
