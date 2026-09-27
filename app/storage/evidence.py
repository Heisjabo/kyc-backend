"""Where the evidence of a verification is kept.

Two things come out of a check and have to survive it: the images and clips it
judged, and the report that will be built from them. This is where they go.

The bytes are written under EVIDENCE_DIR, one folder per verification, and the
row in verification_files records the key, the size and a SHA-256 so the file
can later be proved to be the one that was judged. In production EVIDENCE_DIR
becomes a bucket and only this module changes: a storage key is deliberately an
opaque string rather than a path, so nothing downstream cares where it points.

Nothing here is ever served directly to a browser. These are photographs of
identity documents and faces, so they are read back through the API, by a caller
holding a key, and embedded in the report rather than linked.
"""

import hashlib
import logging
from dataclasses import dataclass
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


@dataclass
class Stored:
    """A file that has been kept, and what proves it is that file."""

    storage_key: str
    content_type: str
    byte_size: int
    sha256: str


def keep(reference: str, data: bytes, kind: str, content_type: str = "image/jpeg") -> Stored | None:
    """Write one piece of evidence and describe it. None if there is nothing.

    `kind` is one of the FileKind values, and it becomes part of the key so a
    folder can be read by a person without opening anything.
    """
    if not data:
        return None

    digest = hashlib.sha256(data).hexdigest()
    name = f"{kind}-{digest[:12]}{_EXTENSIONS.get(content_type, '.bin')}"
    key = f"{reference}/{name}"

    root = Path(settings.EVIDENCE_DIR)
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    logger.info("kept %s for %s: %d bytes", kind, reference, len(data))
    return Stored(storage_key=key, content_type=content_type, byte_size=len(data), sha256=digest)


def read(storage_key: str) -> bytes | None:
    """The bytes back, for the API to serve or the report to embed."""
    path = Path(settings.EVIDENCE_DIR) / storage_key
    return path.read_bytes() if path.is_file() else None
