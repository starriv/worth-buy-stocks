#!/usr/bin/env python3
"""Deterministic option-seller module and offline CLI tests."""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import options_seller as CLI  # noqa: E402
from options_engine.config import OptionsSellerConfig  # noqa: E402
from options_engine.contracts import (  # noqa: E402
    OptionsContractError,
    validate_offline_input,
    validate_result,
)
from options_engine.data import (  # noqa: E402
    fetch_option_chain,
    fetch_option_contracts,
    normalize_offline_chain,
    normalize_option_chain,
)
from options_engine.events import (  # noqa: E402
    event_gates,
    macro_event_gates,
    normalize_events,
)
from options_engine.live import fetch_chain_for_setup  # noqa: E402
from options_engine.regime import evaluate_market_regime  # noqa: E402
from options_engine.scoring import score_spread  # noqa: E402
from options_engine.service import analyze_symbol as _analyze_symbol, screen_underlyings  # noqa: E402
from options_engine.setup import evaluate_bollinger_setup  # noqa: E402
from options_engine.volatility_surface import build_volatility_surface  # noqa: E402


def _bars():
    closes = [100.0 + 0.1 * i for i in range(60)] + [103.0, 105.0]
    start = dt.date(2025, 11, 1)
    return [
        {
            "t": (start + dt.timedelta(days=i)).isoformat() + "T04:00:00Z",
            "o": c - 0.2, "h": c + 1.0, "l": c - 1.0, "c": c,
            "v": 1_000_000 + i,
        }
        for i, c in enumerate(closes)
    ]


def _benchmark_bars(start_price=400.0):
    start = dt.date(2025, 11, 1)
    return [
        {
            "t": (start + dt.timedelta(days=i)).isoformat() + "T04:00:00Z",
            "o": start_price + i * 0.5, "h": start_price + i * 0.5 + 1,
            "l": start_price + i * 0.5 - 1, "c": start_price + i * 0.5,
            "v": 10_000_000,
        }
        for i in range(62)
    ]


def _normal_regime():
    return evaluate_market_regime({"SPY": _benchmark_bars(), "QQQ": _benchmark_bars(350.0)})


def analyze_symbol(*args, **kwargs):
    kwargs.setdefault("market_regime", _normal_regime())
    return _analyze_symbol(*args, **kwargs)


def _contract(symbol, strike, option_type, bid, ask, delta, iv=0.35, oi=500):
    return {
        "symbol": symbol,
        "underlying_symbol": "AAPL",
        "root_symbol": "AAPL",
        "expiration_date": "2026-02-15",
        "type": option_type,
        "style": "american",
        "strike_price": strike,
        "tradable": True,
        "open_interest": oi,
        "open_interest_date": "2025-12-31",
        "multiplier": 100,
        "quote": {
            "bid": bid, "ask": ask, "bid_size": 10, "ask_size": 10,
            "mid": round((bid + ask) / 2, 4),
            "spread_pct": round((ask - bid) / ((bid + ask) / 2) * 100, 2),
            "timestamp": "2026-01-01T20:59:00Z",
        },
        "implied_volatility": iv,
        "greeks": {"delta": delta, "gamma": 0.01, "theta": -0.03, "vega": 0.1, "rho": 0.01},
    }


def _chain(feed="opra"):
    return {
        "status": "ok", "symbol": "AAPL", "feed": feed,
        "as_of": "2026-01-01T21:00:00Z", "data_flags": [],
        "contracts": [
            _contract("AAPL260215P00092000", 92.0, "put", 0.80, 0.84, -0.22),
            _contract("AAPL260215P00090000", 90.0, "put", 0.25, 0.30, -0.14),
        ],
    }


def _bear_bars():
    closes = [100.0] * 60 + [105.0, 100.0]
    start = dt.date(2025, 11, 1)
    return [
        {
            "t": (start + dt.timedelta(days=i)).isoformat() + "T04:00:00Z",
            "o": c, "h": c + 1.0, "l": c - 1.0, "c": c,
            "v": 1_000_000 + i,
        }
        for i, c in enumerate(closes)
    ]


