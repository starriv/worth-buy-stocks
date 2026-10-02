"""Behavioral boundaries for the no-option-market-data scanner."""
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import options_seller as CLI
from options_engine.config import OptionsSellerConfig
from options_engine.prescreen import build_screen, prepare_setups, validate_scan_input
from options_engine.regime import evaluate_market_regime
from options_engine.strategies import resolve_strategy_families
from test_options_calendar_iv import _flat_bars
from test_options_engine import _benchmark_bars


def fixture():
    return {
        "as_of": "2026-01-01T21:10:00Z",
        "bars": {"AAPL": _flat_bars(), "SPY": _benchmark_bars(),
                 "QQQ": _benchmark_bars(350)},
        "events": {"AAPL": {"status": "ok", "earnings_dates": [], "ex_dividend_dates": []}},
        "macro_events": {"status": "ok", "events": []},
    }


def screen(data, families=("iron_condor",)):
    cfg = OptionsSellerConfig()
    setups, rows, diagnostics = prepare_setups({"AAPL": data["bars"]["AAPL"]}, cfg, families)
    return build_screen(setups, rows, diagnostics, data.get("events", {}),
                        data.get("macro_events"), evaluate_market_regime(data["bars"]), cfg,
                        as_of=data["as_of"], selected=["AAPL"], requested=["AAPL"],
                        top=20, families=families)


