#!/usr/bin/env python3
"""portfolio.py normalization tests. No Alpaca CLI/network calls."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import portfolio as P  # noqa: E402


class TestPortfolioNormalize(unittest.TestCase):
    def test_normalizes_account_and_positions_without_identifiers(self):
        raw = {
            "account": {
                "id": "secret-id",
                "account_number": "secret-number",
                "equity": "100000.00",
                "cash": "25000.00",
                "long_market_value": "70000.00",
                "buying_power": "50000.00",
                "trading_blocked": False,
            },
            "positions": [
                {
                    "symbol": "aapl",
                    "qty": "10",
                    "avg_entry_price": "100.50",
                    "current_price": "110.25",
                    "market_value": "1102.50",
                    "unrealized_plpc": "0.097",
                    "change_today": "-0.01",
                }
            ],
        }
        ctx = P.normalize_account_context(raw)
        self.assertEqual(ctx["status"], "ok")
        self.assertEqual(ctx["account"]["cash_pct"], 25.0)
        self.assertEqual(ctx["account"]["long_exposure_pct"], 70.0)
        self.assertNotIn("id", ctx["account"])
        self.assertEqual(ctx["positions"]["AAPL"]["unrealized_pl_pct"], 9.7)
        self.assertEqual(ctx["positions"]["AAPL"]["change_today_pct"], -1.0)

    def test_context_for_symbol_returns_position_or_none(self):
        ctx = P.normalize_account_context({
            "account": {"equity": "100000"},
            "positions": {"TSLA": {"qty": "2", "market_value": "500"}},
        })
        self.assertEqual(P.context_for_symbol(ctx, "tsla")["position"]["symbol"], "TSLA")
        self.assertIsNone(P.context_for_symbol(ctx, "AAPL")["position"])

    def test_unavailable_context_passthrough(self):
        ctx = P.normalize_account_context({"status": "unavailable", "reason": "no auth"})
        self.assertEqual(ctx["status"], "unavailable")
        self.assertIn("no auth", P.context_for_symbol(ctx, "AAPL")["reason"])


if __name__ == "__main__":
    unittest.main()
