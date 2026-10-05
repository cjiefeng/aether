"""Export real ingested news/research events as golden-set candidates (spec §5.3, M7).

    make golden-candidates DB=path/to/aether.db [ARGS="--source live-rss-2026-10-05"]

Reads the DB read-only (`mode=ro`) and writes `evals/golden_candidates.jsonl`: one row per event
with the real title, excerpt, URL, domain, tier and tickers, and no label. Events whose URL is
already in `evals/classifier_golden.jsonl` are skipped. To promote a candidate, copy it into the
golden file with an id, a `label` and `labeled_by` (Claude Code proposes, the owner reviews and
sets `labeled_by: owner`). Never write or paraphrase titles or excerpts.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

GOLDEN = Path("evals/classifier_golden.jsonl")
OUT = Path("evals/golden_candidates.jsonl")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("db", type=Path)
    ap.add_argument("--source", default="ingest", help="label for where the DB came from")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    if not args.db.is_file():
        print(f"no such DB: {args.db}", file=sys.stderr)
        return 2
    known = set()
    if GOLDEN.exists():
        known = {json.loads(line)["url"] for line in GOLDEN.read_text("utf-8").splitlines() if line}
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT e.id, e.url, e.title, e.excerpt, e.source_domain, e.trust_tier, e.published_at, "
        "e.origin, (SELECT group_concat(symbol, ',') FROM "
        "(SELECT symbol FROM event_tickers t WHERE t.event_id = e.id ORDER BY symbol)) "
        "FROM events e WHERE e.origin IN ('rss', 'web_search') ORDER BY e.published_at DESC"
    ).fetchall()
    n = 0
    with args.out.open("w", encoding="utf-8") as fh:
        for eid, url, title, excerpt, domain, tier, published, origin, syms in rows:
            if url in known:
                continue
            fh.write(
                json.dumps(
                    {
                        "source": f"{args.source}:{origin}",
                        "event_id": eid,
                        "url": url,
                        "title": title,
                        "excerpt": excerpt,
                        "domain": domain,
                        "tier": tier,
                        "published_at": published,
                        "tickers": syms.split(",") if syms else [],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            n += 1
    print(f"{n} candidates written to {args.out} ({len(rows) - n} already in the golden set)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