def _bear_chain():
    return {
        "status": "ok", "symbol": "AAPL", "feed": "opra",
        "as_of": "2026-01-01T21:00:00Z", "data_flags": [],
        "contracts": [
            _contract("AAPL260215C00118000", 118.0, "call", 0.80, 0.84, 0.22),
            _contract("AAPL260215C00120000", 120.0, "call", 0.25, 0.30, 0.14),
        ],
    }


class TestSetup(unittest.TestCase):
    def test_detects_lower_band_reentry(self):
        setup = evaluate_bollinger_setup(_bars(), OptionsSellerConfig())
        self.assertEqual(setup["status"], "ok")
        self.assertTrue(setup["signals"]["bull_put"]["eligible"])
        self.assertEqual(setup["signals"]["bull_put"]["boll_score"], 30)

    def test_underlying_screen_is_separate_from_chain(self):
        rows = screen_underlyings({"AAPL": _bars()}, OptionsSellerConfig())
        self.assertEqual(rows[0]["symbol"], "AAPL")
        self.assertEqual(rows[0]["strategy"], "bull_put")

    def test_rejects_unsorted_bars(self):
        setup = evaluate_bollinger_setup(list(reversed(_bars())), OptionsSellerConfig())
        self.assertEqual(setup["status"], "insufficient_data")
        self.assertIn("ascending", setup["reason"])


class TestVolatilityAndRegime(unittest.TestCase):
    def test_surface_computes_expected_move_without_inventing_iv_rank(self):
        surface = build_volatility_surface(
            _chain(), 105.0, as_of_date=dt.date(2026, 1, 1)
        )
        expiry = surface["expirations"]["2026-02-15"]
        self.assertEqual(surface["status"], "ok")
        self.assertEqual(expiry["atm_iv_pct"], 35.0)
        self.assertGreater(expiry["one_sigma_expected_move_abs"], 0)
        self.assertIsNone(surface["historical_iv_rank"])

    def test_market_regime_normal_and_missing_fail_closed(self):
        normal = _normal_regime()
        self.assertEqual(normal["state"], "normal")
        self.assertEqual(normal["risk_multiplier"], 1.0)
        missing = evaluate_market_regime({"SPY": _benchmark_bars()})
        self.assertEqual(missing["status"], "unavailable")
        self.assertEqual(missing["risk_multiplier"], 0.0)

    def test_live_chain_range_covers_far_otm_short_and_protection(self):
        setup = evaluate_bollinger_setup(_bars(), OptionsSellerConfig())
        args = SimpleNamespace(option_feed="opra", timeout=30)
        with patch("options_engine.live.fetch_normalized_chain", return_value={}) as fetch:
            fetch_chain_for_setup(
                "AAPL", setup, OptionsSellerConfig(), args, dt.date(2026, 1, 1)
            )
        kwargs = fetch.call_args.kwargs
        self.assertLessEqual(kwargs["strike_gte"], 105.0 * 0.75 - 10.0)
        self.assertGreaterEqual(kwargs["strike_lte"], 105.0 * 1.25 + 10.0)


