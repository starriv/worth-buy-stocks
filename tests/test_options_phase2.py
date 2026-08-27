#!/usr/bin/env python3
"""IV history, portfolio concentration, and lifecycle tests."""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import options_position_manager as manager  # noqa: E402
from options_engine.config import OptionsSellerConfig  # noqa: E402
from options_engine.iv_history import IVHistoryStore, constant_maturity_iv  # noqa: E402
from options_engine.ledger import OptionsLedger, normalize_ledger_position  # noqa: E402
from options_engine.lifecycle import LifecycleConfig, review_portfolio  # noqa: E402
from options_engine.lifecycle_live import hydrate_live_payload, reconcile_positions  # noqa: E402
from options_engine.lifecycle_notifications import notify_lifecycle  # noqa: E402
from options_engine.lifecycle_reporting import (  # noqa: E402
    build_lifecycle_alerts,
    render_lifecycle_report,
)
from options_engine.portfolio_risk import (  # noqa: E402
    evaluate_portfolio_risk,
    extract_defined_risk_spreads,
    parse_occ_symbol,
)


def _surface(as_of: str, iv: float) -> dict:
    return {
        "status": "ok",
        "as_of": as_of,
        "expirations": {
            "2026-03-01": {"dte": 20, "atm_iv": iv},
            "2026-03-21": {"dte": 40, "atm_iv": iv},
        },
    }


def _account(equity=10_000.0) -> dict:
    return {
        "status": "ok",
        "account": {"equity": equity},
        "positions": {
            "AAPL260215P00092000": {
                "symbol": "AAPL260215P00092000", "asset_class": "us_option",
                "side": "short", "qty": 1,
            },
            "AAPL260215P00090000": {
                "symbol": "AAPL260215P00090000", "asset_class": "us_option",
                "side": "long", "qty": 1,
            },
        },
    }


def _chain() -> dict:
    def contract(symbol, strike, bid, ask, delta):
        return {
            "symbol": symbol, "strike_price": strike, "multiplier": 100,
            "underlying_symbol": "AAPL", "root_symbol": "AAPL",
            "expiration_date": "2026-02-15", "type": "put", "style": "american",
            "quote": {
                "bid": bid, "ask": ask, "bid_size": 10, "ask_size": 10,
                "timestamp": "2026-01-01T20:59:00Z",
            },
            "greeks": {"delta": delta},
        }
    return {
        "status": "ok",
        "contracts": [
            contract("AAPL260215P00092000", 92, 0.75, 0.80, -0.22),
            contract("AAPL260215P00090000", 90, 0.20, 0.25, -0.14),
        ],
    }


def _lifecycle_payload(spot=105.0, initial_credit=1.5) -> dict:
    return {
        "as_of": "2026-01-01T21:00:00Z",
        "positions": [{
            "position_id": "aapl-put-1", "underlying_symbol": "AAPL",
            "strategy": "bull_put", "expiration_date": "2026-02-15",
            "short_strike": 92, "long_strike": 90, "quantity": 1,
            "initial_credit": initial_credit,
            "short_leg_symbol": "AAPL260215P00092000",
            "long_leg_symbol": "AAPL260215P00090000",
        }],
        "option_chains": {"AAPL": _chain()},
        "events": {"AAPL": {
            "status": "ok", "earnings_dates": [], "ex_dividend_dates": [],
        }},
        "underlying_spots": {"AAPL": spot},
    }


def _ledger_position() -> dict:
    return {
        "underlying_symbol": "AAPL", "strategy": "bull_put",
        "expiration_date": "2026-02-15",
        "short_leg_symbol": "AAPL260215P00092000",
        "long_leg_symbol": "AAPL260215P00090000",
        "short_strike": 92, "long_strike": 90,
        "quantity": 1, "initial_credit": 0.50,
        "opened_at": "2026-01-01T15:00:00Z",
    }


