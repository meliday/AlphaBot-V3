"""Application configuration loader (zero external dependencies).

Reads ``config.yaml`` using a simple ``key: value`` parser (no PyYAML
required) and ```.env`` via line-by-line parsing. Secrets (KIS keys,
OpenAI key) are loaded from environment variables / .env and never
stored in the config dataclass.

The ``load_watchlist`` helper supports both YAML-style ``universe:``
lists and flat JSON arrays.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alpha_bot.models import Market


# Values written by this loader may be refreshed when the dashboard updates
# .env. Values that were already present in the process environment (or were
# changed externally afterwards) remain authoritative.
_DOTENV_MANAGED_VALUES: dict[str, str] = {}


@dataclass(frozen=True)
class AppConfig:
    default_market: Market = "US"
    broker: str = "mock"
    data_dir: Path = Path("data")
    approval_queue: Path = Path("pending_orders.json")
    max_positions: int = 5
    risk_per_trade_pct: float = 1.0
    min_score: int = 24
    min_rr: float = 1.5
    # ── Risk guards (P1 hardening) ──
    # Cap any single position at this % of account value. Without this, a
    # tight stop (2.5%) with 1% risk sizing could put ~40% of the account
    # into one name. 0 disables the cap.
    max_position_pct: float = 20.0
    # Circuit breaker: once today's realized losses reach this % of account
    # value (per market), stop opening new positions until tomorrow.
    # 0 disables the breaker.
    daily_loss_limit_pct: float = 3.0
    # Cancel limit orders still unfilled after this many minutes so they
    # can't fill at a stale price later in the session. 0 disables.
    stale_order_minutes: int = 60
    # Opt-in Minervini-style entry discipline: only buy a fresh pivot break
    # on expanding volume (see StrategyParams.require_breakout_confirmation).
    # Off by default — the pre-pivot squeeze entry is also a supported style.
    require_breakout: bool = False
    # Mirror the effective stop as a broker-side conditional order so held
    # positions stay protected while the bot process is down. Off by default:
    # enabling it makes the bot place real standing orders at the venue, and
    # only brokers implementing ProtectiveStopBroker (Toss) honour it.
    protective_stop: bool = False


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    loaded_keys: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        loaded_keys.add(key)
        previous_managed = _DOTENV_MANAGED_VALUES.get(key)
        current = os.environ.get(key)
        if current is None or current == previous_managed:
            os.environ[key] = value
            _DOTENV_MANAGED_VALUES[key] = value
        else:
            # Explicit process/service environment wins over a local .env.
            _DOTENV_MANAGED_VALUES.pop(key, None)

    for key, old_value in list(_DOTENV_MANAGED_VALUES.items()):
        if key not in loaded_keys:
            if os.environ.get(key) == old_value:
                os.environ.pop(key, None)
            _DOTENV_MANAGED_VALUES.pop(key, None)


def load_config(path: Path = Path("config.yaml")) -> AppConfig:
    load_dotenv()
    values: dict[str, Any] = {}
    if path.exists():
        values.update(_parse_simple_yaml(path))

    broker = str(values.get("broker", os.environ.get("BOT_BROKER", "mock")))
    queue = Path(str(values.get("approval_queue", os.environ.get("BOT_APPROVAL_QUEUE", "pending_orders.json"))))
    data_dir = Path(str(values.get("data_dir", "data")))
    market = str(values.get("default_market", "US")).upper()
    if market not in {"KR", "US"}:
        market = "US"

    return AppConfig(
        default_market=market,  # type: ignore[arg-type]
        broker=broker,
        data_dir=data_dir,
        approval_queue=queue,
        max_positions=int(values.get("max_positions", 5)),
        risk_per_trade_pct=float(values.get("risk_per_trade_pct", 1.0)),
        min_score=int(values.get("min_score", 24)),
        min_rr=float(values.get("min_rr", 1.5)),
        max_position_pct=float(values.get("max_position_pct", 20.0)),
        daily_loss_limit_pct=float(values.get("daily_loss_limit_pct", 3.0)),
        stale_order_minutes=int(values.get("stale_order_minutes", 60)),
        require_breakout=bool(values.get("require_breakout", False)),
        protective_stop=bool(values.get("protective_stop", False)),
    )


def load_watchlist(path: Path) -> list[dict[str, str]]:
    """Load a tiny YAML/CSV/JSON-like watchlist without external dependencies."""
    if not path.exists():
        raise FileNotFoundError(path)
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if path.suffix.lower() == ".json":
        import json

        rows = json.loads(text)
        return [
            {
                "ticker": str(row["ticker"]).upper(),
                "market": str(row.get("market", "US")).upper(),
                "company": str(row.get("company", row["ticker"])),
            }
            for row in rows
        ]

    rows: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line == "universe:":
            continue
        if line.startswith("- "):
            if current:
                rows.append(current)
            current = {}
            line = line[2:].strip()
            if line:
                key, value = _split_key_value(line)
                current[key] = value
            continue
        if current is not None and ":" in line:
            key, value = _split_key_value(line)
            current[key] = value
        elif "," in line:
            ticker, market = [part.strip() for part in line.split(",", 1)]
            rows.append({"ticker": ticker.upper(), "market": market.upper(), "company": ticker.upper()})
        elif ":" in line:
            market, ticker = [part.strip() for part in line.split(":", 1)]
            rows.append({"ticker": ticker.upper(), "market": market.upper(), "company": ticker.upper()})
    if current:
        rows.append(current)

    normalized = []
    for row in rows:
        ticker = str(row["ticker"]).upper()
        normalized.append(
            {
                "ticker": ticker,
                "market": str(row.get("market", "US")).upper(),
                "company": str(row.get("company", ticker)),
            }
        )
    return normalized


def _parse_simple_yaml(path: Path) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line or line.startswith("-"):
            continue
        key, value = _split_key_value(line)
        values[key] = _coerce_value(value)
    return values


def _split_key_value(line: str) -> tuple[str, str]:
    key, value = line.split(":", 1)
    return key.strip(), value.strip().strip('"').strip("'")


def _coerce_value(value: str) -> Any:
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value
