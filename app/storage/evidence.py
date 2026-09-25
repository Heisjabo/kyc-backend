"""Where the evidence of a check will be kept.

Two things are worth keeping from a face check: the portrait that was captured,
and a few seconds of video from the liveness challenge. Providers keep the clip
so a disputed verification can be looked at afterwards by a person, and so a
model can be re-run over it when the rules change.

Nothing is stored yet. Real storage means answering retention questions first,
how long biometrics are kept and who may look at them, and those belong with the
database work rather than here. This module is the seam: the pipeline already
hands the evidence over, the size and type are already checked, and turning it on
is a matter of pointing EVIDENCE_DIR at a folder, or replacing the body of these
two functions with an object-storage client.
"""

import logging
import uuid
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)

_EXTENSIONS = {
    "video/webm": ".webm",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "image/jpeg": ".jpg",
    "image/png": ".png",
}


def keep_challenge_video(verification_id: str, data: bytes, content_type: str) -> str | None:
    """Keep the clip recorded while the person followed the head movements."""
    return _keep(verification_id, data, content_type, "challenge")


def keep_portrait(verification_id: str, data: bytes, content_type: str = "image/jpeg") -> str | None:
    """Keep the still taken at the end of the challenge, the one that was matched."""
    return _keep(verification_id, data, content_type, "portrait")


def _keep(verification_id: str, data: bytes, content_type: str, kind: str) -> str | None:
    """Returns where it was put, or None when storage is not configured.

    The return value is deliberately a string rather than a path: once this is
    object storage it will be a key, and callers should not care which.
    """
    if not data:
        return None
    if not settings.EVIDENCE_DIR:
        # Say what was received, so the shape of the traffic is visible before
        # anything is written down.
        logger.info("%s evidence received for %s: %d bytes, %s (not stored)",
                    kind, verification_id, len(data), content_type)
        return None

    folder = Path(settings.EVIDENCE_DIR) / verification_id
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{kind}-{uuid.uuid4().hex[:8]}{_EXTENSIONS.get(content_type, '.bin')}"
    path = folder / name
    path.write_bytes(data)
    logger.info("%s evidence stored for %s at %s", kind, verification_id, path)
    return str(path)
