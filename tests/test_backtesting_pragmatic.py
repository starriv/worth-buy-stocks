#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from backtesting.pragmatic import (  # noqa: E402
    V1_CONFIG,
    confirmation_trigger,
    probe_trigger,
    simulate_pragmatic_scenario,
)
from backtesting.pragmatic_validation import summarize_rows  # noqa: E402


def _dates(count: int) -> list[str]:
    output = []
    day = dt.date(2026, 1, 5)
    while len(output) < count:
        if day.isoweekday() <= 5:
            output.append(day.isoformat())
        day += dt.timedelta(days=1)
    return output


def _record(date: str, *, probe: bool = False, confirm: bool = False) -> dict:
    signals = 3 if confirm else 2 if probe else 0
    return {
        "signal_date": date,
        "status": "ok",
        "core_data_complete": True,
        "verdict": "否",
        "risk_gates": ["价格跌破 MA60"] if probe else [],
        "blocking_reasons": ["价格跌破 MA60"] if probe else [],
        "suggested_position_pct": 40.0,
        "confirmation": {"volume_pct": 80},
        "entry_timing": {"classification": "pullback_reversal" if probe else "trend_continuation"},
        "entry_timing_detail": {
            "classification": "pullback_reversal" if probe else "trend_continuation",
            "ext_atr_above_ma20": -0.5,
            "pullback": {
                "depth_from_30d_high_pct": -8.0,
                "bars_since_recent_low": 1,
                "recent_low_10d": 95.0,
            },
            "bounce": {
                "bounce_day": probe or confirm,
                "up_day": probe or confirm,
                "signal_count": signals,
            },
        },
        "trade_plan": {
            "stop_loss_price": 94.0,
            "trailing_stop_pct": 8.0,
            "max_chase_price": 106.0,
            "break_even_trigger_r": 1.0,
            "break_even_buffer_pct": 0.25,
            "trailing_activation_r": 1.0,
        },
    }


def _fixture(*, chase_open: float = 101.0):
    dates = _dates(9)
    closes = [100, 102, 103, 105, 107, 108, 109, 110, 111]
    rows = []
    for index, (date, close) in enumerate(zip(dates, closes)):
        open_price = chase_open if index == 1 else close - 0.5
        rows.append({
            "t": f"{date}T05:00:00Z", "o": open_price,
            "h": close + 1, "l": close - 1, "c": close, "v": 1_000_000,
        })
    timeline = [_record(date) for date in dates]
    timeline[0] = _record(dates[0], probe=True)
    timeline[2] = _record(dates[2], confirm=True)
    return dates, {"AAPL": rows}, {"symbol": "AAPL", "calendar": dates, "timeline": timeline}


class PragmaticLifecycleTests(unittest.TestCase):
    def test_probe_and_confirmation_are_distinct_next_open_transitions(self):
        dates, bars, replay = _fixture()
        self.assertTrue(probe_trigger(replay["timeline"][0]))
        self.assertTrue(confirmation_trigger(replay["timeline"][2]))

        result = simulate_pragmatic_scenario(replay, bars, cost_bps=10)

        trade = result["trades"][0]
        self.assertEqual(trade["signal_date"], dates[0])
        self.assertEqual(trade["entry_date"], dates[1])
        self.assertEqual(trade["entry_allocation_pct"], 10.0)
        self.assertEqual(trade["position_adjustments"][0]["signal_date"], dates[2])
        self.assertEqual(trade["position_adjustments"][0]["execution_date"], dates[3])
        self.assertEqual(trade["position_adjustments"][0]["target_allocation_pct"], 30.0)
        self.assertEqual(result["metrics"]["chase_violations"], 0)
        reasons = [item["reason"] for item in result["state_transitions"]]
        self.assertIn("probe_executed", reasons)
        self.assertIn("follow_through_add_executed", reasons)

    def test_next_open_above_chase_limit_is_never_filled(self):
        _, bars, replay = _fixture(chase_open=107.0)
        result = simulate_pragmatic_scenario(replay, bars, cost_bps=10)
        self.assertEqual(result["trades"], [])
        self.assertTrue(any(
            item["reason"] == "open_above_max_chase"
            for item in result["skipped_signals"]
        ))
        self.assertEqual(result["metrics"]["chase_violations"], 0)

    def test_long_term_or_market_gates_cannot_be_bypassed(self):
        _, _, replay = _fixture()
        record = replay["timeline"][0]
        record["risk_gates"] = ["大盘 risk-off（SPY 跌破 200 日线）——该 regime 排名分 edge 反转，新开仓封顶为「观察」"]
        record["blocking_reasons"] = list(record["risk_gates"])
        self.assertFalse(probe_trigger(record))

    def test_v1_exits_an_unconfirmed_probe_after_the_frozen_window(self):
        dates, bars, replay = _fixture()
        replay["timeline"][2] = _record(dates[2])
        result = simulate_pragmatic_scenario(
            replay, bars, cost_bps=10, config=V1_CONFIG
        )
        trade = result["trades"][0]
        self.assertEqual(trade["entry_allocation_pct"], 5.0)
        self.assertEqual(trade["exit_date"], dates[7])
        self.assertEqual(trade["exit_reason"], "signal_probe_unconfirmed")

    def test_v1_does_not_add_an_unrelated_cap_to_standard_entries(self):
        dates, bars, replay = _fixture()
        replay["timeline"][0] = _record(dates[0])
        replay["timeline"][0].update({"verdict": "是", "suggested_position_pct": 68.0})
        result = simulate_pragmatic_scenario(
            replay, bars, cost_bps=10, config=V1_CONFIG
        )
        self.assertEqual(result["trades"][0]["entry_kind"], "standard")
        self.assertEqual(result["trades"][0]["entry_allocation_pct"], 68.0)


class PragmaticSummaryTests(unittest.TestCase):
    def test_all_frozen_gates_must_pass_for_promotion(self):
        rows = []
        for index in range(18):
            rows.append({
                "symbol": f"S{index}", "status": "ok",
                "return_delta_pct": 2.0,
                "sharpe_delta": 0.2,
                "mdd_delta_pct": -1.0,
                "positive_benchmark_capture_delta_pct": 10.0,
                "participation_delay_improvement_sessions": 3,
                "exposure_delta_pct": 12.0,
                "candidate_exposure_pct": 35.0,
                "baseline_turnover_sides": 4.0,
                "turnover_ratio": 2.0,
                "candidate_annualized_turnover_sides": 8.0,
                "candidate_chase_violations": 0,
                "state_audit_complete": True,
            })
        self.assertEqual(summarize_rows(rows)["decision"], "promote")
        rows[0]["candidate_chase_violations"] = 1
        self.assertEqual(summarize_rows(rows)["decision"], "reject")


if __name__ == "__main__":
    unittest.main()
