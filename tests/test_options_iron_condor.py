#!/usr/bin/env python3
"""Iron Condor construction, risk gates, registry, and CLI tests."""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

os.sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import options_seller as CLI  # noqa: E402
from options_engine.config import OptionsSellerConfig  # noqa: E402
from options_engine.live import fetch_chain_for_strategies  # noqa: E402
from options_engine.regime import evaluate_market_regime  # noqa: E402
from options_engine.service import analyze_symbol, screen_underlyings  # noqa: E402
from options_engine.setup import evaluate_bollinger_setup  # noqa: E402
from options_engine.strategies import resolve_strategy_families  # noqa: E402


def _flat_bars(price=100.0, volume=1_000_000, count=62):
    start = dt.date(2025, 11, 1)
    return [{
        "t": (start + dt.timedelta(days=index)).isoformat() + "T04:00:00Z",
        "o": price,
        "h": price + 1,
        "l": price - 1,
        "c": price,
        "v": volume,
    } for index in range(count)]


def _benchmark_bars(price):
    start = dt.date(2025, 11, 1)
    return [{
        "t": (start + dt.timedelta(days=index)).isoformat() + "T04:00:00Z",
        "o": price + index * 0.2,
        "h": price + index * 0.2 + 1,
        "l": price + index * 0.2 - 1,
        "c": price + index * 0.2,
        "v": 10_000_000,
    } for index in range(62)]


def _contract(symbol, option_type, strike, bid, ask, delta, theta, vega, gamma):
    return {
        "symbol": symbol,
        "underlying_symbol": "AAPL",
        "root_symbol": "AAPL",
        "expiration_date": "2026-02-15",
        "type": option_type,
        "style": "american",
        "strike_price": float(strike),
        "tradable": True,
        "open_interest": 500,
        "open_interest_date": "2025-12-31",
        "multiplier": 100,
        "quote": {
            "bid": bid,
            "ask": ask,
            "bid_size": 10,
            "ask_size": 10,
            "mid": round((bid + ask) / 2, 4),
            "spread_pct": round((ask - bid) / ((bid + ask) / 2) * 100, 2),
            "timestamp": "2026-01-01T20:59:00Z",
        },
        "implied_volatility": 0.35,
        "greeks": {
            "delta": delta,
            "gamma": gamma,
            "theta": theta,
            "vega": vega,
            "rho": 0.01,
        },
    }


def _chain(feed="opra"):
    return {
        "status": "ok",
        "symbol": "AAPL",
        "feed": feed,
        "as_of": "2026-01-01T21:00:00Z",
        "data_flags": [],
        "contracts": [
            _contract("AAPL260215P00088000", "put", 88, 0.20, 0.22, -0.08, -0.01, 0.04, 0.01),
            _contract("AAPL260215P00090000", "put", 90, 0.70, 0.74, -0.20, -0.04, 0.10, 0.02),
            _contract("AAPL260215C00110000", "call", 110, 0.70, 0.74, 0.20, -0.04, 0.10, 0.02),
            _contract("AAPL260215C00112000", "call", 112, 0.20, 0.22, 0.08, -0.01, 0.04, 0.01),
        ],
    }


def _events(**overrides):
    row = {"status": "ok", "earnings_dates": [], "ex_dividend_dates": []}
    row.update(overrides)
    return row


def _normal_regime():
    return evaluate_market_regime({
        "SPY": _benchmark_bars(400.0),
        "QQQ": _benchmark_bars(350.0),
    })


def _analyze(chain=None, events=None, cfg=None):
    return analyze_symbol(
        "AAPL", _flat_bars(), chain or _chain(), events or _events(),
        cfg or OptionsSellerConfig(), as_of="2026-01-01T21:00:00Z",
        market_regime=_normal_regime(), strategy_families=("iron_condor",),
    )


