#!/usr/bin/env python3
"""Calendar-IV strategy construction, scoring, registry, and CLI tests."""
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


def _contract(symbol, expiration, bid, ask, delta, theta, vega, iv):
    return {
        "symbol": symbol,
        "underlying_symbol": "AAPL",
        "root_symbol": "AAPL",
        "expiration_date": expiration,
        "type": "call",
        "style": "american",
        "strike_price": 100.0,
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
        "implied_volatility": iv,
        "greeks": {
            "delta": delta,
            "gamma": 0.02,
            "theta": theta,
            "vega": vega,
            "rho": 0.01,
        },
    }


def _calendar_chain(feed="opra"):
    return {
        "status": "ok",
        "symbol": "AAPL",
        "feed": feed,
        "as_of": "2026-01-01T21:00:00Z",
        "data_flags": [],
        "contracts": [
            _contract("AAPL260123C00100000", "2026-01-23", 2.00, 2.10, 0.50, -0.06, 0.08, 0.32),
            _contract("AAPL260227C00100000", "2026-02-27", 3.00, 3.10, 0.53, -0.03, 0.18, 0.30),
        ],
    }


def _normal_regime():
    return evaluate_market_regime({
        "SPY": _benchmark_bars(400.0),
        "QQQ": _benchmark_bars(350.0),
    })


def _events(**overrides):
    row = {"status": "ok", "earnings_dates": [], "ex_dividend_dates": []}
    row.update(overrides)
    return row


class TestCalendarIVModule(unittest.TestCase):
    def test_calendar_screen_does_not_require_directional_boll_trigger(self):
        rows = screen_underlyings(
            {"AAPL": _flat_bars()},
            OptionsSellerConfig(),
            ("calendar_iv",),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["strategy_family"], "calendar_iv")
        self.assertEqual(rows[0]["classification"], "bollinger_centered_low_directionality")

    def test_calendar_screen_handles_zero_custom_dollar_volume_floor(self):
        rows = screen_underlyings(
            {"AAPL": _flat_bars()},
            OptionsSellerConfig(min_underlying_dollar_volume=0),
            ("calendar_iv",),
        )
        self.assertEqual(rows[0]["symbol"], "AAPL")

    def test_same_strike_calendar_is_built_with_conservative_debit(self):
        row = analyze_symbol(
            "AAPL",
            _flat_bars(),
            _calendar_chain(),
            _events(),
            OptionsSellerConfig(),
            as_of="2026-01-01T21:00:00Z",
            market_regime=_normal_regime(),
            strategy_families=("calendar_iv",),
        )
        self.assertTrue(row["candidates"])
        candidate = row["candidates"][0]
        self.assertEqual(candidate["strategy"], "long_call_calendar")
        self.assertEqual(candidate["front_expiration_date"], "2026-01-23")
        self.assertEqual(candidate["back_expiration_date"], "2026-02-27")
        self.assertAlmostEqual(candidate["natural_debit"], 1.10)
        self.assertAlmostEqual(candidate["debit_at_risk"], 110.0)
        self.assertAlmostEqual(candidate["abs_net_delta"], 0.03)
        self.assertAlmostEqual(candidate["front_back_iv_edge_vol_points"], 2.0)
        self.assertGreater(candidate["net_greeks_per_contract"]["vega"], 0)
        self.assertIn(candidate["score"]["verdict"], {"适合构建", "观察等待"})
        self.assertIn("calendar_iv", row["recommendation_profiles"])

    def test_delta_is_a_hard_tolerance_not_an_exact_zero_claim(self):
        chain = _calendar_chain()
        chain["contracts"][1]["greeks"]["delta"] = 0.70
        row = analyze_symbol(
            "AAPL", _flat_bars(), chain, _events(), OptionsSellerConfig(),
            as_of="2026-01-01T21:00:00Z", market_regime=_normal_regime(),
            strategy_families=("calendar_iv",),
        )
        score = row["candidates"][0]["score"]
        self.assertEqual(score["verdict"], "回避")
        self.assertIn("calendar_net_delta_outside_tolerance", score["hard_gate_reasons"])

    def test_call_calendar_ex_dividend_before_front_expiry_is_blocked(self):
        row = analyze_symbol(
            "AAPL", _flat_bars(), _calendar_chain(),
            _events(ex_dividend_dates=["2026-01-15"]),
            OptionsSellerConfig(), as_of="2026-01-01T21:00:00Z",
            market_regime=_normal_regime(), strategy_families=("calendar_iv",),
        )
        score = row["candidates"][0]["score"]
        self.assertEqual(score["verdict"], "回避")
        self.assertIn("ex_dividend_before_front_expiration", score["hard_gate_reasons"])

    def test_front_iv_must_not_be_far_cheaper_than_back_iv(self):
        chain = _calendar_chain()
        chain["contracts"][1]["implied_volatility"] = 0.36
        row = analyze_symbol(
            "AAPL", _flat_bars(), chain, _events(), OptionsSellerConfig(),
            as_of="2026-01-01T21:00:00Z", market_regime=_normal_regime(),
            strategy_families=("calendar_iv",),
        )
        self.assertIn(
            "calendar_front_iv_too_cheap_vs_back",
            row["candidates"][0]["score"]["hard_gate_reasons"],
        )

    def test_union_chain_bounds_cover_front_and_back_expirations(self):
        setup = evaluate_bollinger_setup(_flat_bars(), OptionsSellerConfig())
        args = SimpleNamespace(option_feed="opra", timeout=30)
        with patch("options_engine.live.fetch_normalized_chain", return_value={}) as fetch:
            fetch_chain_for_strategies(
                "AAPL", setup, OptionsSellerConfig(), args,
                dt.date(2026, 1, 1), ("calendar_iv",),
            )
        call = fetch.call_args
        self.assertEqual(call.args[1], "2026-01-15")
        self.assertEqual(call.args[2], "2026-04-01")
        self.assertEqual(call.kwargs["strike_gte"], 95.0)
        self.assertEqual(call.kwargs["strike_lte"], 105.0)


