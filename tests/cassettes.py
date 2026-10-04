"""Replay recorded HTTP cassettes (tests/fixtures/cassettes/*.json) through respx.

Record with `make record-cassette NAME=... URL=...` (manual, live). Tests only ever replay.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import respx

CASSETTE_DIR = Path(__file__).parent / "fixtures" / "cassettes"


def load_cassette(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((CASSETTE_DIR / f"{name}.json").read_text("utf-8"))
    return data


def mount_cassette(router: respx.MockRouter, name: str) -> respx.Route:
    c = load_cassette(name)
    req, resp = c["request"], c["response"]
    return router.request(req["method"], req["url"]).mock(
        return_value=httpx.Response(resp["status"], headers=resp["headers"], text=resp["text"])
    )
