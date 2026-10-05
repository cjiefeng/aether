#!/usr/bin/env python3
"""S8 lint: the Tiger Brokers key can trade, so Aether keeps it read-only in code.

Fails on:
- any `tigeropen` import outside `aether/providers/tiger.py`;
- any reference to an order-capable method (`place_order`, `modify_order`, `cancel_order`,
  `create_order`, `preview_order`, `place_forex_order`, ...) anywhere under the given roots.

python scripts/check_broker_readonly.py [root ...]     # default: src
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

TIGER_IMPORT = re.compile(
    r"^\s*(?:from|import)\s+tigeropen\b|__import__\(\s*['\"]tigeropen|import_module\(\s*['\"]tigeropen"
)
ORDER_METHOD = re.compile(
    r"\b(?:place|modify|cancel|create|preview|submit)_\w*order\w*\b", re.IGNORECASE
)
ALLOWED_IMPORTER = "aether/providers/tiger.py"


def check_file(path: Path) -> list[str]:
    if path.suffix != ".py":
        return []
    rel = path.as_posix()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, OSError):
        return []
    problems: list[str] = []
    for n, line in enumerate(lines, 1):
        if TIGER_IMPORT.search(line) and not rel.endswith(ALLOWED_IMPORTER):
            problems.append(
                f"{rel}:{n}: `tigeropen` may only be imported in {ALLOWED_IMPORTER} (S8)"
            )
        if m := ORDER_METHOD.search(line):
            problems.append(f"{rel}:{n}: order method `{m.group(0)}` is banned (S8: read-only)")
    return problems


def main(argv: list[str]) -> int:
    roots = [Path(a) for a in argv] or [Path("src")]
    problems: list[str] = []
    for root in roots:
        files = [root] if root.is_file() else sorted(p for p in root.rglob("*.py") if p.is_file())
        for f in files:
            problems += check_file(f)
    for p in problems:
        print(p)
    if problems:
        print(f"check_broker_readonly: {len(problems)} problem(s)", file=sys.stderr)
        return 1
    print("check_broker_readonly: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
