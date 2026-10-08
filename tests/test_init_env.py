"""M11 `make init`: writes .env (0600) from .env.example, never overwrites, fills the secrets."""

from __future__ import annotations

import importlib.util
import os
import stat
from pathlib import Path
from types import ModuleType

import pytest

from aether.security.auth import PasswordHash
from tests.conftest import REPO


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("init_env", REPO / "scripts" / "init_env.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_writes_env_once_with_mode_0600(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load()
    (tmp_path / ".env.example").write_text((REPO / ".env.example").read_text())
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setenv("AETHER_INIT_PASSWORD", "synthetic test password")  # test value only
    assert mod.main() == 0
    env = tmp_path / ".env"
    assert stat.S_IMODE(os.stat(env).st_mode) == 0o600
    values = dict(
        line.split("=", 1)
        for line in env.read_text().splitlines()
        if "=" in line and line[0] != "#"
    )
    assert PasswordHash.parse(values["AETHER_DASHBOARD_PASSWORD_HASH"]).verify(
        "synthetic test password"
    )
    assert len(values["AETHER_SESSION_SECRET"]) >= 48 and values["AETHER_CSRF_SECRET"]
    assert values["ANTHROPIC_API_KEY"] == ""  # keys stay blank
    before = env.read_text()
    assert mod.main() == 0 and env.read_text() == before  # never overwritten


def test_short_password_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load()
    (tmp_path / ".env.example").write_text((REPO / ".env.example").read_text())
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setenv("AETHER_INIT_PASSWORD", "short")
    with pytest.raises(SystemExit, match="12 characters"):
        mod.main()
    assert not (tmp_path / ".env").exists()
