"""Google Drive as the evidence store.

Credentials are a Google Cloud service account key.
A service account owns no Drive storage of its own, so the folder has to be on a
Shared Drive with the service account as a member, or uploads come back with
"Service Accounts do not have storage quota". The access token is fetched from
the key on first use and refreshed as it expires, in memory.

Files are never shared: they are read back through the API, not from Drive.

Everything goes under one named folder, GOOGLE_DRIVE_ROOT_FOLDER, inside the
configured folder id, with one subfolder per verification named by our own
verification id. Folder ids are cached per process so a lookup does not cost a
round trip per file.
"""

import io
import logging
import threading

from app.core.config import settings

logger = logging.getLogger(__name__)

FOLDER_MIME = "application/vnd.google-apps.folder"
_SCOPES = ["https://www.googleapis.com/auth/drive"]

_service = None
_root: str | None = None
_folders: dict[str, str] = {}
_lock = threading.Lock()


class DriveNotConfigured(RuntimeError):
    """Drive is the chosen store but nothing has been set up to reach it."""


def _service_account_info() -> dict | None:
    """The service account key, from its two fields or from a whole JSON key."""
    if settings.GOOGLE_SERVICE_ACCOUNT_EMAIL and settings.GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY:
        return {
            "type": "service_account",
            "client_email": settings.GOOGLE_SERVICE_ACCOUNT_EMAIL,
            # An environment variable cannot hold a real newline, so the key
            # arrives with them written out and they are put back here.
            "private_key": settings.GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY.replace("\\n", "\n"),
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    if settings.GOOGLE_SERVICE_ACCOUNT_JSON:
        import json

        return json.loads(settings.GOOGLE_SERVICE_ACCOUNT_JSON)
    return None


def configured() -> bool:
    """A folder, plus a key to write into it."""
    return bool(settings.GOOGLE_DRIVE_FOLDER_ID) and _service_account_info() is not None


def anything_set() -> bool:
    """Has anybody started configuring Drive? Nothing set means the local folder."""
    return any(
        (
            settings.GOOGLE_DRIVE_FOLDER_ID,
            settings.GOOGLE_SERVICE_ACCOUNT_EMAIL,
            settings.GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY,
            settings.GOOGLE_SERVICE_ACCOUNT_JSON,
        )
    )


def missing_settings() -> list[str]:
    """What is not set. Empty when Drive is fully configured."""
    missing = []
    if _service_account_info() is None:
        missing.append(
            "GOOGLE_SERVICE_ACCOUNT_EMAIL with GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY "
            "(or GOOGLE_SERVICE_ACCOUNT_JSON)"
        )
    if not settings.GOOGLE_DRIVE_FOLDER_ID:
        missing.append("GOOGLE_DRIVE_FOLDER_ID")
    return missing


def _client():
    """The Drive client, built once."""
    global _service
    if _service is not None:
        return _service

    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    if not configured():
        raise DriveNotConfigured("Drive is not configured: " + ", ".join(missing_settings()))

    credentials = service_account.Credentials.from_service_account_info(
        _service_account_info(), scopes=_SCOPES
    )
    _service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    return _service


def _shared_drive_args() -> dict:
    """Sent on every call: a Shared Drive is invisible without it."""
    return {"supportsAllDrives": True}


def _find_or_make_folder(name: str, parent: str) -> str:
    service = _client()
    safe = name.replace("\\", "\\\\").replace("'", "\\'")
    query = (
        f"name = '{safe}' and mimeType = '{FOLDER_MIME}' "
        f"and '{parent}' in parents and trashed = false"
    )
    found = (
        service.files()
        .list(q=query, fields="files(id)", pageSize=1,
              includeItemsFromAllDrives=True, **_shared_drive_args())
        .execute()
        .get("files", [])
    )
    if found:
        return found[0]["id"]
    return (
        service.files()
        .create(
            body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent]},
            fields="id",
            **_shared_drive_args(),
        )
        .execute()["id"]
    )


def root_folder() -> str:
    """The one folder the platform writes into, made if it is not there."""
    global _root
    with _lock:
        if _root is not None:
            return _root

    parent = settings.GOOGLE_DRIVE_FOLDER_ID
    if not parent:
        raise DriveNotConfigured("Set GOOGLE_DRIVE_FOLDER_ID to the folder evidence should live in")

    folder_id = _find_or_make_folder(settings.GOOGLE_DRIVE_ROOT_FOLDER, parent)
    with _lock:
        _root = folder_id
    return folder_id


def folder_for(verification_id: str) -> str:
    """The folder holding one verification's evidence, made if it is not there.

    Named by our own id. A client's reference is theirs to choose and only
    unique within their account, so two clients using the same string would
    otherwise share a folder.
    """
    key = str(verification_id)
    with _lock:
        if key in _folders:
            return _folders[key]

    folder_id = _find_or_make_folder(key, root_folder())
    with _lock:
        _folders[key] = folder_id
    return folder_id


def upload(verification_id: str, name: str, data: bytes, content_type: str) -> str:
    """Put one file in a verification's folder and return its Drive id.

    The id is what goes in verification_files.storage_key.
    """
    from googleapiclient.http import MediaIoBaseUpload

    service = _client()
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=content_type, resumable=False)
    created = (
        service.files()
        .create(
            body={"name": name, "parents": [folder_for(verification_id)]},
            media_body=media,
            fields="id",
            **_shared_drive_args(),
        )
        .execute()
    )
    logger.info("stored %s for verification %s (%d bytes)", name, verification_id, len(data))
    return created["id"]


def download(file_id: str) -> bytes | None:
    """The bytes back, or None if the file is gone.

    Missing is not an error: a retention rule may have removed it.
    """
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaIoBaseDownload

    service = _client()
    buffer = io.BytesIO()
    try:
        request = service.files().get_media(fileId=file_id, **_shared_drive_args())
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    except HttpError as error:
        if error.resp.status in (404, 410):
            logger.warning("evidence %s is no longer on Drive", file_id)
            return None
        raise
    return buffer.getvalue()


def delete(file_id: str) -> bool:
    """Remove one file."""
    from googleapiclient.errors import HttpError

    try:
        _client().files().delete(fileId=file_id, **_shared_drive_args()).execute()
        return True
    except HttpError as error:
        if error.resp.status in (404, 410):
            return False
        raise


def reset() -> None:
    """Forget the client and the folder ids. For tests and for a credential change."""
    global _service, _root
    with _lock:
        _service = None
        _root = None
        _folders.clear()
