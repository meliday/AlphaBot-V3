"""Web dashboard package for Alpha Strategy Bot.

Re-exports ``DashboardHandler`` and ``main`` so that existing import paths
(``from alpha_bot.web import DashboardHandler``) continue to work.
"""

from alpha_bot.web.server import DashboardHandler, main

__all__ = ["DashboardHandler", "main"]
