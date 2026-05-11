"""Allow ``python3 -m alpha_bot.web`` to work as before."""

from alpha_bot.web.server import main

raise SystemExit(main())
