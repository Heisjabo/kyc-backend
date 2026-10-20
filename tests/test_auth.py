"""Accounts, sessions, password resets and API keys, end to end.

These run against Postgres, in a throwaway schema dropped afterwards, because
that is what production runs on: ids are BIGINT, which SQLite will not
autoincrement, and enum and JSONB columns behave differently there too. A test
on a different database than the one you ship is a test of something else.

They skip when Postgres is not reachable rather than failing, so the rest of the
suite still runs on a machine without it.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.api.deps import account_from_api_key
from app.core.config import settings
from app.db.base import Base
from app.db.models import Account, ApiKey, PasswordReset
from app.db.session import get_session
from app.main import app

GOOD_PASSWORD = "horse-battery"          # 13, inside the 12 to 128 rule
SIGN_UP = {
    "email": "Jean@example.com",
    "password": GOOD_PASSWORD,
    "first_name": "Jean",
    "last_name": "Mugisha",
    "phone": "788123456",
    "country_code": "+250",
    "date_of_birth": "1995-04-12",
    "country": "Rwanda",
    # The optional ones, which an account is perfectly usable without.
    "gender": "male",
    "city": "Kigali",
    "address": "KN 5 Ave",
}


def _reachable() -> bool:
    try:
        create_engine(settings.DATABASE_URL).connect().close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="needs the local Postgres")


@pytest.fixture()
def db_session():
    """A schema of this test's own, so nothing touches the developer's data."""
    schema = f"test_{uuid.uuid4().hex[:12]}"
    engine = create_engine(
        settings.DATABASE_URL,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    admin = create_engine(settings.DATABASE_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    # Every table: making or revoking a key is also written to audit_events.
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture()
def client(db_session):
    app.dependency_overrides[get_session] = lambda: db_session
    # A route of our own, so the API key dependency can be exercised as a caller
    # would meet it rather than by calling the function directly.
    guarded = FastAPI()
    guarded.get("/guarded")(lambda account=Depends(account_from_api_key): {"account": account.email})
    guarded.dependency_overrides[get_session] = lambda: db_session
    with TestClient(app) as main_client, TestClient(guarded) as key_client:
        main_client.guarded = key_client
        yield main_client
    app.dependency_overrides.clear()


def register(client, **overrides):
    return client.post("/v1/auth/register", json={**SIGN_UP, **overrides})


def signed_in(client) -> str:
    return register(client).json()["access_token"]


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# Registering

def test_registering_opens_an_account_and_signs_in(client):
    response = register(client)
    body = response.json()
    assert response.status_code == 201
    assert body["account"]["email"] == "jean@example.com"      # stored lower case
    assert body["account"]["first_name"] == "Jean" and body["account"]["country"] == "Rwanda"
    assert body["expires_in"] > 0 and body["token_type"] == "bearer"
    assert "password" not in str(body)


def test_the_password_is_never_stored_in_the_clear(client, db_session):
    register(client)
    stored = db_session.scalar(select(Account)).password_hash
    assert GOOD_PASSWORD not in stored
    assert stored.startswith("$argon2id$")


def test_an_address_can_only_have_one_account(client):
    register(client)
    assert register(client, first_name="Someone", last_name="Else").status_code == 409
    assert register(client, email="JEAN@example.com").status_code == 409   # case does not matter


@pytest.mark.parametrize("password", ["short", "elevenchars"])
def test_a_password_under_twelve_characters_is_refused(client, password):
    assert register(client, password=password).status_code == 422


def test_long_passphrases_are_allowed_up_to_128(client):
    assert register(client, password="correct horse battery staple " * 4).status_code == 201   # 116
    assert register(client, email="b@example.com", password="a1b2c3d4" * 17).status_code == 422  # 136


@pytest.mark.parametrize("password", ["password1234", "Password123456", "qwertyuiop123", "welcome2025!!", "aaaaaaaaaaaaaa"])
def test_a_common_or_repetitive_password_is_refused(client, password):
    response = register(client, password=password)
    assert response.status_code == 422 and ("common" in str(response.json()) or "repeats" in str(response.json()))


def test_a_new_password_is_checked_on_reset_too(client):
    register(client)
    assert client.post("/v1/auth/reset-password", json={"token": "made-up", "password": "password1234"}).status_code == 422


@pytest.mark.parametrize("missing", ["first_name", "last_name", "phone", "country_code", "date_of_birth", "country"])
def test_the_signup_details_are_required(client, missing):
    """Every contact and identity detail asked for at signup is required."""
    body = {key: value for key, value in SIGN_UP.items() if key != missing}
    assert client.post("/v1/auth/register", json=body).status_code == 422


@pytest.mark.parametrize("optional", ["gender", "city", "address"])
def test_the_softer_details_are_optional(client, optional):
    body = {key: value for key, value in SIGN_UP.items() if key != optional}
    assert client.post("/v1/auth/register", json=body).status_code == 201


# Signing in

def test_signing_in_with_the_right_password(client):
    register(client)
    response = client.post("/v1/auth/login", json={"email": "jean@example.com", "password": GOOD_PASSWORD})
    assert response.status_code == 200 and response.json()["access_token"]


def test_a_wrong_password_and_an_unknown_address_look_the_same(client):
    register(client)
    wrong = client.post("/v1/auth/login", json={"email": "jean@example.com", "password": "not-the-password"})
    unknown = client.post("/v1/auth/login", json={"email": "nobody@example.com", "password": GOOD_PASSWORD})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]


def test_a_suspended_account_cannot_sign_in(client, db_session):
    register(client)
    db_session.scalar(select(Account)).is_active = False
    db_session.commit()
    response = client.post("/v1/auth/login", json={"email": "jean@example.com", "password": GOOD_PASSWORD})
    assert response.status_code == 403


def test_the_session_token_identifies_the_account(client):
    token = signed_in(client)
    me = client.get("/v1/auth/me", headers=auth(token)).json()
    assert me["email"] == "jean@example.com"
    assert me["phone"] == "788123456" and me["country_code"] == "+250"


def test_no_token_and_a_tampered_token_are_both_refused(client):
    token = signed_in(client)
    assert client.get("/v1/auth/me").status_code == 401
    assert client.get("/v1/auth/me", headers=auth(token[:-2] + "xy")).status_code == 401


# Forgetting a password

def test_the_answer_is_the_same_whether_the_address_is_known(client):
    """Confirming which addresses have accounts hands over a list worth stealing."""
    register(client)
    known = client.post("/v1/auth/forgot-password", json={"email": "jean@example.com"})
    unknown = client.post("/v1/auth/forgot-password", json={"email": "nobody@example.com"})
    assert known.status_code == unknown.status_code == 200
    assert known.json()["message"] == unknown.json()["message"]
    assert unknown.json()["reset_token"] is None


def test_a_reset_token_sets_a_new_password(client):
    register(client)
    token = client.post("/v1/auth/forgot-password", json={"email": "jean@example.com"}).json()["reset_token"]
    assert token                                    # development only, while there is no email

    response = client.post("/v1/auth/reset-password", json={"token": token, "password": "brand-new-pass"})
    assert response.status_code == 200

    old = client.post("/v1/auth/login", json={"email": "jean@example.com", "password": GOOD_PASSWORD})
    new = client.post("/v1/auth/login", json={"email": "jean@example.com", "password": "brand-new-pass"})
    assert old.status_code == 401 and new.status_code == 200


def test_a_reset_token_works_once(client):
    register(client)
    token = client.post("/v1/auth/forgot-password", json={"email": "jean@example.com"}).json()["reset_token"]
    client.post("/v1/auth/reset-password", json={"token": token, "password": "brand-new-pass"})
    again = client.post("/v1/auth/reset-password", json={"token": token, "password": "another-pass12"})
    assert again.status_code == 400


def test_an_expired_reset_token_is_refused(client, db_session):
    register(client)
    token = client.post("/v1/auth/forgot-password", json={"email": "jean@example.com"}).json()["reset_token"]
    row = db_session.scalar(select(PasswordReset))
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.commit()
    assert client.post("/v1/auth/reset-password", json={"token": token, "password": "brand-new-pass"}).status_code == 400


def test_an_invented_token_is_refused(client):
    register(client)
    assert client.post("/v1/auth/reset-password", json={"token": "made-up", "password": "brand-new-pass"}).status_code == 400


def test_the_reset_token_is_not_stored_in_the_clear(client, db_session):
    register(client)
    token = client.post("/v1/auth/forgot-password", json={"email": "jean@example.com"}).json()["reset_token"]
    assert db_session.scalar(select(PasswordReset)).token_hash != token


# API keys

def make_key(client, token, **overrides):
    return client.post("/v1/api-keys", json={"name": "production", **overrides}, headers=auth(token))


def test_a_key_is_returned_once_and_never_stored(client, db_session):
    token = signed_in(client)
    body = make_key(client, token).json()
    assert body["key"].startswith("kyc_")
    assert body["is_valid"] is True

    stored = db_session.scalar(select(ApiKey))
    assert body["key"] not in stored.secret_hash
    assert stored.prefix in body["key"]
    assert body["key"] not in str(client.get("/v1/api-keys", headers=auth(token)).json())


def test_keys_expire_and_the_ceiling_is_enforced(client):
    token = signed_in(client)
    short = make_key(client, token, expires_in_days=1).json()
    assert short["expires_at"]
    assert make_key(client, token, expires_in_days=10_000).status_code == 422
    assert make_key(client, token, expires_in_days=0).status_code == 422


def test_a_key_authorises_calls_for_its_account(client):
    token = signed_in(client)
    key = make_key(client, token).json()["key"]
    response = client.guarded.get("/guarded", headers={"X-API-Key": key})
    assert response.status_code == 200 and response.json()["account"] == "jean@example.com"


def test_a_key_whose_secret_contains_an_underscore_still_works(client):
    """token_urlsafe's alphabet includes the underscore, so about half of all
    keys carry one. Splitting on every underscore rejected those as malformed."""
    from app.core import security

    assert security.split_api_key("kyc_abc123def456_aa_bb_cc") == "abc123def456"
    assert all(security.split_api_key(security.new_api_key()[0]) for _ in range(50))


def test_a_missing_wrong_or_malformed_key_is_refused(client):
    token = signed_in(client)
    key = make_key(client, token).json()["key"]
    for header in ({}, {"X-API-Key": "nonsense"}, {"X-API-Key": key[:-3] + "xyz"}):
        assert client.guarded.get("/guarded", headers=header).status_code == 401


def test_a_revoked_key_stops_working(client):
    token = signed_in(client)
    created = make_key(client, token).json()
    assert client.guarded.get("/guarded", headers={"X-API-Key": created["key"]}).status_code == 200

    revoked = client.post(f"/v1/api-keys/{created['id']}/revoke", headers=auth(token))
    assert revoked.status_code == 200 and revoked.json()["is_valid"] is False
    assert client.guarded.get("/guarded", headers={"X-API-Key": created["key"]}).status_code == 401

    # Deleting a key that still works stops it too.
    other = make_key(client, token).json()
    deleted = client.delete(f"/v1/api-keys/{other['id']}", headers=auth(token))
    assert deleted.status_code == 200 and deleted.json()["deleted_at"] and deleted.json()["revoked_at"]
    assert client.guarded.get("/guarded", headers={"X-API-Key": other["key"]}).status_code == 401


def test_an_expired_key_stops_working(client, db_session):
    token = signed_in(client)
    created = make_key(client, token).json()
    row = db_session.scalar(select(ApiKey))
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    assert client.guarded.get("/guarded", headers={"X-API-Key": created["key"]}).status_code == 401


def test_using_a_key_records_when(client, db_session):
    token = signed_in(client)
    key = make_key(client, token).json()["key"]
    assert db_session.scalar(select(ApiKey)).last_used_at is None
    client.guarded.get("/guarded", headers={"X-API-Key": key})
    assert db_session.scalar(select(ApiKey)).last_used_at is not None


def test_a_key_belonging_to_a_suspended_account_stops_working(client, db_session):
    token = signed_in(client)
    key = make_key(client, token).json()["key"]
    db_session.scalar(select(Account)).is_active = False
    db_session.commit()
    assert client.guarded.get("/guarded", headers={"X-API-Key": key}).status_code == 401


def test_one_account_cannot_revoke_another_accounts_key(client):
    mine = signed_in(client)
    created = make_key(client, mine).json()
    theirs = register(client, email="other@example.com", first_name="Other").json()["access_token"]
    assert client.delete(f"/v1/api-keys/{created['id']}", headers=auth(theirs)).status_code == 404
    assert client.guarded.get("/guarded", headers={"X-API-Key": created["key"]}).status_code == 200


def test_keys_are_listed_only_to_their_owner(client):
    mine = signed_in(client)
    make_key(client, mine)
    theirs = register(client, email="other@example.com", first_name="Other").json()["access_token"]
    assert client.get("/v1/api-keys", headers=auth(mine)).json()["total"] == 1
    assert client.get("/v1/api-keys", headers=auth(theirs)).json()["items"] == []


def test_managing_keys_needs_a_session(client):
    assert client.get("/v1/api-keys").status_code == 401
    assert client.post("/v1/api-keys", json={"name": "x"}).status_code == 401


def test_the_auth_endpoints_are_in_the_api_schema(client):
    paths = client.get("/openapi.json").json()["paths"]
    for path in ("/v1/auth/register", "/v1/auth/login", "/v1/auth/me",
                 "/v1/auth/forgot-password", "/v1/auth/reset-password",
                 "/v1/api-keys", "/v1/api-keys/{key_id}"):
        assert path in paths


# Tokens that outlive a password

def test_a_token_stops_working_when_the_password_is_reset(client):
    token = signed_in(client)
    assert client.get("/v1/auth/me", headers=auth(token)).status_code == 200
    reset = client.post("/v1/auth/forgot-password", json={"email": "jean@example.com"}).json()["reset_token"]
    assert client.post("/v1/auth/reset-password", json={"token": reset, "password": "brand-new-pass"}).status_code == 200

    assert client.get("/v1/auth/me", headers=auth(token)).status_code == 401
    new = client.post("/v1/auth/login", json={"email": "jean@example.com", "password": "brand-new-pass"})
    assert client.get("/v1/auth/me", headers=auth(new.json()["access_token"])).status_code == 200
