# AI Trading Bot (CANSLIM + VCP)

## Project Overview
This project is a semi-automated Python-based quantitative trading bot targeting Korean (KR) and US equities via the KIS (Korea Investment & Securities) API. 

It evaluates stocks based on the CANSLIM methodology and Volatility Contraction Pattern (VCP), augmented with an LLM (OpenAI) to analyze recent news for sentiment and severity. The bot supports a strict workflow: **analyze → queue → approve → broker**. It calculates risk (entry zones, stop-loss, targets, R/R ratio) and provides both a local Tkinter GUI (`alpha_bot.gui`) and a web dashboard (`alpha_bot.web`).

Key features include:
- Technical analysis (SMA50, SMA200, RSI, Bollinger Bands, Volume).
- LLM-powered news assessment affecting fundamental scores.
- Mock and live (KIS) broker implementations.
- Auto-pilot execution with position limits and cooldowns.

## Building and Running

**Installation:**
The project uses `setuptools`. You can install it in editable mode as a CLI tool:
```bash
python3 -m pip install -e .
```

**Commands:**
- **Run Tests:**
  ```bash
  python3 -m unittest discover -s tests
  # or
  python3 -m pytest tests/
  ```
- **CLI Analysis (Demo Mode):**
  ```bash
  bot analyze --ticker NVDA --market US --demo
  # Without LLM:
  bot analyze --ticker NVDA --market US --demo --no-llm
  ```
- **Launch GUI:**
  ```bash
  bot-gui
  # or 
  python3 -m alpha_bot.gui
  ```
- **Launch Web Dashboard:**
  ```bash
  python3 -m alpha_bot.web
  ```
- **Auto-pilot (Demo):**
  ```bash
  python3 -m alpha_bot.runner auto --universe watchlist.example.yaml --demo --interval 300 --broker mock
  ```

## Development Conventions

- **Type Safety & Dataclasses:** The core data models (e.g., `Candle`, `TradePlan`, `AnalysisReport`) are strictly defined as frozen dataclasses in `src/alpha_bot/models.py`. Ensure type hints are used throughout new additions.
- **Safety & Execution:** Live orders must never be executed implicitly. The system requires an explicit `approve --broker kis` step. Always default to the `mock` broker during development and testing. 
- **Configuration:** Configuration is managed via `config.yaml` and `.env` (for secrets like `KIS_APP_KEY`, `OPENAI_API_KEY`). Ensure `.env` is never committed.
- **Testing:** The project utilizes `unittest` (and is compatible with `pytest`). All new risk/strategy calculations should have accompanying test cases covering edge conditions (like `tests/test_risk.py`).
- **Dependencies:** The application is designed to function even if the LLM dependencies (`openai`, `requests`, `bs4`, `yfinance`) are not present or fail, falling back to neutral assessments safely. Keep core execution decoupled from external API reliability.
