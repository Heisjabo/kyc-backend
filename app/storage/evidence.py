"""Where the evidence of a verification is kept.

Two things come out of a check and have to survive it: the images and clips it
judged, and the report that will be built from them. This is where they go.

The bytes go to Google Drive, one subfolder per verification named by our own
verification id, and the storage key is the Drive file id. Where Drive is not
configured, which is how the tests and a developer's laptop run, they go to a
folder on this machine and the key is a path under EVIDENCE_DIR. There is no
switch: the Drive settings decide it.

Either way the row in verification_files records the key, the size and a SHA-256,
so the file can later be proved to be the one that was judged. The key is an
opaque string rather than a path, so swapping the backend changes this module and
nothing else.

Nothing here is served directly to a browser: these files are read back through
the API and embedded in the report rather than linked.
"""

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings
from app.storage import drive

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


def _keep_local(verification_id: int | str, name: str, data: bytes, kind: str) -> str:
    key = f"{verification_id}/{name}"
    path = Path(settings.EVIDENCE_DIR) / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    logger.info("kept %s for verification %s locally: %d bytes", kind, verification_id, len(data))
    return key


def keep(
    verification_id: int | str, data: bytes, kind: str, content_type: str = "image/jpeg"
) -> Stored | None:
    """Write one piece of evidence and describe it. None if there is nothing.

    `kind` is one of the FileKind values, and it becomes part of the file's name
    so a folder can be read by a person without opening anything.
    """
    if not data:
        return None

    digest = hashlib.sha256(data).hexdigest()
    name = f"{kind}-{digest[:12]}{_EXTENSIONS.get(content_type, '.bin')}"

    # Always keep local copy for lightning-fast reads
    local_key = _keep_local(verification_id, name, data, kind)

    key = None
    if _on_drive():
        try:
            key = drive.upload(verification_id, name, data, content_type)
            # Also cache by the drive file_id locally so read(key) will find it directly
            cache_file = Path(settings.EVIDENCE_DIR) / key
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_bytes(data)
        except Exception as err:
            logger.warning("Google Drive upload failed (%s); falling back to local storage", err)

    if not key:
        key = local_key

    return Stored(storage_key=key, content_type=content_type, byte_size=len(data), sha256=digest)


def read(storage_key: str) -> bytes | None:
    """The bytes back, for the API to serve or the report to embed. None when the
    file is gone, which a retention rule is allowed to do."""
    # 1. Fast local read: <1ms if cached or kept locally
    path = Path(settings.EVIDENCE_DIR) / storage_key
    if path.is_file():
        return path.read_bytes()

    # 2. Fall back to Google Drive download if not in local cache
    if _on_drive():
        try:
            data = drive.download(storage_key)
            if data is not None:
                # Save to local cache so next read is instant
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                except Exception:
                    pass
                return data
        except Exception as err:
            logger.warning("Failed to fetch evidence %s from Drive: %s", storage_key, err)

    return None


def remove(storage_key: str) -> bool:
    """Delete one piece of evidence."""
    if _on_drive():
        try:
            return drive.delete(storage_key)
        except Exception:
            pass
    path = Path(settings.EVIDENCE_DIR) / storage_key
    if not path.is_file():
        return False
    path.unlink()
    return True


def _on_drive() -> bool:
    return drive.configured()
