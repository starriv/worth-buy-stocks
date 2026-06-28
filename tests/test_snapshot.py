#!/usr/bin/env python3
"""fetching.fetch_snapshots 与 snapshot 契约测试。无真实网络/CLI 调用。"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import fetching as F  # noqa: E402
import agent_contracts as AC  # noqa: E402
import indicators as I  # noqa: E402


def _snap_raw():
    # 模拟 alpaca multi-snapshots 单 symbol 输出（symbol 为顶层 key，此处返回 value）
    return {
        "dailyBar": {"c": 195.5, "h": 196.0, "l": 193.0, "o": 193.5, "v": 12345678,
                     "vw": 194.8, "t": "2026-06-26T04:00:00Z"},
        "latestQuote": {"bp": 195.4, "ap": 195.6, "bs": 100, "as": 200,
                        "t": "2026-06-26T20:00:00Z"},
        "latestTrade": {"p": 195.5, "s": 50, "t": "2026-06-26T19:30:00Z"},
        "minuteBar": {"o": 195.4, "h": 195.6, "l": 195.3, "c": 195.5,
                      "t": "2026-06-26T20:30:00Z"},
    }


class TestNormalizeSnapshot(unittest.TestCase):
    def test_extracts_daily_change_spread_and_trade(self):
        norm = F._normalize_snapshot("AAPL", _snap_raw())
        self.assertEqual(norm["symbol"], "AAPL")
        self.assertEqual(norm["daily_change_pct"], 1.03)  # (195.5/193.5-1)*100
        self.assertEqual(norm["daily_bar"]["close"], 195.5)
        self.assertEqual(norm["spread"], 0.2)
        self.assertEqual(norm["latest_trade"]["price"], 195.5)

    def test_partial_bar_only_still_normalizes(self):
        raw = {"dailyBar": {"c": 100.0, "o": 99.0, "h": 101, "l": 98, "t": "2026-06-26T04:00:00Z"}}
        norm = F._normalize_snapshot("SPY", raw)
        self.assertEqual(norm["daily_change_pct"], 1.01)
        self.assertNotIn("quote", norm)

    def test_empty_raw_returns_none(self):
        self.assertIsNone(F._normalize_snapshot("AAPL", {}))
        self.assertIsNone(F._normalize_snapshot("AAPL", None))


class TestFetchSnapshots(unittest.TestCase):
    def test_ok_normalizes_all_symbols_and_uppercases(self):
        raw = {"AAPL": _snap_raw(), "SPY": _snap_raw()}
        with patch("fetching._run_json", return_value=raw):
            ctx = F.fetch_snapshots(["aapl", "spy"], feed="iex")
        self.assertEqual(ctx["status"], "ok")
        self.assertEqual(list(ctx["symbols"].keys()), ["AAPL", "SPY"])  # 保序去重
        self.assertTrue(ctx["as_of"].endswith("Z"))

    def test_empty_symbols_is_unavailable(self):
        with patch("fetching._run_json") as run:
            ctx = F.fetch_snapshots([], feed="iex")
        self.assertEqual(ctx["status"], "unavailable")
        run.assert_not_called()

    def test_run_json_failure_is_unavailable(self):
        with patch("fetching._run_json", side_effect=RuntimeError("boom")):
            ctx = F.fetch_snapshots(["AAPL"], feed="iex")
        self.assertEqual(ctx["status"], "unavailable")
        self.assertIn("boom", ctx["reason"])

    def test_empty_payload_is_unavailable(self):
        with patch("fetching._run_json", return_value={}):
            ctx = F.fetch_snapshots(["AAPL"], feed="iex")
        self.assertEqual(ctx["status"], "unavailable")


class TestSnapshotContract(unittest.TestCase):
    def test_validate_ok_payload(self):
        payload = {"status": "ok", "as_of": "2026-06-26T00:00:00Z", "feed": "iex",
                   "symbols": {"AAPL": {"symbol": "AAPL"}}}
        summary = AC.validate_payload(AC.KIND_SNAPSHOT, payload)
        self.assertEqual(summary["symbols_count"], 1)

    def test_validate_unavailable_status(self):
        payload = {"status": "unavailable", "reason": "boom"}
        summary = AC.validate_payload(AC.KIND_SNAPSHOT, payload)
        self.assertEqual(summary["symbols_count"], 0)

    def test_validate_rejects_non_dict_symbols(self):
        from agent_contracts import ContractError
        payload = {"status": "ok", "symbols": "not-an-object"}
        with self.assertRaises(ContractError):
            AC.validate_payload(AC.KIND_SNAPSHOT, payload)


class TestIndicatorsSnapshotLoader(unittest.TestCase):
    def test_load_snapshot_context_file(self):
        payload = {"status": "ok", "as_of": "2026-06-26T00:00:00Z", "feed": "iex",
                   "symbols": {"AAPL": {"symbol": "AAPL", "daily_bar": {"close": 195.5}}}}
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
            json.dump(payload, f)
            path = f.name
        try:
            ctx = I._load_snapshot_context(path)
        finally:
            os.unlink(path)
        self.assertEqual(ctx["status"], "ok")
        self.assertEqual(ctx["symbols"]["AAPL"]["daily_bar"]["close"], 195.5)

    def test_invalid_snapshot_context_returns_unavailable(self):
        invalid = {"status": "ok", "symbols": "not-an-object"}
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
            json.dump(invalid, f)
            path = f.name
        try:
            ctx = I._load_snapshot_context(path)
        finally:
            os.unlink(path)
        self.assertEqual(ctx["status"], "unavailable")

    def test_snapshot_off_does_not_fetch(self):
        from types import SimpleNamespace
        args = SimpleNamespace(
            snapshot_context_file=None,
            snapshot="off",
            input=None,
            feed="iex",
            timeout=30,
        )
        with patch("indicators.fetch_snapshots") as fetch:
            self.assertIsNone(I._snapshot_context_from_args(args, ["AAPL"]))
        fetch.assert_not_called()

    def test_snapshot_auto_without_network_offline_path_returns_none(self):
        from types import SimpleNamespace
        # 离线模式（input set）+ auto → 默认不触网
        args = SimpleNamespace(
            snapshot_context_file=None,
            snapshot="auto",
            input="-",
            feed="iex",
            timeout=30,
        )
        with patch("indicators.fetch_snapshots") as fetch:
            self.assertIsNone(I._snapshot_context_from_args(args, ["AAPL"]))
        fetch.assert_not_called()


class TestBuildResultSnapshots(unittest.TestCase):
    def _bars(self, closes, start="2024-01-02"):
        from datetime import date, timedelta
        out, d = [], date.fromisoformat(start)
        for i, c in enumerate(closes):
            while d.isoweekday() > 5:
                d += timedelta(days=1)
            out.append({"t": d.isoformat() + "T04:00:00Z", "o": c, "h": c + 1,
                        "l": c - 1, "c": c, "v": 1_000_000 + i})
            d += timedelta(days=1)
        return out

    def test_snapshot_written_to_supplemental_and_per_symbol(self):
        closes = [100.0 * (1.004 ** i) for i in range(260)]
        flat = [400.0 * (1.0005 ** i) for i in range(260)]
        bars = {"WIN": self._bars(closes), "SPY": self._bars(flat), "QQQ": self._bars(flat)}
        snapshot_context = {
            "status": "ok", "as_of": "2026-06-26T00:00:00Z", "feed": "iex",
            "symbols": {"WIN": {"symbol": "WIN", "daily_bar": {"close": 195.5}}},
        }
        from pipeline import build_result
        res = build_result(["WIN", "SPY", "QQQ"], bars, "iex", "split",
                           snapshot_context=snapshot_context)
        # 顶层 summary
        self.assertEqual(res["supplemental"]["snapshots"]["status"], "ok")
        self.assertEqual(res["supplemental"]["snapshots"]["symbols_count"], 1)
        # per-symbol
        self.assertEqual(res["symbols"]["WIN"]["supplemental"]["snapshot"]["daily_bar"]["close"], 195.5)

    def test_snapshot_unavailable_is_not_written(self):
        closes = [100.0 * (1.004 ** i) for i in range(260)]
        flat = [400.0 * (1.0005 ** i) for i in range(260)]
        bars = {"WIN": self._bars(closes), "SPY": self._bars(flat), "QQQ": self._bars(flat)}
        snapshot_context = {"status": "unavailable", "reason": "boom"}
        from pipeline import build_result
        res = build_result(["WIN", "SPY", "QQQ"], bars, "iex", "split",
                           snapshot_context=snapshot_context)
        # 顶层无 snapshots；评分照常
        self.assertNotIn("snapshots", res.get("supplemental", {}))
        self.assertIn("score", res["symbols"]["WIN"])

    def test_snapshot_none_does_not_break_scoring(self):
        closes = [100.0 * (1.004 ** i) for i in range(260)]
        flat = [400.0 * (1.0005 ** i) for i in range(260)]
        bars = {"WIN": self._bars(closes), "SPY": self._bars(flat), "QQQ": self._bars(flat)}
        from pipeline import build_result
        res = build_result(["WIN", "SPY", "QQQ"], bars, "iex", "split", snapshot_context=None)
        self.assertIn("score", res["symbols"]["WIN"])


if __name__ == "__main__":
    unittest.main()
