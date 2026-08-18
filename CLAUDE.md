# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run all tests
python3 -m unittest discover -s tests
# Run a single test file (pytest sets pythonpath=["src"] via pyproject)
python3 -m pytest tests/test_indicators.py

# Install as editable CLI (exposes `bot` and `bot-gui` entry points)
python3 -m pip install -e .

# Analyze a stock (demo mode uses synthetic data, no real files needed)
python3 -m alpha_bot.runner analyze --ticker NVDA --market US --demo
python3 -m alpha_bot.runner analyze --ticker 005930 --market KR --demo --language ko
python3 -m alpha_bot.runner analyze --ticker NVDA --market US            # local data files
python3 -m alpha_bot.runner analyze --ticker NVDA --market US --no-llm   # skip LLM news step

# Queue an order candidate during analysis
python3 -m alpha_bot.runner analyze --ticker NVDA --market US --demo --queue-order --quantity 1

# Batch-scan a watchlist (LLM is off in scan for speed)
python3 -m alpha_bot.runner scan --universe watchlist.yaml --demo

# Manage pending orders
python3 -m alpha_bot.runner pending
python3 -m alpha_bot.runner approve --order-id <ORDER_ID> --broker mock

# Auto-pilot: repeated news→assess→analyze→enqueue→approve sweeps
python3 -m alpha_bot.runner auto --universe watchlist.yaml --interval 300 --broker mock --demo
python3 -m alpha_bot.runner auto --universe watchlist.yaml --broker kis --auto-size   # live/paper KIS

# Backtest a single ticker's signal history
python3 -m alpha_bot.runner backtest --ticker NVDA --market US
# Portfolio backtest: whole watchlist under shared cash / max_positions / sizing (one run per market)
python3 -m alpha_bot.runner backtest --universe watchlist.yaml --demo --cash 10000

# Real-time exit monitor (Phase 3a): KIS WebSocket ticks drive the standard exit engine
python3 -m alpha_bot.runner monitor --broker kis --kis-data   # live/paper; mock+--demo for dry runs

