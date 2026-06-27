#!/usr/bin/env python3
"""notify_telegram.py 的离线单元测试：纯函数 + 未配置路径，不触网、不发消息。

运行：python3 -m unittest discover -s tests
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "scripts")
sys.path.insert(0, SCRIPTS)

import notify_telegram as N  # noqa: E402


class TestTruncate(unittest.TestCase):
    def test_short_unchanged(self):
        self.assertEqual(N._truncate("hello", 4096), "hello")

    def test_long_truncated_within_limit(self):
        out = N._truncate("x" * 5000, 4096)
        self.assertLessEqual(len(out), 4096)
        self.assertTrue(out.endswith(N.TRUNCATED_MARK))


class TestNotConfigured(unittest.TestCase):
    def test_missing_creds_exit_2(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
        env["WORTH_BUY_STOCKS_ENV_FILE"] = os.path.join(
            tempfile.gettempdir(), "wbs_missing_telegram_test.env"
        )
        res = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "notify_telegram.py"), "--text", "hi"],
            capture_output=True, text=True, env=env)
        self.assertEqual(res.returncode, 2)
        out = json.loads(res.stdout)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "telegram_not_configured")
        self.assertIn("TELEGRAM_BOT_TOKEN", out["missing"])
        self.assertIn("TELEGRAM_CHAT_ID", out["missing"])


if __name__ == "__main__":
    unittest.main()
