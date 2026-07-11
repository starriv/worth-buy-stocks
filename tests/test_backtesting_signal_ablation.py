#!/usr/bin/env python3
"""Semantics tests for the frozen signal-layer ablation transforms."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from backtesting.managed_ablation import (  # noqa: E402
    _confirm_relax_2of3,
    _graduated_overheat,
    _rs_floor_50,
)


def _overheat_record():
    return {
        "status": "ok",
        "base_verdict": "是",
        "verdict": "观察",
        "composite": 85.0,
        "entry_timing": {"classification": "overextended"},
        "suggested_position_pct": 40.0,
    }


class GraduatedOverheatTests(unittest.TestCase):
    def test_overheated_yes_enters_at_half_size(self):
        record = _overheat_record()
        _graduated_overheat(record)
        self.assertEqual(record["verdict"], "是")
        self.assertEqual(record["suggested_position_pct"], 20.0)

    def test_non_overheat_downgrade_is_untouched(self):
        record = _overheat_record()
        record["entry_timing"] = {"classification": "pullback_no_trigger"}
        _graduated_overheat(record)
        self.assertEqual(record["verdict"], "观察")

    def test_base_observe_is_untouched(self):
        record = _overheat_record()
        record["base_verdict"] = "观察"
        _graduated_overheat(record)
        self.assertEqual(record["verdict"], "观察")


class ConfirmRelaxTests(unittest.TestCase):
    def _record(self, technical=60, volume=50, trend_quality=10):
        return {
            "status": "ok",
            "base_verdict": "观察",
            "verdict": "观察",
            "composite": 80.0,
            "risk_gates": [],
            "entry_timing": {"classification": "trend_continuation"},
            "confirmation": {
                "technical_pct": technical,
                "volume_pct": volume,
                "trend_quality_pct": trend_quality,
                "minimums_pct": {"technical": 50, "volume": 40, "trend_quality": 40},
            },
        }

    def test_two_of_three_promotes_in_clear_gates(self):
        record = self._record()
        _confirm_relax_2of3(record)
        self.assertEqual(record["verdict"], "是")

    def test_one_of_three_stays_observe(self):
        record = self._record(volume=10, trend_quality=10)
        _confirm_relax_2of3(record)
        self.assertEqual(record["verdict"], "观察")

    def test_overextended_day_is_not_promoted(self):
        record = self._record()
        record["entry_timing"] = {"classification": "overextended"}
        _confirm_relax_2of3(record)
        self.assertEqual(record["verdict"], "观察")

    def test_gated_day_is_not_promoted(self):
        record = self._record()
        record["risk_gates"] = ["价格跌破 MA60"]
        _confirm_relax_2of3(record)
        self.assertEqual(record["verdict"], "观察")


class RsFloorTests(unittest.TestCase):
    def test_weak_relative_strength_yes_is_demoted(self):
        record = {
            "status": "ok",
            "verdict": "是",
            "factor_breakdown": {"rel_strength": {"score_pct": 30}},
        }
        _rs_floor_50(record)
        self.assertEqual(record["verdict"], "观察")

    def test_strong_relative_strength_yes_survives(self):
        record = {
            "status": "ok",
            "verdict": "是",
            "factor_breakdown": {"rel_strength": {"score_pct": 72}},
        }
        _rs_floor_50(record)
        self.assertEqual(record["verdict"], "是")

    def test_missing_subscore_is_untouched(self):
        record = {"status": "ok", "verdict": "是", "factor_breakdown": {}}
        _rs_floor_50(record)
        self.assertEqual(record["verdict"], "是")


if __name__ == "__main__":
    unittest.main()
