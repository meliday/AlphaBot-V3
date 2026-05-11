from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alpha_bot.models import Market


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


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


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
