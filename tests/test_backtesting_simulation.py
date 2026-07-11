#!/usr/bin/env python3
"""Execution-state and accounting tests for single-symbol simulation."""
from __future__ import annotations

import datetime as dt
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from backtesting.simulation import (  # noqa: E402
    ManagedVariant,
    build_benchmarks,
    run_simulations,
    simulate_managed_scenario,
    simulate_scenario,
    simulate_stateful_managed_scenario,
    simulate_managed_variant,
)


def _fixture(count: int = 12):
    dates = []
    day = dt.date(2026, 1, 5)
    while len(dates) < count:
        if day.isoweekday() <= 5:
            dates.append(day.isoformat())
        day += dt.timedelta(days=1)
    bars = {}
    for symbol, base in (("AAPL", 100.0), ("SPY", 400.0), ("QQQ", 350.0)):
        bars[symbol] = [
            {
                "t": f"{date}T04:00:00Z",
                "o": base + index,
                "h": base + index + 1,
                "l": base + index - 1,
                "c": base + index + 0.5,
                "v": 1_000_000,
            }
            for index, date in enumerate(dates)
        ]
    timeline = []
    for index, date in enumerate(dates):
        yes = index in (0, 1, 5)
        timeline.append({
            "signal_date": date,
            "status": "ok",
            "core_data_complete": True,
            "raw_composite": 80.0 if yes else 50.0,
            "verdict": "是" if yes else "否",
        })
    replay = {"symbol": "AAPL", "calendar": dates, "timeline": timeline}
    return dates, bars, replay


