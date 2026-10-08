"""Every HTTP request the service answers, kept in audit_events.

Each request is one `request.http` event: method, route, path, status, time
taken, the caller's address and browser, and who was signed in (a console or
dashboard account, an API key's account, or nobody, as on the session pages).

Camera frames (POST /v1/document/frame and /v1/face/frame) arrive several
times a second while someone holds up a document or their face, and the health
check is called every 30 seconds by Docker. Those are not kept one by one: the
requests from one address to one of those routes are counted, and written as
one `request.grouped` event per burst, with the count, the statuses and when
the burst began and ended.

The request body is kept too: JSON and form fields as sent, and for an
uploaded file its name, type and size (the file itself is evidence, stored
elsewhere). Passwords, tokens, keys and other secrets are masked wherever they
appear, in the body, the query or a session token in the path.

The row is written after the response, off the request's thread, and a failure
to write it is logged rather than passed on to the caller.
"""

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import parse_qsl

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.db.models import AuditEvent, Verification
from app.db.session import SessionLocal
from app.services import network
from app.services import verifications as service

logger = logging.getLogger(__name__)

REQUEST_EVENT = "request.http"
GROUPED_EVENT = "request.grouped"

# Routes counted per burst instead of kept one by one.
GROUPED_ROUTES = {"/v1/document/frame", "/v1/face/frame", "/v1/health"}
# A burst ends after this long without a request, or once it is this old.
BURST_IDLE_SECONDS = 15
BURST_MAX_SECONDS = 300

MASKED = "[masked]"
# A field is a secret if its name contains one of these, or is one of the exact
# names after them.
_SECRET_PARTS = ("password", "passwd", "passcode", "secret", "token", "api_key", "apikey", "authorization", "private_key", "signature", "otp", "cvv")
_SECRET_NAMES = ("pin", "x_api_key")
_MASKED_PARAMS = ("token",)
# Bodies larger than this, once secrets are masked, are cut.
MAX_BODY_CHARS = 64_000
_BODY_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def _is_secret(name: str) -> bool:
    name = name.lower().replace("-", "_")
    return name in _SECRET_NAMES or any(part in name for part in _SECRET_PARTS)


def mask(value):
    """The value with every secret-named field, at any depth, masked."""
    if isinstance(value, dict):
        return {k: MASKED if isinstance(k, str) and _is_secret(k) and v not in (None, "") else mask(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask(v) for v in value]
    return value


def set_actor(request: Request, account_id: int, via: str, **extra) -> None:
    """Called by the auth dependencies once they know who is calling."""
    request.state.audit_actor = str(account_id)
    request.state.audit_auth = via
    request.state.audit_extra = extra


def _route_template(request: Request) -> str | None:
    route = request.scope.get("route")
    return getattr(route, "path", None)


def _masked_path(request: Request) -> str:
    path = request.url.path
    for name, value in (request.scope.get("path_params") or {}).items():
        if name in _MASKED_PARAMS and isinstance(value, str) and value:
            path = path.replace(value, value[:4] + "****")
    return path


def _query(request: Request) -> dict[str, str]:
    return mask(dict(request.query_params))


async def _read_body(request: Request):
    """The request body as something JSON can hold, secrets masked; None for none."""
    if request.method not in _BODY_METHODS:
        return None
    raw = await request.body()
    if not raw:
        return None
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()

    if content_type == "application/json" or content_type.endswith("+json"):
        try:
            body = mask(json.loads(raw))
        except ValueError:
            body = raw.decode("utf-8", "replace")
    elif content_type == "application/x-www-form-urlencoded":
        body = mask(dict(parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True)))
    elif content_type == "multipart/form-data":
        body = await _read_form(request, raw)
    elif content_type.startswith("text/") or not content_type:
        body = raw.decode("utf-8", "replace")
    else:
        body = {"content_type": content_type, "size": len(raw)}

    text = body if isinstance(body, str) else json.dumps(body, default=str)
    if len(text) > MAX_BODY_CHARS:
        return {"truncated": True, "size": len(raw), "preview": text[:MAX_BODY_CHARS]}
    return body


async def _read_form(request: Request, raw: bytes) -> dict:
    """A multipart body's fields, with each file as its name, type and size."""
    sent = False

    async def replay():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": raw, "more_body": False}

    fields: dict[str, object] = {}
    form = await Request(request.scope, replay).form(max_files=100, max_fields=1000)
    try:
        for name, value in form.multi_items():
            if isinstance(value, UploadFile):
                value = {"file": value.filename, "content_type": value.content_type, "size": value.size}
            elif _is_secret(name) and value:
                value = MASKED
            if name in fields:
                existing = fields[name]
                fields[name] = [*existing, value] if isinstance(existing, list) else [existing, value]
            else:
                fields[name] = value
    finally:
        await form.close()
    return fields