class TestCalendarIVCLI(unittest.TestCase):
    def test_all_selection_round_robins_event_budget_across_modules(self):
        setups = [
            {"symbol": "CAL1", "strategy_family": "calendar_iv"},
            {"symbol": "CAL2", "strategy_family": "calendar_iv"},
            {"symbol": "VERT1", "strategy_family": "vertical_credit"},
            {"symbol": "VERT2", "strategy_family": "vertical_credit"},
        ]
        self.assertEqual(
            CLI.select_screen_symbols(
                setups, 2, ("vertical_credit", "calendar_iv")
            ),
            ["VERT1", "CAL1"],
        )

    def test_offline_analyze_selects_calendar_module(self):
        fixture = {
            "as_of": "2026-01-01T21:00:00Z",
            "bars": {
                "AAPL": _flat_bars(),
                "SPY": _benchmark_bars(400.0),
                "QQQ": _benchmark_bars(350.0),
            },
            "option_chains": {"AAPL": _calendar_chain()},
            "events": {"AAPL": _events()},
        }
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        json.dump(fixture, handle)
        handle.close()
        self.addCleanup(lambda: os.path.exists(handle.name) and os.unlink(handle.name))
        output = io.StringIO()
        with redirect_stdout(output):
            rc = CLI.main([
                "analyze", "--symbol", "AAPL", "--input", handle.name,
                "--strategy", "calendar_iv",
            ])
        self.assertEqual(rc, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["strategy_selection"], "calendar_iv")
        self.assertEqual(
            payload["symbols"]["AAPL"]["candidates"][0]["strategy_family"],
            "calendar_iv",
        )


if __name__ == "__main__":
    unittest.main()