class TestIVHistory(unittest.TestCase):
    def test_constant_maturity_interpolates_total_variance(self):
        result = constant_maturity_iv(_surface("2026-01-01T21:00:00Z", 0.30))
        self.assertEqual(result["method"], "total_variance_interpolation")
        self.assertAlmostEqual(result["iv"], 0.30, places=6)

    def test_history_only_uses_prior_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            store = IVHistoryStore(os.path.join(directory, "iv.sqlite3"))
            start = dt.date(2025, 12, 1)
            for index in range(20):
                date = start + dt.timedelta(days=index)
                store.record(
                    "AAPL", _surface(date.isoformat() + "T21:00:00Z", 0.20 + index / 1000),
                    feed="opra",
                )
            context = store.history_context(
                "AAPL", 0.25, current_session="2026-01-01", min_observations=20,
            )
            self.assertEqual(context["status"], "ok")
            self.assertEqual(context["observations"], 20)
            self.assertEqual(context["iv_percentile"], 100.0)


class TestPortfolioRisk(unittest.TestCase):
    def test_occ_parser_and_vertical_pairing(self):
        parsed = parse_occ_symbol("AAPL260215P00092000")
        self.assertEqual(parsed["underlying_symbol"], "AAPL")
        self.assertEqual(parsed["strike"], 92.0)
        spreads = extract_defined_risk_spreads(_account())["spreads"]
        self.assertEqual(len(spreads), 1)
        self.assertEqual(spreads[0]["conservative_max_risk"], 200.0)

    def test_existing_symbol_risk_blocks_new_candidate(self):
        risk = evaluate_portfolio_risk(
            {"underlying_symbol": "AAPL", "max_loss": 150.0},
            _account(), {}, OptionsSellerConfig(),
        )
        self.assertIn(
            "portfolio_symbol_concentration_above_limit", risk["hard_gates"]
        )
        self.assertEqual(risk["symbol_risk_pct"], 3.5)

    def test_cautious_regime_scales_every_portfolio_limit(self):
        account = {"status": "ok", "account": {"equity": 10_000}, "positions": {}}
        risk = evaluate_portfolio_risk(
            {
                "underlying_symbol": "AAPL", "strategy": "bull_put",
                "expiration_date": "2026-02-15", "max_loss": 150.0,
            },
            account, {}, OptionsSellerConfig(),
            {"status": "ok", "state": "cautious", "risk_multiplier": 0.5},
        )
        self.assertEqual(risk["effective_limits"]["symbol_risk_pct"], 1.0)
        self.assertEqual(risk["effective_limits"]["total_option_risk_pct"], 3.0)
        self.assertIn("portfolio_symbol_concentration_above_limit", risk["hard_gates"])
        self.assertIn("portfolio_expiration_concentration_above_limit", risk["hard_gates"])

    def test_same_expiration_risk_has_an_independent_limit(self):
        risk = evaluate_portfolio_risk(
            {
                "underlying_symbol": "MSFT", "strategy": "bull_put",
                "expiration_date": "2026-02-15", "max_loss": 50.0,
            },
            _account(), {}, OptionsSellerConfig(),
            {"status": "ok", "state": "normal", "risk_multiplier": 1.0},
        )
        self.assertEqual(risk["expiration_risk_pct"], 2.5)
        self.assertIn("portfolio_expiration_concentration_above_limit", risk["hard_gates"])

    def test_opposite_strategy_direction_turns_negative_underlying_correlation_into_loss_correlation(self):
        account = {
            "status": "ok", "account": {"equity": 100_000},
            "positions": {
                "TLT260215C00100000": {
                    "asset_class": "us_option", "side": "short", "qty": 1,
                },
                "TLT260215C00110000": {
                    "asset_class": "us_option", "side": "long", "qty": 1,
                },
            },
        }
        dates = [dt.date(2025, 10, 1) + dt.timedelta(days=index) for index in range(61)]
        aapl_prices = [100.0]
        tlt_prices = [100.0]
        for index in range(1, len(dates)):
            daily_return = 0.01 if index % 2 else -0.01
            aapl_prices.append(aapl_prices[-1] * (1.0 + daily_return))
            tlt_prices.append(tlt_prices[-1] * (1.0 - daily_return))
        bars = {
            "AAPL": [{"t": day.isoformat(), "c": aapl_prices[index]} for index, day in enumerate(dates)],
            "TLT": [{"t": day.isoformat(), "c": tlt_prices[index]} for index, day in enumerate(dates)],
        }
        cfg = OptionsSellerConfig(
            max_symbol_option_risk_pct=1.0,
            max_correlated_option_risk_pct=1.0,
        )
        risk = evaluate_portfolio_risk(
            {
                "underlying_symbol": "AAPL", "strategy": "bull_put",
                "expiration_date": "2026-03-20", "max_loss": 500.0,
            },
            account, bars, cfg,
            {"status": "ok", "state": "normal", "risk_multiplier": 1.0},
        )
        self.assertLess(risk["correlations"]["TLT"], -0.9)
        self.assertGreater(risk["loss_correlations"]["TLT"], 0.9)
        self.assertIn("portfolio_correlated_concentration_above_limit", risk["hard_gates"])