@dataclass
class _Burst:
    route: str
    method: str
    ip: str | None
    user_agent: str | None
    started: float = field(default_factory=time.monotonic)
    last: float = field(default_factory=time.monotonic)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    count: int = 0
    statuses: dict[str, int] = field(default_factory=dict)
    total_ms: float = 0.0


class _Bursts:
    """Grouped requests waiting to be written, keyed by route and address."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._open: dict[tuple, _Burst] = {}

    def add(self, route: str, method: str, ip: str | None, user_agent: str | None, status: int, ms: float) -> None:
        key = (route, method, ip)
        with self._lock:
            burst = self._open.get(key)
            if burst is None:
                burst = self._open[key] = _Burst(route=route, method=method, ip=ip, user_agent=user_agent)
            burst.count += 1
            burst.last = time.monotonic()
            burst.ended_at = datetime.now(UTC)
            burst.statuses[str(status)] = burst.statuses.get(str(status), 0) + 1
            burst.total_ms += ms

    def take_finished(self, everything: bool = False) -> list[_Burst]:
        now = time.monotonic()
        with self._lock:
            done = [
                k for k, b in self._open.items()
                if everything or now - b.last >= BURST_IDLE_SECONDS or now - b.started >= BURST_MAX_SECONDS
            ]
            return [self._open.pop(k) for k in done]


bursts = _Bursts()


def _write(events: list[AuditEvent], token: str | None = None) -> None:
    try:
        with SessionLocal() as db:
            for event in events:
                # A token or id in the path may not belong to any verification.
                verification = (
                    service.by_token(db, token) if token
                    else db.get(Verification, event.verification_id) if event.verification_id
                    else None
                )
                event.verification_id = verification.id if verification else None
                event.applicant_id = verification.applicant_id if verification else None
            db.add_all(events)
            db.commit()
    except Exception:  # noqa: BLE001, the audit row must never fail a request
        logger.exception("Could not record a request in audit_events")


def _burst_event(burst: _Burst) -> AuditEvent:
    return AuditEvent(
        event_type=GROUPED_EVENT,
        actor="anonymous",
        payload={
            "method": burst.method,
            "route": burst.route,
            "count": burst.count,
            "statuses": burst.statuses,
            "average_ms": round(burst.total_ms / burst.count, 1) if burst.count else None,
            "started_at": burst.started_at.isoformat(),
            "ended_at": burst.ended_at.isoformat(),
            "ip": burst.ip,
            "user_agent": burst.user_agent,
        },
    )


def flush_bursts(everything: bool = False) -> None:
    finished = bursts.take_finished(everything)
    if finished:
        _write([_burst_event(b) for b in finished])


async def flush_bursts_forever() -> None:
    """Writes finished bursts every few seconds, for the app's lifetime."""
    while True:
        await asyncio.sleep(5)
        await run_in_threadpool(flush_bursts)


_pending: set[asyncio.Task] = set()


class RequestAuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        started = time.perf_counter()
        status = 500
        body = None
        if request.url.path not in GROUPED_ROUTES:
            try:
                body = await _read_body(request)
            except Exception:  # noqa: BLE001, a body we cannot read is still passed on
                logger.exception("Could not read a request body for audit_events")
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            ms = (time.perf_counter() - started) * 1000
            try:
                self._record(request, status, ms, body)
            except Exception:  # noqa: BLE001
                logger.exception("Could not record a request in audit_events")

    def _record(self, request: Request, status: int, ms: float, body) -> None:
        route = _route_template(request)
        ip = network.client_ip(request)
        user_agent = request.headers.get("user-agent")

        if route in GROUPED_ROUTES:
            bursts.add(route, request.method, ip, user_agent, status, ms)
            return

        state = request.state
        params = request.scope.get("path_params") or {}
        verification_id = None
        if route and route.startswith("/v1/console/") and str(params.get("verification_id", "")).isdigit():
            verification_id = int(params["verification_id"])
        token = params.get("token") if isinstance(params.get("token"), str) else None

        event = AuditEvent(
            event_type=REQUEST_EVENT,
            actor=getattr(state, "audit_actor", None) or "anonymous",
            verification_id=verification_id,
            payload={
                "method": request.method,
                "route": route,
                "path": _masked_path(request),
                "query": _query(request),
                "body": body,
                "status": status,
                "duration_ms": round(ms, 1),
                "auth": getattr(state, "audit_auth", None) or ("session" if token else "none"),
                **(getattr(state, "audit_extra", None) or {}),
                "ip": ip,
                "user_agent": user_agent,
            },
        )
        task = asyncio.get_running_loop().create_task(run_in_threadpool(_write, [event], token))
        _pending.add(task)
        task.add_done_callback(_pending.discard)