class SimulationTests(unittest.TestCase):
    def test_next_open_and_holding_period_have_no_off_by_one(self):
        dates, bars, replay = _fixture()
        result = simulate_scenario(
            replay, bars, policy="disciplined_verdict", horizon=2, cost_bps=10
        )
        first = result["trades"][0]
        self.assertEqual(first["signal_date"], dates[0])
        self.assertEqual(first["entry_date"], dates[1])
        self.assertEqual(first["exit_date"], dates[3])
        self.assertNotEqual(first["signal_date"], first["entry_date"])
        self.assertEqual(first["holding_sessions"], 2)

    def test_cost_is_applied_on_entry_and_exit(self):
        dates, bars, replay = _fixture()
        result = simulate_scenario(
            replay, bars, policy="rank_only", horizon=2, cost_bps=10
        )
        trade = result["trades"][0]
        gross_factor = trade["exit_price"] / trade["entry_price"]
        expected = (gross_factor * (1 - 0.001) ** 2 - 1) * 100
        self.assertAlmostEqual(trade["net_return_pct"], expected, places=4)
        self.assertGreater(result["metrics"]["total_cost_equity"], 0)

    def test_repeated_signal_while_active_is_skipped(self):
        _, bars, replay = _fixture()
        result = simulate_scenario(
            replay, bars, policy="rank_only", horizon=3, cost_bps=0
        )
        self.assertTrue(any(
            row["reason"] == "position_or_order_active"
            for row in result["skipped_signals"]
        ))

    def test_tail_position_is_open_not_fabricated_exit(self):
        dates, bars, replay = _fixture(6)
        replay["timeline"] = [{
            "signal_date": dates[-2], "status": "ok", "core_data_complete": True,
            "raw_composite": 80.0, "verdict": "是",
        }]
        result = simulate_scenario(
            replay, bars, policy="disciplined_verdict", horizon=3, cost_bps=10
        )
        self.assertEqual(result["trades"][0]["status"], "open")
        self.assertIsNone(result["trades"][0]["exit_date"])
        self.assertEqual(result["metrics"]["trade_count"], 0)
        self.assertEqual(result["metrics"]["open_trade_count"], 1)

    def test_zero_trade_metrics_are_serializable(self):
        _, bars, replay = _fixture()
        for row in replay["timeline"]:
            row["raw_composite"] = 20
            row["verdict"] = "否"
        result = simulate_scenario(
            replay, bars, policy="disciplined_verdict", horizon=2, cost_bps=10
        )
        self.assertEqual(result["trades"], [])
        self.assertIsNone(result["metrics"]["win_rate_pct"])
        for value in result["metrics"].values():
            if isinstance(value, float):
                self.assertTrue(math.isfinite(value))

    def test_seven_scenarios_and_two_benchmarks(self):
        _, bars, replay = _fixture()
        scenarios, benchmarks = run_simulations(replay, bars, horizons=(2, 4), cost_bps=10)
        self.assertEqual(set(scenarios), {
            "rank_only_2", "rank_only_4",
            "disciplined_verdict_2", "disciplined_verdict_4",
            "rank_only_managed", "disciplined_verdict_managed",
            "disciplined_verdict_stateful",
        })
        self.assertEqual(set(benchmarks), {"AAPL", "SPY"})

    def test_managed_without_trade_plan_skips_not_crashes(self):
        _, bars, replay = _fixture()
        result = simulate_managed_scenario(
            replay, bars, policy="disciplined_verdict", cost_bps=10
        )
        self.assertEqual(result["trades"], [])
        self.assertTrue(any(
            row["reason"] == "plan_exit_unavailable"
            for row in result["skipped_signals"]
        ))

    def test_benchmark_curve_is_normalized(self):
        _, bars, replay = _fixture()
        benchmarks = build_benchmarks(replay, bars)
        self.assertEqual(benchmarks["AAPL"]["curve"][0]["equity"], 1.0)
        self.assertGreater(benchmarks["AAPL"]["metrics"]["total_return_pct"], 0)

    def test_terminal_symbol_and_spy_benchmarks_use_same_interval(self):
        dates, bars, replay = _fixture(6)
        bars["AAPL"] = bars["AAPL"][:4]
        result = simulate_scenario(
            replay, bars, policy="disciplined_verdict", horizon=3, cost_bps=0
        )
        benchmarks = build_benchmarks(replay, bars)
        self.assertEqual(result["equity_curve"][-1]["date"], dates[3])
        self.assertEqual(benchmarks["AAPL"]["curve"][0]["date"], dates[0])
        self.assertEqual(benchmarks["SPY"]["curve"][0]["date"], dates[0])
        self.assertEqual(benchmarks["AAPL"]["curve"][-1]["date"], dates[3])
        self.assertEqual(benchmarks["SPY"]["curve"][-1]["date"], dates[3])

    def test_bankrupt_terminal_closes_positions_at_zero_and_extends_comparables(self):
        dates, bars, replay = _fixture(6)
        bars["SIVB"] = bars.pop("AAPL")[:3]
        replay["symbol"] = "SIVB"
        replay["timeline"][0]["trade_plan"] = {
            "stop_loss_price": 50.0,
            "trailing_stop_pct": 20.0,
        }
        fixed = simulate_scenario(
            replay, bars, policy="disciplined_verdict", horizon=4, cost_bps=10
        )
        managed = simulate_managed_scenario(
            replay, bars, policy="disciplined_verdict", cost_bps=10
        )
        benchmarks = build_benchmarks(replay, bars)
        for scenario in (fixed, managed):
            trade = scenario["trades"][0]
            self.assertEqual(trade["status"], "closed")
            self.assertEqual(trade["exit_date"], dates[3])
            self.assertEqual(trade["exit_price"], 0.0)
            self.assertEqual(trade["exit_reason"], "bankrupt_terminal")
            self.assertEqual(trade["net_return_pct"], -100.0)
            self.assertEqual(scenario["equity_curve"][-1]["date"], dates[-1])
            self.assertEqual(scenario["equity_curve"][-1]["equity"], 0.0)
        self.assertEqual(benchmarks["SIVB"]["curve"][-1]["date"], dates[-1])
        self.assertEqual(benchmarks["SIVB"]["curve"][-1]["equity"], 0.0)
        self.assertEqual(benchmarks["SPY"]["curve"][-1]["date"], dates[-1])


def _managed_fixture(ohlc, *, stop, trail, signal_index=0):
    """Build a single-symbol replay whose day-``signal_index`` signal carries a plan."""
    dates = []
    day = dt.date(2026, 2, 2)
    while len(dates) < len(ohlc):
        if day.isoweekday() <= 5:
            dates.append(day.isoformat())
        day += dt.timedelta(days=1)
    bars = {"AAPL": [
        {"t": f"{date}T05:00:00Z", "o": o, "h": h, "l": l, "c": c, "v": 1_000_000}
        for date, (o, h, l, c) in zip(dates, ohlc)
    ]}
    timeline = []
    for index, date in enumerate(dates):
        record = {
            "signal_date": date, "status": "ok", "core_data_complete": True,
            "raw_composite": 40.0, "verdict": "否",
        }
        if index == signal_index:
            record.update({
                "raw_composite": 90.0, "verdict": "是",
                "trade_plan": {"stop_loss_price": stop, "trailing_stop_pct": trail},
            })
        timeline.append(record)
    return dates, bars, {"symbol": "AAPL", "calendar": dates, "timeline": timeline}