class TestOptionsPrescreen(unittest.TestCase):
    def test_default_income_excludes_debit_and_high_gamma_strategies(self):
        args = CLI._parser(["scan"])
        self.assertEqual(args.strategy, "income")
        self.assertEqual(resolve_strategy_families(args.strategy), ("vertical_credit", "iron_condor"))
        data = fixture()
        payload = screen(data, resolve_strategy_families(args.strategy))
        self.assertEqual(payload["objective"], "net_credit_income")
        candidates = payload["symbols"]["AAPL"]["candidates"]
        self.assertTrue(candidates)
        self.assertTrue(all(c["premium_flow"] == "net_credit_required" for c in candidates))
        self.assertFalse(any(c["strategy_family"] in {"calendar_iv", "iron_butterfly"} for c in candidates))

    def test_income_blackout_does_not_substitute_a_calendar(self):
        data = fixture()
        data["macro_events"]["events"] = [{"date": "2026-01-02", "type": "employment"}]
        payload = screen(data, resolve_strategy_families("income"))
        self.assertEqual(payload["symbols"]["AAPL"]["status"], "回避")
        self.assertNotIn("calendar_iv", payload["rankings_by_strategy"])

    def test_directional_credit_template_identifies_put_and_leg_order(self):
        from test_options_engine import _bars
        data = fixture()
        data["bars"]["AAPL"] = _bars()
        payload = screen(data, ("vertical_credit",))
        candidate = next(c for c in payload["symbols"]["AAPL"]["candidates"] if c["strategy"] == "bull_put")
        self.assertEqual(candidate["direction"], "偏多")
        self.assertEqual(candidate["option_types"], ["put"])
        self.assertEqual([leg["action"] for leg in candidate["leg_template"]], ["sell", "buy"])
        self.assertEqual([leg["strike_relation"] for leg in candidate["leg_template"]], ["higher", "lower"])
        self.assertEqual(candidate["expiration_structure"], "same_expiration")

    def test_explicit_calendar_remains_available_and_is_labelled_debit(self):
        payload = screen(fixture(), resolve_strategy_families("calendar_iv"))
        c = payload["symbols"]["AAPL"]["candidates"][0]
        self.assertEqual(c["premium_flow"], "net_debit")
        self.assertEqual(c["income_eligibility"], "not_income_strategy")

    def test_bars_only_input_ignores_legacy_option_and_account_content(self):
        data = fixture()
        del data["events"]
        data.update(option_chains="invalid legacy content", account_context="ignored", iv_history="ignored")
        validate_scan_input(data)
        self.assertEqual(screen(data)["symbols"]["AAPL"]["status"], "观察等待")

    def test_no_market_data_or_state_side_effects_in_offline_scan(self):
        data = fixture()
        with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
            json.dump(data, handle)
            handle.flush()
            output = io.StringIO()
            with ExitStack() as stack:
                for name in ("fetch_chain_for_strategies", "_iv_context", "analyze_symbol",
                             "fetch_account_context", "fetch_options_assets", "fetch_liquidity",
                             "fetch_pool_bars", "collect_live_events", "collect_live_macro_events"):
                    stack.enter_context(patch.object(CLI, name, side_effect=AssertionError(name)))
                stack.enter_context(redirect_stdout(output))
                CLI.main(["scan", "--input", handle.name, "--symbols", "AAPL"])
            payload = json.loads(output.getvalue())
        self.assertEqual(payload["scope"], "underlying_only")
        candidate = payload["symbols"]["AAPL"]["candidates"][0]
        self.assertEqual(candidate["verdict"], "优先核对")
        for key in ("legs", "natural_credit", "natural_debit", "max_loss", "score", "iv_rank", "net_greeks"):
            self.assertNotIn(key, candidate)

    def test_earnings_inside_window_preserves_shorter_search_horizon(self):
        data = fixture()
        data["events"]["AAPL"]["earnings_dates"] = ["2026-02-01"]
        candidate = screen(data)["symbols"]["AAPL"]["candidates"][0]
        self.assertEqual(candidate["verdict"], "优先核对")
        self.assertEqual(candidate["horizon"]["latest_event_safe_date"], "2026-01-31")
        data["events"]["AAPL"]["earnings_dates"] = ["2026-01-10"]
        self.assertEqual(screen(data)["symbols"]["AAPL"]["status"], "回避")

    def test_macro_blackout_blocks_credit_but_not_calendar_shape(self):
        data = fixture()
        data["macro_events"]["events"] = [{"date": "2026-01-02", "type": "employment"}]
        row = screen(data, ("iron_condor", "calendar_iv"))["symbols"]["AAPL"]
        outcomes = {c["strategy_family"]: c["verdict"] for c in row["candidates"]}
        self.assertEqual(outcomes, {"iron_condor": "回避", "calendar_iv": "优先核对"})
        data.pop("macro_events")
        self.assertEqual(screen(data)["symbols"]["AAPL"]["status"], "观察等待")

    def test_live_scan_keeps_explicit_missing_symbols_and_skips_option_calls(self):
        data = fixture()
        output = io.StringIO()
        with ExitStack() as stack:
            for name in ("fetch_chain_for_strategies", "_iv_context", "analyze_symbol", "fetch_account_context"):
                stack.enter_context(patch.object(CLI, name, side_effect=AssertionError(name)))
            stack.enter_context(patch.object(CLI, "fetch_liquidity", return_value=([], [])))
            bars = stack.enter_context(patch.object(CLI, "fetch_pool_bars", return_value=(data["bars"], [])))
            stack.enter_context(patch.object(CLI, "collect_live_events", return_value=(data["events"], [])))
            stack.enter_context(patch.object(CLI, "collect_live_macro_events", return_value=(data["macro_events"], [])))
            stack.enter_context(patch.object(CLI, "_market_now", return_value=dt.datetime(2026, 1, 1, 16, 10)))
            stack.enter_context(patch.object(CLI, "_utc_now", return_value=data["as_of"]))
            stack.enter_context(redirect_stdout(output))
            CLI.main(["scan", "--symbols", "AAPL,MISSING", "--top", "1"])
            self.assertEqual(bars.call_args.args[0], ["AAPL"])
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["symbols"]["MISSING"]["status"], "未通过初筛")
        self.assertIn("AAPL", payload["symbols"])

    def test_small_caps_cannot_bypass_universe_with_explicit_or_offline_input(self):
        data = fixture()
        data["bars"]["ONDS"] = data["bars"]["AAPL"]
        with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
            json.dump(data, handle)
            handle.flush()
            for symbols in ([], ["--symbols", "AAPL,ONDS"]):
                payload = CLI.scan(CLI._parser(["scan", "--input", handle.name] + symbols))
                self.assertFalse(any(c["symbol"] == "ONDS" for cs in
                                     payload["rankings_by_strategy"].values() for c in cs))
                if symbols:
                    self.assertEqual(payload["symbols"]["ONDS"]["candidates"], [])
                    self.assertEqual(payload["symbols"]["ONDS"]["status"], "未通过初筛")

    def test_default_live_pool_intersects_option_enabled_assets_before_fetching(self):
        data = fixture()
        with ExitStack() as stack:
            stack.enter_context(patch.object(CLI, "fetch_options_assets", return_value=[]))
            stack.enter_context(patch.object(CLI, "filter_options_assets", return_value=["AAPL", "ONDS", "SPY"]))
            snapshots = stack.enter_context(patch.object(CLI, "fetch_liquidity", return_value=([{"symbol": "AAPL"}], [])))
            stack.enter_context(patch.object(CLI, "fetch_pool_bars", return_value=(data["bars"], [])))
            stack.enter_context(patch.object(CLI, "collect_live_events", return_value=(data["events"], [])))
            stack.enter_context(patch.object(CLI, "collect_live_macro_events", return_value=(data["macro_events"], [])))
            CLI.scan(CLI._parser(["scan"]))
            self.assertEqual(snapshots.call_args.args[0], ["AAPL", "SPY"])

    def test_weak_stock_liquidity_still_fails_inside_core_pool(self):
        data = fixture()
        data["bars"]["AAPL"] = _flat_bars(volume=150_000)
        with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
            json.dump(data, handle)
            handle.flush()
            payload = CLI.scan(CLI._parser(["scan", "--input", handle.name, "--symbols", "AAPL"]))
        self.assertEqual(payload["symbols"]["AAPL"]["status"], "未通过初筛")
        self.assertEqual(payload["symbols"]["AAPL"]["candidates"], [])

    def test_option_quote_flags_are_rejected_by_scan_parser(self):
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            CLI._parser(["scan", "--option-feed", "auto"])


if __name__ == "__main__":
    unittest.main()
