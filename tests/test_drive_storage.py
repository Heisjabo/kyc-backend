"""Evidence on Google Drive.

The Drive API is stood in for, so these test our pipeline rather than Google's:
that a file lands in the right folder, that the key we keep is the one that
reads it back, and that the rest of the service neither knows nor cares which
store is behind it.
"""

import pytest

from app.core.config import settings
from app.storage import drive, evidence


class FakeFiles:
    """The two calls we make: list and create, plus get_media and delete."""

    def __init__(self, store):
        self.store = store

    def list(self, q=None, **kwargs):
        name = q.split("name = '")[1].split("'")[0]
        parent = q.split("and '")[1].split("'")[0]
        match = [
            {"id": key}
            for key, item in self.store.folders.items()
            if item == (name, parent)
        ]
        return _Call({"files": match[:1]})

    def create(self, body=None, media_body=None, **kwargs):
        self.store.calls.append(("create", body, kwargs))
        new_id = f"id-{len(self.store.folders) + len(self.store.blobs) + 1}"
        if body.get("mimeType") == drive.FOLDER_MIME:
            self.store.folders[new_id] = (body["name"], body["parents"][0])
        else:
            data = media_body._fd.getvalue() if media_body is not None else b""
            self.store.blobs[new_id] = (body["name"], body["parents"][0], data)
        return _Call({"id": new_id})

    def get_media(self, fileId=None, **kwargs):
        return _Media(self.store.blobs.get(fileId, (None, None, None))[2])

    def delete(self, fileId=None, **kwargs):
        self.store.blobs.pop(fileId, None)
        return _Call({})


class _Call:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class _Media:
    def __init__(self, data):
        self.data = data


class FakeDrive:
    def __init__(self):
        self.folders, self.blobs, self.calls = {}, {}, []

    def files(self):
        return FakeFiles(self)


@pytest.fixture()
def fake(monkeypatch):
    store = FakeDrive()
    drive.reset()
    monkeypatch.setattr(drive, "_client", lambda: store)
    monkeypatch.setattr(settings, "GOOGLE_SERVICE_ACCOUNT_EMAIL", "kyc@project.iam.gserviceaccount.com")
    monkeypatch.setattr(settings, "GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY",
                        "-----BEGIN PRIVATE KEY-----\\nx\\n-----END PRIVATE KEY-----")
    monkeypatch.setattr(settings, "GOOGLE_DRIVE_FOLDER_ID", "configured-folder")
    monkeypatch.setattr(settings, "GOOGLE_DRIVE_ROOT_FOLDER", "KYC Verifications")

    # The download helper streams; with a fake there is nothing to stream.
    def fake_download(file_id):
        found = store.blobs.get(file_id)
        return found[2] if found else None

    monkeypatch.setattr(drive, "download", fake_download)
    yield store
    drive.reset()


def test_a_file_goes_into_a_folder_named_by_our_verification_id(fake):
    """Never the client's reference: that is a string they choose, unique only
    within their account, so two clients could land in one folder."""
    kept = evidence.keep(41, b"document bytes", "document_front")
    assert kept.byte_size == len(b"document bytes")

    root = _folder_named(fake, "KYC Verifications")
    assert fake.folders[root][1] == "configured-folder"

    case = _folder_named(fake, "41")
    assert fake.folders[case][1] == root

    stored_name, stored_parent, data = fake.blobs[kept.storage_key]
    assert stored_parent == case
    assert data == b"document bytes"
    assert stored_name.startswith("document_front-")


def test_every_verification_sits_under_the_one_platform_folder(fake):
    evidence.keep(1, b"a", "portrait")
    evidence.keep(2, b"b", "portrait")

    root = _folder_named(fake, "KYC Verifications")
    assert {name: parent for name, parent in fake.folders.values()} == {
        "KYC Verifications": "configured-folder",
        "1": root,
        "2": root,
    }


def _folder_named(fake, name):
    return next(key for key, item in fake.folders.items() if item[0] == name)


def test_the_key_we_keep_is_the_drive_id(fake):
    kept = evidence.keep(2, b"portrait bytes", "portrait")
    assert kept.storage_key in fake.blobs
    assert evidence.read(kept.storage_key) == b"portrait bytes"


def test_a_second_file_reuses_the_same_folder(fake):
    first = evidence.keep(3, b"front", "document_front")
    second = evidence.keep(3, b"back", "document_back")
    assert fake.blobs[first.storage_key][1] == fake.blobs[second.storage_key][1]
    assert len(fake.folders) == 2                # the platform folder, and this one