class ManagedExitTests(unittest.TestCase):
    def test_trailing_stop_ratchets_from_peak_close_and_exits_at_level(self):
        dates, bars, replay = _managed_fixture(
            [
                (100, 101, 99, 100),    # signal day
                (100, 105, 99, 104),    # entry at 100; peak close 104
                (106, 112, 105, 110),   # peak close 110 -> trail 99
                (105, 106, 98, 100),    # low 98 <= 99 -> exit at 99
                (101, 102, 100, 101),
            ],
            stop=94, trail=10,
        )
        result = simulate_managed_scenario(
            replay, bars, policy="disciplined_verdict", cost_bps=0
        )
        trade = result["trades"][0]
        self.assertEqual(trade["status"], "closed")
        self.assertEqual(trade["exit_date"], dates[3])
        self.assertEqual(trade["exit_price"], 100.25)
        self.assertEqual(trade["exit_reason"], "break_even_stop")
        self.assertTrue(trade["break_even_activated"])
        self.assertEqual(trade["holding_sessions"], 3)

    def test_same_day_close_cannot_raise_same_day_trail(self):
        # Day 2 closes high but also dips: the dip is checked against the
        # previous trail, not one derived from that day's own close.
        dates, bars, replay = _managed_fixture(
            [
                (100, 101, 99, 100),
                (100, 101, 99, 100),    # entry; peak 100 -> trail 90
                (100, 120, 91, 119),    # low 91 > max(stop 80, trail 90)? 91 > 90 ok; peak 119
                (108, 110, 107.5, 108), # trail now 107.1; low 107.5 > 107.1 survives
                (107, 108, 106, 106.5), # open 107 <= 107.1 -> gap exit at open
            ],
            stop=90, trail=10,
        )
        result = simulate_managed_scenario(
            replay, bars, policy="disciplined_verdict", cost_bps=0
        )
        trade = result["trades"][0]
        self.assertEqual(trade["exit_date"], dates[4])
        self.assertEqual(trade["exit_price"], 107.0)
        self.assertEqual(trade["exit_reason"], "gap_open_trailing_stop")

    def test_managed_uses_suggested_position_and_keeps_cash(self):
        dates, bars, replay = _managed_fixture(
            [
                (100, 101, 99, 100),
                (100, 101, 99, 100),
                (110, 111, 109, 110),
            ],
            stop=80, trail=20,
        )
        replay["timeline"][0]["suggested_position_pct"] = 10.0
        result = simulate_managed_scenario(
            replay, bars, policy="disciplined_verdict", cost_bps=0
        )
        trade = result["trades"][0]
        self.assertEqual(trade["entry_allocation_pct"], 10.0)
        self.assertEqual(trade["entry_notional_equity"], 0.1)
        self.assertAlmostEqual(result["equity_curve"][-1]["equity"], 1.01, places=6)

    def test_close_exit_signal_executes_next_open(self):
        dates, bars, replay = _managed_fixture(
            [
                (100, 101, 99, 100),
                (100, 102, 99, 101),
                (105, 107, 104, 106),
                (103, 104, 102, 103),
            ],
            stop=80, trail=20,
        )
        replay["timeline"][2]["position_exit"] = {
            "action": "exit_next_open",
            "classification": "overextended_reversal",
        }
        result = simulate_managed_scenario(
            replay, bars, policy="disciplined_verdict", cost_bps=0
        )
        trade = result["trades"][0]
        self.assertEqual(trade["exit_date"], dates[3])
        self.assertEqual(trade["exit_price"], 103.0)
        self.assertEqual(trade["exit_reason"], "signal_overextended_reversal")

    def test_gap_entry_open_does_not_seed_close_based_trailing_stop(self):
        dates, bars, replay = _managed_fixture(
            [
                (70, 71, 69, 70),
                (100, 101, 79, 80),   # entry gaps up, but highest close is only 80
                (86, 87, 85, 86),     # must survive: prior close-based trail is 72
                (87, 88, 86, 87),
            ],
            stop=50, trail=10,
        )
        result = simulate_managed_scenario(
            replay, bars, policy="disciplined_verdict", cost_bps=0
        )
        trade = result["trades"][0]
        self.assertEqual(trade["status"], "open")
        self.assertIsNone(trade["exit_date"])
        self.assertEqual(trade["mark_date"], dates[-1])

    def test_gap_open_below_initial_stop_exits_at_open(self):
        dates, bars, replay = _managed_fixture(
            [
                (100, 101, 99, 100),
                (100, 101, 98, 99),     # entry at 100
                (90, 92, 89, 91),       # gap open 90 < stop 95
            ],
            stop=95, trail=20,
        )
        result = simulate_managed_scenario(
            replay, bars, policy="disciplined_verdict", cost_bps=0
        )
        trade = result["trades"][0]
        self.assertEqual(trade["exit_date"], dates[2])
        self.assertEqual(trade["exit_price"], 90.0)
        self.assertEqual(trade["exit_reason"], "gap_open_stop_loss")

    def test_entry_day_intraday_stop_still_fires(self):
        dates, bars, replay = _managed_fixture(
            [
                (100, 101, 99, 100),
                (100, 101, 96, 98),     # entry at 100; low 96 <= stop 97
                (99, 100, 98, 99),
            ],
            stop=97, trail=20,
        )
        result = simulate_managed_scenario(
            replay, bars, policy="disciplined_verdict", cost_bps=0
        )
        trade = result["trades"][0]
        self.assertEqual(trade["exit_date"], dates[1])
        self.assertEqual(trade["exit_price"], 97.0)
        self.assertEqual(trade["exit_reason"], "stop_loss")
        self.assertEqual(trade["holding_sessions"], 1)

    def test_entry_abandoned_when_open_at_or_below_stop(self):
        _, bars, replay = _managed_fixture(
            [
                (100, 101, 99, 100),
                (95, 96, 94, 95),       # open 95 <= stop 95 -> abandon
                (96, 97, 95, 96),
            ],
            stop=95, trail=20,
        )
        result = simulate_managed_scenario(
            replay, bars, policy="disciplined_verdict", cost_bps=0
        )
        self.assertEqual(result["trades"], [])
        self.assertTrue(any(
            row["reason"] == "open_at_or_below_stop"
            for row in result["skipped_signals"]
        ))

    def test_managed_costs_match_fixed_horizon_accounting(self):
        dates, bars, replay = _managed_fixture(
            [
                (100, 101, 99, 100),
                (100, 105, 99, 104),
                (106, 112, 105, 110),
                (105, 106, 98, 100),
            ],
            stop=94, trail=10,
        )
        result = simulate_managed_scenario(
            replay, bars, policy="disciplined_verdict", cost_bps=10
        )
        trade = result["trades"][0]
        gross_factor = trade["exit_price"] / trade["entry_price"]
        expected = (gross_factor * (1 - 0.001) ** 2 - 1) * 100
        self.assertAlmostEqual(trade["net_return_pct"], expected, places=4)
        self.assertGreater(result["metrics"]["total_cost_equity"], 0)

    def test_tail_open_position_marked_not_fabricated(self):
        dates, bars, replay = _managed_fixture(
            [
                (100, 101, 99, 100),
                (100, 105, 99, 104),
                (106, 112, 105, 110),
            ],
            stop=94, trail=10,
        )
        result = simulate_managed_scenario(
            replay, bars, policy="disciplined_verdict", cost_bps=10
        )
        trade = result["trades"][0]
        self.assertEqual(trade["status"], "open")
        self.assertIsNone(trade["exit_date"])
        self.assertEqual(result["metrics"]["open_trade_count"], 1)