# Launch interfaces
python3 -m alpha_bot.gui       # Tkinter desktop GUI (bot-gui)
python3 -m alpha_bot.web       # http.server dashboard on port 8501
```

## Architecture

The bot follows a strict **(news → LLM assess) → analyze → queue → approve → broker** flow.
The `auto` command collapses `queue → approve` into a single automatic step,
but **no live KIS order is ever sent without `--broker kis` explicitly chosen** (`mock` is the default and always accepts).

### Package layout (`src/alpha_bot/`)

`auto.py` and `web.py` were split into subpackages (`auto/`, `web/`) in commit `74272bf` — refer to the modules below, not the old single files.

**Core types & config**
- **`models.py`** — All frozen dataclasses (the central type system): `Candle`, `FundamentalsQuarter`, `Catalyst`, `MarketContext`, `IndicatorSnapshot`, `Scoreboard`, `TradePlan`, `TechnicalBreakdown`, `AnalysisReport`, `NewsAssessment`, `OrderRequest`, `OrderResult`, `OrderCandidate`, `AccountBalance`, `Position`, `OrderFill`. Each carries its own `to_dict`/`from_mapping` for JSON round-tripping. `Market`/`Signal`/`Side`/`OrderStatus`/`Sentiment`/`Severity` are `Literal` types. `OrderCandidate` tracks the split-exit state: `exit_order_id` (the closing sell), `partial_exit_ids` (target-1 scale-outs that reduce without closing), and `trail_stop` (ratcheting trailing stop for the runner half).
- **`config.py`** — `load_config()` reads `config.yaml` + `.env` into a frozen `AppConfig`; `load_watchlist()` parses YAML/CSV/JSON universes. Hand-rolled parsers, **no PyYAML**. Secrets are read from env, never stored in the dataclass.
- **`errors.py`** — Exception hierarchy rooted at `BotError` (`DataError`, `StrategyError`, `BrokerError`, `ApprovalError`). `runner.main()` catches `BotError`/`FileNotFoundError`/`ValueError` and exits `2`.
- **`utils.py`** — `validate_market`, `safe_float`, `infer_us_exchange_code` + the process-wide `_US_EXCD_CACHE` (remembers which US exchange a ticker resolved to).

**Strategy**
- **`strategy/analyzer.py`** — `StrategyAnalyzer.analyze()` is the main entry point. Runs CANSLIM+VCP scoring over candles + fundamentals + catalysts + context + optional `NewsAssessment`, returns an `AnalysisReport`. All tuneable thresholds live in the `StrategyParams` dataclass. **Always construct via `analyzer_from_config(config)`** (used by CLI/auto/web alike) so config-driven toggles like `require_breakout` can't diverge between interfaces. See **Scoring & signal logic** below.
- **`strategy/indicators.py`** — Pure functions: `latest_sma`, `latest_rsi`, `latest_atr`, `latest_bollinger` (sample stdev, n−1), `detect_vcp`, `volume_accumulation_summary`, `breakout_status` (pivot-break classifier: confirmed / no_breakout / extended / low_volume / insufficient). All degrade gracefully to partial windows when history is short.
- **`risk.py`** — `build_trade_plan()` computes entry zone, ATR-based stop-loss, target1/target2, and R/R from candles + SMA50. Stop = wider of (2×ATR below close) vs structural floor (below 20-day low / SMA50), bounded to **[2.5%, 7%]** of price (Minervini). Emits an `alternate_entry` when the base R/R misses `min_rr`. **`rr_ratio` is measured at `entry_high` (close×1.01), not at close** — every caller posts its buy limit there, so a close-based ratio would gate on a trade the bot never takes (inflated ~20–75% depending on stop width). Owns `TRAIL_ATR_MULT` (2.0), shared by the live position manager and the backtester so simulated exits can't drift from production.

**Market filters**
- **`market_regime.py`** — CANSLIM "M": blocks new entries when the broad index (`^GSPC`/`^KS11` via yfinance) is below its 200-day SMA, **or** when `count_distribution_days` sees ≥6 IBD distribution days (≥0.2% drop on rising volume) in the last 25 sessions while still above it — an early top warning. Distribution counting fails open (`None`) when the index volume series is unusable. Also exposes `return_3m` (index 63-day return) as the benchmark leg of the relative-strength score. 6h TTL cache, thread-locked, **fail-open** if the fetch fails.
- **`market_hours.py`** — Open/closed gate (`market_status`, `any_market_open`). Consults **two sources in order**: the venue calendar via `market_calendar`, then the baked-in `_HOLIDAYS` table (KR 09:00–15:30, US 09:30–16:00 local). The table still fails closed on years absent from `_COMPLETE_HOLIDAY_YEARS`; the venue calendar removes that annual cliff. The calendar path can never raise into the gate — `market_status` gates every sweep, exit check and entry, so a calendar failure degrades to the table.
- **`market_calendar.py`** — Regular-session windows from `GET /api/v1/market-calendar/{KR|US}` (token-only, no account). Process-wide 6h TTL cache behind a lock, built lazily from env, **fail-open** (`None` → fall back). Collects the regular sessions of previous/today/next in one list so a US session spanning 22:30–05:00 KST is matched by simple membership instead of date arithmetic. Regular hours only: pre/after-market are thinner, and Toss fires KR conditional stops during the KRX regular session alone, so trading outside it would mean holding positions the broker-side stop cannot protect.

**Auto-trading (`auto/`)**
- **`auto/orchestrator.py`** — `run_auto_iteration()` is the single source of truth for one watchlist sweep, shared by `runner.py cmd_auto` and the web auto-pilot. Order of operations: sync fills → cancel stale orders → reconcile queue vs broker → manage open positions → **kill-switch gate** (blocks all new buys; exit management above already ran) → cap check (`max_positions`) → per-ticker loop (market-hours gate → regime gate → **daily-loss breaker** → cooldown → analyze → force-exit-if-held → size → cash + position-cap pre-flight → enqueue+approve). Individual ticker failures never abort the sweep.
- **`auto/protective_stops.py`** — Mirrors the *currently effective* stop (hard stop before the target-1 scale-out, trail after) as a broker-side conditional order so a dead bot process no longer means an unprotected position. SINGLE + MARKET, not OCO: a Toss conditional order carries one quantity for the whole group and cannot express "half at target-1, the rest at target-2", so the venue owns only the disaster brake while the bot keeps targets, scale-outs and news exits. Core invariant is **at most one seller**: `stop_engaged()` suspends the polling ladder while a venue stop is firing (fails *closed* on an unknown status), and `release_protective_stop()` retires the venue stop before any bot-initiated sell — a failed release aborts that sell rather than risking an oversell. Opt-in via `protective_stop` in config; `supports_protective_stops()` duck-types the capability (Toss + mock only). Coverage limit: Toss triggers KR conditionals during the KRX regular session only.
- **`auto/guards.py`** — Kill switch (a `KILL_SWITCH` file at repo root, path override via `BOT_KILL_SWITCH`; first line = operator reason; delete to resume) and the daily-loss circuit breaker (`realized_pnl_today` pairs filled sells with parent buys via `exit_order_id`/`partial_exit_ids`; `daily_loss_exceeded` trips per market at `daily_loss_limit_pct` of account value; fail-open on balance-query errors).
- **`auto/analysis.py`** — `analyze_ticker()` (news→LLM→analyze one-shot, used by CLI + auto) plus `make_provider`/`make_broker` factories and news-cache integration.
- **`auto/position_manager.py`** — Post-fill exit ladder: before target-1, hard stop (market-sell all) or **target-1 scale-out** (limit-sell the larger half); after target-1, the runner half is governed by a **2×ATR trailing stop** (ratchets up only, floored at breakeven) and **target-2**. Structured as pure decision → side effects: `_evaluate_exit()` (no I/O, unit-testable) → `_ratchet_trail()` / `_submit_exit()` (enqueue→link→approve→notify); `manage_open_positions` is the orchestrating loop. `remaining_quantity()` nets out partial exits and defers new sells while one is in flight. `should_force_exit` fires immediately on severe LLM news, but `earnings_caution` alone requires price confirmation (close < SMA50) before liquidating. Also `reconcile_queue_with_broker`/`find_held_buy`/`count_open_positions` (detect manual sells, prevent double-entry).
- **`auto/live_monitor.py`** — Phase 3a: tick-driven exits WITHOUT a second exit engine. `TickPriceCache` (latest tick + generation counter) + `StreamPricedProvider` (tick price first, TTL-cached daily candles for ATR math) + `LiveExitMonitor.evaluate_if_fresh()` which simply reruns `manage_open_positions` when new ticks arrived — broker verification, scale-outs, trail ratchet, alerts all included for free. Subscriptions track `held_kr_tickers()` (KR only; US positions stay on the 5-min loop). Kill switch never gates this module. CLI: `runner monitor`.
- **`auto/sizing.py`** — `compute_position_size()`: shares = total_value × `risk_per_trade_pct` ÷ per-share-risk, capped by available cash **and** by `max_position_pct` of account value (a 2.5% stop with 1% risk would otherwise size to ~40% of equity). The fixed-quantity path enforces the same cap in the orchestrator pre-flight. `usable_cash()` (KIS paper cash=0 fallback) lives here and is shared with the orchestrator pre-flight.

**News / LLM (`news/`)**
- **`news/assessor.py`** — `assess_news()` calls OpenAI (`OPENAI_MODEL`, default `gpt-4o-mini`) with a strict JSON schema → `NewsAssessment`. Returns a `neutral_assessment` on missing key / import failure / API error / parse error, so the pipeline is deterministic without the LLM.
- **`news/cache.py`** — On-disk TTL cache (`news_cache.json`, default 3600s) keyed by `market:ticker`. Neutral fallbacks (`source="default"`) are **not** cached so the next scan retries the LLM. TTL/path overridable via `NEWS_CACHE_TTL_SECONDS`/`NEWS_CACHE_PATH`.

**Data (`data/`)**
- **`data/providers.py`** — `DataProvider` protocol + `FixtureDataProvider` (local CSV/JSON), `SyntheticDataProvider` (`--demo`, deterministic by ticker seed), `KisPriceDataProvider` (KIS REST prices; US probes NAS→NYS→AMS and caches the hit; falls back to fixtures for fundamentals/news). `get_current_price()` returns a live quote (KIS 현재가 API for the KIS provider, last close for fixtures/demo) — exit monitoring prefers it over the lagging daily candle. `compound_return()` helper lives here.
- **`data/fundamentals.py`** — Live fundamentals: `fetch_us_fundamentals` (yfinance, incl. ETF-proxy aggregation for leveraged/basket ETFs) and `fetch_kr_fundamentals` (via KIS client).
- **`data/scraper.py`** — Best-effort news text: `fetch_us_news` (yfinance), `fetch_kr_news` (Naver Finance HTML). Failure is non-fatal.
- **`data/quotes.py`** — `fetch_quotes()`: current price + day change for dashboard tables.
- **`data/stream.py`** — KIS real-time WebSocket (KR 체결가 `H0STCNT0`). `parse_frame`/`build_subscribe_frame` are pure (wire protocol unit-tested offline); `KisStreamClient` is a thin thread with approval-key fetch (`/oauth2/Approval`, note `secretkey` field), auto-reconnect + resubscribe, PINGPONG echo. URLs by `KIS_MODE`: paper `:31000`, live `:21000`. H0STCNT0 field indices are constants — verify once against live paper data.
- **`data/bars.py`** — `BarAggregator`: tick → fixed-interval intraday bars (OHLCV + session VWAP), pure/in-memory. Stale ticks dropped, gaps produce no synthetic bars, `force_close` flushes at session end. Feeds Phase-3b intraday strategies.

**Approval & broker**
- **`approval/queue.py`** — `ApprovalQueue` persists `OrderCandidate`s to `pending_orders.json`. Per-path re-entrant lock + atomic temp-file rename. Blocks duplicate active orders and re-entry into an unexited filled buy. Key methods: `enqueue`, `approve` (broker call outside the lock), `sync_with_broker`, `cancel_stale_orders` (voids limit orders unfilled past `stale_order_minutes`; partially-filled orders are deliberately left alone), `mark_externally_closed` (synthesises a filled sell), `update`.
- **`broker/base.py`** — `Broker` protocol: `place_order`, `get_cash_balance`, `get_positions`, `get_order_fill`, `cancel_order`.
- **`broker/mock.py`** — Always-accept mock (state in `mock_orders.json`), used for dry-runs and the dashboard's mock panel. Cancelled ledger rows stop counting toward cash/positions/fills.
- **`broker/kis.py`** — KIS REST adapter: `KisSettings`/`KisRestClient` (token cache `.kis_token_paper.json`, throttling, hashkey) + `KisBroker` (domestic/overseas orders, balances, positions, fills, cancellation via the 정정취소 endpoints — verify TR ids against 모의투자 before first live use; paper & live via `KIS_MODE`).

**Interfaces**
- **`runner.py`** — CLI entry (`bot`). Subcommands: `analyze`, `scan`, `pending`, `approve`, `auto`, `backtest`. `--no-llm` bypasses the LLM on `analyze`/`auto`.
- **`web/`** — `http.server` dashboard on port 8501. `server.py` routes GET/POST to handler modules split by concern: `handlers_analysis`, `handlers_orders`, `handlers_portfolio`, `handlers_config`, `handlers_mock`. `autopilot_state.py` holds the background auto-pilot thread state. Static UI in `static/dashboard.html`.
- **`gui.py`** — Thin Tkinter desktop wrapper (`bot-gui`).

**Reporting & observability**
- **`notify.py`** — Best-effort Telegram alerts (buy submitted, exits, forced exits, circuit breaker, kill switch). Configured via `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` in `.env`; silent no-op when unset. De-duplicates identical alerts for 30 min so the auto-pilot loop can't spam. stdlib urllib only.
- **`report/markdown.py`** — Renders `AnalysisReport` (incl. LLM assessment section) as Markdown in EN or KO.
- **`backtest.py`** — `Backtester`/`BacktestResult` with win rate, total return, Sharpe, max drawdown. Guards against look-ahead twice: fundamentals filtered to those visible as-of the trade date (`visible_fundamentals`/`visible_catalysts`, reporting-lag heuristic), and `use_live_market_data=False` so today's regime/benchmark never colors historical signals. **`ladder_step()` is the single source of truth for simulated exits** (stop → target-1 scale-out → 2×ATR trail floored at breakeven → target-2/trail/time; worst-case intra-bar ordering, gap fills at the open), shared by `Backtester._walk_exits` and the portfolio engine; each trade blends its legs into one weighted record with composite outcomes like `target1+trail`. `split_exits=False` restores the old single-stage model for A/B. Remaining divergences: no LLM replay, and `max_hold_days` time exit has no live counterpart.
- **`portfolio_backtest.py`** — `PortfolioBacktester.run(list[TickerSeries])` replays a whole universe day by day under the live constraints: shared cash, `max_positions` slots, risk-%-of-equity sizing capped by cash and `max_position_pct`, share-level scale-outs (larger half, matching live), signals on each close → fills at the next open, forced flatten at the end. Equity is marked to market daily, so max-drawdown/Sharpe come from the portfolio path, not per-trade returns. **One run = one market/currency** (mixing KRW+USD cash would be meaningless — the CLI groups the watchlist per market). CLI: `backtest --universe watchlist.yaml [--cash N]`.
- **`audit_log.py`** — Append-only JSONL logs under `logs/` (`log_llm`, `log_query`, `log_queue`, `log_trade`, `log_cash_snapshot`). All call sites wrap it in `try/except pass` — logging never breaks trading.
- **`daily_report.py`** — `build_daily_summary()` aggregates the audit JSONL into a per-day rollup for the dashboard.

### Scoring & signal logic (`strategy/analyzer.py`)

- **Score: max 30** = fundamentals 10 + technical trend 10 + momentum/flow 10 (each sub-score clamped to [0,10]).
- **Buy** requires `score ≥ min_score` (default 24) **and** `rr ≥ min_rr` (default 1.5) **and** price above SMA200.
- **Strong Buy**: `strong_buy_score=27` and `strong_buy_rr=3.0`.
- **LLM adjustment**: `NewsAssessment.score_adjustment` (±3) moves the *fundamentals* sub-score; `earnings_caution` can be forced on.
- **Relative strength (+1 momentum)**: explicit `MarketContext` returns take precedence; otherwise, live mode derives it from the candle series vs the regime cache's index `return_3m`.
- **Relaxation rules**: high-conviction score (27+/29+) relaxes `min_rr` to 1.2/1.0; high R/R (≥3.0) relaxes `min_score` to 22 — but only if `technical_trend ≥ 6` (guard against broken-trend names with far stops).
- **Three vetoes → `Hold Off`** (evaluated in `_final_signal`): ① LLM severe negative news (`severity=high` & `sentiment=negative`); ② bearish market regime (index < SMA200); ③ price below SMA200. When SMA200 history is insufficient, a sub-SMA close returns `Wait` (advisory) instead.
- **Breakout gate (opt-in, `require_breakout: true` in config)**: after the vetoes, demand a *fresh* pivot break — close above the 60-day pivot (excluding the last 5 bars), broken within 5 bars, breakout-day volume ≥1.4× its 50-day average, and ≤5% past the pivot — else `Wait`. Default **off**: the pre-pivot squeeze entry (scenario A1) is the bot's native style, and A/B on the SK Hynix fixture showed the strict gate skips parabolic runs entirely (54/68 Buy days were >5% extended → 0 trades vs +81%). Thresholds in `StrategyParams.breakout_*`.
- **`use_live_market_data` flag on `analyze()`**: gates every lookup of *today's* market state (regime veto ②, live RS benchmark). The Backtester passes `False` — mixing today's index into historical signals is look-ahead bias. Any new "fetch current market state" feature must respect this flag.
- Every analysis is best-effort logged via `audit_log.log_query`.

### Data files

Local data at `data/{prices,fundamentals,news,contexts}/{MARKET}_{TICKER}.{csv,json}` (see README for formats). `FixtureDataProvider` needs ≥220 price rows for the 200-day filter. `--demo` bypasses all file I/O via `SyntheticDataProvider`.

State files at repo root: `pending_orders.json` (queue), `mock_orders.json` (mock broker), `news_cache.json` (LLM cache), `.kis_token_*.json` (KIS token), `logs/*.jsonl` (audit).

### Configuration

- `config.yaml` / `config.example.yaml` — `broker`, `min_score`, `min_rr`, `risk_per_trade_pct`, `max_positions`, `default_market`, `data_dir`, `approval_queue`, plus risk guards: `max_position_pct` (default 20, 0=off), `daily_loss_limit_pct` (default 3, 0=off), `stale_order_minutes` (default 60, 0=off), the entry-style toggle `require_breakout` (default false), and `protective_stop` (default false — enabling it places real standing orders at the venue).
- Pre-trade **tradability gate**: `_tradability_block()` in the orchestrator asks the venue whether a buy candidate is actually buyable (listing status, 정리매매, KRX 거래정지, and the `BLOCKING_STOCK_WARNINGS` designations incl. VI halts). Unlike the regime/LLM gates it **fails closed per ticker** — an unverifiable symbol waits for the next sweep, because buying into a delisting is not something the exit ladder can recover from.
- Toss orders at/above ₩100M are refused locally unless `TOSS_ALLOW_HIGH_VALUE_ORDERS=true`, so the venue's fat-finger threshold surfaces as a clear message instead of a permanent-looking rejection.
- `.env` — KIS creds (`KIS_APP_KEY`, `KIS_APP_SECRET`, `KIS_ACCOUNT_NO`, `KIS_MODE=paper`), optional `OPENAI_API_KEY` / `OPENAI_MODEL`, optional `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` for alerts. Also honors `NEWS_CACHE_*`, `BOT_BROKER`, `BOT_APPROVAL_QUEUE`, `BOT_KILL_SWITCH`.
- `watchlist.yaml` / `watchlist.example.yaml` — universe for `scan` and `auto`.
- Emergency stop: `touch KILL_SWITCH` (optionally write a reason on line 1) halts all new buys on the next iteration while exit management keeps protecting held positions; `rm KILL_SWITCH` resumes.

### Key invariants

- **Graceful degradation everywhere**: LLM, news scraping, market regime, and audit logging are all non-fatal. Core KIS-REST + scoring works with `--no-llm` or when optional deps/keys are absent.
- **Single source of truth**: `run_auto_iteration()` is shared by CLI and web — change auto-trading behavior there, not in duplicate loops.
- **Immutability**: all domain objects are frozen dataclasses; mutate via `dataclasses.replace`.
- Runtime deps: `openai`, `requests`, `beautifulsoup4`, `yfinance`, `websocket-client` (Python 3.11–3.13). `pythonpath = ["src"]` set in `pyproject.toml`; tests use `unittest`/`pytest`. On this machine the working interpreter is **miniforge `python3.12`** — the python.org 3.13 on PATH has no CA certs (SSL fails) and no deps installed.
- **Auto-trading safety layers**: mock-by-default broker · kill switch · daily-loss circuit breaker (per market) · `max_positions` cap · `max_position_pct` per-name cap (both sizing paths) · per-ticker `--cooldown-hours` (default 24h) · market-hours gate · regime gate · cash pre-flight before every buy · stale-order auto-cancel · live-quote exit checks (daily-close fallback) · queue↔broker reconciliation for manual sells · per-ticker failures isolated · Telegram alerts on orders/exits/breakers (no-op unless configured).
</content>
</invoke>
