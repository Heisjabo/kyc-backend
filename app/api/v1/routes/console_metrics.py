"""Numbers for the admin console's Overview and Analytics pages.

Every figure is over the verifications created in a window of whole UTC days
(both ends included), across every client, and the headline ones are compared
with the window of the same length just before it. A verification's status is
the one a client would see now, so an open session past its link's expiry
counts as expired. Reading writes nothing.

Pass rate is passed out of passed plus failed: verifications still open or
waiting for review have not been decided.
"""

import re
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import String, and_, cast, func, select, true
from sqlalchemy.orm import Session

from app.api.deps import console_product
from app.api.v1.routes.console_verifications import _EFFECTIVE_STATUS
from app.db.models import Account, Applicant, DocumentScan, Step, StepStatus, Verification, VerificationStatus, VerificationStep
from app.db.session import get_session
from app.schemas.console import (
    ConsoleAnalyticsMetrics,
    ConsoleAwaitingReview,
    ConsoleMetricCount,
    ConsoleMetricPoint,
    ConsoleMetricRange,
    ConsoleMetricSeries,
    ConsoleMetricValue,
    ConsoleOverviewMetrics,
    ConsoleStepTime,
)

router = APIRouter(prefix="/console/metrics", tags=["admin console"])

# Past this many days the series is weekly; day by day stops being readable.
DAILY_UP_TO = 92
# Long enough for twelve months and the year before it.
MAX_DAYS = 400

_PASSED = VerificationStatus.passed.value
_FAILED = VerificationStatus.failed.value
_REVIEW = VerificationStatus.needs_review.value
_DECIDED = (_PASSED, _FAILED, _REVIEW)

_MOBILE = re.compile(r"mobi|android|iphone|ipad|ipod", re.IGNORECASE)


def _start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


def _window(start: date, end: date, account_id: int | None = None):
    """Verifications created in the window, of one client account when one is given."""
    created = and_(Verification.created_at >= _start(start), Verification.created_at < _start(end + timedelta(days=1)))
    return created if account_id is None else and_(created, Verification.account_id == account_id)


def _rate(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def _range(start: date | None, end: date | None) -> tuple[date, date, int, date, date]:
    """The window asked for, and the one of the same length just before it."""
    if start is None or end is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Give both from and to, as YYYY-MM-DD")
    if start > end:
        start, end = end, start
    days = (end - start).days + 1
    if days > MAX_DAYS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"A window can be at most {MAX_DAYS} days")
    previous_end = start - timedelta(days=1)
    return start, end, days, previous_end - timedelta(days=days - 1), previous_end


def _status_counts(db: Session, start: date, end: date, account_id: int | None = None) -> dict[str, int]:
    rows = db.execute(
        select(_EFFECTIVE_STATUS.label("s"), func.count()).where(_window(start, end, account_id)).group_by("s")
    ).all()
    return {row[0]: row[1] for row in rows}


def _pass_rate(counts: dict[str, int]) -> float | None:
    return _rate(counts.get(_PASSED, 0), counts.get(_PASSED, 0) + counts.get(_FAILED, 0))


def _median_completion(db: Session, start: date, end: date, account_id: int | None = None) -> float | None:
    seconds = func.extract("epoch", Verification.completed_at - func.coalesce(Verification.started_at, Verification.created_at))
    value = db.scalar(
        select(func.percentile_cont(0.5).within_group(seconds)).where(
            _window(start, end, account_id),
            Verification.completed_at.is_not(None),
            Verification.status.in_([VerificationStatus(s) for s in _DECIDED]),
        )
    )
    return float(value) if value is not None else None


def _attempts_per_step(db: Session, start: date, end: date, account_id: int | None = None) -> float | None:
    """Rows in verification_steps per (verification, step) taken: retakes push it above 1."""
    taken = (
        select(func.max(Step.attempt).label("attempts"))
        .join(Verification, Verification.id == Step.verification_id)
        .where(_window(start, end, account_id), Step.status != StepStatus.pending, Step.status != StepStatus.skipped)
        .group_by(Step.verification_id, Step.step)
        .subquery()
    )
    value = db.scalar(select(func.avg(taken.c.attempts)))
    return float(value) if value is not None else None


def _series(db: Session, start: date, end: date, days: int, account_id: int | None = None) -> ConsoleMetricSeries:
    day = func.date(func.timezone("UTC", Verification.created_at))
    rows = db.execute(
        select(
            day.label("d"),
            func.count(),
            func.count().filter(_EFFECTIVE_STATUS == _PASSED),
            func.count().filter(_EFFECTIVE_STATUS == _FAILED),
        )
        .where(_window(start, end, account_id))
        .group_by("d")
    ).all()
    by_day = {row[0]: (row[1], row[2], row[3]) for row in rows}

    weekly = days > DAILY_UP_TO
    step = 7 if weekly else 1
    points = []
    for offset in range(0, days, step):
        first = start + timedelta(days=offset)
        bucket = [by_day.get(first + timedelta(days=i), (0, 0, 0)) for i in range(min(step, days - offset))]
        started, passed, failed = (sum(v[i] for v in bucket) for i in range(3))
        points.append(
            ConsoleMetricPoint(date=first, verifications=started, passed=passed, failed=failed, pass_rate=_rate(passed, passed + failed))
        )
    return ConsoleMetricSeries(granularity="week" if weekly else "day", points=points)


