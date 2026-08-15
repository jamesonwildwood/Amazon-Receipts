"""Not authentication -- this dashboard has none, and the state-changing
routes (reapply, create-transaction, reset) accept plain unauthenticated
POSTs. On a LAN behind Traefik with no auth, any page a browser has open
could fire a cross-site POST at the dashboard's hostname (DNS rebinding
makes this realistic even for "internal" hostnames) -- see
docs/IMPROVEMENTS.md item 7.

This is the cheap partial mitigation the plan calls for: reject a
state-changing request only when it carries an Origin header that doesn't
match the Host it's actually talking to. Deliberately does NOT reject a
request with no Origin at all -- some legitimate same-origin requests omit
it, and false-positiving those would break real usage for a check that's
explicitly "not full auth" in the first place.
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse

_STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class RejectCrossOriginWrites(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method in _STATE_CHANGING_METHODS:
            origin = request.headers.get("origin")
            host = request.headers.get("host")
            if origin and host and _origin_host_mismatch(origin, host):
                return PlainTextResponse("Cross-origin request rejected", status_code=403)
        return await call_next(request)


def _origin_host_mismatch(origin: str, host: str) -> bool:
    # Origin looks like "https://example.com" or "http://example.com:8420";
    # Host is just "example.com" or "example.com:8420" (no scheme). Strip the
    # scheme from Origin and compare the rest directly.
    origin_host = origin.split("://", 1)[-1]
    return origin_host != host
