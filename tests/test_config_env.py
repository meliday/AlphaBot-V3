from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alpha_bot.config import _DOTENV_MANAGED_VALUES, load_dotenv


class DotenvPrecedenceTests(unittest.TestCase):
    def test_explicit_process_environment_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("TOSS_CLIENT_ID=file-value\n", encoding="utf-8")
            with patch.dict(os.environ, {"TOSS_CLIENT_ID": "service-value"}, clear=True):
                _DOTENV_MANAGED_VALUES.clear()
                load_dotenv(path)
                self.assertEqual(os.environ["TOSS_CLIENT_ID"], "service-value")

    def test_loader_managed_value_refreshes_after_dashboard_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            with patch.dict(os.environ, {}, clear=True):
                _DOTENV_MANAGED_VALUES.clear()
                path.write_text("TOSS_CLIENT_ID=first\n", encoding="utf-8")
                load_dotenv(path)
                self.assertEqual(os.environ["TOSS_CLIENT_ID"], "first")
                path.write_text("TOSS_CLIENT_ID=second\n", encoding="utf-8")
                load_dotenv(path)
                self.assertEqual(os.environ["TOSS_CLIENT_ID"], "second")


if __name__ == "__main__":
    unittest.main()
