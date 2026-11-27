"""
Data Intelligence Lab - local request guard.

The backend listens on a loopback port, but loopback is reachable from any web page
the user opens: a page can rebind its own hostname to 127.0.0.1 (DNS rebinding) or
send a cross-site form POST to the port. Binding alone therefore does not limit who
can talk to the API. This middleware accepts a request only when:

1. it carries exactly one Host header naming a loopback host (127.0.0.1, localhost
   or [::1], with an optional numeric port). A rebound request carries the
   attacker's hostname and is refused;
2. a browser-supplied Sec-Fetch-Site, if present, is "same-origin" or "none". The
   frontend proxy forwards the browser's own value, which is "same-origin" for the
   app's UI; non-browser clients send none;
3. an Origin header, if present, is an http:// loopback origin (covers browsers that
   do not send Sec-Fetch-Site). The proxy forwards the frontend's own origin.

Everything else receives a 403 problem response before routing or body intake.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Tuple

from starlette.types import ASGIApp, Receive, Scope, Send

from backend.errors import _FORBIDDEN_REQUEST, _serialize_problem

LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "[::1]"})
ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})
_HOST_RE = re.compile(r"^(\[::1\]|[a-z0-9.-]+)(?::([0-9]{1,5}))?$")


def _single(headers: Iterable[Tuple[bytes, bytes]], name: bytes) -> Tuple[int, Optional[str]]:
    values: List[bytes] = [v for k, v in headers if k.lower() == name]
    if not values:
        return 0, None
    try:
        return len(values), values[0].decode("ascii")
    except UnicodeDecodeError:
        return len(values), None


def is_loopback_authority(value: str) -> bool:
    """True for "127.0.0.1", "localhost" or "[::1]", each with an optional port 1-65535."""
    match = _HOST_RE.fullmatch(value.strip().lower())
    if not match or match.group(1) not in LOOPBACK_HOSTNAMES:
        return False
    port = match.group(2)
    return port is None or (port[0] != "0" and 1 <= int(port) <= 65535)


def is_allowed_request(headers: Iterable[Tuple[bytes, bytes]]) -> bool:
    headers = list(headers)
    count, host = _single(headers, b"host")
    if count != 1 or host is None or not is_loopback_authority(host):
        return False
    count, site = _single(headers, b"sec-fetch-site")
    if count > 1 or (count == 1 and (site is None or site.strip().lower() not in ALLOWED_FETCH_SITES)):
        return False
    count, origin = _single(headers, b"origin")
    if count > 1:
        return False
    if count == 1:
        if origin is None or not origin.lower().startswith("http://"):
            return False
        authority = origin[len("http://"):]
        if "/" in authority or "@" in authority or not is_loopback_authority(authority):
            return False
    return True


class LocalRequestGuard:
    """ASGI middleware enforcing is_allowed_request() on every HTTP request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not is_allowed_request(scope.get("headers", [])):
            response = _serialize_problem(_FORBIDDEN_REQUEST, scope.get("path", ""))
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
