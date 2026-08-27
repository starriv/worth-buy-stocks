#!/usr/bin/env python3
"""Four-leg ledger, reconciliation, pricing, and lifecycle regression tests."""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from options_engine.ledger import (  # noqa: E402
    OptionsLedger,
    normalize_ledger_position,
    position_from_candidate,
)
from options_engine.lifecycle import LifecycleConfig, review_portfolio  # noqa: E402
from options_engine.lifecycle_live import hydrate_live_payload, reconcile_positions  # noqa: E402
from options_engine.lifecycle_reporting import render_lifecycle_report  # noqa: E402


EXPIRY = "2026-02-15"
QUOTED_AT = "2026-01-01T20:59:00Z"


def _leg(symbol: str, side: str, role: str, option_type: str, strike: float) -> dict:
    return {
        "symbol": symbol, "side": side, "role": role,
        "type": option_type, "strike": strike,
    }


def _condor_legs() -> list[dict]:
    return [
        _leg("AAPL260215P00090000", "buy_to_open", "long_put_wing", "put", 90),
        _leg("AAPL260215P00095000", "sell_to_open", "short_put", "put", 95),
        _leg("AAPL260215C00105000", "sell_to_open", "short_call", "call", 105),
        _leg("AAPL260215C00110000", "buy_to_open", "long_call_wing", "call", 110),
    ]


def _butterfly_legs() -> list[dict]:
    return [
        _leg("AAPL260215P00095000", "buy_to_open", "long_put_wing", "put", 95),
        _leg("AAPL260215P00100000", "sell_to_open", "short_put_body", "put", 100),
        _leg("AAPL260215C00100000", "sell_to_open", "short_call_body", "call", 100),
        _leg("AAPL260215C00105000", "buy_to_open", "long_call_wing", "call", 105),
    ]


def _position(strategy: str = "iron_condor", *, quantity: int = 1) -> dict:
    return {
        "position_id": f"aapl-{strategy}",
        "strategy": strategy,
        "legs": _condor_legs() if strategy == "iron_condor" else _butterfly_legs(),
        "quantity": quantity,
        "initial_credit": 2.0,
        "opened_at": "2026-01-01T15:00:00Z",
    }


def _contract(leg: dict, bid: float, ask: float, delta: float) -> dict:
    return {
        "symbol": leg["symbol"], "underlying_symbol": "AAPL", "root_symbol": "AAPL",
        "expiration_date": EXPIRY, "type": leg["type"], "strike_price": leg["strike"],
        "style": "american", "multiplier": 100,
        "quote": {
            "bid": bid, "ask": ask, "bid_size": 10, "ask_size": 10,
            "timestamp": QUOTED_AT,
        },
        "greeks": {"delta": delta},
    }


def _chain(strategy: str = "iron_condor") -> dict:
    legs = _condor_legs() if strategy == "iron_condor" else _butterfly_legs()
    prices = (
        [(0.20, 0.25, -0.08), (0.70, 0.75, -0.20),
         (0.65, 0.70, 0.21), (0.25, 0.30, 0.09)]
        if strategy == "iron_condor" else
        [(0.30, 0.35, -0.12), (1.20, 1.25, -0.48),
         (1.10, 1.15, 0.47), (0.25, 0.30, 0.11)]
    )
    return {
        "status": "ok", "feed": "opra",
        "contracts": [_contract(leg, *price) for leg, price in zip(legs, prices)],
    }


def _payload(strategy: str = "iron_condor", *, spot: float = 100.0) -> dict:
    position = normalize_ledger_position(_position(strategy))
    return {
        "as_of": "2026-01-01T21:00:00Z",
        "positions": [position],
        "option_chains": {"AAPL": _chain(strategy)},
        "events": {"AAPL": {"status": "ok", "earnings_dates": [], "ex_dividend_dates": []}},
        "underlying_spots": {"AAPL": spot},
    }


def _account(legs: list[dict], quantity: int = 1) -> dict:
    return {
        "status": "ok", "as_of": "2026-01-01T21:00:00Z",
        "positions": {
            leg["symbol"]: {
                "symbol": leg["symbol"],
                "side": "short" if "sell" in leg["side"] else "long",
                "qty": quantity,
            }
            for leg in legs
        },
    }


class TestFourLegLedger(unittest.TestCase):
    def test_candidate_import_and_partial_close_preserve_all_legs(self):
        candidate = {"strategy": "iron_condor", "underlying_symbol": "AAPL", "legs": _condor_legs()}
        position = position_from_candidate(
            candidate, quantity=2, initial_credit=2.0, opened_at="2026-01-01T15:00:00Z",
        )
        with tempfile.TemporaryDirectory() as directory:
            ledger = OptionsLedger(os.path.join(directory, "ledger.sqlite3"))
            added = ledger.add(position)
            self.assertEqual(len(added["legs"]), 4)
            ledger.close(
                added["position_id"], closed_at="2026-01-05T20:00:00Z",
                close_debit=1.0, quantity=1,
            )
            listed = ledger.list("open")[0]
            self.assertEqual(listed["quantity"], 1)
            self.assertEqual(len(listed["legs"]), 4)
            performance = ledger.performance()
            self.assertEqual(performance["trades"][0]["max_risk"], 300.0)
            self.assertIn("iron_condor", performance["by_strategy"])

    def test_invalid_butterfly_body_and_calendar_are_rejected(self):
        position = _position("iron_butterfly")
        position["legs"][2] = _leg(
            "AAPL260215C00101000", "sell_to_open", "short_call_body", "call", 101,
        )
        with self.assertRaisesRegex(ValueError, "body strike"):
            normalize_ledger_position(position)
        with self.assertRaisesRegex(ValueError, "strategy must"):
            normalize_ledger_position({**_position(), "strategy": "calendar_iv"})