class TestChainNormalization(unittest.TestCase):
    def test_joins_raw_contract_and_snapshot(self):
        contracts = [{
            "symbol": "AAPL260215P00102000", "underlying_symbol": "AAPL",
            "root_symbol": "AAPL",
            "expiration_date": "2026-02-15", "type": "put", "strike_price": "102",
            "tradable": True, "open_interest": "250", "size": "100",
        }]
        snapshots = {"AAPL260215P00102000": {
            "latestQuote": {"bp": 0.8, "ap": 0.84, "bs": 5, "as": 6, "t": "2026-01-01T20:59:00Z"},
            "greeks": {"delta": -0.22}, "impliedVolatility": 0.35,
        }}
        chain = normalize_option_chain("AAPL", contracts, snapshots, feed="opra")
        row = chain["contracts"][0]
        self.assertEqual(row["open_interest"], 250)
        self.assertEqual(row["quote"]["mid"], 0.82)
        self.assertEqual(row["greeks"]["delta"], -0.22)
        self.assertEqual(row["root_symbol"], "AAPL")

    def test_canonical_offline_rows_are_revalidated(self):
        chain = normalize_offline_chain("AAPL", _chain())
        self.assertEqual(chain["status"], "ok")
        self.assertEqual(len(chain["contracts"]), 2)

    def test_contract_transport_follows_page_tokens(self):
        pages = [
            {"option_contracts": [{"symbol": "ONE"}], "next_page_token": "next"},
            {"option_contracts": [{"symbol": "TWO"}], "next_page_token": ""},
        ]
        with patch("options_engine.data._run_json", side_effect=pages) as run:
            rows = fetch_option_contracts("AAPL", "2026-01-01", "2026-02-01")
        self.assertEqual([row["symbol"] for row in rows], ["ONE", "TWO"])
        self.assertIn("--page-token", run.call_args_list[1].args[0])

    def test_auto_feed_only_falls_back_on_subscription_error(self):
        with patch("options_engine.data._run_json", side_effect=[
            RuntimeError("403 subscription required"),
            {"snapshots": {}, "next_page_token": ""},
        ]):
            rows, feed, flags = fetch_option_chain(
                "AAPL", "2026-01-01", "2026-02-01", feed="auto"
            )
        self.assertEqual(rows, {})
        self.assertEqual(feed, "indicative")
        self.assertTrue(flags)

    def test_explicit_opra_does_not_fall_back(self):
        with patch("options_engine.data._run_json", side_effect=RuntimeError("403 subscription required")):
            with self.assertRaises(RuntimeError):
                fetch_option_chain("AAPL", "2026-01-01", "2026-02-01", feed="opra")


class TestEvents(unittest.TestCase):
    def test_earnings_blocks_both_and_dividend_only_bear_call(self):
        events = normalize_events({
            "status": "ok", "earnings_dates": ["2026-02-01"],
            "ex_dividend_dates": ["2026-02-05"],
        }, "AAPL")
        self.assertIn("earnings_before_expiration", event_gates("bull_put", "2026-02-15", events))
        bear = event_gates("bear_call", "2026-02-15", events)
        self.assertIn("earnings_before_expiration", bear)
        self.assertIn("ex_dividend_before_expiration", bear)

    def test_material_news_flag_is_hard_event_gate(self):
        events = normalize_events({
            "status": "ok",
            "earnings_dates": [],
            "ex_dividend_dates": [],
            "sources": [{"id": "news-1", "published_at": "2025-12-31"}],
            "red_flags": [{
                "type": "dilution", "severity": "medium", "source_id": "news-1",
            }],
        }, "AAPL")
        self.assertIn("material_event_risk", event_gates("bull_put", "2026-02-15", events))

    def test_past_event_does_not_block_new_spread(self):
        events = normalize_events({
            "status": "ok", "earnings_dates": ["2025-01-01"],
            "ex_dividend_dates": [],
        }, "AAPL")
        self.assertNotIn(
            "earnings_before_expiration",
            event_gates(
                "bull_put", "2026-02-15", events, as_of_date="2026-01-01"
            ),
        )

    def test_ok_status_requires_both_calendar_fields(self):
        events = normalize_events({"status": "ok"}, "AAPL")
        self.assertEqual(events["status"], "unavailable")

    def test_stale_news_source_cannot_trigger_material_gate(self):
        events = normalize_events({
            "status": "ok", "earnings_dates": [], "ex_dividend_dates": [],
            "sources": [{"id": "old", "published_at": "2025-01-01"}],
            "red_flags": [{
                "type": "dilution", "severity": "high", "source_id": "old",
            }],
        }, "AAPL", as_of_date="2026-01-01")
        self.assertNotIn("material_event_risk", event_gates("bull_put", "2026-02-15", events))

    def test_next_trading_day_macro_release_blocks_new_credit(self):
        hard, data = macro_event_gates({
            "status": "ok",
            "events": [{
                "date": "2026-01-05", "type": "cpi", "country": "US",
                "name": "Consumer Price Index",
            }],
        }, as_of_date="2026-01-02", blackout_days=1)
        self.assertEqual(data, [])
        self.assertIn("macro_event_blackout:cpi:2026-01-05", hard)

    def test_missing_explicit_macro_calendar_fails_closed(self):
        hard, data = macro_event_gates(
            {"status": "unavailable", "events": []},
            as_of_date="2026-01-02", blackout_days=1,
        )
        self.assertEqual(hard, [])
        self.assertIn("macro_event_calendar_unavailable", data)


