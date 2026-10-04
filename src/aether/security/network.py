"""S2: reject any request whose client IP is outside AETHER_ALLOWED_CIDRS (403).

X-Forwarded-For is ignored unless the direct peer is AETHER_TRUSTED_PROXY; then the
right-most XFF entry (the one that proxy appended) is the client.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Sequence

from starlette.types import ASGIApp, Receive, Scope, Send

from aether.config import IPNetwork

log = logging.getLogger(__name__)

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _parse_ip(value: str | None) -> IPAddress | None:
    if not value:
        return None
    try:
        ip = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    # Normalise IPv4-mapped IPv6 (::ffff:192.168.1.5) so v4 CIDRs match.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def resolve_client_ip(scope: Scope, trusted_proxy: IPAddress | None) -> IPAddress | None:
    client = scope.get("client")
    peer = _parse_ip(client[0]) if client else None
    if peer is None or trusted_proxy is None or peer != trusted_proxy:
        return peer
    xff_values = [
        v.decode("latin-1") for k, v in scope.get("headers", []) if k == b"x-forwarded-for"
    ]
    if not xff_values:
        return peer
    hops = [h for h in ",".join(xff_values).split(",") if h.strip()]
    return _parse_ip(hops[-1]) if hops else None


class CIDRAllowListMiddleware:
    def __init__(
        self, app: ASGIApp, allowed: Sequence[IPNetwork], trusted_proxy: str | None = None
    ) -> None:
        self.app = app
        self.allowed = tuple(allowed)
        self.trusted_proxy = _parse_ip(trusted_proxy) if trusted_proxy else None

    def is_allowed(self, ip: IPAddress | None) -> bool:
        return ip is not None and any(ip in net for net in self.allowed)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        ip = resolve_client_ip(scope, self.trusted_proxy)
        if self.is_allowed(ip):
            scope.setdefault("state", {})["client_ip"] = str(ip)
            await self.app(scope, receive, send)
            return
        log.warning("blocked request from %s (not in allowed CIDRs)", ip)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            }
        )
        await send({"type": "http.response.body", "body": b"Forbidden"})