class ManagedVariantTests(unittest.TestCase):
    def test_noop_variant_matches_baseline_metrics(self):
        _, bars, replay = _managed_fixture(
            [
                (100, 101, 99, 100),
                (100, 105, 99, 104),
                (106, 112, 105, 110),
                (105, 106, 98, 100),
            ],
            stop=94, trail=10,
        )
        baseline = simulate_managed_scenario(
            replay, bars, policy="disciplined_verdict", cost_bps=10
        )
        variant = simulate_managed_variant(
            replay, bars, cost_bps=10, variant=ManagedVariant(name="noop")
        )
        self.assertEqual(variant["scenario"], "disciplined_verdict_managed_noop")
        self.assertEqual(variant["metrics"], baseline["metrics"])
        self.assertEqual(
            [trade["exit_reason"] for trade in variant["trades"]],
            [trade["exit_reason"] for trade in baseline["trades"]],
        )

    def test_ignored_exit_class_holds_through_overextended_signal(self):
        dates, bars, replay = _managed_fixture(
            [
                (100, 101, 99, 100),
                (100, 102, 99, 101),
                (105, 107, 104, 106),
                (103, 104, 102, 103),
            ],
            stop=80, trail=20,
        )
        replay["timeline"][2]["position_exit"] = {
            "action": "exit_next_open",
            "classification": "overextended_reversal",
        }
        result = simulate_managed_variant(
            replay, bars, cost_bps=0,
            variant=ManagedVariant(
                name="hold_through_heat",
                ignored_exit_classes=frozenset({"overextended_reversal"}),
            ),
        )
        trade = result["trades"][0]
        self.assertEqual(trade["status"], "open")
        self.assertIsNone(trade["exit_date"])
        self.assertEqual(trade["mark_date"], dates[-1])

    def test_ignored_exit_class_keeps_trend_break_exit_live(self):
        dates, bars, replay = _managed_fixture(
            [
                (100, 101, 99, 100),
                (100, 102, 99, 101),
                (105, 107, 104, 106),
                (103, 104, 102, 103),
            ],
            stop=80, trail=20,
        )
        replay["timeline"][2]["position_exit"] = {
            "action": "exit_next_open",
            "classification": "trend_break",
        }
        result = simulate_managed_variant(
            replay, bars, cost_bps=0,
            variant=ManagedVariant(
                name="hold_through_heat",
                ignored_exit_classes=frozenset({"overextended_reversal"}),
            ),
        )
        trade = result["trades"][0]
        self.assertEqual(trade["exit_date"], dates[3])
        self.assertEqual(trade["exit_reason"], "signal_trend_break")

    def test_excluded_entry_timing_is_skipped_with_reason(self):
        _, bars, replay = _managed_fixture(
            [
                (100, 101, 99, 100),
                (100, 102, 99, 101),
                (101, 103, 100, 102),
            ],
            stop=80, trail=20,
        )
        replay["timeline"][0]["entry_timing"] = {
            "classification": "pullback_reversal",
        }
        result = simulate_managed_variant(
            replay, bars, cost_bps=0,
            variant=ManagedVariant(
                name="skip_pullback_entry",
                excluded_entry_timings=frozenset({"pullback_reversal"}),
            ),
        )
        self.assertEqual(result["trades"], [])
        self.assertTrue(any(
            row["reason"] == "entry_timing_filtered"
            for row in result["skipped_signals"]
        ))

    def test_allocation_override_is_full_but_recovery_stays_capped(self):
        _, bars, replay = _managed_fixture(
            [
                (100, 101, 99, 100),
                (100, 102, 99, 101),
                (101, 103, 100, 102),
            ],
            stop=80, trail=20,
        )
        replay["timeline"][0]["suggested_position_pct"] = 40.0
        replay["timeline"][0]["entry_timing"] = {
            "classification": "trend_continuation",
        }
        full = simulate_managed_variant(
            replay, bars, cost_bps=0,
            variant=ManagedVariant(name="full_allocation", allocation_override_pct=100.0),
        )
        self.assertEqual(full["trades"][0]["entry_allocation_pct"], 100.0)
        replay["timeline"][0]["entry_timing"] = {
            "classification": "recovery_reversal",
        }
        replay["timeline"][0]["suggested_position_pct"] = 15.0
        capped = simulate_managed_variant(
            replay, bars, cost_bps=0,
            variant=ManagedVariant(name="full_allocation", allocation_override_pct=100.0),
        )
        self.assertEqual(capped["trades"][0]["entry_allocation_pct"], 15.0)

    def test_variant_rejects_stateful_combination(self):
        _, bars, replay = _managed_fixture(
            [
                (100, 101, 99, 100),
                (100, 102, 99, 101),
            ],
            stop=80, trail=20,
        )
        from backtesting.simulation import _simulate_managed_scenario

        with self.assertRaises(ValueError):
            _simulate_managed_scenario(
                replay, bars, policy="disciplined_verdict", cost_bps=0,
                stateful=True, variant=ManagedVariant(name="noop"),
            )


