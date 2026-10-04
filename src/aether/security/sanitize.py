"""S5: output encoding for untrusted text.

This is the ONLY module allowed to construct `Markup` (enforced by scripts/check_no_safe.py).
Templates keep Jinja autoescape on and never use the `safe` filter. Rich text goes through `md`,
links through `extlink`.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import nh3
from jinja2 import Environment
from markdown_it import MarkdownIt
from markupsafe import Markup, escape

ALLOWED_SCHEMES = frozenset({"http", "https"})
LINK_REL = "noopener noreferrer nofollow"

ALLOWED_TAGS = {
    "p", "br", "hr", "strong", "em", "b", "i", "code", "pre", "blockquote",
    "ul", "ol", "li", "a", "h3", "h4", "h5", "h6",
}  # fmt: skip
ALLOWED_ATTRIBUTES = {"a": {"href", "title"}}

# Raw HTML in markdown is disabled at the parser; nh3 is the second line of defence.
_md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})


def safe_url(url: str | None) -> str | None:
    """Return the URL if it's absolute http(s) with a host and no whitespace/control chars."""
    if not url or any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in ALLOWED_SCHEMES or not parts.hostname:
        return None
    return url


def sanitize_html(html: str) -> str:
    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes=set(ALLOWED_SCHEMES),
        link_rel=LINK_REL,
        strip_comments=True,
    )


def render_markdown(text: str | None) -> Markup:
    """Render untrusted markdown (LLM output, excerpts) to sanitized HTML."""
    if not text:
        return Markup("")
    return Markup(sanitize_html(_md.render(text)))  # noqa: S704 — sanitized by nh3 above


def external_link(url: str | None, text: str | None = None) -> Markup:
    """`<a>` for http(s) URLs only; anything else renders as escaped plain text."""
    label = text if text is not None else (url or "")
    good = safe_url(url)
    if good is None:
        return escape(label)
    return Markup('<a href="{}" rel="{}">{}</a>').format(good, LINK_REL, label)


def register_filters(env: Environment) -> None:
    """Install `md` and `extlink` on a Jinja2 Environment."""
    env.filters["md"] = render_markdown
    env.filters["extlink"] = external_link
