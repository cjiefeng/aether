"""Registrable-domain helper shared by news dedupe (M6) and trust-tier caps (M7)."""

from __future__ import annotations

# Second-level suffixes where the registrable domain has three labels. Not the full Public Suffix
# List (no dependency); unknown multi-part suffixes fall back to the last two labels.
_MULTI_SUFFIXES = frozenset(
    {
        "co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au", "com.sg", "edu.sg",
        "gov.sg", "co.jp", "ne.jp", "com.cn", "com.hk", "co.in", "co.kr", "co.nz", "com.br",
        "com.tw", "co.za", "com.my",
    }
)  # fmt: skip


def registrable_domain(host: str) -> str:
    labels = host.lower().rstrip(".").removeprefix("www.").split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in _MULTI_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])
