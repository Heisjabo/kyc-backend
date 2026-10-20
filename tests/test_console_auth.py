"""Signing in to the admin console, and keeping clients and staff apart.

Against Postgres in a throwaway schema, like the account tests, and skipped when
it is not reachable.
"""

import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.base import Base
from app.db.models import Account, ApiKey, AuditEvent, PasswordReset
from app.db.session import get_session
from app.main import app
from app.scripts.console_admin import (
    ConsoleAdminError,
    create_console_account,
    grant_console_access,
    revoke_console_access,
)

PASSWORD = "horse-battery"
STAFF = {
    "email": "Admin@example.com",
    "password": PASSWORD,
    "first_name": "Jean",
    "last_name": "Mugisha",
    "role": "owner",
    "products": ["kyc"],
    "phone": "788123456",
    "country_code": "+250",
    "country": "Rwanda",
    "date_of_birth": date(1995, 4, 12),
}
CLIENT = {
    "email": "client@example.com",
    "password": PASSWORD,
    "first_name": "Amina",
    "last_name": "Wanjiru",
    "phone": "712004118",
    "country_code": "+254",
    "date_of_birth": "1990-01-01",
    "country": "Kenya",
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
    schema = f"test_{uuid.uuid4().hex[:12]}"
    engine = create_engine(settings.DATABASE_URL, connect_args={"options": f"-csearch_path={schema}"})
    admin = create_engine(settings.DATABASE_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    # Every table: signing in is written to audit_events, which points at the others.
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
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def console_login(client, email=STAFF["email"], password=PASSWORD):
    return client.post("/v1/console/auth/login", json={"email": email, "password": password})


# Signing in

def test_staff_sign_in_and_see_their_role_and_products(client, db_session):
    create_console_account(db_session, **STAFF)
    response = console_login(client, email="  ADMIN@example.com ")
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["user"]["email"] == "admin@example.com"
    assert body["user"]["role"] == "owner"
    assert body["user"]["products"] == ["kyc"]
    assert body["user"]["last_login_at"] is not None

    me = client.get("/v1/console/auth/me", headers=auth(body["access_token"]))
    assert me.status_code == 200
    assert me.json()["role"] == "owner"


def test_a_wrong_password_and_a_client_account_get_the_same_answer(client, db_session):
    create_console_account(db_session, **STAFF)
    client.post("/v1/auth/register", json=CLIENT)

    wrong_password = console_login(client, password="not-the-one")
    client_account = console_login(client, email=CLIENT["email"])
    nobody = console_login(client, email="nobody@example.com")

    assert wrong_password.status_code == client_account.status_code == nobody.status_code == 401
    assert wrong_password.json() == client_account.json() == nobody.json()


def test_a_suspended_staff_account_cannot_sign_in(client, db_session):
    account = create_console_account(db_session, **STAFF)
    account.is_active = False
    db_session.commit()
    assert console_login(client).status_code == 403


# Keeping the two kinds of token apart

def test_a_client_token_does_not_open_the_console(client):
    token = client.post("/v1/auth/register", json=CLIENT).json()["access_token"]
    assert client.get("/v1/console/auth/me", headers=auth(token)).status_code == 401


def test_a_console_token_does_not_work_as_a_client_token(client, db_session):
    create_console_account(db_session, **STAFF)
    token = console_login(client).json()["access_token"]
    assert client.get("/v1/auth/me", headers=auth(token)).status_code == 401


def test_taking_the_role_away_locks_out_a_token_already_issued(client, db_session):
    create_console_account(db_session, **STAFF)
    token = console_login(client).json()["access_token"]
    revoke_console_access(db_session, email=STAFF["email"])
    assert client.get("/v1/console/auth/me", headers=auth(token)).status_code == 401


def test_without_a_token_the_console_asks_to_sign_in(client):
    assert client.get("/v1/console/auth/me").status_code == 401
    assert client.get("/v1/console/auth/me", headers=auth("not-a-token")).status_code == 401


# The command-line helpers

def test_granting_console_access_to_an_existing_client_account(client, db_session):
    client.post("/v1/auth/register", json=CLIENT)
    account = grant_console_access(db_session, email=CLIENT["email"], role="reviewer", products=["kyc"])
    assert account.console_role == "reviewer"
    assert account.console_products == ["kyc"]
    assert console_login(client, email=CLIENT["email"]).status_code == 200


def test_bad_roles_products_and_passwords_are_refused(db_session):
    with pytest.raises(ConsoleAdminError):
        create_console_account(db_session, **{**STAFF, "role": "superuser"})
    with pytest.raises(ConsoleAdminError):
        create_console_account(db_session, **{**STAFF, "products": ["payments"]})
    with pytest.raises(ConsoleAdminError):
        create_console_account(db_session, **{**STAFF, "password": "short"})


def test_creating_an_address_that_already_has_an_account_is_refused(db_session):
    create_console_account(db_session, **STAFF)
    with pytest.raises(ConsoleAdminError):
        create_console_account(db_session, **STAFF)


def test_a_console_token_stops_working_when_the_password_changes(client, db_session):
    create_console_account(db_session, **STAFF)
    token = console_login(client).json()["access_token"]
    changed = client.post("/v1/console/auth/password", headers=auth(token),
                          json={"current_password": PASSWORD, "new_password": "a-much-longer-passphrase"})
    assert changed.status_code == 200
    assert client.get("/v1/console/auth/me", headers=auth(token)).status_code == 401
    again = console_login(client, password="a-much-longer-passphrase")
    assert client.get("/v1/console/auth/me", headers=auth(again.json()["access_token"])).status_code == 200
