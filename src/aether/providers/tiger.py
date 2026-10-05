"""S8: Tiger Brokers OpenAPI, read-only. The ONLY module allowed to import `tigeropen`
(`scripts/check_broker_readonly.py` enforces it in `make lint`).

The Tiger key can place orders, so this wrapper exposes an explicit allow-list of read calls and
nothing else: today just `positions()` (US stock positions). It keeps the SDK's bound
`get_positions` method only, never the TradeClient itself.

- Worker only: credentials (`TIGER_ID`, `TIGER_PRIVATE_KEY`, `TIGER_ACCOUNT`) are passed to the
  worker service alone; the dashboard requests a sync through the command queue.
- Fail closed: missing or malformed credentials disable the module (`TigerConfig.from_settings`
  returns None and a reason).
- Hosts: dynamic-domain discovery is turned off, so the SDK talks only to its documented default
  gateway (https://openapi.tigerfintech.com/gateway). Its props/token files would live in /tmp.
- No leakage: positions and the account number never go into logs, prompts or alerts; errors are
  reduced to the exception type plus Tiger's error code. The account is shown masked.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from aether.config import Settings

log = logging.getLogger(__name__)

PROPS_DIR = "/tmp/tigeropen"  # noqa: S108 - tmpfs in the container; the SDK only reads here
TIMEOUT_SECONDS = 20
ACCOUNT_RE = re.compile(r"^[A-Za-z0-9]{4,32}$")
TIGER_ID_RE = re.compile(r"^[0-9]{4,20}$")
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


class TigerError(RuntimeError):
    """A Tiger call failed. The message never contains positions, keys or the account."""


@dataclass(frozen=True)
class TigerConfig:
    tiger_id: str
    private_key: str = ""
    account: str = ""

    def __repr__(self) -> str:  # never print the key or account
        return f"TigerConfig(tiger_id={self.tiger_id!r}, account={mask_account(self.account)})"

    @classmethod
    def from_settings(cls, settings: Settings) -> tuple[TigerConfig | None, str | None]:
        tid = settings.tiger_id
        key = settings.tiger_private_key.get_secret_value() if settings.tiger_private_key else None
        acct = settings.tiger_account.get_secret_value() if settings.tiger_account else None
        if tid is None and key is None and acct is None:
            return None, "TIGER_ID / TIGER_PRIVATE_KEY / TIGER_ACCOUNT not set"
        if tid is None or key is None or acct is None:
            return (
                None,
                "Tiger credentials incomplete: need TIGER_ID, TIGER_PRIVATE_KEY and TIGER_ACCOUNT",
            )
        if not TIGER_ID_RE.fullmatch(tid.strip()):
            return None, "TIGER_ID is malformed (expected digits)"
        if not ACCOUNT_RE.fullmatch(acct.strip()):
            return None, "TIGER_ACCOUNT is malformed"
        body = _key_body(key)
        if body is None:
            return None, "TIGER_PRIVATE_KEY is malformed (expected an RSA private key)"
        return cls(tid.strip(), body, acct.strip()), None


def _key_body(key: str) -> str | None:
    """The base64 body of a PEM (or bare base64) RSA private key, as the SDK expects."""
    text = key.replace("\\n", "\n").strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("-----")]
    body = "".join(lines)
    if len(body) < 200 or not re.fullmatch(r"[A-Za-z0-9+/=]+", body):
        return None
    return body


def mask_account(account: str) -> str:
    return "••••" + account[-4:] if len(account) >= 4 else "••••"


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    quantity: Decimal
    average_cost: Decimal | None


def _dec(v: Any) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def parse_positions(raw: list[Any] | None) -> list[BrokerPosition]:
    """SDK `Position` objects → US-dollar stock positions. `position_qty` is the decimal
    quantity (`quantity` is scaled by `position_scale` for fractional shares)."""
    out: list[BrokerPosition] = []
    for p in raw or []:
        contract = getattr(p, "contract", None)
        symbol = getattr(contract, "symbol", None)
        if not isinstance(symbol, str) or not SYMBOL_RE.fullmatch(symbol):
            continue
        if getattr(contract, "sec_type", "STK") != "STK":
            continue
        if getattr(contract, "currency", "USD") not in ("USD", None):
            continue
        qty = _dec(getattr(p, "position_qty", None))
        if qty is None:
            qty = _dec(getattr(p, "quantity", None))
        if qty is None or qty < 0:
            continue  # short positions are out of scope for the sleeve
        qty = qty.quantize(Decimal("0.000001"))
        cost = _dec(getattr(p, "average_cost", None))
        out.append(
            BrokerPosition(symbol, qty, cost.quantize(Decimal("0.000001")) if cost else None)
        )
    return out


PositionsCall = Callable[..., Any]


class TigerReadOnly:
    """Read-only facade. Holds only the bound `get_positions` call."""

    __slots__ = ("_account", "_get_positions")

    def __init__(self, get_positions: PositionsCall, account: str) -> None:
        self._get_positions = get_positions
        self._account = account

    @classmethod
    def connect(cls, config: TigerConfig) -> TigerReadOnly:
        """Build the SDK client. No network call happens here (dynamic domains are off)."""
        from tigeropen.tiger_open_config import TigerOpenClientConfig
        from tigeropen.trade.trade_client import TradeClient

        cfg = TigerOpenClientConfig(enable_dynamic_domain=False, props_path=PROPS_DIR)
        cfg.tiger_id = config.tiger_id
        cfg.private_key = config.private_key
        cfg.account = config.account
        cfg.timeout = TIMEOUT_SECONDS
        sdk_log = logging.getLogger("tiger_openapi")
        sdk_log.setLevel(logging.WARNING)
        client = TradeClient(cfg, logger=sdk_log)
        return cls(client.get_positions, config.account)

    @property
    def account_masked(self) -> str:
        return mask_account(self._account)

    def positions(self) -> list[BrokerPosition]:
        try:
            raw = self._get_positions(sec_type="STK", currency="USD", market="US")
        except Exception as exc:
            code = getattr(exc, "code", None)
            detail = (
                f" (code {code})" if isinstance(code, int | str) and str(code).isalnum() else ""
            )
            raise TigerError(f"Tiger get_positions failed: {type(exc).__name__}{detail}") from None
        return parse_positions(raw)
