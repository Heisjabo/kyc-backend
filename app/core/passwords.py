"""The password rule, applied wherever a person chooses a new password.

Length is what matters most (NIST SP 800-63B): at least 12 characters, and long
passphrases are welcome, so the ceiling is high. No mixture of character classes is
demanded; instead a password is refused if it is one of the commonly leaked ones.
The list is bundled, so nothing about a password ever leaves this service.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from app.core.config import settings

_LIST = Path(__file__).with_name("common_passwords.txt")


@lru_cache(maxsize=1)
def _common() -> frozenset[str]:
    return frozenset(line.strip() for line in _LIST.read_text().splitlines() if line.strip())


def _stripped(password: str) -> str:
    """Lower case, with the digits and punctuation people tack on the end taken off."""
    return re.sub(r"[\d\W_]+$", "", password.lower())


def problem_with(password: str) -> str | None:
    """Why this password is not allowed, in words for the person; None if it is fine."""
    if len(password) < settings.MIN_PASSWORD_LENGTH:
        return f"The password must be at least {settings.MIN_PASSWORD_LENGTH} characters."
    if len(password) > settings.MAX_PASSWORD_LENGTH:
        return f"The password must be at most {settings.MAX_PASSWORD_LENGTH} characters."
    lowered = password.lower()
    if lowered in _common() or _stripped(password) in _common():
        return "That password is too common. Choose something harder to guess."
    if len(set(lowered)) < 5:
        return "That password repeats too few characters. Choose something harder to guess."
    return None


def check(password: str) -> str:
    """For a pydantic validator: return the password or raise ValueError."""
    message = problem_with(password)
    if message:
        raise ValueError(message)
    return password