def _breakdown(
    db: Session, key, start: date, end: date, joins=(), limit: int | None = None, account_id: int | None = None
) -> list[ConsoleMetricCount]:
    """Verifications in the window grouped by `key`, most first, with each group's share and pass rate."""
    query = select(
        key.label("k"),
        func.count(),
        func.count().filter(_EFFECTIVE_STATUS == _PASSED),
        func.count().filter(_EFFECTIVE_STATUS == _FAILED),
    ).select_from(Verification)
    for target, on in joins:
        query = query.outerjoin(target, on)
    rows = db.execute(query.where(_window(start, end, account_id)).group_by("k").order_by(func.count().desc())).all()
    total = sum(row[1] for row in rows)
    shown = rows[:limit] if limit else rows
    return [
        ConsoleMetricCount(
            key=str(row[0]) if row[0] is not None else "unknown",
            count=row[1],
            share=_rate(row[1], total),
            pass_rate=_rate(row[2], row[2] + row[3]),
        )
        for row in shown
    ]


@router.get("/overview", response_model=ConsoleOverviewMetrics, response_model_by_alias=True)
def overview(
    from_: date | None = Query(default=None, alias="from", description="First day, YYYY-MM-DD (UTC)"),
    to: date | None = Query(default=None, description="Last day, YYYY-MM-DD (UTC), included"),
    _: Account = Depends(console_product("kyc")),
    db: Session = Depends(get_session),
) -> ConsoleOverviewMetrics:
    """Volume, pass rate, completion time, outcomes and decline reasons for the Overview page."""
    return overview_metrics(db, from_, to)


def overview_metrics(db: Session, from_: date | None, to: date | None, account_id: int | None = None) -> ConsoleOverviewMetrics:
    """The Overview figures, across every client or for one account's verifications
    only. Shared with the client dashboard, which always passes its own account."""
    start, end, days, before_start, before_end = _range(from_, to)
    now, before = _status_counts(db, start, end, account_id), _status_counts(db, before_start, before_end, account_id)

    waiting_filters = [Verification.status == VerificationStatus.needs_review]
    if account_id is not None:
        waiting_filters.append(Verification.account_id == account_id)
    waiting = db.execute(
        select(func.count(), func.min(func.coalesce(Verification.completed_at, Verification.created_at))).where(
            *waiting_filters
        )
    ).one()

    reason = func.jsonb_array_elements_text(Verification.reasons).table_valued("value").alias("reason")
    reasons = db.execute(
        select(reason.c.value, func.count())
        .select_from(Verification)
        .join(reason, true())
        .where(_window(start, end, account_id), _EFFECTIVE_STATUS == _FAILED)
        .group_by(reason.c.value)
        .order_by(func.count().desc())
        .limit(6)
    ).all()

    order = [s.value for s in VerificationStatus]
    return ConsoleOverviewMetrics(
        range=ConsoleMetricRange(from_=start, to=end, days=days),
        verifications=ConsoleMetricValue(value=sum(now.values()), previous=sum(before.values())),
        pass_rate=ConsoleMetricValue(value=_pass_rate(now), previous=_pass_rate(before)),
        completion_seconds=ConsoleMetricValue(
            value=_median_completion(db, start, end, account_id),
            previous=_median_completion(db, before_start, before_end, account_id),
        ),
        awaiting_review=ConsoleAwaitingReview(count=waiting[0], oldest_at=waiting[1]),
        series=_series(db, start, end, days, account_id),
        outcomes=[
            ConsoleMetricCount(key=s, count=now[s], share=_rate(now[s], sum(now.values())))
            for s in sorted(now, key=lambda s: order.index(s) if s in order else 99)
            if now[s]
        ],
        decline_reasons=[ConsoleMetricCount(key=row[0], count=row[1]) for row in reasons],
    )


@router.get("/analytics", response_model=ConsoleAnalyticsMetrics, response_model_by_alias=True)
def analytics(
    from_: date | None = Query(default=None, alias="from", description="First day, YYYY-MM-DD (UTC)"),
    to: date | None = Query(default=None, description="Last day, YYYY-MM-DD (UTC), included"),
    _: Account = Depends(console_product("kyc")),
    db: Session = Depends(get_session),
) -> ConsoleAnalyticsMetrics:
    """Completion, review and retake rates, and breakdowns by country, document,
    step, client and device, for the Analytics page."""
    return analytics_metrics(db, from_, to)


