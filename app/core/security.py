"""Passwords, session tokens and API keys.

Three different secrets, handled three different ways, because they are not the
same kind of thing:

  * **Passwords** are chosen by people, so they are low in entropy and have to be
    hashed slowly. Argon2id, the current OWASP recommendation, with the library's
    own parameters.
  * **API keys** are generated here from 32 random bytes. That is already far
    beyond guessing, so a slow hash buys nothing and would cost a hash on every
    request. SHA-256 is right for them, and the comparison is constant time.
  * **Session tokens** are signed rather than stored: a JWT the caller returns to
    us, checked against a secret. Nothing to look up, and nothing to leak.

The key is shown to its owner once, when it is made. What we keep is a prefix,
which is how a presented key is found, and a hash. A copy of the table is not a
set of working credentials.
"""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.core.config import settings

_hasher = PasswordHasher()

# A key reads as kyc_<prefix>_<secret>, so it is recognisable in a log or a
# support ticket without being usable, and the prefix can be looked up directly.
KEY_PREFIX_BYTES = 6
KEY_SECRET_BYTES = 32


# Passwords

def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, stored: str) -> bool:
    """Constant time by construction: Argon2 verification takes the same work
    whether the password is right or wrong."""
    try:
        return _hasher.verify(stored, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def needs_rehash(stored: str) -> bool:
    """True when the stored hash was made with weaker parameters than we use now,
    so a password can be upgraded quietly the next time someone signs in."""
    try:
        return _hasher.check_needs_rehash(stored)
    except (InvalidHashError, ValueError):
        return True


# Session tokens

def issue_access_token(account_id: str, email: str, version: str = "") -> tuple[str, int]:
    """A signed token for the dashboard, and how many seconds it lasts."""
    seconds = settings.ACCESS_TOKEN_MINUTES * 60
    now = datetime.now(UTC)
    payload = {
        "sub": str(account_id),
        "email": email,
        "iat": now,
        "exp": now + timedelta(seconds=seconds),
        "typ": "access",
        "ver": version,
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256"), seconds


def read_access_token(token: str) -> dict | None:
    """The token's claims, or None if it is expired, altered or not ours."""
    try:
        claims = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    return claims if claims.get("typ") == "access" else None


# Admin console tokens
#
# The console gets a token of its own type. A client's dashboard
# token is never accepted by the console endpoints, and a console token is never
# accepted by the client ones, because each reader checks `typ`. The role and
# products travel in the token for the console's convenience only; the endpoints
# read them again from the account, so a change takes effect at once.

def issue_console_token(
    account_id: str, email: str, role: str, products: list[str], version: str = ""
) -> tuple[str, int]:
    """A signed token for the admin console, and how many seconds it lasts."""
    seconds = settings.ACCESS_TOKEN_MINUTES * 60
    now = datetime.now(UTC)
    payload = {
        "sub": str(account_id),
        "email": email,
        "role": role,
        "products": products,
        "iat": now,
        "exp": now + timedelta(seconds=seconds),
        "typ": "console",
        "ver": version,
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256"), seconds


def read_console_token(token: str) -> dict | None:
    """A console token's claims, or None if it is expired, altered or not one."""
    try:
        claims = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    return claims if claims.get("typ") == "console" else None


# API keys

def new_api_key() -> tuple[str, str, str]:
    """A key to show the owner once, plus the prefix and hash we keep.

    Returns (key, prefix, hash).
    """
    prefix = secrets.token_hex(KEY_PREFIX_BYTES)
    secret = secrets.token_urlsafe(KEY_SECRET_BYTES)
    key = f"kyc_{prefix}_{secret}"
    return key, prefix, fingerprint(key)


def split_api_key(key: str) -> str | None:
    """The prefix inside a presented key, or None if it is not shaped like one.

    Split at most twice: the secret comes from token_urlsafe, whose alphabet
    includes the underscore, so roughly half of all keys carry one. Splitting on
    every underscore rejected those keys as malformed.
    """
    parts = key.split("_", 2)
    if len(parts) != 3 or parts[0] != "kyc" or not parts[1] or not parts[2]:
        return None
    return parts[1]


def fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def same_secret(offered: str, stored: str) -> bool:
    """Constant time, so a wrong key cannot be found one character at a time by
    measuring how long the answer takes."""
    return hmac.compare_digest(offered, stored)


# Callback signatures
#
# Each account signs with a key of its own, derived from the server secret and the
# account id, so a client can check that a callback came from us and one client
# cannot forge a callback to another. The client reads its key from
# GET /v1/auth/callback-secret.

def callback_secret(account_id) -> str:
    return hmac.new(
        settings.JWT_SECRET.encode(), f"callback-v1:{account_id}".encode(), hashlib.sha256
    ).hexdigest()


def sign_callback(account_id, body: bytes, timestamp: int) -> str:
    """sha256=<hex>, over "<timestamp>.<body>". The timestamp is sent too, so the
    receiver can refuse an old copy."""
    key = callback_secret(account_id).encode()
    digest = hmac.new(key, f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# Single-use tokens, for password resets

def new_reset_token() -> tuple[str, str]:
    """The token to send by email, and the hash to store. Returns (token, hash)."""
    token = secrets.token_urlsafe(32)
    return token, fingerprint(token)


def check_evidence_store() -> None:
    """Refuse to start with Drive half configured.

    The key and the folder, or neither. Finding out mid-verification means a
    document is read, judged and then dropped, with somebody standing there
    holding their passport.
    """
    from app.storage import drive

    if drive.configured() or not drive.anything_set():
        # Fully configured, or not configured at all (evidence goes to the local
        # folder, which is how tests and a developer's machine run).
        return
    raise RuntimeError(
        "Google Drive is partly configured: " + ", ".join(drive.missing_settings()) + " not set. "
        "Set the key and the folder, or neither to keep evidence on this machine."
    )


def check_schema() -> None:
    """Refuse to serve a database that is behind this code.

    The alternative is what happened when the generated-image check first shipped:
    the code inserted a column the database did not have, so every verification
    creation returned 500 while /v1/health stayed green, which is the worst of both
    worlds. Failing here is loud, the deploy's health poll catches it, and the log
    says which revision is missing.

    Skipped when there is no alembic_version table at all: the tests build their
    schema straight from the models, and a brand new database has nothing to be
    behind. Skipped in development too, where a developer migrates when they like.
    """
    if settings.ENVIRONMENT == "development":
        return

    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, inspect, text

    engine = create_engine(settings.DATABASE_URL)
    try:
        with engine.connect() as connection:
            if not inspect(connection).has_table("alembic_version"):
                return
            applied = connection.execute(text("select version_num from alembic_version")).scalar()
    finally:
        engine.dispose()

    head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    if applied != head:
        raise RuntimeError(
            f"the database is at revision {applied!r} and this code expects {head!r}. "
            "Run `alembic upgrade head` before serving, or let the entrypoint do it "
            "by leaving RUN_MIGRATIONS unset."
        )


def check_configuration() -> None:
    """Refuse to start with a signing key anyone could guess.

    The default in config.py is published in this repository. Anywhere but a
    developer's machine, that means anybody could mint a session token for any
    account, so the service stops rather than pretending to be guarded.
    """
    check_evidence_store()
    check_schema()

    default = "development-secret-not-for-anywhere-else"
    if settings.ENVIRONMENT == "development":
        return
    if settings.JWT_SECRET == default or len(settings.JWT_SECRET) < 32:
        raise RuntimeError(
            "JWT_SECRET is the published default or too short, and ENVIRONMENT is "
            f"{settings.ENVIRONMENT!r}. Set a long random JWT_SECRET before starting."
        )
    if not (settings.EMAIL_ENABLED and settings.SMTP_HOST):
        # Not fatal: the service is useful without password resets, and email is set up separately.
        import logging

        logging.getLogger(__name__).warning(
            "Email is not configured outside development: password resets will be created but "
            "never delivered. Set EMAIL_ENABLED=true and the SMTP_ settings."
        )
    if settings.CORS_ORIGINS.strip() == "*":
        import logging

        logging.getLogger(__name__).warning(
            "CORS_ORIGINS is '*' outside development: any website may call this API from a browser. "
            "Set it to the front-end addresses."
        )
