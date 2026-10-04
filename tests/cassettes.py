"""Replay recorded HTTP cassettes (tests/fixtures/cassettes/*.json[.gz]) through respx.

Record with `make record-cassette NAME=... URL=...` (manual, live). Tests only ever replay.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import httpx
import respx

CASSETTE_DIR = Path(__file__).parent / "fixtures" / "cassettes"


def load_cassette(name: str) -> dict[str, Any]:
    plain = CASSETTE_DIR / f"{name}.json"
    if plain.exists():
        raw = plain.read_text("utf-8")
    else:
        raw = gzip.decompress((CASSETTE_DIR / f"{name}.json.gz").read_bytes()).decode("utf-8")
    data: dict[str, Any] = json.loads(raw)
    return data


def cassette_text(name: str) -> str:
    text: str = load_cassette(name)["response"]["text"]
    return text


def mount_cassette(router: respx.MockRouter, name: str) -> respx.Route:
    c = load_cassette(name)
    req, resp = c["request"], c["response"]
    return router.request(req["method"], req["url"]).mock(
        return_value=httpx.Response(resp["status"], headers=resp["headers"], text=resp["text"])
    )
