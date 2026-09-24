"""Engine and session factory.

One engine per process; sessions are short-lived and belong to a single request.
"""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

# pool_pre_ping: a local Postgres gets restarted under the developer, and a
# connection that died while pooled should cost a reconnect, not a 500.
engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)

# expire_on_commit=False: handlers read the object they just committed, and
# refreshing every attribute for that is a second round trip per response.
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency giving one session per request."""
    with SessionLocal() as session:
        yield session
