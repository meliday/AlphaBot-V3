# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run all tests
python3 -m unittest discover -s tests

# Run a single test file
python3 -m pytest tests/test_indicators.py

# Install as editable CLI
python3 -m pip install -e .

# Analyze a stock (demo mode uses synthetic data, no real files needed)
python3 -m alpha_bot.runner analyze --ticker NVDA --market US --demo
python3 -m alpha_bot.runner analyze --ticker 005930 --market KR --demo --language ko

# Analyze using local data files
python3 -m alpha_bot.runner analyze --ticker NVDA --market US

# Queue an order candidate during analysis
python3 -m alpha_bot.runner analyze --ticker NVDA --market US --demo --queue-order --quantity 1

# Manage pending orders
python3 -m alpha_bot.runner pending
python3 -m alpha_bot.runner approve --order-id <ORDER_ID> --broker mock

# Launch GUI
python3 -m alpha_bot.gui
```

## Architecture

The bot follows a strict **(news → LLM assess) → analyze → queue → approve → broker** flow.
The `auto` command can collapse `queue → approve` into a single automatic step,
but no live KIS order is ever sent without `--broker kis` explicitly chosen.

### Package layout (`src/alpha_bot/`)

- **`models.py`** — All frozen dataclasses: `Candle`, `FundamentalsQuarter`, `Catalyst`, `MarketContext`, `IndicatorSnapshot`, `Scoreboard`, `TradePlan`, `AnalysisReport`, `NewsAssessment`, `OrderRequest`, `OrderResult`, `OrderCandidate`. Central type system for the whole project.
- **`config.py`** — `load_config()` reads `config.yaml` + `.env` into `AppConfig`. Uses a hand-rolled YAML/dotenv parser.
- **`strategy/analyzer.py`** — `StrategyAnalyzer.analyze()` is the main entry point: takes candles + fundamentals + catalysts + context + optional `NewsAssessment`, runs CANSLIM+VCP scoring, and returns an `AnalysisReport`. The LLM assessment can adjust the fundamentals score (±3) and force an earnings-caution flag; severity=high & sentiment=negative vetoes the signal to `Hold Off`. All scoring thresholds live in `StrategyParams`.
- **`strategy/indicators.py`** — Pure functions: `latest_sma`, `latest_rsi`, `latest_bollinger`, `detect_vcp`, `volume_accumulation_summary`.
- **`news/assessor.py`** — `assess_news()` calls OpenAI with a strict JSON schema and returns a `NewsAssessment`. Returns a neutral assessment on missing key / API failure / parse error.
- **`auto.py`** — Single source of truth for the "fetch news → LLM assess → analyze → enqueue → approve" pipeline. `run_auto_iteration()` is shared by `runner.py cmd_auto` and `web.py auto_pilot_loop`. Enforces `max_positions` and per-ticker cooldown.
- **`risk.py`** — `build_trade_plan()` computes entry zone, stop-loss, target1/target2, and R/R ratio from candles + SMA50.
- **`data/providers.py`** — `DataProvider` protocol + three implementations: `FixtureDataProvider`, `SyntheticDataProvider`, `KisPriceDataProvider`.
- **`data/scraper.py`** — Best-effort news fetchers (`yfinance` for US, Naver Finance HTML for KR). Failure is non-fatal.
- **`approval/queue.py`** — `ApprovalQueue` persists order candidates to `pending_orders.json`. Prevents duplicate pending orders for same ticker/market/side.
- **`broker/{base,mock,kis}.py`** — Broker protocol + always-accept mock + KIS REST adapter (paper/live).
- **`report/markdown.py`** — Renders `AnalysisReport` (incl. LLM assessment section) as Markdown in EN or KO.
- **`backtest.py`** — `BacktestResult` with metrics (win rate, Sharpe, max drawdown).
- **`gui.py`** — Tkinter desktop GUI. **`web.py`** — `http.server`-based dashboard at port 8501.
- **`runner.py`** — CLI entry point (`bot` command). Supports `analyze --no-llm` and `auto --no-llm` to bypass the LLM.

### Data files

Local data expected at `data/{prices,fundamentals,news,contexts}/{MARKET}_{TICKER}.{csv,json}`. See README for exact formats. `--demo` flag bypasses all file I/O using `SyntheticDataProvider`.

### Configuration

- `config.yaml` / `config.example.yaml` — bot settings (`broker`, `min_score`, `min_rr`, `risk_per_trade_pct`, `max_positions`, etc.)
- `.env` — KIS API credentials (`KIS_APP_KEY`, `KIS_APP_SECRET`, `KIS_ACCOUNT_NO`, `KIS_MODE=paper`) **and** optional `OPENAI_API_KEY` / `OPENAI_MODEL` for the LLM news pipeline.
- `watchlist.yaml` / `watchlist.example.yaml` — universe for batch scanning and auto-pilot.

### Key invariants

- Runtime deps: `openai`, `requests`, `beautifulsoup4`, `yfinance`. KIS REST + scoring still works without them when `--no-llm` is set or LLM gracefully fails.
- `pythonpath = ["src"]` is set in `pyproject.toml` so imports resolve correctly. Tests use `unittest`/`pytest`.
- Scoring: max 30 points (10 fundamentals + 10 technical trend + 10 momentum/flow). Default `min_score=24`, `min_rr=1.5` for a Buy signal; `strong_buy_score=27` + `strong_buy_rr=3.0` for Strong Buy. LLM news adjustment can move the fundamentals sub-score by ±3 (clamped to [0,10]).
- Auto-trading safety: `max_positions` caps concurrent open orders; `--cooldown-hours` (default 24h) prevents re-ordering the same ticker; failures on individual tickers never abort the iteration.
