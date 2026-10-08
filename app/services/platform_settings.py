"""Platform settings that console owners and admins can change.

Each setting has a default from the environment (app/core/config.py). Changing
one in the console writes a row to console_settings, which then wins over the
default. Removing the row, by setting it back to the default, hands it back to
the environment.

Values are cached for a few seconds per process, so the pipeline can read them
on every verification without a query each time, and a change made in the
console reaches every worker within that time.
"""

import threading
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import ConsoleSetting
from app.db.session import SessionLocal

CACHE_SECONDS = 15


@dataclass(frozen=True)
class Definition:
    key: str
    kind: type
    minimum: float
    maximum: float

    def default(self):
        return _DEFAULTS[self.key]()


_DEFAULTS = {
    # Minutes a verification link stays open when the client sends no ttl.
    "session_ttl_minutes": lambda: settings.SESSION_TTL_MINUTES,
}

DEFINITIONS: dict[str, Definition] = {
    "session_ttl_minutes": Definition("session_ttl_minutes", int, 5, settings.MAX_SESSION_TTL_MINUTES),
}

_lock = threading.Lock()
_cache: dict[str, object] = {}
_cached_at = 0.0


def _load(db: Session) -> dict[str, object]:
    stored = {row.key: row.value.get("value") for row in db.scalars(select(ConsoleSetting))}
    return {key: stored.get(key, definition.default()) for key, definition in DEFINITIONS.items()}


def current(db: Session | None = None) -> dict[str, object]:
    """Every setting's value, stored or default."""
    global _cache, _cached_at
    with _lock:
        if _cache and time.monotonic() - _cached_at < CACHE_SECONDS:
            return dict(_cache)
    if db is not None:
        values = _load(db)
    else:
        with SessionLocal() as own:
            values = _load(own)
    with _lock:
        _cache, _cached_at = values, time.monotonic()
    return dict(values)


def value(key: str):
    """One setting's value. Falls back to the default if the database cannot be read."""
    try:
        return current()[key]
    except Exception:  # noqa: BLE001, a setting must never stop a verification
        return DEFINITIONS[key].default()


def forget() -> None:
    """Drop the cache, so this process reads a change at once."""
    global _cache, _cached_at
    with _lock:
        _cache, _cached_at = {}, 0.0


def stored_keys(db: Session) -> set[str]:
    return set(db.scalars(select(ConsoleSetting.key)))


def validate(changes: dict[str, object]) -> dict[str, object]:
    """The changes, converted to each setting's type, or ValueError saying what is wrong."""
    clean: dict[str, object] = {}
    for key, raw in changes.items():
        definition = DEFINITIONS.get(key)
        if definition is None:
            raise ValueError(f"There is no setting called {key}")
        try:
            number = definition.kind(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be a number") from exc
        if definition.kind is int and float(raw) != number:
            raise ValueError(f"{key} must be a whole number")
        if not definition.minimum <= number <= definition.maximum:
            raise ValueError(f"{key} must be between {definition.minimum:g} and {definition.maximum:g}")
        clean[key] = number
    return clean


def save(db: Session, changes: dict[str, object], account_id: int) -> None:
    """Write validated changes. A value equal to its default removes the row."""
    for key, number in changes.items():
        row = db.get(ConsoleSetting, key)
        if number == DEFINITIONS[key].default():
            if row is not None:
                db.delete(row)
            continue
        if row is None:
            db.add(ConsoleSetting(key=key, value={"value": number}, updated_by_id=account_id))
        else:
            row.value = {"value": number}
            row.updated_by_id = account_id
    db.commit()
    forget()