class TestFourLegReconciliation(unittest.TestCase):
    def test_all_four_legs_are_required_and_capacity_is_not_reused(self):
        position = normalize_ledger_position(_position())
        account = _account(_condor_legs())
        confirmations = reconcile_positions([position, position], account)
        self.assertEqual(confirmations[0]["status"], "confirmed")
        self.assertEqual(confirmations[0]["confirmed_legs"], 4)
        self.assertEqual(confirmations[1]["status"], "mismatch")
        account["positions"].pop("AAPL260215C00110000")
        mismatch = reconcile_positions([position], account)[0]
        self.assertEqual(mismatch["status"], "mismatch")
        self.assertTrue(any("long_call_wing" in item for item in mismatch["unconfirmed_legs"]))

    def test_live_hydration_fetches_full_strike_range(self):
        position = normalize_ledger_position(_position())
        captured = {}

        def chain_fetcher(symbol, expiration_gte, expiration_lte, **kwargs):
            captured.update(kwargs)
            return _chain()

        payload = hydrate_live_payload(
            [position],
            account_fetcher=lambda timeout: _account(_condor_legs()),
            chain_fetcher=chain_fetcher,
            snapshot_fetcher=lambda *args, **kwargs: {
                "status": "ok", "symbols": {"AAPL": {"latest_trade": {"price": 100.0}}},
            },
            events_fetcher=lambda *args: ({"AAPL": {
                "status": "ok", "earnings_dates": [], "ex_dividend_dates": [],
            }}, []),
            now_fn=lambda: dt.datetime(2026, 1, 1, 21, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(captured["strike_gte"], 90.0)
        self.assertEqual(captured["strike_lte"], 110.0)
        self.assertEqual(payload["positions"][0]["account_confirmation"]["status"], "confirmed")


class TestFourLegLifecycle(unittest.TestCase):
    def test_condor_natural_debit_and_profit_capture_use_all_four_legs(self):
        row = review_portfolio(_payload(), LifecycleConfig())["positions"][0]
        self.assertEqual(row["natural_close_debit"], 1.0)
        self.assertEqual(row["profit_capture_pct"], 50.0)
        self.assertEqual(row["action"], "止盈退出")
        self.assertEqual(set(row["short_deltas"]), {"short_put", "short_call"})

    def test_either_condor_boundary_or_short_delta_triggers_defense(self):
        row = review_portfolio(_payload(spot=94.0), LifecycleConfig())["positions"][0]
        self.assertIn("underlying_breached_short_strike", row["reasons"])
        payload = _payload()
        payload["option_chains"]["AAPL"]["contracts"][2]["greeks"]["delta"] = 0.50
        row = review_portfolio(payload, LifecycleConfig())["positions"][0]
        self.assertIn("short_delta_defense_triggered", row["reasons"])

    def test_missing_protective_leg_fails_closed_but_known_breach_survives(self):
        payload = _payload(spot=94.0)
        payload["option_chains"]["AAPL"]["contracts"].pop()
        row = review_portfolio(payload, LifecycleConfig())["positions"][0]
        self.assertEqual(row["action"], "立即退出评估")
        self.assertIn("underlying_breached_short_strike", row["reasons"])
        self.assertIn("current_leg_quotes_unavailable", row["data_warnings"])

    def test_butterfly_break_even_and_pin_risk_are_explicit(self):
        row = review_portfolio(_payload("iron_butterfly", spot=97.5), LifecycleConfig())["positions"][0]
        self.assertIn("underlying_breached_break_even", row["reasons"])
        payload = _payload("iron_butterfly", spot=100.2)
        payload["as_of"] = "2026-02-14T21:00:00Z"
        for contract in payload["option_chains"]["AAPL"]["contracts"]:
            contract["quote"]["timestamp"] = "2026-02-14T20:59:00Z"
        row = review_portfolio(payload, LifecycleConfig())["positions"][0]
        self.assertIn("expiration_pin_risk", row["reasons"])
        self.assertEqual(row["action"], "立即退出评估")

    def test_short_call_structures_apply_ex_dividend_risk(self):
        payload = _payload()
        payload["events"]["AAPL"]["ex_dividend_dates"] = ["2026-01-20"]
        row = review_portfolio(payload, LifecycleConfig())["positions"][0]
        self.assertIn("ex_dividend_before_expiration", row["reasons"])

    def test_report_renders_strategy_and_all_four_legs(self):
        result = review_portfolio(_payload(), LifecycleConfig())
        report = render_lifecycle_report(result)
        self.assertIn("Iron Condor", report)
        self.assertIn("90.00P+", report)
        self.assertIn("95.00P−", report)
        self.assertIn("105.00C−", report)
        self.assertIn("110.00C+", report)


if __name__ == "__main__":
    unittest.main()
