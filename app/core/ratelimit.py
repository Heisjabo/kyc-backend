"""Limits on how often one caller can try something.

Nothing limited how often a password, a reset email, a session step or a document could
be tried: sixty wrong passwords in a row were all answered, and every attempt on a
session ran the OCR and face models.

A sliding window per key, held in memory. That is right for one process, which is how
the container runs (WEB_CONCURRENCY defaults to 1). With several workers each has its
own counters, so the effective limit is multiplied by the worker count, and nginx's
limit_req (see NGINX-SETUP.txt) is the second layer that does not depend on this.
"""

import math
import threading
import time
from collections import deque

from fastapi import Depends, HTTPException, Request, status

from app.core.config import settings
from app.services.network import client_ip


class SlidingWindow:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._calls = 0

    def _trim(self, hits: deque, window: float, now: float) -> None:
        while hits and hits[0] <= now - window:
            hits.popleft()

    def hit(self, key: str, limit: int, window: float, now: float | None = None) -> float:
        """Record an attempt. Returns 0 when it is allowed, else the seconds to wait."""
        now = time.monotonic() if now is None else now
        with self._lock:
            self._calls += 1
            if self._calls % 2000 == 0:
                self._prune(now)
            hits = self._hits.setdefault(key, deque())
            self._trim(hits, window, now)
            if len(hits) >= limit:
                return max(hits[0] + window - now, 0.001)
            hits.append(now)
            return 0.0

    def count(self, key: str, window: float, now: float | None = None) -> tuple[int, float]:
        """How many attempts are in the window, and when the oldest leaves it."""
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.get(key)
            if not hits:
                return 0, 0.0
            self._trim(hits, window, now)
            return len(hits), (hits[0] + window - now) if hits else 0.0

    def record(self, key: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._hits.setdefault(key, deque()).append(now)

    def clear(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def _prune(self, now: float) -> None:
        for key in [k for k, v in self._hits.items() if not v or v[-1] <= now - 7200]:
            del self._hits[key]


limiter = SlidingWindow()


def too_many(retry_after: float) -> HTTPException:
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        "Too many attempts. Please wait and try again.",
        headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
    )


def _ip(request: Request) -> str:
    return client_ip(request) or "unknown"


def _token(request: Request) -> str:
    return str(request.path_params.get("token", ""))[:80] or "none"


def limit(scope: str, limit_: int, window: float, by: str = "ip"):
    """A dependency that refuses the call once `limit_` have been made in `window`
    seconds by the same caller (`by` is "ip" or "token")."""

    def check(request: Request) -> None:
        if not settings.RATE_LIMITS_ENABLED:
            return
        key = _ip(request) if by == "ip" else _token(request)
        retry = limiter.hit(f"{scope}:{by}:{key}", limit_, window)
        if retry:
            raise too_many(retry)

    return Depends(check)


# Failed sign-ins. Counted per address and account together (five in fifteen minutes),
# and per account alone at a higher number, so one person cannot lock an account for
# everybody by guessing, but a spread-out attack on one account still stops.
FAILED_PER_PAIR = (5, 15 * 60)
FAILED_PER_ACCOUNT = (20, 60 * 60)


def check_sign_in_allowed(request: Request, email: str) -> None:
    if not settings.RATE_LIMITS_ENABLED:
        return
    for key, (limit_, window) in (
        (f"signin-fail:pair:{_ip(request)}:{email}", FAILED_PER_PAIR),
        (f"signin-fail:acct:{email}", FAILED_PER_ACCOUNT),
    ):
        used, wait = limiter.count(key, window)
        if used >= limit_:
            raise too_many(wait)


def record_failed_sign_in(request: Request, email: str) -> None:
    if not settings.RATE_LIMITS_ENABLED:
        return
    limiter.record(f"signin-fail:pair:{_ip(request)}:{email}")
    limiter.record(f"signin-fail:acct:{email}")


def clear_failed_sign_ins(request: Request, email: str) -> None:
    limiter.clear(f"signin-fail:pair:{_ip(request)}:{email}")


def limit_per_email(request: Request, scope: str, email: str, limit_: int, window: float) -> None:
    """For something asked by email address in the body, such as a password reset."""
    if not settings.RATE_LIMITS_ENABLED:
        return
    retry = limiter.hit(f"{scope}:email:{email}", limit_, window)
    if retry:
        raise too_many(retry)
