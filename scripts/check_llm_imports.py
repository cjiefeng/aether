#!/usr/bin/env python3
"""S1/S3 lint: every LLM call goes through `aether/llm/client.py` (budget guard, tool gate,
`llm_calls` logging, redaction).

Fails on any `anthropic` import outside that module.

python scripts/check_llm_imports.py [root ...]     # default: src
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ANTHROPIC_IMPORT = re.compile(
    r"^\s*(?:from|import)\s+anthropic\b|__import__\(\s*['\"]anthropic|import_module\(\s*['\"]anthropic"
)
ALLOWED_IMPORTER = "aether/llm/client.py"


def check_file(path: Path) -> list[str]:
    if path.suffix != ".py":
        return []
    rel = path.as_posix()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, OSError):
        return []
    return [
        f"{rel}:{n}: `anthropic` may only be imported in {ALLOWED_IMPORTER}"
        for n, line in enumerate(lines, 1)
        if ANTHROPIC_IMPORT.search(line) and not rel.endswith(ALLOWED_IMPORTER)
    ]


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
        print(f"check_llm_imports: {len(problems)} problem(s)", file=sys.stderr)
        return 1
    print("check_llm_imports: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
