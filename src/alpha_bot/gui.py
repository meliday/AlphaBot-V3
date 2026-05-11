"""AlphaBot desktop launcher.

Spawns the Toss-styled web dashboard in a local HTTP server and opens it in
the default browser. The dashboard itself lives in ``alpha_bot/static/`` and is
served by ``alpha_bot.web``.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpha_bot.web import DashboardHandler

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AlphaBot Toss-styled dashboard launcher")
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open the browser")
    parser.add_argument("--check", action="store_true", help="Validate launch path without starting the server")
    args = parser.parse_args(argv)

    if args.check:
        return 0

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"\n  🚀 AlphaBot Dashboard")
    print(f"  ➜ Local:   {url}")
    print(f"  ➜ Browser: {'auto-open' if not args.no_browser else 'disabled'}\n")

    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  🛑 Shutting down...")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
