#!/usr/bin/env python3
"""scanner.py 的确定性单元测试。

只测纯函数与离线（--input）路径，不触网、不调用 alpaca/finnhub CLI。
运行：python3 -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import scanner as S  # noqa: E402
from pipeline import build_result  # noqa: E402


def _bars(closes, start=date(2024, 1, 2), vol=1_000_000):
    """把收盘价序列变成 multi-bars 形状的日线，跳过周末，OHLC 围绕收盘。"""
    out = []
    d = start
    for i, c in enumerate(closes):
        while d.isoweekday() > 5:
            d += timedelta(days=1)
        out.append({"t": d.isoformat() + "T04:00:00Z",
                    "o": c, "h": c + 1, "l": c - 1, "c": c, "v": vol + i})
        d += timedelta(days=1)
    return out


def _rising(n=300, base=10.0, slope=0.6):
    """一条足够长、足够陡的上行序列，让 momentum/rel_strength 拿高分。"""
    return [base + slope * i for i in range(n)]


def _healthy_uptrend(n=300, base=50.0, slope=0.3):
    """有规律回吐但不过热的强趋势，用于可买候选夹具。"""
    pattern = [0.0, 0.6, 1.2, 0.6, 0.0]
    return [base + slope * i + pattern[i % len(pattern)] for i in range(n)]


def _flat(n=300, base=100.0):
    return [base for _ in range(n)]


def _bench_bars():
    """上行基准：SPY/QQQ 站上自身 MA200，避免触发 market_risk_off 闸门
    （该闸门会把所有新开仓封顶为「观察」而非「是」）。"""
    return _bars(_rising(300, 400.0, 0.1))


class TestFilterCommonTickers(unittest.TestCase):
    def test_drops_preferred_warrants_units_and_nonfractionable(self):
        assets = [
            {"symbol": "AAPL", "fractionable": True},
            {"symbol": "T.PRC", "fractionable": True},   # 优先股（含点）
            {"symbol": "NVDA", "fractionable": True},
            {"symbol": "FOOBAR", "fractionable": True},   # 长度>5
            {"symbol": "ABC", "fractionable": False},     # 不可分股
            {"symbol": "AAPL", "fractionable": True},     # 重复（小写输入也会被去重）
            {"symbol": "warr", "fractionable": True},     # 小写普通股
        ]
        self.assertEqual(S.filter_common_tickers(assets), ["AAPL", "NVDA", "WARR"])

    def test_empty_and_garbage(self):
        self.assertEqual(S.filter_common_tickers([]), [])
        self.assertEqual(S.filter_common_tickers([None, "x", 5, {}]), [])


class TestFilterLiquidity(unittest.TestCase):
    def _ctx(self, mapping):
        return {"status": "ok", "symbols": {
            s: {"daily_bar": v} for s, v in mapping.items()}}

    def test_filters_by_price_and_volume(self):
        ctx = self._ctx({
            "AAPL": {"c": 150.0, "v": 120000},
            "PENNY": {"c": 2.0, "v": 500000},   # 价<5
            "THIN": {"c": 50.0, "v": 1000},      # 量<5万
        })
        pool = S.filter_liquidity(ctx, 5.0, 50000)
        self.assertEqual([p["symbol"] for p in pool], ["AAPL"])
        self.assertEqual(pool[0]["close"], 150.0)

    def test_sorted_by_volume_desc(self):
        ctx = self._ctx({
            "A": {"c": 10.0, "v": 70000},
            "B": {"c": 10.0, "v": 300000},
            "C": {"c": 10.0, "v": 90000},
        })
        self.assertEqual([p["symbol"] for p in S.filter_liquidity(ctx, 5, 50000)],
                         ["B", "C", "A"])

    def test_missing_fields_dropped(self):
        ctx = self._ctx({"A": {"c": 10.0}, "B": {"v": 100000}, "C": {"c": 10.0, "v": 100000}})
        self.assertEqual([p["symbol"] for p in S.filter_liquidity(ctx, 5, 50000)], ["C"])

    def test_unavailable_context_returns_empty(self):
        self.assertEqual(S.filter_liquidity({"status": "unavailable"}, 5, 50000), [])


class TestExtractCandidates(unittest.TestCase):
    def _result_with(self, sym_closes):
        bars = {s: _bars(c) for s, c in sym_closes.items()}
        for b in ("SPY", "QQQ"):
            bars.setdefault(b, _bench_bars())
        syms = list(bars.keys())
        return build_result(syms, bars, "iex", "split")

    def test_only_yes_verdict_extracted_and_sorted(self):
        # 强势票 → 是；平盘票 → 非"是"
        result = self._result_with({
            "STRG": _healthy_uptrend(),  # 强势但不过热
            "FLAT": _flat(300, 50),  # 平盘
        })
        cands = S.extract_candidates(result)
        syms = [c["symbol"] for c in cands]
        self.assertIn("STRG", syms)
        self.assertNotIn("FLAT", syms)
        self.assertNotIn("SPY", syms)   # 基准不进候选
        self.assertNotIn("QQQ", syms)
        # 候选字段齐全
        c = next(c for c in cands if c["symbol"] == "STRG")
        self.assertIsNotNone(c["composite"])
        self.assertIn("trade_plan", c)
        self.assertIn("suggested_entry_price", c["trade_plan"])
        self.assertIn("factor_breakdown", c)

    def test_top_n_truncates(self):
        result = self._result_with({"STRG": _healthy_uptrend()})
        cands = S.extract_candidates(result, top_n=0)
        self.assertEqual(cands, [])

    def test_entry_mode_filters_by_classification(self):
        candidates = [
            {"symbol": "PULL", "entry_timing": {"classification": "pullback_reversal"}},
            {"symbol": "TREND", "entry_timing": {"classification": "trend_continuation"}},
            {"symbol": "WAIT", "entry_timing": {"classification": "pullback_no_trigger"}},
        ]
        self.assertEqual([c["symbol"] for c in S._filter_entry(candidates, "pullback")], ["PULL"])
        self.assertEqual([c["symbol"] for c in S._filter_entry(candidates, "trend")], ["TREND"])
        self.assertIs(S._filter_entry(candidates, "all"), candidates)


class TestScanEntryFilterOrdering(unittest.TestCase):
    @staticmethod
    def _analysis(classification):
        return {
            "last_close": 100.0,
            "entry_timing": {"classification": classification, "timing_score_pct": 80},
            "score": {
                "composite": 90.0, "verdict": "是", "suggested_action": "entry",
                "factor_breakdown": {}, "trade_plan": {}, "data_flags": [],
                "blocking_reasons": [], "llm_overlay": None,
            },
        }

    def test_entry_filter_runs_before_finnhub_verification(self):
        lean = {"symbols": {
            "PULL": self._analysis("pullback_reversal"),
            "TREND": self._analysis("trend_continuation"),
            "SPY": {}, "QQQ": {},
        }}
        verified = {"symbols": {
            "PULL": self._analysis("pullback_reversal"),
            "SPY": {}, "QQQ": {},
        }}
        args = S._build_args([
            "--symbols", "PULL,TREND", "--entry", "pullback",
            "--verify-news", "on", "--ai-opinion", "off",
        ])
        with unittest.mock.patch.object(
            S, "fetch_liquidity",
            return_value=([{"symbol": "PULL"}, {"symbol": "TREND"}], []),
        ), unittest.mock.patch.object(
            S, "fetch_pool_bars", return_value=({}, [])
        ), unittest.mock.patch.object(
            S, "build_result", side_effect=[lean, verified]
        ), unittest.mock.patch.object(
            S, "fetch_finnhub_context", return_value={"status": "ok", "symbols": {}}
        ) as fetch_news, unittest.mock.patch.object(
            S, "llm_context_from_finnhub", return_value={}
        ):
            result = S.scan(args)
        self.assertEqual(fetch_news.call_args.args[0], ["PULL"])
        self.assertEqual([c["symbol"] for c in result["candidates"]], ["PULL"])
        self.assertEqual(result["counts"]["candidates_lean"], 2)
        self.assertEqual(result["counts"]["candidates_entry_filtered"], 1)


class TestMergeNewsDowngrades(unittest.TestCase):
    def _lean_cand(self, sym, composite):
        return {"symbol": sym, "composite": composite, "verdict": "是",
                "factor_breakdown": {}, "last_close": 10.0,
                "relative_strength_pct": {}, "trade_plan": {},
                "data_flags": [], "blocking_reasons": []}

    def test_splits_final_and_downgraded(self):
        # 复核轮：KEEP 仍是"是"；DROP 被降为"观察"带 cap 74
        bars = {s: _bars(_healthy_uptrend()) for s in ("KEEP", "DROP")}
        for b in ("SPY", "QQQ"):
            bars[b] = _bench_bars()
        verified = build_result(list(bars.keys()), bars, "iex", "split")
        # 手动改 DROP 的 score 模拟新闻降级
        drop_sc = verified["symbols"]["DROP"]["score"]
        drop_sc["verdict"] = "观察"
        drop_sc["cap_applied"] = 74
        drop_sc["llm_overlay"] = {"cap": 74, "downgrade_reasons": ["软红旗（litigation）：xxx"]}

        lean = [self._lean_cand("KEEP", 90.0), self._lean_cand("DROP", 92.0)]
        final, down = S.merge_news_downgrades(lean, verified)
        self.assertEqual([f["symbol"] for f in final], ["KEEP"])
        self.assertEqual([d["symbol"] for d in down], ["DROP"])
        self.assertEqual(down[0]["verified_verdict"], "观察")
        self.assertEqual(down[0]["cap_applied"], 74)
        self.assertIn("软红旗", down[0]["downgrade_reasons"][0])

    def test_all_downgraded_when_verified_missing(self):
        lean = [self._lean_cand("X", 90.0)]
        final, down = S.merge_news_downgrades(lean, {"symbols": {}})
        self.assertEqual(final, [])
        self.assertEqual(down[0]["verified_verdict"], "无法评分")


class TestOfflineScan(unittest.TestCase):
    def test_offline_input_produces_structured_output(self):
        # 构造离线输入：一只强势 + 一只平盘 + 基准 + snapshot + (无 finnhub)
        assets = [{"symbol": "STRG", "fractionable": True},
                  {"symbol": "FLAT", "fractionable": True}]
        snapshots = {"status": "ok", "symbols": {
            "STRG": {"daily_bar": {"c": 100.0, "v": 200000}},
            "FLAT": {"daily_bar": {"c": 100.0, "v": 200000}}}}
        bars = {
            "STRG": _bars(_healthy_uptrend()),
            "FLAT": _bars(_flat(300, 100)),
            "SPY": _bench_bars(),
            "QQQ": _bench_bars(),
        }
        payload = {"assets": assets, "snapshots": snapshots, "bars": bars}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(payload, f)
            path = f.name
        try:
            args = S._build_args(["--input", path, "--verify-news", "off"])
            result = S._scan_offline(args)
            self.assertEqual(result["offline"], True)
            self.assertEqual(result["counts"]["universe"], 2)
            self.assertEqual(result["counts"]["liquidity_pool"], 2)
            self.assertIn("verdict_dist", result["counts"])
            self.assertIn("market_regime", result)
            cand_syms = [c["symbol"] for c in result["candidates"]]
            self.assertIn("STRG", cand_syms)
            self.assertNotIn("FLAT", cand_syms)
        finally:
            os.unlink(path)


class TestAttachAiOpinions(unittest.TestCase):
    """AI 意见只对最终候选调用、只附加展示字段、不影响排序与评分。"""

    def _scored_result(self, syms=("STRG",)):
        bars = {s: _bars(_healthy_uptrend()) for s in syms}
        bars["SPY"] = _bench_bars()
        bars["QQQ"] = _bench_bars()
        return build_result(list(syms) + ["SPY", "QQQ"], bars, "iex", "split")

    def _candidates(self, result):
        return S.extract_candidates(result, top_n=None)

    def _artifact(self, syms):
        return {
            "contract_version": "worth-buy-stocks.agent.v1",
            "kind": "ai_opinion",
            "status": "ok",
            "symbols": {
                s: {
                    "status": "ok", "stance": "buy", "confidence": "medium",
                    "view": "趋势领先", "key_risks": [], "agreement": "agree",
                    "model": "claude-opus-4-8", "generated_at": "2026-07-09T00:00:00Z",
                } for s in syms
            },
        }

    def test_off_mode_never_calls(self):
        result = self._scored_result()
        cands = self._candidates(result)
        with unittest.mock.patch.object(S, "opinions_for_result") as gen:
            flag = S._attach_ai_opinions(cands, result, "off")
        gen.assert_not_called()
        self.assertIsNone(flag)
        self.assertNotIn("ai_opinion", cands[0])

    def test_auto_without_key_returns_data_flag(self):
        result = self._scored_result()
        cands = self._candidates(result)
        with unittest.mock.patch.object(S, "ai_opinion_has_api_key", return_value=False), \
             unittest.mock.patch.object(S, "opinions_for_result") as gen:
            flag = S._attach_ai_opinions(cands, result, "auto")
        gen.assert_not_called()
        self.assertIn("ANTHROPIC_API_KEY", flag)

    def test_offline_default_does_not_call(self):
        result = self._scored_result()
        cands = self._candidates(result)
        with unittest.mock.patch.object(S, "opinions_for_result") as gen:
            flag = S._attach_ai_opinions(cands, result, "auto", offline=True)
        gen.assert_not_called()
        self.assertIsNone(flag)

    def test_offline_explicit_on_still_does_not_call(self):
        result = self._scored_result()
        cands = self._candidates(result)
        with unittest.mock.patch.object(S, "opinions_for_result") as gen:
            flag = S._attach_ai_opinions(cands, result, "on", offline=True)
        gen.assert_not_called()
        self.assertIsNone(flag)

    def test_calls_scoped_to_final_candidates_only(self):
        result = self._scored_result(("STRG", "STR2"))
        cands = self._candidates(result)[:1]  # 模拟 top-N 截断后只剩 1 个
        only = cands[0]["symbol"]
        with unittest.mock.patch.object(S, "ai_opinion_sdk_available", return_value=True), \
             unittest.mock.patch.object(S, "ai_opinion_has_api_key", return_value=True), \
             unittest.mock.patch.object(S, "opinions_for_result",
                                        return_value=self._artifact([only])) as gen:
            flag = S._attach_ai_opinions(cands, result, "auto")
        self.assertIsNone(flag)
        gen.assert_called_once()
        self.assertEqual(gen.call_args.kwargs["symbols"], [only])
        # llm_context 必须透传给 opinions_for_result，否则新闻上下文会丢失
        self.assertEqual(gen.call_args.kwargs.get("llm_context"), None)
        self.assertEqual(cands[0]["ai_opinion"]["stance"], "buy")

    def test_ranking_fields_untouched_and_failure_degrades_symbol_only(self):
        result = self._scored_result(("STRG", "STR2"))
        cands = self._candidates(result)
        before = [(c["symbol"], c["composite"], c["verdict"]) for c in cands]
        artifact = self._artifact([cands[0]["symbol"]])
        artifact["symbols"][cands[1]["symbol"]] = {"status": "unavailable", "reason": "api timeout"}
        with unittest.mock.patch.object(S, "ai_opinion_sdk_available", return_value=True), \
             unittest.mock.patch.object(S, "ai_opinion_has_api_key", return_value=True), \
             unittest.mock.patch.object(S, "opinions_for_result", return_value=artifact):
            S._attach_ai_opinions(cands, result, "auto")
        self.assertEqual(before, [(c["symbol"], c["composite"], c["verdict"]) for c in cands])
        self.assertEqual(cands[0]["ai_opinion"]["status"], "ok")
        self.assertEqual(cands[1]["ai_opinion"]["status"], "unavailable")

    def test_on_mode_raises_when_unavailable(self):
        result = self._scored_result()
        cands = self._candidates(result)
        with unittest.mock.patch.object(S, "opinions_for_result",
                                        return_value={"status": "unavailable", "reason": "no key"}):
            with self.assertRaises(RuntimeError):
                S._attach_ai_opinions(cands, result, "on")

    def test_on_mode_raises_when_one_symbol_is_unavailable(self):
        result = self._scored_result()
        cands = self._candidates(result)
        artifact = self._artifact([cands[0]["symbol"]])
        artifact["symbols"][cands[0]["symbol"]] = {
            "status": "unavailable", "reason": "timeout"
        }
        with unittest.mock.patch.object(S, "opinions_for_result", return_value=artifact):
            with self.assertRaisesRegex(RuntimeError, "timeout"):
                S._attach_ai_opinions(cands, result, "on")

    def test_offline_scan_output_has_no_ai_opinion_by_default(self):
        assets = [{"symbol": "STRG", "fractionable": True}]
        snapshots = {"status": "ok", "symbols": {"STRG": {"daily_bar": {"c": 100.0, "v": 200000}}}}
        bars = {"STRG": _bars(_healthy_uptrend()), "SPY": _bench_bars(), "QQQ": _bench_bars()}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"assets": assets, "snapshots": snapshots, "bars": bars}, f)
            path = f.name
        try:
            args = S._build_args(["--input", path, "--verify-news", "off"])
            result = S._scan_offline(args)
        finally:
            os.unlink(path)
        for cand in result["candidates"]:
            self.assertNotIn("ai_opinion", cand)


if __name__ == "__main__":
    unittest.main()
