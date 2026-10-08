"""`make init`: create `.env` (mode 0600) from `.env.example` for a fresh clone (M11).

Fills the dashboard password hash (prompted), a session secret and a CSRF secret. API keys stay
blank: the stack runs without them, with the features that need them disabled (fail closed). Add
them to `.env` later and re-run `./deploy.sh`.

Never overwrites an existing `.env`. For scripted installs only, the password can come from
`AETHER_INIT_PASSWORD` instead of the prompt.
"""

from __future__ import annotations

import getpass
import os
import re
import secrets
import sys
from pathlib import Path

from aether.security.auth import hash_password

ROOT = Path(__file__).resolve().parent.parent
MIN_LEN = 12


def _password() -> str:
    pw = os.environ.get("AETHER_INIT_PASSWORD")
    if pw is None:
        pw = getpass.getpass("Dashboard password (12+ characters): ")
        if getpass.getpass("Repeat: ") != pw:
            raise SystemExit("Passwords don't match.")
    if len(pw) < MIN_LEN:
        raise SystemExit(f"Use at least {MIN_LEN} characters.")
    return pw


def render(template: str, values: dict[str, str]) -> str:
    out = template
    for key, value in values.items():
        out, n = re.subn(rf"(?m)^{key}=.*$", f"{key}={value}", out)
        if n != 1:
            raise SystemExit(f".env.example has no single {key}= line")
    return out


def main() -> int:
    env = ROOT / ".env"
    if env.exists():
        print(".env already exists; leaving it alone (edit it, or delete it to start over).")
        return 0
    values = {
        "AETHER_DASHBOARD_PASSWORD_HASH": hash_password(_password()),
        "AETHER_SESSION_SECRET": secrets.token_urlsafe(48),
        "AETHER_CSRF_SECRET": secrets.token_urlsafe(32),
    }
    text = render((ROOT / ".env.example").read_text(), values)
    fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(env, 0o600)
    print("Wrote .env (mode 0600). Next: set SEC_USER_AGENT (and any keys) in .env; ./deploy.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