class StatefulLifecycleTests(unittest.TestCase):
    def test_trend_break_is_watch_only_but_overextended_exits_next_open(self):
        dates, bars, replay = _managed_fixture(
            [
                (100, 101, 99, 100),
                (100, 102, 99, 101),
                (101, 103, 100, 102),
                (102, 104, 101, 103),
                (104, 105, 103, 104),
            ],
            stop=80, trail=20,
        )
        replay["timeline"][2]["position_exit"] = {
            "action": "exit_next_open", "classification": "trend_break",
        }
        replay["timeline"][3]["position_exit"] = {
            "action": "exit_next_open", "classification": "overextended_reversal",
        }
        result = simulate_stateful_managed_scenario(replay, bars, cost_bps=0)
        trade = result["trades"][0]
        self.assertEqual(trade["exit_date"], dates[4])
        self.assertEqual(trade["exit_reason"], "signal_overextended_reversal")
        self.assertTrue(any(
            row["reason"] == "trend_break_risk_watch"
            for row in result["state_transitions"]
        ))

    def test_protective_exit_can_reenter_on_confirmed_pullback_next_open(self):
        dates, bars, replay = _managed_fixture(
            [
                (100, 101, 99, 100),
                (100, 102, 99, 100),
                (96, 97, 94, 95),
                (96, 98, 95, 97),
                (98, 100, 97, 99),
                (100, 102, 99, 101),
            ],
            stop=95, trail=20,
        )
        trigger = replay["timeline"][3]
        trigger.update({
            "status": "ok", "core_data_complete": True,
            "raw_composite": 55.0, "verdict": "观察",
            "confirmation": {"volume_pct": 60},
            "entry_timing": {"classification": "pullback_reversal"},
            "entry_timing_detail": {"classification": "pullback_reversal"},
            "suggested_position_pct": 30.0,
            "trade_plan": {"stop_loss_price": 90, "trailing_stop_pct": 20},
        })
        replay["timeline"][0]["suggested_position_pct"] = 40.0
        result = simulate_stateful_managed_scenario(replay, bars, cost_bps=0)
        self.assertEqual(len(result["trades"]), 2)
        first, second = result["trades"]
        self.assertEqual(first["exit_reason"], "stop_loss")
        self.assertEqual(second["entry_date"], dates[4])
        self.assertEqual(second["entry_kind"], "reentry")
        self.assertEqual(second["reentry_trigger"], "pullback_reversal")
        self.assertEqual(second["entry_allocation_pct"], 40.0)
        self.assertEqual(second["prior_exit_reason"], "stop_loss")
        self.assertEqual(second["recovery_wait_sessions"], 1)

    def test_repeated_yes_restores_allocation_at_next_open(self):
        dates, bars, replay = _managed_fixture(
            [
                (100, 101, 99, 100),
                (100, 102, 99, 101),
                (101, 103, 100, 102),
                (102, 104, 101, 103),
                (103, 105, 102, 104),
            ],
            stop=80, trail=20,
        )
        replay["timeline"][0]["suggested_position_pct"] = 20.0
        replay["timeline"][2].update({
            "raw_composite": 85.0,
            "verdict": "是",
            "suggested_position_pct": 60.0,
            "trade_plan": {"stop_loss_price": 80, "trailing_stop_pct": 20},
        })
        result = simulate_stateful_managed_scenario(replay, bars, cost_bps=0)
        trade = result["trades"][0]
        self.assertEqual(trade["entry_allocation_pct"], 20.0)
        self.assertEqual(trade["max_target_allocation_pct"], 60.0)
        self.assertEqual(len(trade["position_adjustments"]), 1)
        self.assertEqual(trade["position_adjustments"][0]["execution_date"], dates[3])
        self.assertGreater(result["equity_curve"][-1]["equity"], 1.01)

    def test_recovery_window_expiry_requires_standard_entry_again(self):
        count = 70
        ohlc = [(100, 101, 99, 100)] * count
        dates, bars, replay = _managed_fixture(ohlc, stop=95, trail=20)
        bars["AAPL"][2].update({"o": 96, "h": 97, "l": 94, "c": 95})
        late = replay["timeline"][66]
        late.update({
            "status": "ok", "core_data_complete": True,
            "raw_composite": 55.0, "verdict": "观察",
            "confirmation": {"volume_pct": 60},
            "entry_timing": {"classification": "pullback_reversal"},
            "entry_timing_detail": {"classification": "pullback_reversal"},
            "trade_plan": {"stop_loss_price": 90, "trailing_stop_pct": 20},
        })
        result = simulate_stateful_managed_scenario(replay, bars, cost_bps=0)
        self.assertEqual(len(result["trades"]), 1)
        self.assertTrue(any(
            row["reason"] == "recovery_window_expired"
            for row in result["state_transitions"]
        ))


if __name__ == "__main__":
    unittest.main()