class TestIronCondorModule(unittest.TestCase):
    def test_screen_does_not_require_directional_band_touch(self):
        rows = screen_underlyings(
            {"AAPL": _flat_bars()}, OptionsSellerConfig(), ("iron_condor",)
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["strategy_family"], "iron_condor")
        self.assertEqual(rows[0]["classification"], "bollinger_centered_range_bound")

    def test_four_leg_natural_credit_risk_break_evens_and_greeks(self):
        row = _analyze()
        self.assertTrue(row["candidates"])
        candidate = row["candidates"][0]
        self.assertEqual(len(candidate["legs"]), 4)
        self.assertEqual(candidate["put_width"], 2.0)
        self.assertEqual(candidate["call_width"], 2.0)
        self.assertAlmostEqual(candidate["natural_credit"], 0.96)
        self.assertAlmostEqual(candidate["max_profit"], 96.0)
        self.assertAlmostEqual(candidate["downside_max_loss"], 104.0)
        self.assertAlmostEqual(candidate["upside_max_loss"], 104.0)
        self.assertAlmostEqual(candidate["max_loss"], 104.0)
        self.assertAlmostEqual(candidate["lower_break_even"], 89.04)
        self.assertAlmostEqual(candidate["upper_break_even"], 110.96)
        self.assertAlmostEqual(candidate["abs_net_delta"], 0.0)
        self.assertAlmostEqual(candidate["net_greeks_per_contract"]["theta"], 0.06)
        self.assertAlmostEqual(candidate["net_greeks_per_contract"]["vega"], -0.12)
        self.assertIn(candidate["score"]["verdict"], {"适合构建", "观察等待"})
        self.assertTrue(candidate["candidate_id"].endswith(
            "long_call_wing:AAPL260215C00112000"
        ))

    def test_net_delta_is_a_hard_tolerance(self):
        chain = _chain()
        chain["contracts"][3]["greeks"]["delta"] = -0.08
        candidate = _analyze(chain=chain)["candidates"][0]
        self.assertEqual(candidate["score"]["verdict"], "回避")
        self.assertIn(
            "iron_condor_net_delta_outside_tolerance",
            candidate["score"]["hard_gate_reasons"],
        )

    def test_earnings_and_call_assignment_events_are_hard_gates(self):
        for events, reason in (
            (_events(earnings_dates=["2026-01-20"]), "earnings_before_expiration"),
            (_events(ex_dividend_dates=["2026-01-20"]), "ex_dividend_before_expiration"),
        ):
            with self.subTest(reason=reason):
                candidate = _analyze(events=events)["candidates"][0]
                self.assertEqual(candidate["score"]["verdict"], "回避")
                self.assertIn(reason, candidate["score"]["hard_gate_reasons"])

    def test_wide_quote_is_not_executable(self):
        chain = _chain()
        chain["contracts"][0]["quote"]["spread_pct"] = 25.0
        candidate = _analyze(chain=chain)["candidates"][0]
        self.assertEqual(candidate["score"]["verdict"], "回避")
        self.assertIn(
            "long_put_leg_quote_spread_too_wide",
            candidate["score"]["hard_gate_reasons"],
        )

    def test_missing_gamma_or_short_iv_is_data_insufficient(self):
        for field, contract_index, reason in (
            ("gamma", 0, "iron_condor_net_gamma_unavailable"),
            ("implied_volatility", 1, "iron_condor_short_iv_unavailable"),
        ):
            with self.subTest(field=field):
                chain = _chain()
                if field == "gamma":
                    chain["contracts"][contract_index]["greeks"].pop(field)
                else:
                    chain["contracts"][contract_index].pop(field)
                candidate = _analyze(chain=chain)["candidates"][0]
                self.assertEqual(candidate["score"]["verdict"], "数据不足")
                self.assertIn(reason, candidate["score"]["data_flags"])

    def test_missing_short_delta_is_rejected_before_four_leg_enumeration(self):
        chain = _chain()
        chain["contracts"][1]["greeks"].pop("delta")
        row = _analyze(chain=chain)
        self.assertEqual(row["candidates"], [])
        self.assertEqual(
            row["spread_diagnostics"]["iron_condor"]["rejections"][
                "short_put_delta_missing"
            ],
            1,
        )

    def test_round_trip_cost_cannot_consume_all_max_profit(self):
        candidate = _analyze(
            cfg=OptionsSellerConfig(round_trip_cost_per_spread=100.0)
        )["candidates"][0]
        self.assertEqual(candidate["score"]["verdict"], "回避")
        self.assertEqual(candidate["net_max_profit"], -4.0)
        self.assertIn(
            "iron_condor_net_profit_after_cost_not_positive",
            candidate["score"]["hard_gate_reasons"],
        )

    def test_asymmetric_wings_are_rejected_in_first_version(self):
        chain = _chain()
        chain["contracts"][3]["strike_price"] = 113.0
        row = _analyze(chain=chain)
        self.assertEqual(row["candidates"], [])
        self.assertEqual(
            row["spread_diagnostics"]["iron_condor"]["rejections"]["asymmetric_wing_width"],
            1,
        )

    def test_union_chain_bounds_include_condor_wings(self):
        setup = evaluate_bollinger_setup(_flat_bars(), OptionsSellerConfig())
        args = SimpleNamespace(option_feed="opra", timeout=30)
        with patch("options_engine.live.fetch_normalized_chain", return_value={}) as fetch:
            fetch_chain_for_strategies(
                "AAPL", setup, OptionsSellerConfig(), args,
                dt.date(2026, 1, 1), ("iron_condor",),
            )
        call = fetch.call_args
        self.assertEqual(call.args[1], "2026-01-22")
        self.assertEqual(call.args[2], "2026-02-15")
        self.assertEqual(call.kwargs["strike_gte"], 65.0)
        self.assertEqual(call.kwargs["strike_lte"], 135.0)

    def test_all_registry_selection_includes_iron_condor(self):
        self.assertEqual(
            resolve_strategy_families("all"),
            ("vertical_credit", "calendar_iv", "iron_condor", "iron_butterfly"),
        )


