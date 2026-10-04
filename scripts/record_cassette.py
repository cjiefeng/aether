#!/usr/bin/env python3
"""Record ONE live HTTP response into tests/fixtures/cassettes/<name>.json (run manually, never in
tests). Request headers are not stored; response headers are reduced to an allow-list.

    make record-cassette NAME=sec_submissions_acme URL=https://... [UA="Name email"] [GZIP=1]

`--gzip` writes `<name>.json.gz` instead, for large documents (e.g. a multi-MB prospectus).
"""

from __future__ import annotations

import argparse
import gzip
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
    ap.add_argument("--gzip", action="store_true")
    a = ap.parse_args()
    resp = httpx.get(a.url, headers={"User-Agent": a.user_agent}, timeout=60, follow_redirects=True)
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
    body = json.dumps(cassette, indent=2, ensure_ascii=False) + "\n"
    if a.gzip:
        out = CASSETTE_DIR / f"{a.name}.json.gz"
        # mtime=0: byte-identical output for identical responses.
        out.write_bytes(gzip.compress(body.encode("utf-8"), mtime=0))
    else:
        out = CASSETTE_DIR / f"{a.name}.json"
        out.write_text(body, encoding="utf-8")
    print(f"wrote {out} ({resp.status_code}, {len(resp.text)} chars)")
    return 0 if resp.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