class TestScoringService(unittest.TestCase):
    def test_valid_opra_credit_spread_is_ranked(self):
        row = analyze_symbol(
            "AAPL", _bars(), _chain(),
            {"status": "ok", "earnings_dates": [], "ex_dividend_dates": []},
            OptionsSellerConfig(), as_of="2026-01-01T21:00:00Z",
        )
        self.assertTrue(row["candidates"])
        candidate = row["candidates"][0]
        self.assertEqual(candidate["strategy"], "bull_put")
        self.assertEqual(candidate["natural_credit"], 0.5)
        self.assertEqual(candidate["max_loss"], 150.0)
        self.assertIn(candidate["score"]["verdict"], {"适合构建", "观察等待"})
        self.assertGreaterEqual(
            candidate["volatility_surface"]["expected_move_coverage"],
            OptionsSellerConfig().min_expected_move_coverage,
        )
        self.assertTrue(row["recommendation_profiles"])
        self.assertIn("dynamic_width_bounds", row["spread_diagnostics"]["bull_put"])

    def test_missing_market_regime_is_data_insufficient(self):
        row = _analyze_symbol(
            "AAPL", _bars(), _chain(),
            {"status": "ok", "earnings_dates": [], "ex_dividend_dates": []},
            OptionsSellerConfig(), as_of="2026-01-01T21:00:00Z",
        )
        self.assertEqual(row["candidates"][0]["score"]["verdict"], "数据不足")
        self.assertIn(
            "market_regime_unavailable", row["candidates"][0]["score"]["data_flags"]
        )

    def test_stress_regime_blocks_new_credit_spread(self):
        stress_bars = list(reversed(_benchmark_bars()))
        for index, row in enumerate(stress_bars):
            row["t"] = (dt.date(2025, 11, 1) + dt.timedelta(days=index)).isoformat() + "T04:00:00Z"
        regime = evaluate_market_regime({"SPY": stress_bars, "QQQ": stress_bars})
        row = _analyze_symbol(
            "AAPL", _bars(), _chain(),
            {"status": "ok", "earnings_dates": [], "ex_dividend_dates": []},
            OptionsSellerConfig(), as_of="2026-01-01T21:00:00Z", market_regime=regime,
        )
        self.assertEqual(regime["state"], "stress")
        self.assertIn("market_regime_stress", row["candidates"][0]["score"]["hard_gate_reasons"])

    def test_missing_event_calendar_is_data_insufficient(self):
        row = analyze_symbol(
            "AAPL", _bars(), _chain(), {"status": "unavailable"},
            OptionsSellerConfig(), as_of="2026-01-01T21:00:00Z",
        )
        self.assertEqual(row["candidates"][0]["score"]["verdict"], "数据不足")

    def test_indicative_feed_cannot_be_suitable(self):
        row = analyze_symbol(
            "AAPL", _bars(), _chain("indicative"),
            {"status": "ok", "earnings_dates": [], "ex_dividend_dates": []},
            OptionsSellerConfig(), as_of="2026-01-01T21:00:00Z",
        )
        self.assertNotEqual(row["candidates"][0]["score"]["verdict"], "适合构建")

    def test_earnings_before_expiration_is_hard_gate(self):
        row = analyze_symbol(
            "AAPL", _bars(), _chain(),
            {"status": "ok", "earnings_dates": ["2026-02-01"], "ex_dividend_dates": []},
            OptionsSellerConfig(), as_of="2026-01-01T21:00:00Z",
        )
        score = row["candidates"][0]["score"]
        self.assertEqual(score["verdict"], "回避")
        self.assertIn("earnings_before_expiration", score["hard_gate_reasons"])

    def test_account_risk_limit_is_hard_gate(self):
        row = analyze_symbol(
            "AAPL", _bars(), _chain(),
            {"status": "ok", "earnings_dates": [], "ex_dividend_dates": []},
            OptionsSellerConfig(), as_of="2026-01-01T21:00:00Z", account_equity=10_000,
        )
        score = row["candidates"][0]["score"]
        self.assertEqual(score["verdict"], "回避")
        self.assertIn("account_risk_above_limit", score["hard_gate_reasons"])
        self.assertEqual(score["max_spreads_by_risk"], 0)

    def test_account_risk_budget_reports_max_spreads(self):
        row = analyze_symbol(
            "AAPL", _bars(), _chain(),
            {"status": "ok", "earnings_dates": [], "ex_dividend_dates": []},
            OptionsSellerConfig(), as_of="2026-01-01T21:00:00Z", account_equity=50_000,
        )
        score = row["candidates"][0]["score"]
        self.assertEqual(score["account_risk_budget"], 250.0)
        self.assertEqual(score["max_spreads_by_risk"], 1)

    def test_user_supplied_round_trip_cost_adjusts_risk(self):
        row = analyze_symbol(
            "AAPL", _bars(), _chain(),
            {"status": "ok", "earnings_dates": [], "ex_dividend_dates": []},
            OptionsSellerConfig(round_trip_cost_per_spread=2.0),
            as_of="2026-01-01T21:00:00Z", account_equity=50_000,
        )
        candidate = row["candidates"][0]
        self.assertEqual(candidate["net_max_profit"], 48.0)
        self.assertEqual(candidate["net_max_loss"], 152.0)
        self.assertEqual(candidate["net_credit"], 0.48)
        self.assertEqual(candidate["net_credit_width_pct"], 24.0)
        self.assertEqual(candidate["score"]["max_spreads_by_risk"], 1)

    def test_cost_can_make_vertical_credit_economics_ineligible(self):
        row = analyze_symbol(
            "AAPL", _bars(), _chain(),
            {"status": "ok", "earnings_dates": [], "ex_dividend_dates": []},
            OptionsSellerConfig(round_trip_cost_per_spread=55.0),
            as_of="2026-01-01T21:00:00Z", account_equity=50_000,
        )
        score = row["candidates"][0]["score"]
        self.assertEqual(score["verdict"], "回避")
        self.assertIn("net_profit_after_cost_not_positive", score["hard_gate_reasons"])

    def test_known_macro_blackout_is_a_hard_gate(self):
        row = analyze_symbol(
            "AAPL", _bars(), _chain(),
            {"status": "ok", "earnings_dates": [], "ex_dividend_dates": []},
            OptionsSellerConfig(), as_of="2026-01-01T21:00:00Z",
            macro_events={
                "status": "ok",
                "events": [{
                    "date": "2026-01-02", "type": "pce", "country": "US",
                    "name": "PCE Price Index",
                }],
            },
        )
        score = row["candidates"][0]["score"]
        self.assertEqual(score["verdict"], "回避")
        self.assertIn("macro_event_blackout:pce:2026-01-02", score["hard_gate_reasons"])

    def test_directional_ma60_confirmation_is_a_hard_gate(self):
        row = analyze_symbol(
            "AAPL", _bars(), _chain(),
            {"status": "ok", "earnings_dates": [], "ex_dividend_dates": []},
            OptionsSellerConfig(), as_of="2026-01-01T21:00:00Z",
        )
        spread = row["candidates"][0]
        setup = dict(row["underlying"])
        setup["signals"] = dict(setup["signals"])
        setup["signals"]["bull_put"] = dict(setup["signals"]["bull_put"])
        setup["signals"]["bull_put"]["trend_checks"] = {"price_vs_ma60": False}
        score = score_spread(
            spread, setup, OptionsSellerConfig(), option_feed="opra",
            events={"status": "ok", "earnings_dates": [], "ex_dividend_dates": []},
            as_of="2026-01-01T21:00:00Z", market_regime=row["market_regime"],
            portfolio_risk={
                "status": "ok", "hard_gates": [], "data_flags": [],
                "portfolio_fit_score": 5,
            },
        )
        self.assertEqual(score["verdict"], "回避")
        self.assertIn(
            "directional_ma60_trend_not_confirmed", score["hard_gate_reasons"]
        )

    def test_unconfirmed_portfolio_and_observation_liquidity_cap_suitability(self):
        row = analyze_symbol(
            "AAPL", _bars(), _chain(),
            {"status": "ok", "earnings_dates": [], "ex_dividend_dates": []},
            OptionsSellerConfig(), as_of="2026-01-01T21:00:00Z",
        )
        score = row["candidates"][0]["score"]
        self.assertFalse(score["portfolio_confirmed"])
        self.assertFalse(score["suitable_liquidity"])
        self.assertNotEqual(score["verdict"], "适合构建")

    def test_same_utc_date_but_previous_market_session_quote_is_stale(self):
        chain = _chain()
        for contract in chain["contracts"]:
            contract["quote"]["timestamp"] = "2026-01-01T00:00:00Z"
        row = analyze_symbol(
            "AAPL", _bars(), chain,
            {"status": "ok", "earnings_dates": [], "ex_dividend_dates": []},
            OptionsSellerConfig(), as_of="2026-01-01T21:00:00Z",
        )
        self.assertEqual(row["candidates"][0]["score"]["verdict"], "数据不足")
        self.assertIn("short_leg_quote_stale", row["candidates"][0]["score"]["data_flags"])

    def test_adjusted_contracts_are_not_combined(self):
        chain = _chain()
        for contract in chain["contracts"]:
            contract["root_symbol"] = "AAPL1"
            contract["multiplier"] = 10
        row = analyze_symbol(
            "AAPL", _bars(), chain,
            {"status": "ok", "earnings_dates": [], "ex_dividend_dates": []},
            OptionsSellerConfig(), as_of="2026-01-01T21:00:00Z",
        )
        self.assertEqual(row["candidates"], [])
        self.assertIn(
            "spread_rejected:adjusted_or_mismatched_root=2", row["data_flags"]
        )

    def test_no_candidate_explains_delta_rejections(self):
        chain = _chain()
        for contract in chain["contracts"]:
            contract["greeks"]["delta"] = -0.50
        row = analyze_symbol(
            "AAPL", _bars(), chain,
            {"status": "ok", "earnings_dates": [], "ex_dividend_dates": []},
            OptionsSellerConfig(), as_of="2026-01-01T21:00:00Z",
        )
        self.assertEqual(row["candidates"], [])
        self.assertIn("spread_rejected:short_delta_outside_range=2", row["data_flags"])

    def test_bear_call_path_is_ranked(self):
        row = analyze_symbol(
            "AAPL", _bear_bars(), _bear_chain(),
            {"status": "ok", "earnings_dates": [], "ex_dividend_dates": []},
            OptionsSellerConfig(), as_of="2026-01-01T21:00:00Z",
        )
        self.assertTrue(row["candidates"])
        self.assertEqual(row["candidates"][0]["strategy"], "bear_call")


