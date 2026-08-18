from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Literal

Market = Literal["KR", "US"]
Signal = Literal["Strong Buy", "Buy", "Wait", "Hold Off", "Sell"]
Side = Literal["buy", "sell"]
OrderStatus = Literal[
    "pending",
    "submitting",
    "unknown",
    "submitted",
    "partially_filled",
    "partially_filled_cancelled",
    "filled",
    "rejected",
    "cancelled",
]
Sentiment = Literal["positive", "neutral", "negative"]
Severity = Literal["low", "medium", "high"]


def parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value[:10])


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class Candle:
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: int

    @classmethod
    def from_mapping(cls, row: dict[str, Any]) -> "Candle":
        return cls(
            date=parse_date(row["date"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=int(float(row["volume"])),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


@dataclass(frozen=True)
class FundamentalsQuarter:
    period: str
    eps_yoy: float | None = None
    revenue_yoy: float | None = None
    eps_surprise: float | None = None
    revenue_surprise: float | None = None
    reported_at: date | None = None  # earnings release date (needed for backtest filtering)
    etf_proxy: bool = False           # True when derived from ETF top-holdings aggregation

    @classmethod
    def from_mapping(cls, row: dict[str, Any]) -> "FundamentalsQuarter":
        raw_date = row.get("reported_at")
        return cls(
            period=str(row["period"]),
            eps_yoy=_optional_float(row.get("eps_yoy")),
            revenue_yoy=_optional_float(row.get("revenue_yoy")),
            eps_surprise=_optional_float(row.get("eps_surprise")),
            revenue_surprise=_optional_float(row.get("revenue_surprise")),
            reported_at=parse_date(raw_date) if raw_date else None,
            etf_proxy=bool(row.get("etf_proxy", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "eps_yoy": self.eps_yoy,
            "revenue_yoy": self.revenue_yoy,
            "eps_surprise": self.eps_surprise,
            "revenue_surprise": self.revenue_surprise,
            "reported_at": self.reported_at.isoformat() if self.reported_at else None,
            "etf_proxy": self.etf_proxy,
        }


@dataclass(frozen=True)
class Catalyst:
    headline: str
    summary: str
    source: str = "manual"
    published_at: date | None = None

    @classmethod
    def from_mapping(cls, row: dict[str, Any]) -> "Catalyst":
        raw_date = row.get("published_at")
        return cls(
            headline=str(row.get("headline", "")),
            summary=str(row.get("summary", "")),
            source=str(row.get("source", "manual")),
            published_at=parse_date(raw_date) if raw_date else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "headline": self.headline,
            "summary": self.summary,
            "source": self.source,
            "published_at": self.published_at.isoformat() if self.published_at else None,
        }


@dataclass(frozen=True)
class MarketContext:
    benchmark_return_3m: float | None = None
    stock_return_3m: float | None = None
    sector_theme: str = "unknown"
    foreign_flow: str = "unknown"
    institutional_flow: str = "unknown"
    broker_coverage: str = "unknown"

    @classmethod
    def from_mapping(cls, row: dict[str, Any]) -> "MarketContext":
        return cls(
            benchmark_return_3m=_optional_float(row.get("benchmark_return_3m")),
            stock_return_3m=_optional_float(row.get("stock_return_3m")),
            sector_theme=str(row.get("sector_theme", "unknown")),
            foreign_flow=str(row.get("foreign_flow", "unknown")),
            institutional_flow=str(row.get("institutional_flow", "unknown")),
            broker_coverage=str(row.get("broker_coverage", "unknown")),
        )


@dataclass(frozen=True)
class Scoreboard:
    fundamentals: int
    fundamentals_note: str
    technical_trend: int
    technical_trend_note: str
    momentum_flow: int
    momentum_flow_note: str

    @property
    def total(self) -> int:
        return self.fundamentals + self.technical_trend + self.momentum_flow


@dataclass(frozen=True)
class IndicatorSnapshot:
    close: float
    sma50: float
    sma200: float
    rsi14: float
    bollinger_mid: float
    bollinger_upper: float
    bollinger_lower: float
    bollinger_width: float
    resistance_60d: float
    support_20d: float
    volume_summary: str
    vcp_pattern: str
    vcp_score: int
    vcp_details: str


@dataclass(frozen=True)
class TradePlan:
    entry_low: float
    entry_high: float
    stop_loss: float
    target1: float
    target2: float
    rr_ratio: float
    entry_reference: float
    stop_pct: float
    target1_pct: float
    target2_pct: float
    alternate_entry: float | None = None
    alternate_rr_ratio: float | None = None


@dataclass(frozen=True)
class TechnicalBreakdown:
    trend: str
    pattern: str
    volume: str
    risk_factors: str
    relative_strength: str


@dataclass(frozen=True)
class NewsAssessment:
    """LLM-derived view of recent news, used as an input to the strategy."""

    sentiment: Sentiment
    severity: Severity
    earnings_caution: bool
    score_adjustment: int  # clamped to [-3, +3]
    reasoning: str
    raw_news: str = ""
    source: str = "default"


@dataclass(frozen=True)
class AnalysisReport:
    ticker: str
    company_name: str
    market: Market
    signal: Signal
    reason: str
    scoreboard: Scoreboard
    indicators: IndicatorSnapshot
    trade_plan: TradePlan
    technical: TechnicalBreakdown
    earnings_caution: bool
    catalyst_summary: str
    as_of: date
    language: Literal["en", "ko"] = "en"
    news_assessment: NewsAssessment | None = None


@dataclass(frozen=True)
class OrderRequest:
    ticker: str
    market: Market
    side: Side
    quantity: int
    order_type: Literal["limit", "market"] = "limit"
    limit_price: float | None = None
    reason: str = ""
    client_order_id: str | None = None


@dataclass(frozen=True)
class OrderResult:
    broker: str
    accepted: bool
    broker_order_id: str
    message: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OrderCandidate:
    id: str
    request: OrderRequest
    status: OrderStatus
    created_at: str
    stop_loss: float | None = None
    target1: float | None = None
    target2: float | None = None
    analysis_signal: Signal | None = None
    submitted_at: str | None = None
    broker_order_id: str | None = None
    broker_message: str | None = None
    rejection_code: str | None = None
    rejection_retryable: bool | None = None
    broker: str = "unbound"
    broker_instance_id: str | None = None
    broker_account_id: str | None = None
    broker_mode: str | None = None
    filled_quantity: int = 0
    avg_fill_price: float | None = None
    last_synced_at: str | None = None
    exit_order_id: str | None = None
    exit_reason: str | None = None
    # Scale-out support: sells that reduce the position without closing it
    # (target-1 half-exit). The position is only "closed" when exit_order_id
    # fills; partial exits shrink the remaining quantity.
    partial_exit_ids: list[str] = field(default_factory=list)
    # Ratcheting trailing stop for the runner half after a target-1 scale-out.
    # Only ever moves up; floored at breakeven (avg_fill_price).
    trail_stop: float | None = None
    # Broker-side protective stop (Toss conditional order) mirroring the
    # currently effective stop. The polling ladder still owns targets and the
    # trail ratchet; this is the disaster brake that keeps working when the
    # bot process is not. ``_price``/``_quantity`` record what is actually
    # armed at the broker so we only re-send on a real divergence.
    protective_stop_id: str | None = None
    protective_stop_price: float | None = None
    protective_stop_quantity: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "request": {
                "ticker": self.request.ticker,
                "market": self.request.market,
                "side": self.request.side,
                "quantity": self.request.quantity,
                "order_type": self.request.order_type,
                "limit_price": self.request.limit_price,
                "reason": self.request.reason,
                "client_order_id": self.request.client_order_id,
            },
            "status": self.status,
            "created_at": self.created_at,
            "stop_loss": self.stop_loss,
            "target1": self.target1,
            "target2": self.target2,
            "analysis_signal": self.analysis_signal,
            "submitted_at": self.submitted_at,
            "broker_order_id": self.broker_order_id,
            "broker_message": self.broker_message,
            "rejection_code": self.rejection_code,
            "rejection_retryable": self.rejection_retryable,
            "broker": self.broker,
            "broker_instance_id": self.broker_instance_id,
            "broker_account_id": self.broker_account_id,
            "broker_mode": self.broker_mode,
            "filled_quantity": self.filled_quantity,
            "avg_fill_price": self.avg_fill_price,
            "last_synced_at": self.last_synced_at,
            "exit_order_id": self.exit_order_id,
            "exit_reason": self.exit_reason,
            "partial_exit_ids": list(self.partial_exit_ids),
            "trail_stop": self.trail_stop,
            "protective_stop_id": self.protective_stop_id,
            "protective_stop_price": self.protective_stop_price,
            "protective_stop_quantity": self.protective_stop_quantity,
        }

    @classmethod
    def from_mapping(cls, row: dict[str, Any]) -> "OrderCandidate":
        req = row["request"]
        return cls(
            id=str(row["id"]),
            request=OrderRequest(
                ticker=str(req["ticker"]),
                market=req["market"],
                side=req["side"],
                quantity=int(req["quantity"]),
                order_type=req.get("order_type", "limit"),
                limit_price=_optional_float(req.get("limit_price")),
                reason=str(req.get("reason", "")),
                client_order_id=(
                    str(req["client_order_id"])
                    if req.get("client_order_id")
                    else None
                ),
            ),
            status=row.get("status", "pending"),
            created_at=str(row["created_at"]),
            stop_loss=_optional_float(row.get("stop_loss")),
            target1=_optional_float(row.get("target1")),
            target2=_optional_float(row.get("target2")),
            analysis_signal=row.get("analysis_signal"),
            submitted_at=row.get("submitted_at"),
            broker_order_id=row.get("broker_order_id"),
            broker_message=row.get("broker_message"),
            rejection_code=(
                str(row["rejection_code"]) if row.get("rejection_code") else None
            ),
            rejection_retryable=(
                bool(row["rejection_retryable"])
                if row.get("rejection_retryable") is not None
                else None
            ),
            broker=str(row.get("broker", "unbound")),
            broker_instance_id=(
                str(row["broker_instance_id"])
                if row.get("broker_instance_id")
                else None
            ),
            broker_account_id=(
                str(row["broker_account_id"])
                if row.get("broker_account_id")
                else None
            ),
            broker_mode=(str(row["broker_mode"]) if row.get("broker_mode") else None),
            filled_quantity=int(row.get("filled_quantity", 0) or 0),
            avg_fill_price=_optional_float(row.get("avg_fill_price")),
            last_synced_at=row.get("last_synced_at"),
            exit_order_id=row.get("exit_order_id"),
            exit_reason=row.get("exit_reason"),
            partial_exit_ids=[str(x) for x in (row.get("partial_exit_ids") or [])],
            trail_stop=_optional_float(row.get("trail_stop")),
            protective_stop_id=(
                str(row["protective_stop_id"])
                if row.get("protective_stop_id")
                else None
            ),
            protective_stop_price=_optional_float(row.get("protective_stop_price")),
            protective_stop_quantity=int(row.get("protective_stop_quantity", 0) or 0),
        )


@dataclass(frozen=True)
class AccountBalance:
    """Cash + total evaluation summary from a broker."""

    broker: str
    market: Market
    currency: str
    cash: float
    securities_value: float
    total_value: float
    pnl: float | None = None
    pnl_pct: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Position:
    """A single holding with current valuation."""

    broker: str
    ticker: str
    market: Market
    quantity: int
    avg_price: float
    current_price: float
    market_value: float
    pnl: float
    pnl_pct: float
    currency: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OrderFill:
    """Broker-reported fill state for a previously submitted order."""

    broker_order_id: str
    status: OrderStatus
    filled_quantity: int
    ordered_quantity: int
    avg_fill_price: float | None
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)
