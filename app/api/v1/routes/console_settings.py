"""Platform settings, for the admin console.

Every console member can read them; owners and admins can change them. Each
change is kept in audit_events as settings.updated, with the values before and
after. See app/services/platform_settings.py for where each one is used.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import console_product, console_role
from app.core.config import settings
from app.db.models import Account, AuditEvent
from app.db.session import get_session
from app.schemas.console import ConsoleSettings, ConsoleSettingsUpdate, ConsoleSettingsView
from app.services import platform_settings

router = APIRouter(prefix="/console/settings", tags=["admin console"])

EDITORS = ("owner", "admin")


def _view(db: Session) -> ConsoleSettingsView:
    platform_settings.forget()
    values = platform_settings.current(db)
    return ConsoleSettingsView(
        values=ConsoleSettings(**values),
        defaults=ConsoleSettings(**{k: d.default() for k, d in platform_settings.DEFINITIONS.items()}),
        changed=sorted(platform_settings.stored_keys(db)),
        max_session_ttl_minutes=settings.MAX_SESSION_TTL_MINUTES,
    )


@router.get("", response_model=ConsoleSettingsView)
def read_settings(
    _: Account = Depends(console_product("kyc")),
    db: Session = Depends(get_session),
) -> ConsoleSettingsView:
    return _view(db)


@router.patch("", response_model=ConsoleSettingsView)
def update_settings(
    body: ConsoleSettingsUpdate,
    staff: Account = Depends(console_role("kyc", EDITORS)),
    db: Session = Depends(get_session),
) -> ConsoleSettingsView:
    changes = body.model_dump(exclude_none=True)
    if not changes:
        return _view(db)
    platform_settings.forget()
    before = platform_settings.current(db)
    try:
        clean = platform_settings.validate(changes)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    changed = {k: v for k, v in clean.items() if v != before[k]}
    if changed:
        db.add(
            AuditEvent(
                event_type="settings.updated",
                actor=str(staff.id),
                payload={
                    "by": staff.email,
                    "changed": sorted(changed),
                    "before": {k: before[k] for k in changed},
                    "after": changed,
                },
            )
        )
        platform_settings.save(db, changed, staff.id)
    return _view(db)
