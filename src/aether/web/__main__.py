"""`python -m aether.web`: serve the dashboard on AETHER_BIND (LAN only)."""

from __future__ import annotations

import logging

import uvicorn

from aether.config import get_settings
from aether.web.app import create_app


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    host, port = settings.bind_host_port
    uvicorn.run(
        create_app(settings),
        host=host,
        port=port,
        # The CIDR middleware is the only authority on client IPs; never let uvicorn rewrite them.
        proxy_headers=False,
        server_header=False,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
