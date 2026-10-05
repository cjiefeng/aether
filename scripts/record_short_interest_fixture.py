#!/usr/bin/env python3
"""Record real FINRA short-interest files as a test fixture (needs network; run manually).

python scripts/record_short_interest_fixture.py 2026-08-29 2026-09-15

Writes tests/fixtures/finra/shrt<YYYYMMDD>.csv for each date: FINRA's header line plus only the
rows for the strategy universe (QTUM + pure-plays in config/watchlist.yaml). Real data, recorded
once and filtered; tests replay it with the network blocked.
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import httpx

from aether.config import load_watchlist
from aether.providers.finra import file_url

OUT = Path("tests/fixtures/finra")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dates", nargs="+")
    a = ap.parse_args()
    wl = load_watchlist(Path("config"))
    keep = {t.symbol for t in wl.tickers if t.type in ("etf", "pure_play")}
    OUT.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=60, headers={"User-Agent": "aether/0.1"}) as client:
        for d in a.dates:
            url = file_url(date.fromisoformat(d))
            r = client.get(url)
            r.raise_for_status()
            lines = r.text.splitlines()
            rows = [ln for ln in lines[1:] if ln.split("|")[1].strip('"') in keep]
            path = OUT / f"shrt{d.replace('-', '')}.csv"
            path.write_text("\n".join([lines[0], *rows]) + "\n", encoding="utf-8")
            print(f"{path}: {len(rows)} rows from {url}")


if __name__ == "__main__":
    main()
