#!/usr/bin/env python3
"""Offline end-to-end checks for stdout payloads and structured stderr."""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _bars(count: int = 300, *, base: float = 50.0, slope: float = 0.2):
    rows = []
    day = dt.date(2024, 1, 2)
    for index in range(count):
        while day.isoweekday() > 5:
            day += dt.timedelta(days=1)
        close = base + slope * index
        rows.append({
            "t": f"{day.isoformat()}T04:00:00Z",
            "o": close - 0.2,
            "h": close + 0.8,
            "l": close - 0.8,
            "c": close,
            "v": 1_000_000 + index,
        })
        day += dt.timedelta(days=1)
    return rows


def _json_lines(raw: str) -> list[dict]:
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


class LoggingEntrypointTests(unittest.TestCase):
    def run_script(self, name: str, *args: str, input_text: str | None = None, env=None):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / name), *args],
            cwd=ROOT,
            input=input_text,
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def test_validator_keeps_error_json_on_stdout_and_log_json_on_stderr(self):
        result = self.run_script(
            "validate_agent_contract.py",
            "--log-format", "json",
            "--kind", "bars",
            "-",
            input_text="{",
        )
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error_code"], "json_invalid")
        events = _json_lines(result.stderr)
        self.assertEqual(events[0]["event"], "artifact_json_invalid")
        self.assertEqual(events[0]["logger"], "worth_buy_stocks.validate_agent_contract")

    def test_indicators_offline_warning_does_not_pollute_result_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload_path = Path(tmp) / "bars.json"
            snapshot_path = Path(tmp) / "bad-snapshot.json"
            bars = {
                "AAPL": _bars(base=50.0, slope=0.25),
                "SPY": _bars(base=400.0, slope=0.10),
                "QQQ": _bars(base=350.0, slope=0.12),
            }
            payload_path.write_text(json.dumps({"bars": bars}), encoding="utf-8")
            snapshot_path.write_text(
                json.dumps({"status": "ok", "symbols": []}), encoding="utf-8"
            )
            result = self.run_script(
                "indicators.py",
                "--log-format", "json",
                "--input", str(payload_path),
                "--symbols", "AAPL,SPY,QQQ",
                "--account-context", "off",
                "--finnhub-context", "off",
                "--snapshot-context-file", str(snapshot_path),
                "--ai-opinion", "off",
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("AAPL", payload["symbols"])
        events = _json_lines(result.stderr)
        self.assertEqual([event["event"] for event in events], ["overlay_contract_invalid"])

    def test_scanner_offline_stdout_remains_single_json_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload_path = Path(tmp) / "scan.json"
            payload = {
                "assets": [{"symbol": "STRG", "fractionable": True}],
                "snapshots": {
                    "status": "ok",
                    "symbols": {"STRG": {"daily_bar": {"c": 100.0, "v": 200_000}}},
                },
                "bars": {
                    "STRG": _bars(base=50.0, slope=0.30),
                    "SPY": _bars(base=400.0, slope=0.10),
                    "QQQ": _bars(base=350.0, slope=0.12),
                },
            }
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            result = self.run_script(
                "scanner.py",
                "--log-format", "json",
                "--input", str(payload_path),
                "--verify-news", "off",
                "--ai-opinion", "off",
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["offline"])
        _json_lines(result.stderr)

    def test_chart_keeps_render_on_stdout_and_artifact_event_on_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload_path = Path(tmp) / "bars.json"
            html_path = Path(tmp) / "chart.html"
            payload_path.write_text(
                json.dumps({"bars": {"AAPL": _bars(40)}}), encoding="utf-8"
            )
            result = self.run_script(
                "chart.py",
                "--log-format", "json",
                "--input", str(payload_path),
                "--symbol", "AAPL",
                "--no-color",
                "--html", str(html_path),
            )
            self.assertTrue(html_path.is_file())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("AAPL", result.stdout)
        event = _json_lines(result.stderr)[0]
        self.assertEqual(event["event"], "chart_html_written")
        self.assertNotIn("\x1b[", result.stderr)

    def test_notify_missing_credentials_is_json_on_both_channels(self):
        env = {
            key: value for key, value in os.environ.items()
            if key not in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
        }
        env["WORTH_BUY_STOCKS_ENV_FILE"] = str(
            Path(tempfile.gettempdir()) / "wbs_logging_missing.env"
        )
        result = self.run_script(
            "notify_telegram.py",
            "--log-format", "json",
            "--text", "offline fixture",
            env=env,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"], "telegram_not_configured")
        event = _json_lines(result.stderr)[0]
        self.assertEqual(event["event"], "telegram_configuration_missing")
        self.assertEqual(event["missing"], ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"])


if __name__ == "__main__":
    unittest.main()
