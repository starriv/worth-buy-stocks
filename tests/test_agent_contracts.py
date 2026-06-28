#!/usr/bin/env python3
"""Tests for multi-agent artifact contracts."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import agent_contracts as C  # noqa: E402


class TestNewsContract(unittest.TestCase):
    def test_direct_news_context_is_backward_compatible(self):
        payload = {
            "AAPL": {
                "sources": [{"id": "s1", "url": "https://example.com/a"}],
                "red_flags": [{"type": "dilution", "severity": "medium", "note": "Offering", "source_id": "s1"}],
                "data_trust": "ok",
            }
        }
        result = C.validate_payload(C.KIND_NEWS, payload)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["symbols_count"], 1)

    def test_news_unavailable_is_valid(self):
        payload = {
            "contract_version": C.CONTRACT_VERSION,
            "kind": C.KIND_NEWS,
            "status": "unavailable",
            "reason": "search disabled",
        }
        result = C.validate_payload(C.KIND_NEWS, payload)
        self.assertEqual(result["symbols_count"], 0)

    def test_bad_news_severity_rejected(self):
        payload = {"AAPL": {"red_flags": [{"type": "rumor", "severity": "bullish", "note": "target"}]}}
        with self.assertRaises(C.ContractError):
            C.validate_payload(C.KIND_NEWS, payload)


class TestBarsContract(unittest.TestCase):
    def test_bars_require_ohlcv_fields(self):
        payload = {
            "contract_version": C.CONTRACT_VERSION,
            "kind": C.KIND_BARS,
            "status": "ok",
            "bars": {
                "AAPL": [{"t": "2026-06-26T04:00:00Z", "o": 1, "h": 2, "l": 1, "c": 2, "v": 100}],
                "SPY": [],
                "QQQ": [],
            },
        }
        result = C.validate_payload(C.KIND_BARS, payload)
        self.assertEqual(result["bars_count"], 1)
        self.assertEqual(result["symbols_count"], 3)

    def test_missing_bar_close_rejected(self):
        payload = {"bars": {"AAPL": [{"t": "2026-06-26T04:00:00Z", "o": 1, "h": 2, "l": 1, "v": 100}]}}
        with self.assertRaises(C.ContractError):
            C.validate_payload(C.KIND_BARS, payload)


class TestOtherContracts(unittest.TestCase):
    def test_account_context_validates_positions(self):
        payload = {
            "contract_version": C.CONTRACT_VERSION,
            "kind": C.KIND_ACCOUNT,
            "status": "ok",
            "account": {"equity": 100000},
            "positions": [{"symbol": "AAPL", "qty": 5}],
        }
        self.assertEqual(C.validate_payload(C.KIND_ACCOUNT, payload)["positions_count"], 1)

    def test_finnhub_rate_limited_is_valid_unavailable_state(self):
        payload = {
            "contract_version": C.CONTRACT_VERSION,
            "kind": C.KIND_FINNHUB,
            "status": "rate_limited",
            "reason": "429",
        }
        self.assertEqual(C.validate_payload(C.KIND_FINNHUB, payload)["symbols_count"], 0)

    def test_result_requires_score_for_non_error_symbol(self):
        payload = {"symbols": {"AAPL": {"last_close": 100}}}
        with self.assertRaises(C.ContractError):
            C.validate_payload(C.KIND_RESULT, payload)

    def test_valid_result_summary(self):
        payload = {
            "symbols": {
                "AAPL": {
                    "score": {
                        "verdict": "观察",
                        "composite": 68.5,
                        "blocking_reasons": [],
                        "trade_plan": {},
                        "account_overlay": {},
                        "llm_overlay": None,
                    }
                }
            }
        }
        result = C.validate_payload(C.KIND_RESULT, payload)
        self.assertEqual(result["scored_symbols_count"], 1)


class TestValidateCli(unittest.TestCase):
    def test_cli_returns_json_status(self):
        payload = {"bars": {"AAPL": [{"t": "2026-06-26T04:00:00Z", "o": 1, "h": 2, "l": 1, "c": 2, "v": 100}]}}
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
            json.dump(payload, f)
            path = f.name
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        res = subprocess.run(
            [sys.executable, "scripts/validate_agent_contract.py", "--kind", C.KIND_BARS, path],
            cwd=os.path.join(os.path.dirname(__file__), ".."),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0)
        out = json.loads(res.stdout)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["kind"], C.KIND_BARS)


if __name__ == "__main__":
    unittest.main()