def analytics_metrics(db: Session, from_: date | None, to: date | None, account_id: int | None = None) -> ConsoleAnalyticsMetrics:
    """The Analytics figures, across every client or for one account's
    verifications only. For one account there is no breakdown by client, and
    there is one by the steps it asked for instead."""
    start, end, days, before_start, before_end = _range(from_, to)
    now, before = _status_counts(db, start, end, account_id), _status_counts(db, before_start, before_end, account_id)

    def completion(counts: dict[str, int]) -> float | None:
        return _rate(sum(counts.get(s, 0) for s in _DECIDED), sum(counts.values()))

    def review(counts: dict[str, int]) -> float | None:
        return _rate(counts.get(_REVIEW, 0), sum(counts.values()))

    # The document each verification was decided on: its latest scan.
    scan = (
        select(DocumentScan.verification_id, DocumentScan.document_type)
        .distinct(DocumentScan.verification_id)
        .order_by(DocumentScan.verification_id, DocumentScan.created_at.desc())
        .subquery()
    )
    documents = _breakdown(
        db, cast(scan.c.document_type, String), start, end, joins=[(scan, scan.c.verification_id == Verification.id)], account_id=account_id
    )
    documents = [d for d in documents if d.key != "unknown"]
    with_document = sum(d.count for d in documents)
    for d in documents:
        d.share = _rate(d.count, with_document)

    countries = _breakdown(
        db,
        func.upper(func.coalesce(Verification.country, Applicant.country)),
        start,
        end,
        joins=[(Applicant, Applicant.id == Verification.applicant_id)],
        limit=10,
        account_id=account_id,
    )

    clients = _breakdown(db, Verification.account_id, start, end, limit=10) if account_id is None else []
    names = {
        a.id: (f"{a.first_name} {a.last_name}".strip() or a.email)
        for a in db.scalars(select(Account).where(Account.id.in_([int(c.key) for c in clients if c.key.isdigit()])))
    } if clients else {}
    for c in clients:
        c.label = names.get(int(c.key)) if c.key.isdigit() else None

    seconds = func.extract("epoch", Step.completed_at - Step.started_at)
    step_rows = db.execute(
        select(Step.step, func.percentile_cont(0.5).within_group(seconds), func.count())
        .join(Verification, Verification.id == Step.verification_id)
        .where(_window(start, end, account_id), Step.started_at.is_not(None), Step.completed_at.is_not(None))
        .group_by(Step.step)
    ).all()
    step_order = [s.value for s in VerificationStep]
    step_times = sorted(
        (ConsoleStepTime(step=row[0].value, median_seconds=round(float(row[1]), 1), count=row[2]) for row in step_rows),
        key=lambda s: step_order.index(s.step),
    )

    # Mobile when the browser that sent the document or face step says it is one.
    agents = db.execute(
        select(Step.verification_id, Step.device["user_agent"].astext)
        .join(Verification, Verification.id == Step.verification_id)
        .where(
            _window(start, end, account_id),
            Step.step.in_([VerificationStep.document, VerificationStep.face]),
            Step.device["user_agent"].astext.is_not(None),
        )
    ).all()
    mobile: dict[int, bool] = {}
    for verification_id, agent in agents:
        mobile[verification_id] = mobile.get(verification_id, False) or bool(_MOBILE.search(agent or ""))
    on_mobile = sum(mobile.values())
    devices = [
        ConsoleMetricCount(key="mobile", count=on_mobile, share=_rate(on_mobile, len(mobile))),
        ConsoleMetricCount(key="desktop", count=len(mobile) - on_mobile, share=_rate(len(mobile) - on_mobile, len(mobile))),
    ] if mobile else []

    return ConsoleAnalyticsMetrics(
        range=ConsoleMetricRange(from_=start, to=end, days=days),
        completion_rate=ConsoleMetricValue(value=completion(now), previous=completion(before)),
        review_rate=ConsoleMetricValue(value=review(now), previous=review(before)),
        attempts_per_step=ConsoleMetricValue(
            value=_attempts_per_step(db, start, end, account_id),
            previous=_attempts_per_step(db, before_start, before_end, account_id),
        ),
        series=_series(db, start, end, days, account_id),
        countries=countries,
        documents=documents,
        step_times=step_times,
        clients=clients,
        devices=devices,
        services=_services(db, start, end, account_id) if account_id is not None else None,
    )


def _services(db: Session, start: date, end: date, account_id: int) -> list[ConsoleMetricCount]:
    """How often each step was asked for in the window, with that step's own pass
    rate: passes out of passes and fails, retakes included."""
    created = db.scalar(select(func.count()).select_from(Verification).where(_window(start, end, account_id))) or 0
    out = []
    for step in VerificationStep:
        asked = db.scalar(
            select(func.count())
            .select_from(Verification)
            .where(_window(start, end, account_id), Verification.services.contains([step.value]))
        ) or 0
        if not asked:
            continue
        passed, failed = db.execute(
            select(
                func.count().filter(Step.status == StepStatus.passed),
                func.count().filter(Step.status == StepStatus.failed),
            )
            .join(Verification, Verification.id == Step.verification_id)
            .where(_window(start, end, account_id), Step.step == step)
        ).one()
        out.append(ConsoleMetricCount(key=step.value, count=asked, share=_rate(asked, created), pass_rate=_rate(passed, passed + failed)))
    return out
