"""Numbers over verifications, for whoever is asking.

A client (signed in to the dashboard or with an API key) gets them over its own
verifications, with a breakdown by the steps it asked for; staff get
them across every client, or one client's with client_id, the same figures the
console shows. How each is worked out is in console_metrics.
"""

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import Caller, current_caller
from app.api.v1.routes import console_metrics
from app.db.session import get_session
from app.schemas.console import ConsoleAnalyticsMetrics, ConsoleOverviewMetrics

router = APIRouter(prefix="/metrics", tags=["metrics"])

_FROM = Query(default=None, alias="from", description="First day, YYYY-MM-DD (UTC)")
_TO = Query(default=None, description="Last day, YYYY-MM-DD (UTC), included")
_CLIENT = Query(default=None, description="Staff only: one client's verifications")


@router.get("/overview", response_model=ConsoleOverviewMetrics, response_model_by_alias=True)
def overview(
    from_: date | None = _FROM,
    to: date | None = _TO,
    client_id: int | None = _CLIENT,
    caller: Caller = Depends(current_caller),
    db: Session = Depends(get_session),
) -> ConsoleOverviewMetrics:
    """Volume, pass rate, completion time, outcomes and decline reasons."""
    return console_metrics.overview_metrics(db, from_, to, account_id=caller.scope_for(client_id))


@router.get("/analytics", response_model=ConsoleAnalyticsMetrics, response_model_by_alias=True)
def analytics(
    from_: date | None = _FROM,
    to: date | None = _TO,
    client_id: int | None = _CLIENT,
    caller: Caller = Depends(current_caller),
    db: Session = Depends(get_session),
) -> ConsoleAnalyticsMetrics:
    """Completion, review and retake rates, and the breakdowns."""
    return console_metrics.analytics_metrics(db, from_, to, account_id=caller.scope_for(client_id))
