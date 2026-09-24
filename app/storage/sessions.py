"""Short-lived in-memory verification sessions.

A document scan opens a session so the face step can reuse the document without
the caller uploading it again. This lives in memory only: it is cleared on
restart, is not shared between servers, and is a stand-in until the database and
object storage are built.
"""

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.core.config import settings


@dataclass
class VerificationSession:
    id: str
    created_at: float
    document_type: str
    document_decision: str
    document_fields: dict[str, Any]
    # Embedding of the portrait printed on the document, for the face match.
    document_embedding: np.ndarray | None
    # The document photo cropped to the face, shown on the page (base64 JPEG).
    document_face_base64: str | None
    # True when the portrait is smaller than a reference photo should be.
    document_face_small: bool = False
    # The person's resting face mesh, learned while the camera is being checked,
    # so the live guidance can tell which way they have moved.
    resting_mesh: Any | None = None
    face_result: dict[str, Any] | None = field(default=None)


_sessions: dict[str, VerificationSession] = {}
_lock = threading.Lock()


def create(**fields) -> VerificationSession:
    session = VerificationSession(id=secrets.token_urlsafe(16), created_at=time.time(), **fields)
    with _lock:
        _prune()
        _sessions[session.id] = session
    return session


def get(session_id: str) -> VerificationSession | None:
    with _lock:
        _prune()
        return _sessions.get(session_id)


def _prune() -> None:
    cutoff = time.time() - settings.SESSION_TTL_MINUTES * 60
    for key in [key for key, session in _sessions.items() if session.created_at < cutoff]:
        del _sessions[key]
