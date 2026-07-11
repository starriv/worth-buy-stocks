#!/usr/bin/env python3
"""Frozen holdout aggregation tests for the stateful lifecycle experiment."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from backtesting.lifecycle_validation import (  # noqa: E402
    VALIDATION_SYMBOLS,
    summarize_rows,
    validation_semantic_hash,
)


def _row(symbol: str, *, delta: float = 10.0) -> dict:
    return {
        "symbol": symbol,
        "status": "ok",
        "return_delta_pct": delta,
        "sharpe_delta": 0.2,
        "capture_delta_pct": 20.0,
        "mdd_delta_pct": -2.0,
        "turnover_ratio": 1.5,
    }


class LifecycleValidationTests(unittest.TestCase):
    def test_frozen_basket_excludes_qqq_and_has_minimum_scope(self):
        self.assertNotIn("QQQ", VALIDATION_SYMBOLS)
        self.assertGreaterEqual(len(VALIDATION_SYMBOLS), 12)
        self.assertIn("SPY", VALIDATION_SYMBOLS)

    def test_all_frozen_conditions_must_pass_for_promotion(self):
        rows = [_row(symbol) for symbol in VALIDATION_SYMBOLS]
        summary = summarize_rows(rows)
        self.assertEqual(summary["decision"], "promote")
        self.assertTrue(all(item["pass"] for item in summary["conditions"].values()))

        rows[0] = _row(VALIDATION_SYMBOLS[0], delta=-20.0)
        for row in rows[1:8]:
            row["return_delta_pct"] = -1.0
        rejected = summarize_rows(rows)
        self.assertEqual(rejected["decision"], "reject")
        self.assertFalse(
            rejected["conditions"]["return_improvement_rate_pct"]["pass"]
        )

    def test_insufficient_symbols_and_risk_fail_closed(self):
        rows = [_row(symbol) for symbol in VALIDATION_SYMBOLS[:11]]
        for row in rows[:4]:
            row["mdd_delta_pct"] = -12.0
        summary = summarize_rows(rows)
        self.assertEqual(summary["decision"], "reject")
        self.assertFalse(summary["conditions"]["valid_symbols"]["pass"])
        self.assertFalse(
            summary["conditions"]["severe_mdd_degradation_rate_pct"]["pass"]
        )

    def test_semantic_hash_ignores_generated_time(self):
        report = {"generated_at": "one", "rows": [], "summary": {"decision": "reject"}}
        first = validation_semantic_hash(report)
        report["generated_at"] = "two"
        self.assertEqual(first, validation_semantic_hash(report))


if __name__ == "__main__":
    unittest.main()
