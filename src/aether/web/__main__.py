"""`python -m aether.web`: serve the dashboard on AETHER_BIND (LAN only)."""

from __future__ import annotations

import logging

import uvicorn

from aether.config import get_settings
from aether.logs import setup_logging
from aether.security.auth import AuthConfigError
from aether.web.app import create_app

log = logging.getLogger("aether.web")


def main() -> None:
    settings = get_settings()
    setup_logging(settings)
    host, port = settings.bind_host_port
    try:
        app = create_app(settings)
    except AuthConfigError as exc:
        # S2 fail closed: no valid password hash / session secret, no dashboard.
        log.error("refusing to start: %s (run `make hash-password`)", exc)
        raise SystemExit(1) from None
    uvicorn.run(
        app,
        host=host,
        port=port,
        # Never let uvicorn rewrite the client address from X-Forwarded-For.
        proxy_headers=False,
        server_header=False,
        log_level=settings.log_level.lower(),
        log_config=None,  # keep the root handler from `setup_logging` (JSON, redaction)
    )


if __name__ == "__main__":
    main()
