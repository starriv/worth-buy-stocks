#!/usr/bin/env python3
"""finnhub.py tests. No real network calls."""
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import finnhub as F  # noqa: E402


class _Resp:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


def _fake_urlopen(req, timeout=15):  # noqa: ARG001
    url = req.full_url
    if "secret-token" in url or "file-token" in url:
        raise AssertionError("token leaked into URL")
    if "/quote" in url:
        return _Resp({"c": 110.25, "d": 1.2, "dp": 1.1, "h": 111, "l": 108,
                      "o": 109, "pc": 109.05, "t": 1782528000})
    if "/stock/profile2" in url:
        return _Resp({"ticker": "AAPL", "name": "Apple Inc", "exchange": "NASDAQ",
                      "currency": "USD", "marketCapitalization": 3000000,
                      "finnhubIndustry": "Technology"})
    if "/company-news" in url:
        return _Resp([
            {"id": 1, "headline": "Apple news", "source": "Example",
             "datetime": 1782528000, "url": "https://example.com/a"}
        ])
    if "/calendar/earnings" in url:
        return _Resp({"earningsCalendar": [
            {"date": "2026-07-01", "hour": "amc", "epsEstimate": 2.1,
             "revenueEstimate": 1000, "quarter": 2, "year": 2026}
        ]})
    return _Resp({})


class TestFinnhubFetch(unittest.TestCase):
    def _env_file(self, text):
        f = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        f.write(text)
        f.close()
        self.addCleanup(lambda: os.path.exists(f.name) and os.unlink(f.name))
        return f.name

    def test_missing_token_is_unavailable_without_network(self):
        with patch("finnhub.urllib.request.urlopen") as urlopen:
            ctx = F.fetch_finnhub_context(["AAPL"], token="")
        self.assertEqual(ctx["status"], "unavailable")
        urlopen.assert_not_called()

    def test_fetch_context_reads_token_from_local_env_file(self):
        env_path = self._env_file("FINNHUB_API_KEY=file-token\n")
        with patch.dict(os.environ, {"WORTH_BUY_STOCKS_ENV_FILE": env_path}, clear=True), \
                patch("finnhub.urllib.request.urlopen", _fake_urlopen):
            ctx = F.fetch_finnhub_context(["aapl"], today=date(2026, 6, 27))
        self.assertEqual(ctx["status"], "ok")
        self.assertIn("AAPL", ctx["symbols"])

    def test_fetch_context_normalizes_all_endpoints(self):
        with patch("finnhub.urllib.request.urlopen", _fake_urlopen):
            ctx = F.fetch_finnhub_context(
                ["aapl"], token="secret-token", today=date(2026, 6, 27)
            )
        aapl = ctx["symbols"]["AAPL"]
        self.assertEqual(ctx["status"], "ok")
        self.assertEqual(aapl["quote"]["current_price"], 110.25)
        self.assertEqual(aapl["profile"]["name"], "Apple Inc")
        self.assertEqual(aapl["news"][0]["headline"], "Apple news")
        self.assertEqual(aapl["earnings"][0]["date"], "2026-07-01")
        self.assertEqual(aapl["data_flags"], [])

    def test_rate_limit_is_structured(self):
        def rate_limited(req, timeout=15):  # noqa: ARG001
            raise HTTPError(req.full_url, 429, "Too Many Requests", {}, io.BytesIO(b"limit"))

        with patch("finnhub.urllib.request.urlopen", rate_limited):
            ctx = F.fetch_finnhub_context(["AAPL"], token="secret-token")
        self.assertEqual(ctx["status"], "rate_limited")
        self.assertTrue(ctx["symbols"]["AAPL"]["data_flags"])

    def test_multi_symbol_parallel_preserves_input_order(self):
        # 多 symbol 并行后，symbols 字典顺序必须与输入顺序一致（确定性）。
        symbols = ["MSFT", "AAPL", "GOOG"]
        with patch("finnhub.urllib.request.urlopen", _fake_urlopen):
            ctx = F.fetch_finnhub_context(symbols, token="secret-token")
        self.assertEqual(list(ctx["symbols"].keys()), symbols)
        self.assertEqual(ctx["status"], "ok")
        for sym in symbols:
            self.assertEqual(ctx["symbols"][sym]["status"], "ok")

    def test_multi_symbol_one_failure_does_not_block_others(self):
        # 单 symbol 全端点失败只影响自身，不阻断其余 symbol 的采集。
        def fail_aapl(req, timeout=15):  # noqa: ARG001
            # symbol=AAPL 的所有端点都 429；其余 symbol 走正常 fake
            if "symbol=AAPL" in req.full_url:
                raise HTTPError(req.full_url, 429, "Too Many Requests", {}, io.BytesIO(b"limit"))
            return _fake_urlopen(req, timeout)

        with patch("finnhub.urllib.request.urlopen", fail_aapl):
            ctx = F.fetch_finnhub_context(["MSFT", "AAPL"], token="secret-token")
        self.assertEqual(ctx["status"], "ok")
        self.assertEqual(ctx["symbols"]["MSFT"]["status"], "ok")
        self.assertEqual(ctx["symbols"]["AAPL"]["status"], "rate_limited")


