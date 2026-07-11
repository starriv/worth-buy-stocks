#!/usr/bin/env python3
"""Frozen stratified holdout tests for the index-ETF lifecycle hypothesis."""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from backtesting.index_lifecycle_validation import (  # noqa: E402
    BROAD_ETF_SYMBOLS,
    SECTOR_ETF_SYMBOLS,
    VALIDATION_SYMBOLS,
    run_validation,
    summarize_index_rows,
    validation_semantic_hash,
)
from backtesting.data import BacktestDataError  # noqa: E402


def _row(symbol: str, *, delta: float = 10.0) -> dict:
    return {
        "symbol": symbol,
        "group": "broad" if symbol in BROAD_ETF_SYMBOLS else "sector",
        "status": "ok",
        "return_delta_pct": delta,
        "sharpe_delta": 0.2,
        "capture_delta_pct": 20.0,
        "mdd_delta_pct": -2.0,
        "turnover_ratio": 1.5,
    }


class IndexLifecycleValidationTests(unittest.TestCase):
    def test_frozen_groups_are_disjoint_and_exclude_development_indices(self):
        self.assertEqual(len(VALIDATION_SYMBOLS), 15)
        self.assertFalse(set(BROAD_ETF_SYMBOLS) & set(SECTOR_ETF_SYMBOLS))
        self.assertNotIn("QQQ", VALIDATION_SYMBOLS)
        self.assertNotIn("SPY", VALIDATION_SYMBOLS)

    def test_all_overall_and_stratum_conditions_are_required(self):
        rows = [_row(symbol) for symbol in VALIDATION_SYMBOLS]
        summary = summarize_index_rows(rows)
        self.assertEqual(summary["decision"], "promote")
        self.assertTrue(all(item["pass"] for item in summary["conditions"].values()))

        for symbol in BROAD_ETF_SYMBOLS[:3]:
            row = next(item for item in rows if item["symbol"] == symbol)
            row["return_delta_pct"] = -20.0
            row["sharpe_delta"] = -0.4
        rejected = summarize_index_rows(rows)
        self.assertEqual(rejected["decision"], "reject")
        self.assertFalse(
            rejected["conditions"]["broad_return_improvement_rate_pct"]["pass"]
        )
        self.assertFalse(
            rejected["conditions"]["broad_median_sharpe_delta"]["pass"]
        )

    def test_insufficient_stratum_and_risk_fail_closed(self):
        rows = [_row(symbol) for symbol in BROAD_ETF_SYMBOLS[:2]]
        rows.extend(_row(symbol) for symbol in SECTOR_ETF_SYMBOLS[:10])
        for row in rows[:3]:
            row["mdd_delta_pct"] = -12.0
        summary = summarize_index_rows(rows)
        self.assertEqual(summary["decision"], "reject")
        self.assertFalse(summary["conditions"]["broad_valid_symbols"]["pass"])
        self.assertFalse(
            summary["conditions"]["severe_mdd_degradation_rate_pct"]["pass"]
        )

    def test_excluded_rows_are_disclosed_but_not_counted(self):
        rows = [_row(symbol) for symbol in VALIDATION_SYMBOLS]
        rows[-1] = {
            "symbol": VALIDATION_SYMBOLS[-1],
            "group": "sector",
            "status": "excluded",
            "reason": "coverage",
        }
        summary = summarize_index_rows(rows)
        self.assertEqual(summary["valid_symbols"], 14)
        self.assertEqual(summary["excluded_symbols"], 1)

    def test_cache_failure_excludes_every_frozen_symbol_without_substitution(self):
        error = BacktestDataError("cache_coverage_incomplete", "coverage")
        with patch(
            "backtesting.index_lifecycle_validation.load_backtest_data",
            side_effect=error,
        ):
            report = run_validation(cache_db="/missing.sqlite3")
        self.assertEqual(len(report["rows"]), len(VALIDATION_SYMBOLS))
        self.assertTrue(all(row["status"] == "excluded" for row in report["rows"]))
        self.assertEqual(report["summary"]["decision"], "reject")
        self.assertEqual(
            [row["symbol"] for row in report["rows"]], list(VALIDATION_SYMBOLS)
        )

    def test_semantic_hash_ignores_generated_time(self):
        report = {
            "report_type": "index_etf_stateful_lifecycle_holdout",
            "generated_at": "one",
            "rows": [],
            "summary": {"decision": "reject"},
        }
        first = validation_semantic_hash(report)
        report["generated_at"] = "two"
        self.assertEqual(first, validation_semantic_hash(report))


if __name__ == "__main__":
    unittest.main()
