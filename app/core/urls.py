"""URLs a client gives us, and what is safe to do with them.

Two fields take a URL from whoever holds an API key: the callback, which this server
POSTs the outcome to, and the redirect, which the person's browser is sent to when
they finish. Neither was checked, so a callback could point at the cloud metadata
service or an internal address, and a redirect could be a javascript: link that runs
on the verification site.

  * A **redirect** must be an https address with a host and no embedded login.
  * A **callback** must be that, and the host must resolve only to public addresses,
    checked when the verification is made and again when the callback is sent.

http is allowed only in development, and private hosts only when
CALLBACK_ALLOW_PRIVATE_HOSTS is set, which is for tests and a developer's machine.
"""

import ipaddress
import socket
from urllib.parse import urlsplit

from app.core.config import settings

MAX_URL_LENGTH = 2048


class UnsafeUrl(ValueError):
    """The URL is not one we will send people or data to."""


def _parts(url: str):
    if not isinstance(url, str) or not url.strip():
        raise UnsafeUrl("must not be empty")
    url = url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise UnsafeUrl("is too long")
    if any(ord(c) < 32 or ord(c) == 127 for c in url) or "\\" in url:
        raise UnsafeUrl("contains characters that are not allowed")
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        raise UnsafeUrl("is not a valid web address") from None
    allowed = {"https"} | ({"http"} if settings.ENVIRONMENT == "development" else set())
    if parts.scheme.lower() not in allowed:
        raise UnsafeUrl("must start with https://")
    if not host:
        raise UnsafeUrl("has no host")
    if parts.username is not None or parts.password is not None:
        raise UnsafeUrl("must not contain a username or password")
    return url, parts, host


def check_redirect(url: str) -> str:
    """The redirect URL, cleaned, or UnsafeUrl."""
    return _parts(url)[0]


def _public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip.is_global


def host_is_public(host: str) -> bool:
    """True when every address the host resolves to is a public one."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    addresses = {info[4][0] for info in infos}
    return bool(addresses) and all(_public(a) for a in addresses)


def check_callback(url: str) -> str:
    """The callback URL, cleaned, or UnsafeUrl. Resolves the host."""
    url, _, host = _parts(url)
    if not settings.CALLBACK_ALLOW_PRIVATE_HOSTS and not host_is_public(host):
        raise UnsafeUrl("must point to a public address, not an internal or unreachable one")
    return url


def redirect_or_none(url: str | None) -> str | None:
    """A stored redirect URL if it is still safe to hand to a browser, else None.

    Verifications made before the check existed can hold a javascript: link, so the
    value is checked again on the way out.
    """
    if not url:
        return None
    try:
        return check_redirect(url)
    except UnsafeUrl:
        return None