class TestFinnhubNormalize(unittest.TestCase):
    def test_normalize_offline_context_uppercases_symbols(self):
        raw = {"symbols": {"aapl": {"symbol": "aapl", "quote": {"current_price": 100}, "data_flags": []}}}
        ctx = F.normalize_finnhub_context(raw)
        self.assertEqual(ctx["status"], "ok")
        self.assertIn("AAPL", ctx["symbols"])
        self.assertEqual(F.context_for_symbol(ctx, "aapl")["symbol"], "AAPL")

    def test_summary_collects_flags(self):
        raw = {
            "symbols": {
                "AAPL": {"status": "ok", "data_flags": ["quote unavailable"]},
                "MSFT": {"status": "ok", "data_flags": []},
            }
        }
        summary = F.summarize_finnhub_context(F.normalize_finnhub_context(raw))
        self.assertEqual(summary["symbols_count"], 2)
        self.assertIn("AAPL: quote unavailable", summary["data_flags"])


class TestFinnhubLLMContext(unittest.TestCase):
    def test_news_offering_becomes_medium_red_flag(self):
        ctx = F.normalize_finnhub_context({
            "symbols": {
                "AAPL": {
                    "status": "ok",
                    "as_of": "2026-06-27T00:00:00Z",
                    "news": [{
                        "headline": "Apple announces secondary offering",
                        "summary": "The company announced a public offering.",
                        "published_at": "2026-06-26",
                        "url": "https://example.com/offering",
                    }],
                    "data_flags": [],
                }
            }
        })
        llm = F.llm_context_from_finnhub(ctx, today=date(2026, 6, 27))
        rf = llm["AAPL"]["red_flags"][0]
        self.assertEqual(rf["type"], "dilution")
        self.assertEqual(rf["severity"], "medium")
        self.assertEqual(llm["AAPL"]["sources"][0]["url"], "https://example.com/offering")

    def test_positive_news_does_not_create_red_flag(self):
        ctx = F.normalize_finnhub_context({
            "symbols": {
                "AAPL": {
                    "status": "ok",
                    "news": [{"headline": "Apple shares rise after upbeat product launch"}],
                    "data_flags": [],
                }
            }
        })
        self.assertEqual(F.llm_context_from_finnhub(ctx), {})

    def test_scans_past_first_five_news_for_red_flags(self):
        ctx = F.normalize_finnhub_context({
            "symbols": {
                "AAPL": {
                    "status": "ok",
                    "news": [
                        {"headline": f"Apple routine product update {i}"}
                        for i in range(5)
                    ] + [{
                        "headline": "Apple announces public offering",
                        "published_at": "2026-06-26",
                        "url": "https://example.com/offering",
                    }],
                    "data_flags": [],
                }
            }
        })
        llm = F.llm_context_from_finnhub(ctx, today=date(2026, 6, 27), max_news_flags=5)
        self.assertEqual(llm["AAPL"]["red_flags"][0]["type"], "dilution")
        self.assertEqual(llm["AAPL"]["sources"][0]["id"], "finnhub_news_6")

    def test_nearby_earnings_is_low_severity_event_only(self):
        ctx = F.normalize_finnhub_context({
            "symbols": {
                "AAPL": {
                    "status": "ok",
                    "earnings": [{"date": "2026-07-01", "hour": "amc"}],
                    "data_flags": [],
                }
            }
        })
        llm = F.llm_context_from_finnhub(ctx, today=date(2026, 6, 27))
        rf = llm["AAPL"]["red_flags"][0]
        self.assertEqual(rf["type"], "earnings_event")
        self.assertEqual(rf["severity"], "low")
        self.assertIn("2026-07-01", llm["AAPL"]["catalyst"])

    def test_merge_appends_sources_and_red_flags(self):
        primary = {"AAPL": {"sources": [{"id": "manual"}],
                            "red_flags": [{"type": "manual", "severity": "medium"}],
                            "data_trust": "suspect"}}
        supplemental = {"AAPL": {"sources": [{"id": "finnhub"}],
                                 "red_flags": [{"type": "earnings_event", "severity": "low"}],
                                 "catalyst": "财报临近"}}
        merged = F.merge_llm_contexts(primary, supplemental)
        self.assertEqual(len(merged["AAPL"]["sources"]), 2)
        self.assertEqual(len(merged["AAPL"]["red_flags"]), 2)
        self.assertEqual(merged["AAPL"]["data_trust"], "suspect")
        self.assertEqual(merged["AAPL"]["catalyst"], "财报临近")

    def test_empty_merge_returns_none(self):
        self.assertIsNone(F.merge_llm_contexts(None, {}))


if __name__ == "__main__":
    unittest.main()
