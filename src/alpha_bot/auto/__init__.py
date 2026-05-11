"""Unified analyze + auto-trade pipeline.

This package is the single source of truth for the "fetch news → LLM assess
→ score → enqueue → approve" flow used by both the CLI auto-pilot and the
web dashboard.

All public names are re-exported here so existing ``from alpha_bot.auto import ...``
statements continue to work unchanged.
"""

from alpha_bot.auto.analysis import (
    analyze_ticker,
    make_broker,
    make_provider,
)
from alpha_bot.auto.orchestrator import (
    AutoTradeOptions,
    run_auto_iteration,
)
from alpha_bot.auto.position_manager import (
    count_open_positions,
    find_held_buy,
    manage_open_positions,
    reconcile_queue_with_broker,
    should_force_exit,
    trigger_forced_exit,
)
from alpha_bot.auto.sizing import compute_position_size

__all__ = [
    # analysis
    "analyze_ticker",
    "make_broker",
    "make_provider",
    # orchestrator
    "AutoTradeOptions",
    "run_auto_iteration",
    # position_manager
    "count_open_positions",
    "find_held_buy",
    "manage_open_positions",
    "reconcile_queue_with_broker",
    "should_force_exit",
    "trigger_forced_exit",
    # sizing
    "compute_position_size",
]
