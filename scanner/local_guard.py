"""Keep the scanner's HTTP and WebSocket endpoints to this machine.

CORS stops a foreign web page from READING a response; it does not stop it
SENDING one: a bodyless or no-Content-Type POST is a "simple" request that goes
out without a preflight. And CORS does nothing against DNS rebinding, where an
attacker's hostname is re-pointed at 127.0.0.1 so their page is same-origin
with the scanner and may read and write everything. Three checks close that:

  - the Host header must name this machine (the attacker's rebound hostname
    arrives as Host, so it is refused before any route runs);
  - a state-changing request that carries an Origin must come from this machine;
  - a POST must be application/json, which no foreign page can send without a
    preflight that CORS then refuses.

Scripts and bots send no Origin and are still let in. Names other than the
loopback ones are allowed only on purpose: the --host a scanner listens on
(`allow_hosts`), or SCANNER_ALLOWED_HOSTS=name1,name2 in .env for LAN use.
"""
from __future__ import annotations

import os
from typing import Optional
from urllib.parse import urlsplit

from starlette.responses import JSONResponse

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_WILDCARDS = frozenset({"", "0.0.0.0", "::"})

_extra_hosts: set[str] = set()


def hostname(value: Optional[str]) -> str:
    """'localhost:7777' -> 'localhost', '[::1]:7777' -> '::1', '::1' -> '::1'."""
    v = (value or "").strip().lower()
    if v.startswith("["):
        return v[1:v.find("]")] if "]" in v else ""
    if v.count(":") == 1:
        return v.split(":", 1)[0]
    return v


def allow_hosts(*names: str) -> None:
    """Also accept these names (the --host a scanner was started with)."""
    for n in names:
        h = hostname(n)
        if h not in _WILDCARDS:
            _extra_hosts.add(h)


def allowed_hosts() -> set[str]:
    env = os.environ.get("SCANNER_ALLOWED_HOSTS", "")
    return set(LOOPBACK_HOSTS) | _extra_hosts | {hostname(x) for x in env.split(",") if x.strip()}


def host_allowed(host: Optional[str]) -> bool:
    """A missing Host is not a browser (browsers always send one), so it passes."""
    return not host or hostname(host) in allowed_hosts()


def origin_allowed(origin: Optional[str]) -> bool:
    """No Origin (scripts, bots) passes; 'null' (sandboxed pages, file://) does not."""
    if not origin:
        return True
    try:
        u = urlsplit(origin.strip())
    except ValueError:
        return False
    return u.scheme in ("http", "https") and (u.hostname or "") in allowed_hosts()


def refusal(kind: str, method: str, headers: dict[str, str]) -> Optional[tuple[int, str]]:
    """(status, reason) when a request must be refused, else None."""
    if not host_allowed(headers.get("host")):
        return 400, "host not allowed"
    if kind == "websocket" or method in _UNSAFE_METHODS:
        if not origin_allowed(headers.get("origin")):
            return 403, "origin not allowed"
    if method == "POST":
        ctype = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            return 415, "POST needs Content-Type: application/json"
    return None


class LocalOnlyMiddleware:
    """Pure ASGI, so it sees WebSockets too (BaseHTTPMiddleware does not)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers") or []}
        refused = refusal(kind, scope.get("method", "GET").upper(), headers)
        if refused is None:
            await self.app(scope, receive, send)
            return
        status, reason = refused
        if kind == "websocket":
            await receive()                   # the websocket.connect event
            await send({"type": "websocket.close", "code": 1008, "reason": reason})
            return
        await JSONResponse({"error": reason}, status_code=status)(scope, receive, send)