class TestOfflineCLI(unittest.TestCase):
    def _fixture_file(self):
        fixture = {
            "as_of": "2026-01-01T21:00:00Z",
            "bars": {
                "AAPL": _bars(), "SPY": _benchmark_bars(), "QQQ": _benchmark_bars(350.0),
            },
            "option_chains": {"AAPL": _chain()},
            "events": {"AAPL": {"status": "ok", "earnings_dates": [], "ex_dividend_dates": []}},
        }
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        json.dump(fixture, handle)
        handle.close()
        self.addCleanup(lambda: os.path.exists(handle.name) and os.unlink(handle.name))
        return handle.name

    def test_analyze_is_offline_and_emits_valid_contract(self):
        path = self._fixture_file()
        output = io.StringIO()
        with redirect_stdout(output):
            rc = CLI.main(["analyze", "--symbol", "AAPL", "--input", path])
        self.assertEqual(rc, 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["offline"])
        self.assertEqual(validate_result(payload)["symbols_count"], 1)

    def test_scan_uses_same_offline_contract_without_network(self):
        path = self._fixture_file()
        output = io.StringIO()
        with redirect_stdout(output):
            rc = CLI.main(["scan", "--symbols", "AAPL", "--input", path])
        self.assertEqual(rc, 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["offline"])
        self.assertEqual(payload["scope"], "underlying_only")
        self.assertNotIn("chains_requested", payload["counts"])
        self.assertIn("AAPL", payload["symbols"])

    def test_contract_rejects_unsorted_bars(self):
        fixture = {
            "as_of": "2026-01-01T21:00:00Z",
            "bars": {"AAPL": list(reversed(_bars()))},
            "option_chains": {"AAPL": _chain()},
            "events": {"AAPL": {
                "status": "ok", "earnings_dates": [], "ex_dividend_dates": [],
            }},
        }
        with self.assertRaises(OptionsContractError):
            validate_offline_input(fixture)

    def test_contract_rejects_mismatched_underlying(self):
        chain = _chain()
        chain["contracts"][0]["underlying_symbol"] = "MSFT"
        fixture = {
            "as_of": "2026-01-01T21:00:00Z",
            "bars": {"AAPL": _bars()},
            "option_chains": {"AAPL": chain},
            "events": {"AAPL": {
                "status": "ok", "earnings_dates": [], "ex_dividend_dates": [],
            }},
        }
        with self.assertRaises(OptionsContractError):
            validate_offline_input(fixture)

    def test_required_second_stage_symbol_needs_chain_and_events(self):
        fixture = {
            "as_of": "2026-01-01T21:00:00Z",
            "bars": {"AAPL": _bars()},
            "option_chains": {},
            "events": {},
        }
        validate_offline_input(fixture)
        with self.assertRaises(OptionsContractError):
            validate_offline_input(fixture, required_symbols=["AAPL"])

    def test_optional_iv_history_rejects_out_of_range_percentile(self):
        fixture = {
            "as_of": "2026-01-01T21:00:00Z",
            "bars": {"AAPL": _bars()},
            "option_chains": {"AAPL": _chain()},
            "events": {"AAPL": {
                "status": "ok", "earnings_dates": [], "ex_dividend_dates": [],
            }},
            "iv_history": {"AAPL": {
                "status": "ok", "iv_rank": 50, "iv_percentile": 101,
            }},
        }
        with self.assertRaises(OptionsContractError):
            validate_offline_input(fixture)

    def test_live_historical_end_is_rejected(self):
        with self.assertRaises(ValueError):
            CLI._validate_live_end("2025-12-31", dt.date(2026, 1, 1))


class TestAssetUniverse(unittest.TestCase):
    def test_current_option_attribute_and_supported_symbols_survive(self):
        assets = [
            {"symbol": "AAPL", "exchange": "NASDAQ", "status": "active", "tradable": True,
             "attributes": ["has_options"]},
            {"symbol": "BRK.B", "exchange": "NYSE", "status": "active", "tradable": True,
             "attributes": ["has_options"]},
            {"symbol": "SPY", "exchange": "ARCA", "status": "active", "tradable": True,
             "attributes": ["has_options"]},
            {"symbol": "NOOPT", "exchange": "NYSE", "status": "active", "tradable": True,
             "attributes": []},
        ]
        self.assertEqual(CLI.filter_options_assets(assets), ["AAPL", "BRK.B", "SPY"])


if __name__ == "__main__":
    unittest.main()
