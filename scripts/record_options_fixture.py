#!/usr/bin/env python3
"""Record one real yfinance option chain as a test fixture (needs network; run manually).

python scripts/record_options_fixture.py IONQ

Writes tests/fixtures/options/<SYMBOL>_<YYYY-MM-DD>.json.gz: the spot and, per expiry, the
contract fields the snapshot uses (the expiries the job would pick; strikes within ±60% of
spot, to keep it small). Real data, recorded once; tests replay it with the network blocked.
"""

from __future__ import annotations

import argparse
import gzip
import json
from datetime import datetime
from pathlib import Path

from aether.config import load_options_config
from aether.options.snapshot import choose_expiries
from aether.providers.options import _yf_fetch_raw
from aether.providers.prices import US_EASTERN

OUT = Path("tests/fixtures/options")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    a = ap.parse_args()
    today = datetime.now(US_EASTERN).date()
    cfg = load_options_config(Path("config"))
    raw = _yf_fetch_raw(a.symbol, lambda listed: choose_expiries(listed, today, cfg))
    spot = raw["spot"]
    if spot:
        for e in raw["expiries"]:
            for side in ("calls", "puts"):
                e[side] = [c for c in e[side] if 0.4 * spot <= c["strike"] <= 1.6 * spot]
    raw["recorded_on"] = today.isoformat()
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{a.symbol}_{today.isoformat()}.json.gz"
    path.write_bytes(gzip.compress(json.dumps(raw, sort_keys=True, default=str).encode(), mtime=0))
    print(f"wrote {path} ({len(raw['expiries'])} expiries)")


if __name__ == "__main__":
    main()
