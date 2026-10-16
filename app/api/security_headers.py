"""Headers that tell a browser how to treat what this service sends.

The API answers JSON, so almost nothing it sends should ever be run or framed. The few
HTML pages (switched on only with ENABLE_LEGACY_ENDPOINTS) keep their own scripts and are
given only the headers that do not stop them working.
"""

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings

_COMMON = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "cross-origin-resource-policy": "same-site",
    # The legacy capture pages open the camera on their own origin; nothing else is allowed.
    "permissions-policy": "camera=(self), microphone=(), geolocation=(), payment=(), usb=()",
}
_JSON_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
_HTML_CSP = "frame-ancestors 'none'; base-uri 'none'; object-src 'none'"


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        https = scope.get("scheme") == "https"

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _COMMON.items():
                    headers.setdefault(name, value)
                html = headers.get("content-type", "").startswith("text/html")
                docs = scope.get("path", "").startswith("/docs")
                if not docs:
                    headers.setdefault("content-security-policy", _HTML_CSP if html else _JSON_CSP)
                if https and settings.HSTS_SECONDS:
                    headers.setdefault("strict-transport-security", f"max-age={settings.HSTS_SECONDS}; includeSubDomains")
            await send(message)

        await self.app(scope, receive, with_headers)