class TestLedgerAndLiveHydration(unittest.TestCase):
    def test_ledger_add_list_close_and_review_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = OptionsLedger(os.path.join(directory, "ledger.sqlite3"))
            added = ledger.add(_ledger_position())
            self.assertEqual(added["status"], "open")
            with self.assertRaises(ValueError):
                ledger.add(_ledger_position())
            self.assertEqual(len(ledger.list("open")), 1)
            count = ledger.record_reviews("2026-01-02T21:00:00Z", [{
                "position_id": added["position_id"], "action": "继续持有",
            }])
            self.assertEqual(count, 1)
            self.assertEqual(len(ledger.reviews(added["position_id"])), 1)
            closed = ledger.close(
                added["position_id"], closed_at="2026-01-10T20:00:00Z", close_debit=0.20,
            )
            self.assertEqual(closed["status"], "closed")
            self.assertEqual(ledger.list("open"), [])

    def test_performance_uses_actual_fills_and_links_prior_reviews(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = OptionsLedger(os.path.join(directory, "ledger.sqlite3"))
            first = ledger.add(_ledger_position())
            second_input = _ledger_position()
            second_input["opened_at"] = "2026-01-02T15:00:00Z"
            second = ledger.add(second_input)
            ledger.record_reviews("2026-01-05T20:00:00Z", [{
                "position_id": first["position_id"], "action": "止盈退出",
            }])
            ledger.close(
                first["position_id"], closed_at="2026-01-06T20:00:00Z", close_debit=0.20,
            )
            ledger.close(
                second["position_id"], closed_at="2026-01-07T20:00:00Z", close_debit=0.80,
            )
            result = ledger.performance()
            self.assertEqual(result["summary"]["closed_positions"], 2)
            self.assertEqual(result["summary"]["wins"], 1)
            self.assertEqual(result["summary"]["losses"], 1)
            self.assertEqual(result["summary"]["net_pnl"], 0.0)
            self.assertEqual(result["management_reviews"]["realized_close_records_with_reviews"], 1)
            self.assertEqual(result["management_reviews"]["first_exit_action_counts"], {"止盈退出": 1})
            self.assertEqual(result["evidence"]["status"], "insufficient_history")

    def test_partial_close_preserves_remaining_quantity_and_fill_performance(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = OptionsLedger(os.path.join(directory, "ledger.sqlite3"))
            position = _ledger_position()
            position["quantity"] = 2
            added = ledger.add(position)
            partial = ledger.close(
                added["position_id"], closed_at="2026-01-06T20:00:00Z",
                close_debit=0.20, quantity=1,
            )
            self.assertEqual(partial["status"], "open")
            self.assertEqual(partial["remaining_quantity"], 1)
            open_row = ledger.list("open")[0]
            self.assertEqual(open_row["opened_quantity"], 2)
            self.assertEqual(open_row["quantity"], 1)
            partial_performance = ledger.performance()
            self.assertEqual(partial_performance["summary"]["realized_close_records"], 1)
            self.assertEqual(partial_performance["summary"]["partial_open_positions"], 1)
            completed = ledger.close(
                added["position_id"], closed_at="2026-01-07T20:00:00Z",
                close_debit=0.30, quantity=1,
            )
            self.assertEqual(completed["status"], "closed")
            result = ledger.performance()
            self.assertEqual(result["summary"]["realized_close_records"], 2)
            self.assertEqual(result["summary"]["closed_positions"], 1)
            self.assertEqual(result["summary"]["net_pnl"], 50.0)

    def test_legacy_full_close_is_migrated_to_closure_record(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "legacy.sqlite3")
            with sqlite3.connect(path) as connection:
                connection.executescript("""
                    CREATE TABLE option_spreads (
                        position_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                        underlying_symbol TEXT NOT NULL, strategy TEXT NOT NULL,
                        expiration_date TEXT NOT NULL, short_leg_symbol TEXT NOT NULL,
                        long_leg_symbol TEXT NOT NULL, short_strike REAL NOT NULL,
                        long_strike REAL NOT NULL, quantity INTEGER NOT NULL,
                        initial_credit REAL NOT NULL, opened_at TEXT NOT NULL,
                        status TEXT NOT NULL, closed_at TEXT, close_debit REAL,
                        notes TEXT NOT NULL, created_at TEXT NOT NULL
                    );
                    CREATE TABLE option_spread_reviews (
                        review_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        position_id TEXT NOT NULL, reviewed_at TEXT NOT NULL,
                        action TEXT NOT NULL, payload_json TEXT NOT NULL
                    );
                """)
                connection.execute(
                    """INSERT INTO option_spreads VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        "legacy", "legacy-fingerprint", "AAPL", "bull_put", "2026-02-15",
                        "AAPL260215P00092000", "AAPL260215P00090000", 92, 90, 1,
                        0.5, "2026-01-01T15:00:00Z", "closed",
                        "2026-01-10T20:00:00Z", 0.2, "", "2026-01-01T15:00:00Z",
                    ),
                )
            result = OptionsLedger(path).performance()
            self.assertEqual(result["summary"]["realized_close_records"], 1)
            self.assertEqual(result["summary"]["net_pnl"], 30.0)
            self.assertEqual(OptionsLedger(path).list("closed")[0]["remaining_quantity"], 0)

    def test_adjusted_occ_root_is_rejected(self):
        position = _ledger_position()
        position["short_leg_symbol"] = "AAPL1260215P00092000"
        with self.assertRaises(ValueError):
            normalize_ledger_position(position)

    def test_live_hydration_confirms_account_and_fills_market_data(self):
        position = normalize_ledger_position(_ledger_position())

        def account_fetcher(timeout):
            return _account(50_000)

        def chain_fetcher(*args, **kwargs):
            chain = _chain()
            chain["feed"] = "opra"
            return chain

        def snapshot_fetcher(*args, **kwargs):
            return {"status": "ok", "symbols": {
                "AAPL": {"latest_trade": {"price": 105.0}},
            }}

        def events_fetcher(symbols, start, end, args):
            return ({"AAPL": {
                "status": "ok", "earnings_dates": [], "ex_dividend_dates": [],
            }}, [])

        payload = hydrate_live_payload(
            [position], account_fetcher=account_fetcher,
            chain_fetcher=chain_fetcher, snapshot_fetcher=snapshot_fetcher,
            events_fetcher=events_fetcher,
            now_fn=lambda: dt.datetime(2026, 1, 1, 21, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(payload["positions"][0]["account_confirmation"]["status"], "confirmed")
        self.assertEqual(payload["underlying_spots"]["AAPL"], 105.0)
        self.assertEqual(payload["option_chains"]["AAPL"]["feed"], "opra")

    def test_account_quantity_cannot_confirm_two_ledger_rows(self):
        first = normalize_ledger_position(_ledger_position())
        second_input = _ledger_position()
        second_input["opened_at"] = "2026-01-02T15:00:00Z"
        second = normalize_ledger_position(second_input)
        confirmations = reconcile_positions([first, second], _account(50_000))
        self.assertEqual(confirmations[0]["status"], "confirmed")
        self.assertEqual(confirmations[1]["status"], "mismatch")

    def test_snapshot_and_event_exceptions_degrade_independently(self):
        position = normalize_ledger_position(_ledger_position())

        def snapshot_failure(*args, **kwargs):
            raise RuntimeError("snapshot down")

        def event_failure(*args, **kwargs):
            raise RuntimeError("events down")

        payload = hydrate_live_payload(
            [position], account_fetcher=lambda timeout: _account(50_000),
            chain_fetcher=lambda *args, **kwargs: _chain(),
            snapshot_fetcher=snapshot_failure, events_fetcher=event_failure,
            now_fn=lambda: dt.datetime(2026, 1, 1, 21, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(payload["snapshot_status"], "unavailable")
        self.assertEqual(payload["events"]["AAPL"]["status"], "unavailable")
        self.assertNotIn("AAPL", payload["underlying_spots"])
        self.assertTrue(any("Snapshot" in flag for flag in payload["data_flags"]))
        self.assertTrue(any("事件日历" in flag for flag in payload["data_flags"]))


class TestLifecycle(unittest.TestCase):
    def test_profit_capture_generates_exit_plan(self):
        result = review_portfolio(_lifecycle_payload(), LifecycleConfig())
        row = result["positions"][0]
        self.assertEqual(row["action"], "止盈退出")
        self.assertGreaterEqual(row["profit_capture_pct"], 50)

    def test_short_strike_breach_has_priority(self):
        result = review_portfolio(_lifecycle_payload(spot=90.0), LifecycleConfig())
        row = result["positions"][0]
        self.assertEqual(row["action"], "立即退出评估")
        self.assertIn("underlying_breached_short_strike", row["reasons"])

    def test_market_date_does_not_expire_position_on_prior_new_york_evening(self):
        payload = _lifecycle_payload()
        payload["as_of"] = "2026-02-13T01:00:00Z"  # Feb 12, 20:00 New York
        payload["positions"][0]["expiration_date"] = "2026-02-13"
        row = review_portfolio(payload, LifecycleConfig())['positions'][0]
        self.assertEqual(row["dte"], 1)
        self.assertNotIn("expiration_reached", row["reasons"])

    def test_seven_dte_enters_hard_exit_window(self):
        payload = _lifecycle_payload(initial_credit=0.8)
        payload["as_of"] = "2026-02-08T21:00:00Z"
        for contract in payload["option_chains"]["AAPL"]["contracts"]:
            contract["quote"]["timestamp"] = "2026-02-08T20:59:00Z"
        row = review_portfolio(payload, LifecycleConfig())["positions"][0]
        self.assertEqual(row["dte"], 7)
        self.assertEqual(row["action"], "立即退出评估")
        self.assertIn("expiration_hard_exit_window", row["reasons"])

    def test_known_breach_survives_event_calendar_failure(self):
        payload = _lifecycle_payload(spot=90.0)
        payload["events"]["AAPL"] = {"status": "unavailable"}
        result = review_portfolio(payload, LifecycleConfig())
        row = result["positions"][0]
        self.assertEqual(row["action"], "立即退出评估")
        self.assertIn("underlying_breached_short_strike", row["reasons"])
        self.assertIn("event_calendar_unavailable", row["data_warnings"])
        self.assertEqual(result["counts"]["data_warning_positions"], 1)

    def test_intraday_stale_quotes_are_rejected(self):
        payload = _lifecycle_payload(initial_credit=0.8)
        payload["as_of"] = "2026-01-01T20:00:00Z"
        for contract in payload["option_chains"]["AAPL"]["contracts"]:
            contract["quote"]["timestamp"] = "2026-01-01T17:00:00Z"
        row = review_portfolio(payload, LifecycleConfig())["positions"][0]
        self.assertEqual(row["action"], "数据不足")
        self.assertIn("current_quote_stale", row["reasons"])

    def test_cross_leg_market_and_zero_size_are_rejected(self):
        payload = _lifecycle_payload(initial_credit=0.8)
        payload["option_chains"]["AAPL"]["contracts"][1]["quote"].update({
            "bid": 0.90, "ask": 0.95,
        })
        row = review_portfolio(payload, LifecycleConfig())["positions"][0]
        self.assertEqual(row["action"], "数据不足")
        self.assertIn("current_close_debit_invalid", row["reasons"])
        payload = _lifecycle_payload(initial_credit=0.8)
        payload["option_chains"]["AAPL"]["contracts"][0]["quote"]["ask_size"] = 0
        row = review_portfolio(payload, LifecycleConfig())["positions"][0]
        self.assertIn("current_quote_or_delta_invalid", row["reasons"])

    def test_premarket_uses_previous_business_day_quote(self):
        payload = _lifecycle_payload(initial_credit=0.8)
        payload["as_of"] = "2026-01-05T13:00:00Z"
        for contract in payload["option_chains"]["AAPL"]["contracts"]:
            contract["quote"]["timestamp"] = "2026-01-02T20:59:00Z"
        row = review_portfolio(payload, LifecycleConfig())["positions"][0]
        self.assertNotEqual(row["action"], "数据不足")

    def test_data_failure_keeps_position_identity_for_audit(self):
        payload = _lifecycle_payload()
        payload["option_chains"] = {"AAPL": {"status": "unavailable"}}
        row = review_portfolio(payload, LifecycleConfig())["positions"][0]
        self.assertEqual(row["action"], "数据不足")
        self.assertEqual(row["position_id"], "aapl-put-1")
        self.assertEqual(row["underlying_symbol"], "AAPL")

    def test_cli_is_offline_and_emits_json(self):
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        json.dump(_lifecycle_payload(), handle)
        handle.close()
        self.addCleanup(lambda: os.path.exists(handle.name) and os.unlink(handle.name))
        output = io.StringIO()
        with redirect_stdout(output):
            rc = manager.main(["review", "--input", handle.name])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(output.getvalue())["counts"]["planned_exit"], 1)

    def test_cli_offline_rejects_ledger_and_notification_side_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path = os.path.join(directory, "input.json")
            ledger_path = os.path.join(directory, "must-not-exist.sqlite3")
            with open(input_path, "w", encoding="utf-8") as handle:
                json.dump(_lifecycle_payload(), handle)
            with self.assertRaisesRegex(ValueError, "offline review cannot read"):
                manager.main([
                    "review", "--input", input_path, "--ledger-db", ledger_path,
                    "--include-performance",
                ])
            self.assertFalse(os.path.exists(ledger_path))
            with self.assertRaisesRegex(ValueError, "offline review cannot send"):
                manager.main(["review", "--input", input_path, "--notify", "on"])

    def test_cli_add_and_list_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            position_path = os.path.join(directory, "position.json")
            ledger_path = os.path.join(directory, "ledger.sqlite3")
            with open(position_path, "w", encoding="utf-8") as handle:
                json.dump(_ledger_position(), handle)
            added_output = io.StringIO()
            with redirect_stdout(added_output):
                rc = manager.main([
                    "add", "--input", position_path, "--ledger-db", ledger_path,
                ])
            self.assertEqual(rc, 0)
            added = json.loads(added_output.getvalue())
            self.assertEqual(added["position"]["status"], "open")
            listed_output = io.StringIO()
            with redirect_stdout(listed_output):
                rc = manager.main(["list", "--ledger-db", ledger_path])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(listed_output.getvalue())["count"], 1)

    def test_alert_and_chinese_report_are_derived_from_review(self):
        result = review_portfolio(_lifecycle_payload(spot=90.0), LifecycleConfig())
        result["data_flags"] = []
        alerts = build_lifecycle_alerts(result)
        self.assertEqual(alerts[0]["severity"], "critical")
        self.assertIn("标的价格已越过短腿执行价", alerts[0]["message"])
        report = render_lifecycle_report(result)
        self.assertIn("定义风险信用价差生命周期日报", report)
        self.assertIn("立即退出评估", report)
        self.assertIn("aapl-put-1", report)
        self.assertIn("2026-02-15", report)
        self.assertIn("92.00/90.00P", report)
        self.assertIn("只读检查", report)

    def test_notification_is_explicit_and_best_effort(self):
        result = review_portfolio(_lifecycle_payload(), LifecycleConfig())
        calls = []

        def fake_env(name):
            return {"TELEGRAM_BOT_TOKEN": "secret", "TELEGRAM_CHAT_ID": "123"}[name]

        def fake_send(token, chat_id, text, parse_mode, timeout):
            calls.append((token, chat_id, text, parse_mode, timeout))
            return {"ok": True, "result": {"message_id": 9}}

        self.assertEqual(notify_lifecycle(result)["status"], "disabled")
        sent = notify_lifecycle(
            result, mode="alerts", sender=fake_send, env_getter=fake_env,
        )
        self.assertEqual(sent, {"status": "sent", "message_id": 9})
        self.assertEqual(len(calls), 1)
        self.assertNotIn("secret", calls[0][2])

        def failed_send(*args):
            raise RuntimeError("https://api.telegram.org/botsecret/sendMessage")

        failed = notify_lifecycle(
            result, mode="on", sender=failed_send, env_getter=fake_env,
        )
        self.assertEqual(failed["reason"], "telegram_send_failed")
        self.assertNotIn("secret", json.dumps(failed))

    def test_cli_writes_explicit_report_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path = os.path.join(directory, "lifecycle.json")
            with open(input_path, "w", encoding="utf-8") as handle:
                json.dump(_lifecycle_payload(), handle)
            output = io.StringIO()
            with redirect_stdout(output):
                rc = manager.main([
                    "review", "--input", input_path, "--report-dir", directory,
                    "--report-format", "both",
                ])
            result = json.loads(output.getvalue())
            self.assertEqual(rc, 0)
            self.assertEqual(len(result["report_files"]), 2)
            self.assertTrue(all(os.path.exists(path) for path in result["report_files"]))

    def test_cli_performance_command(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = os.path.join(directory, "ledger.sqlite3")
            ledger = OptionsLedger(ledger_path)
            added = ledger.add(_ledger_position())
            ledger.close(
                added["position_id"], closed_at="2026-01-10T20:00:00Z", close_debit=0.20,
            )
            output = io.StringIO()
            with redirect_stdout(output):
                rc = manager.main(["performance", "--ledger-db", ledger_path])
            result = json.loads(output.getvalue())
            self.assertEqual(rc, 0)
            self.assertEqual(result["summary"]["net_pnl"], 30.0)

    def test_cli_partial_close_forwards_quantity(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = os.path.join(directory, "ledger.sqlite3")
            position = _ledger_position()
            position["quantity"] = 2
            added = OptionsLedger(ledger_path).add(position)
            output = io.StringIO()
            with redirect_stdout(output):
                rc = manager.main([
                    "close", "--ledger-db", ledger_path,
                    "--position-id", added["position_id"], "--quantity", "1",
                    "--closed-at", "2026-01-10T20:00:00Z", "--close-debit", "0.20",
                ])
            result = json.loads(output.getvalue())["position"]
            self.assertEqual(rc, 0)
            self.assertEqual(result["status"], "open")
            self.assertEqual(result["remaining_quantity"], 1)


if __name__ == "__main__":
    unittest.main()
