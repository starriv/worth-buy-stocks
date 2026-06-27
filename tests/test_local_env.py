#!/usr/bin/env python3
"""local_env.py tests. Uses temporary files only; never reads real .env values."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import local_env as E  # noqa: E402


class TestLocalEnv(unittest.TestCase):
    def _env_file(self, text):
        f = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        f.write(text)
        f.close()
        self.addCleanup(lambda: os.path.exists(f.name) and os.unlink(f.name))
        return f.name

    def test_parse_env_file_handles_export_quotes_and_comments(self):
        path = self._env_file(
            """
            # comment
            export FINNHUB_API_KEY="file-token"
            TELEGRAM_CHAT_ID='12345'
            TELEGRAM_BOT_TOKEN=bot-token # inline comment
            INVALID_LINE
            """
        )
        parsed = E.parse_env_file(path)
        self.assertEqual(parsed["FINNHUB_API_KEY"], "file-token")
        self.assertEqual(parsed["TELEGRAM_CHAT_ID"], "12345")
        self.assertEqual(parsed["TELEGRAM_BOT_TOKEN"], "bot-token")
        self.assertNotIn("INVALID_LINE", parsed)

    def test_get_env_prefers_file_over_shell(self):
        path = self._env_file("FINNHUB_API_KEY=file-token\n")
        with patch.dict(os.environ, {"FINNHUB_API_KEY": "shell-token"}, clear=True):
            self.assertEqual(E.get_env("FINNHUB_API_KEY", path=path), "file-token")

    def test_get_env_falls_back_to_shell(self):
        path = self._env_file("")
        with patch.dict(os.environ, {"FINNHUB_API_KEY": "shell-token"}, clear=True):
            self.assertEqual(E.get_env("FINNHUB_API_KEY", path=path), "shell-token")

    def test_env_path_override(self):
        path = self._env_file("FINNHUB_API_KEY=file-token\n")
        with patch.dict(os.environ, {E.ENV_PATH_OVERRIDE: path}, clear=True):
            self.assertEqual(E.get_env("FINNHUB_API_KEY"), "file-token")


if __name__ == "__main__":
    unittest.main()
