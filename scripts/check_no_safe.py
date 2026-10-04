#!/usr/bin/env python3
"""S5 lint: fail on `|safe` in templates/code and on `Markup` outside the sanitizer.

python scripts/check_no_safe.py [root ...]     # default: src
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SAFE_FILTER = re.compile(r"\|\s*safe\b")
MARKUP_USE = re.compile(
    r"\bMarkup\s*\(|\bimport\s+Markup\b|\bMarkup\b\s*(?:,|$)|markupsafe\.Markup"
)
# The only module permitted to construct Markup (it sanitizes with nh3 first).
MARKUP_ALLOWED = ("aether/security/sanitize.py",)
TEMPLATE_SUFFIXES = {".html", ".htm", ".jinja", ".j2", ".txt", ".xml"}


def check_file(path: Path) -> list[str]:
    problems: list[str] = []
    rel = path.as_posix()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, OSError):
        return problems
    is_py = path.suffix == ".py"
    if not (is_py or path.suffix in TEMPLATE_SUFFIXES):
        return problems
    for n, line in enumerate(lines, 1):
        if SAFE_FILTER.search(line):
            problems.append(f"{rel}:{n}: `|safe` is banned (S5); use the `md`/`extlink` filters")
        if is_py and not rel.endswith(MARKUP_ALLOWED) and MARKUP_USE.search(line):
            problems.append(f"{rel}:{n}: `Markup` outside security/sanitize.py is banned (S5)")
    return problems


def main(argv: list[str]) -> int:
    roots = [Path(a) for a in argv] or [Path("src")]
    problems: list[str] = []
    for root in roots:
        files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
        for f in files:
            problems += check_file(f)
    for p in problems:
        print(p)
    if problems:
        print(f"check_no_safe: {len(problems)} problem(s)", file=sys.stderr)
        return 1
    print("check_no_safe: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