class TestIronCondorCLI(unittest.TestCase):
    @staticmethod
    def _offline_fixture():
        return {
            "as_of": "2026-01-01T21:00:00Z",
            "bars": {
                "AAPL": _flat_bars(),
                "SPY": _benchmark_bars(400.0),
                "QQQ": _benchmark_bars(350.0),
            },
            "option_chains": {"AAPL": _chain()},
            "events": {"AAPL": _events()},
        }

    def test_offline_analyze_selects_iron_condor_module(self):
        fixture = self._offline_fixture()
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        json.dump(fixture, handle)
        handle.close()
        self.addCleanup(lambda: os.path.exists(handle.name) and os.unlink(handle.name))
        output = io.StringIO()
        with redirect_stdout(output):
            rc = CLI.main([
                "analyze", "--symbol", "AAPL", "--input", handle.name,
                "--strategy", "iron_condor",
            ])
        self.assertEqual(rc, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["strategy_selection"], "iron_condor")
        self.assertEqual(
            payload["symbols"]["AAPL"]["candidates"][0]["strategy_family"],
            "iron_condor",
        )

    def test_scan_ignores_account_context_equity_and_account_file(self):
        fixture = self._offline_fixture()
        fixture["account_context"] = {
            "status": "ok",
            "account": {
                "equity": 1.0,
                "trading_blocked": True,
                "account_blocked": True,
            },
            "positions": {},
        }
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        json.dump(fixture, handle)
        handle.close()
        self.addCleanup(lambda: os.path.exists(handle.name) and os.unlink(handle.name))

        output = io.StringIO()
        with redirect_stdout(output):
            rc = CLI.main([
                "scan", "--input", handle.name, "--symbols", "AAPL",
                "--strategy", "iron_condor", "--chain-top", "1", "--top", "1",
                "--account-context", "on", "--account-equity", "1",
                "--account-context-file", "/path/that/must/not/be/read.json",
            ])

        self.assertEqual(rc, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["account_context_policy"], "ignored_for_scan")
        score = payload["symbols"]["AAPL"]["candidates"][0]["score"]
        self.assertNotIn("account_trading_blocked", score["hard_gate_reasons"])
        self.assertNotIn("account_risk_above_limit", score["hard_gate_reasons"])
        self.assertEqual(
            score["portfolio_risk"]["scope"], "account_independent_scan"
        )


if __name__ == "__main__":
    unittest.main()