def test_the_file_name_says_what_it_is(fake):
    kept = evidence.keep(4, b"clip", "challenge_video", "video/webm")
    assert fake.blobs[kept.storage_key][0].startswith("challenge_video-")
    assert fake.blobs[kept.storage_key][0].endswith(".webm")


def test_nothing_is_ever_shared(fake):
    """No permission is granted and no link is made: these files are read back
    through the API."""
    evidence.keep(5, b"bytes", "portrait")
    assert all(call[0] != "share" for call in fake.calls)
    for _, body, _ in fake.calls:
        assert "permissions" not in body and "shared" not in body


def test_a_missing_file_reads_as_nothing_rather_than_an_error(fake):
    assert evidence.read("no-such-id") is None


def test_evidence_can_be_deleted(fake):
    kept = evidence.keep(6, b"bytes", "portrait")
    assert evidence.remove(kept.storage_key) is True
    assert kept.storage_key not in fake.blobs


def test_a_service_account_key_is_read_from_its_two_fields(monkeypatch):
    """An environment variable cannot hold a real newline, so the private key
    arrives with them written out and has to be put back, or the key will not
    parse."""
    monkeypatch.setattr(settings, "GOOGLE_SERVICE_ACCOUNT_EMAIL", "kyc@project.iam.gserviceaccount.com")
    monkeypatch.setattr(settings, "GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY",
                        "-----BEGIN PRIVATE KEY-----\\nsecret\\n-----END PRIVATE KEY-----")
    info = drive._service_account_info()
    assert info["type"] == "service_account"
    assert info["client_email"] == "kyc@project.iam.gserviceaccount.com"
    assert "\n" in info["private_key"] and "\\n" not in info["private_key"]


def test_a_whole_key_file_works_too(monkeypatch):
    import json

    monkeypatch.setattr(settings, "GOOGLE_SERVICE_ACCOUNT_EMAIL", "")
    monkeypatch.setattr(settings, "GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY", "")
    monkeypatch.setattr(settings, "GOOGLE_SERVICE_ACCOUNT_JSON",
                        json.dumps({"type": "service_account", "client_email": "a@b.iam.gserviceaccount.com"}))
    assert drive._service_account_info()["client_email"] == "a@b.iam.gserviceaccount.com"


def test_a_key_without_a_folder_is_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_SERVICE_ACCOUNT_EMAIL", "kyc@project.iam.gserviceaccount.com")
    monkeypatch.setattr(settings, "GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY", "a-key")
    monkeypatch.setattr(settings, "GOOGLE_DRIVE_FOLDER_ID", "")
    assert drive.configured() is False
    assert drive.missing_settings() == ["GOOGLE_DRIVE_FOLDER_ID"]


def test_without_drive_settings_evidence_stays_on_this_machine(monkeypatch, tmp_path):
    """How the tests and a developer's laptop run. There is no switch: the Drive
    settings being empty is what decides it."""
    for name in ("GOOGLE_SERVICE_ACCOUNT_EMAIL", "GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY",
                 "GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_DRIVE_FOLDER_ID"):
        monkeypatch.setattr(settings, name, "")
    monkeypatch.setattr(settings, "EVIDENCE_DIR", str(tmp_path))
    kept = evidence.keep(8, b"bytes", "portrait")
    assert kept.storage_key == "8/portrait-" + kept.sha256[:12] + ".jpg"
    assert evidence.read(kept.storage_key) == b"bytes"


def test_the_service_refuses_to_start_with_drive_half_configured(monkeypatch):
    """The key and the folder, or neither. Finding out mid-verification means a
    document is read, judged and then dropped, with somebody standing there
    holding their passport."""
    from app.core import security

    monkeypatch.setattr(settings, "GOOGLE_SERVICE_ACCOUNT_EMAIL", "kyc@project.iam.gserviceaccount.com")
    monkeypatch.setattr(settings, "GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY", "a-key")
    monkeypatch.setattr(settings, "GOOGLE_DRIVE_FOLDER_ID", "")

    with pytest.raises(RuntimeError) as refused:
        security.check_evidence_store()
    assert "GOOGLE_DRIVE_FOLDER_ID" in str(refused.value)

    monkeypatch.setattr(settings, "GOOGLE_DRIVE_FOLDER_ID", "a-folder")
    security.check_evidence_store()            # a key and a folder: starts


def test_no_drive_settings_at_all_is_a_fine_way_to_start(monkeypatch):
    from app.core import security

    for name in ("GOOGLE_SERVICE_ACCOUNT_EMAIL", "GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY",
                 "GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_DRIVE_FOLDER_ID"):
        monkeypatch.setattr(settings, name, "")
    security.check_evidence_store()
