#!/usr/bin/env python3
"""Cross-symbol pipeline: aggregate analysis, relative strength, and scoring.

``build_result`` was previously in ``analysis.py``; it lives here now so that
``analysis`` only owns single-symbol analysis and cross-symbol relative
strength, while this module wires those together with ``scoring``,
``portfolio``, and ``finnhub`` overlays to produce the full result dict.
"""
from __future__ import annotations

from typing import Any

from analysis import _load_input, analyze_symbol, relative_strength  # noqa: F401
from finnhub import context_for_symbol as finnhub_context_for_symbol
from finnhub import summarize_finnhub_context
from portfolio import context_for_symbol, summarize_account_context
from scoring import score


def build_result(
    symbols: list[str],
    bars: dict[str, list[dict[str, Any]]],
    feed: str,
    adjustment: str,
    feed_note: str | None = None,
    llm_context: dict[str, dict[str, Any]] | None = None,
    account_context: dict[str, Any] | None = None,
    finnhub_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """llm_context：可选 {symbol: {as_of/sources/red_flags/data_trust/catalyst}} 映射，按票传给
    score() 的非对称 LLM 风控 overlay。

    account_context：可选 Alpaca 账户/持仓上下文；按票传给 score() 的账户 overlay，用于
    生成贴合当前持仓敞口的建议。默认 None → 纯价量管道，分数与回测完全不变。

    finnhub_context：可选 Finnhub 补充上下文；只写入 supplemental，不参与 score。
    """
    llm_context = llm_context or {}
    result: dict[str, Any] = {"feed": feed, "adjustment": adjustment, "symbols": {}}
    if feed_note:
        result["feed_note"] = feed_note
    account_summary = summarize_account_context(account_context)
    if account_summary:
        result["account_context"] = account_summary
    finnhub_summary = summarize_finnhub_context(finnhub_context)
    if finnhub_summary:
        result.setdefault("supplemental", {})["finnhub"] = finnhub_summary
    analyses: dict[str, dict[str, Any]] = {}
    for sym in symbols:
        sb = bars.get(sym)
        analyses[sym] = analyze_symbol(sb) if sb else {"error": "无数据返回"}

    benches = {b: analyses.get(b, {}).get("returns_pct", {}) for b in ("SPY", "QQQ")}
    # 大盘 regime：SPY 跌破自身 200 日线 → 全局 risk-off（alpha 在此反转，见
    # backtest_robustness）。仅 SPY 明确在 MA200 下方时为 True；above=True 或历史
    # 不足(None) → None（不触发市场闸门）。点时点、无未来函数（用同一切片的 SPY）。
    spy_a = analyses.get("SPY")
    spy_above200 = spy_a.get("ma", {}).get("above_MA200") if isinstance(spy_a, dict) else None
    market_risk_off = True if spy_above200 is False else None
    if market_risk_off:
        result["market_risk_off"] = True
    for sym, a in analyses.items():
        if "error" not in a:
            a["relative_strength_pct"] = {
                bench: relative_strength(a["returns_pct"], benches[bench])
                for bench in ("SPY", "QQQ") if benches.get(bench)
            }
            sym_finnhub = finnhub_context_for_symbol(finnhub_context, sym)
            if sym_finnhub:
                a.setdefault("supplemental", {})["finnhub"] = sym_finnhub
            # 相对强度就绪后再确定性打分（依赖 relative_strength_pct）
            s = score(
                a,
                market_risk_off,
                llm_context.get(sym),
                context_for_symbol(account_context, sym),
            )
            if s is not None:
                a["score"] = s
        result["symbols"][sym] = a
    return result
