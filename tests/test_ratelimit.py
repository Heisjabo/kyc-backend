"""How often one caller may try something."""

import pytest
from fastapi.testclient import TestClient

from app.core import ratelimit
from app.core.config import settings
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def limits_on(monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMITS_ENABLED", True)
    ratelimit.limiter.reset()


def login(email="nobody@example.com", password="wrong-password", ip="203.0.113.7"):
    return client.post(
        "/v1/auth/login", json={"email": email, "password": password}, headers={"X-Real-IP": ip}
    )


def test_the_window_lets_n_through_then_refuses_then_recovers():
    window = ratelimit.SlidingWindow()
    assert [window.hit("k", 3, 10, now=t) for t in (0, 1, 2)] == [0, 0, 0]
    wait = window.hit("k", 3, 10, now=3)
    assert wait == pytest.approx(7)
    assert window.hit("k", 3, 10, now=10.5) == 0   # the first one has left the window


def test_keys_are_independent():
    window = ratelimit.SlidingWindow()
    for _ in range(3):
        window.hit("a", 3, 10, now=0)
    assert window.hit("a", 3, 10, now=1) > 0
    assert window.hit("b", 3, 10, now=1) == 0


def test_sixty_wrong_passwords_are_not_all_answered():
    codes = [login(email=f"user{i}@example.com", password=f"bad-{i}").status_code for i in range(60)]
    assert codes.count(429) > 40
    assert codes[:10].count(429) == 0          # the first ten per address go through


def test_a_refusal_carries_retry_after():
    for i in range(10):
        login(email=f"u{i}@example.com")
    blocked = login(email="u99@example.com")
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1


def test_repeated_failures_for_one_account_lock_that_address_out_even_with_the_right_password(db_session_with_account):
    email, password = db_session_with_account
    for _ in range(5):
        assert login(email=email, password="wrong-wrong").status_code == 401
    assert login(email=email, password=password).status_code == 429


def test_a_different_address_is_not_locked_out_by_someone_elses_guesses(db_session_with_account):
    email, password = db_session_with_account
    for _ in range(5):
        login(email=email, password="wrong-wrong", ip="198.51.100.1")
    assert login(email=email, password=password, ip="198.51.100.2").status_code == 200


def test_a_good_sign_in_clears_the_failures(db_session_with_account):
    email, password = db_session_with_account
    for _ in range(4):
        login(email=email, password="wrong-wrong")
    assert login(email=email, password=password).status_code == 200
    for _ in range(4):
        assert login(email=email, password="wrong-wrong").status_code == 401


def test_reset_emails_are_limited_per_address():
    codes = [
        client.post("/v1/auth/forgot-password", json={"email": "victim@example.com"},
                    headers={"X-Real-IP": f"198.51.100.{i}"}).status_code
        for i in range(6)
    ]
    assert codes[:3] == [200, 200, 200]
    assert 429 in codes[3:]


def test_registration_is_limited_per_address():
    codes = [client.post("/v1/auth/register", json={}, headers={"X-Real-IP": "203.0.113.9"}).status_code
             for _ in range(7)]
    assert codes[:5] == [422] * 5      # refused as invalid, but counted
    assert codes[5] == 429


def test_session_steps_are_limited_per_link():
    codes = [client.post("/v1/sessions/sometoken/address", headers={"X-Real-IP": "203.0.113.20"}).status_code
             for _ in range(25)]
    assert 429 in codes
    assert codes[0] != 429


def test_off_means_off(monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMITS_ENABLED", False)
    assert 429 not in [login(email=f"x{i}@example.com").status_code for i in range(30)]


@pytest.fixture()
def db_session_with_account(monkeypatch):
    """An account in the local Postgres, in a schema of its own, signed in through the API."""
    import uuid

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from app.core import security
    from app.db.base import Base
    from app.db.models import Account
    from app.db.session import get_session

    schema = f"test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(settings.DATABASE_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(settings.DATABASE_URL, connect_args={"options": f"-csearch_path={schema}"})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    password = "a-decent-password"
    session.add(Account(
        email="real@example.com", password_hash=security.hash_password(password), first_name="R", last_name="L",
        phone="1", country_code="+254", date_of_birth="1990-01-01", country="Kenya",
    ))
    session.commit()
    app.dependency_overrides[get_session] = lambda: session
    try:
        yield "real@example.com", password
    finally:
        app.dependency_overrides.clear()
        session.close()
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
