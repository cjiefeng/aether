#!/usr/bin/env python3
"""Record ONE live HTTP response into tests/fixtures/cassettes/<name>.json (run manually, never in
tests). Request headers are not stored; response headers are reduced to an allow-list.

    make record-cassette NAME=sec_submissions_acme URL=https://... [UA="Name email"]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

KEEP_RESPONSE_HEADERS = {"content-type", "content-encoding", "last-modified", "etag"}
CASSETTE_DIR = Path("tests/fixtures/cassettes")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("url")
    ap.add_argument("--user-agent", default="aether-cassette-recorder")
    a = ap.parse_args()
    resp = httpx.get(a.url, headers={"User-Agent": a.user_agent}, timeout=30, follow_redirects=True)
    cassette = {
        "request": {"method": "GET", "url": a.url},
        "response": {
            "status": resp.status_code,
            "headers": {
                k: v
                for k, v in resp.headers.items()
                if k.lower() in KEEP_RESPONSE_HEADERS and k.lower() != "content-encoding"
            },
            "text": resp.text,
        },
    }
    CASSETTE_DIR.mkdir(parents=True, exist_ok=True)
    out = CASSETTE_DIR / f"{a.name}.json"
    out.write_text(json.dumps(cassette, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out} ({resp.status_code}, {len(resp.text)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
