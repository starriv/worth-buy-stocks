#!/usr/bin/env python3
"""Tests for the display-only AI opinion overlay (no real network)."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import agent_contracts as C  # noqa: E402
import ai_opinion  # noqa: E402


def _analysis(verdict="是"):
    return {
        "last_close": 100.0,
        "last_date": "2026-07-07",
        "bars_count": 300,
        "ma": {"MA60": 95.0, "MA200": 90.0, "above_MA60": True, "above_MA200": True},
        "weekly": {"bearish_alignment": False},
        "structure_30d": {"range_position_pct": 80.0, "dist_to_30d_high_pct": -1.5},
        "returns_pct": {"r1m_21d": 4.0, "r3m_63d": 12.0, "r6m_126d": 25.0},
        "relative_strength_pct": {"SPY": {"r3m_63d": 5.0}, "QQQ": {"r3m_63d": 3.0}},
        "momentum": {"m12_1_pct": 30.0},
        "volatility": {"ann_vol_3m_pct": 25.0},
        "risk": {"max_drawdown_6m_pct": -12.0},
        "trend_quality": {"adx": 28.0},
        "macd": {"hist": 0.5},
        "rsi": {"RSI14": 62.0},
        "kdj": {"K": 70.0},
        "volume": {"ratio_vs_ma20": 1.2},
        "score": {
            "verdict": verdict,
            "composite": 81.0,
            "raw_composite": 85.0,
            "blocking_reasons": [],
            "trade_plan": {"entry": 99.0, "stop": 93.0},
            "account_overlay": {},
            "suggested_position_pct": 10,
            "data_flags": ["低流动性"],
        },
    }


def _result(verdict="是"):
    return {
        "feed": "iex",
        "adjustment": "split",
        "symbols": {
            "AAPL": _analysis(verdict),
            "SPY": {"last_close": 500.0, "score": {"verdict": "观察"}},
            "QQQ": {"last_close": 400.0},
        },
    }


def _model_reply(stance="buy", confidence="medium"):
    return {
        "stance": stance,
        "confidence": confidence,
        "view": "趋势与相对强度领先，量价确认充分",
        "key_risks": ["财报临近"],
    }


def _available(test):
    """Patch SDK/key availability on for the duration of a test."""
    for target, value in (("sdk_available", True), ("has_api_key", True)):
        p = mock.patch.object(ai_opinion, target, return_value=value)
        p.start()
        test.addCleanup(p.stop)


class TestEvidencePayload(unittest.TestCase):
    def test_evidence_never_contains_conclusion_fields(self):
        """防锚定红线：证据里不得出现 verdict/composite/trade_plan 等结论字段。"""
        evidence = ai_opinion.build_evidence(
            _analysis(), market_risk_off=None,
            news_context={"red_flags": [{"type": "dilution", "severity": "medium", "note": "x"}]},
        )
        blob = json.dumps(evidence, ensure_ascii=False)
        for forbidden in ("verdict", "composite", "trade_plan", "suggested_position",
                          "blocking_reasons", "account_overlay", "score"):
            self.assertNotIn(forbidden, blob, f"evidence must not contain {forbidden}")

    def test_evidence_keeps_whitelisted_indicators_and_facts(self):
        evidence = ai_opinion.build_evidence(
            _analysis(), market_risk_off=True,
            news_context={"red_flags": [{"type": "dilution"}], "catalyst": "earnings"},
        )
        for key in ("ma", "weekly", "structure_30d", "relative_strength_pct", "volume"):
            self.assertIn(key, evidence)
        self.assertTrue(evidence["market_risk_off"])
        self.assertEqual(evidence["news"]["catalyst"], "earnings")
        self.assertEqual(evidence["data_flags"], ["低流动性"])


class TestAgreement(unittest.TestCase):
    def test_mapping_table(self):
        cases = [
            ("是", "buy", "agree"), ("是", "watch", "diverge"), ("是", "avoid", "diverge"),
            ("观察", "watch", "agree"), ("观察", "buy", "diverge"),
            ("否", "avoid", "agree"), ("否", "buy", "diverge"),
            ("持仓需减风险", "avoid", "agree"), ("持仓需减风险", "buy", "diverge"),
            ("无法评分", "buy", "not_applicable"), (None, "buy", "not_applicable"),
        ]
        for verdict, stance, expected in cases:
            self.assertEqual(
                ai_opinion.compute_agreement(verdict, stance), expected,
                f"verdict={verdict} stance={stance}",
            )

    def test_unavailable_opinions_checks_each_requested_symbol(self):
        artifact = {
            "status": "ok",
            "symbols": {
                "AAPL": {"status": "ok"},
                "MSFT": {"status": "unavailable", "reason": "timeout"},
            },
        }
        self.assertEqual(
            ai_opinion.unavailable_opinions(artifact, ["AAPL", "MSFT", "NVDA"]),
            {"MSFT": "timeout", "NVDA": "AI 意见条目缺失"},
        )


class TestOpinionsForResult(unittest.TestCase):
    def test_no_key_returns_unavailable_without_calling_model(self):
        with mock.patch.object(ai_opinion, "sdk_available", return_value=True), \
             mock.patch.object(ai_opinion, "has_api_key", return_value=False), \
             mock.patch.object(ai_opinion, "_call_model") as call:
            artifact = ai_opinion.opinions_for_result(_result())
        self.assertEqual(artifact["status"], "unavailable")
        call.assert_not_called()

    def test_missing_sdk_returns_unavailable(self):
        with mock.patch.object(ai_opinion, "sdk_available", return_value=False):
            artifact = ai_opinion.opinions_for_result(_result())
        self.assertEqual(artifact["status"], "unavailable")
        self.assertIn("SDK", artifact["reason"])

    def test_ok_path_skips_benchmarks_and_validates_contract(self):
        _available(self)
        with mock.patch.object(ai_opinion, "_call_model", return_value=_model_reply()) as call:
            artifact = ai_opinion.opinions_for_result(_result())
        C.validate_payload(C.KIND_AI_OPINION, artifact)
        self.assertEqual(set(artifact["symbols"]), {"AAPL"})  # SPY/QQQ 不调用
        self.assertEqual(call.call_count, 1)
        entry = artifact["symbols"]["AAPL"]
        self.assertEqual(entry["agreement"], "agree")
        self.assertEqual(entry["model"], ai_opinion.MODEL)

    def test_diverge_is_computed_by_script(self):
        _available(self)
        with mock.patch.object(ai_opinion, "_call_model", return_value=_model_reply(stance="avoid")):
            artifact = ai_opinion.opinions_for_result(_result(verdict="是"))
        self.assertEqual(artifact["symbols"]["AAPL"]["agreement"], "diverge")

    def test_unscorable_symbol_never_hits_the_api(self):
        _available(self)
        with mock.patch.object(ai_opinion, "_call_model") as call:
            artifact = ai_opinion.opinions_for_result(_result(verdict="无法评分"))
        call.assert_not_called()
        entry = artifact["symbols"]["AAPL"]
        self.assertEqual(entry["status"], "unavailable")
        C.validate_payload(C.KIND_AI_OPINION, artifact)

    def test_api_failure_degrades_symbol_only(self):
        _available(self)
        result = _result()
        result["symbols"]["MSFT"] = _analysis("观察")
        replies = {"AAPL": _model_reply()}

        def flaky(symbol, evidence):
            if symbol in replies:
                return replies[symbol]
            raise TimeoutError("read timeout")

        with mock.patch.object(ai_opinion, "_call_model", side_effect=flaky):
            artifact = ai_opinion.opinions_for_result(result)
        self.assertEqual(artifact["status"], "ok")
        self.assertEqual(artifact["symbols"]["AAPL"]["status"], "ok")
        self.assertEqual(artifact["symbols"]["MSFT"]["status"], "unavailable")
        self.assertIn("TimeoutError", artifact["symbols"]["MSFT"]["reason"])
        C.validate_payload(C.KIND_AI_OPINION, artifact)

    def test_invalid_model_output_degrades(self):
        _available(self)
        with mock.patch.object(ai_opinion, "_call_model", return_value=_model_reply(stance="strong_buy")):
            artifact = ai_opinion.opinions_for_result(_result())
        self.assertEqual(artifact["symbols"]["AAPL"]["status"], "unavailable")

    def test_symbols_filter(self):
        _available(self)
        result = _result()
        result["symbols"]["MSFT"] = _analysis("观察")
        with mock.patch.object(ai_opinion, "_call_model", return_value=_model_reply()):
            artifact = ai_opinion.opinions_for_result(result, symbols=["MSFT"])
        self.assertEqual(set(artifact["symbols"]), {"MSFT"})


if __name__ == "__main__":
    unittest.main()
