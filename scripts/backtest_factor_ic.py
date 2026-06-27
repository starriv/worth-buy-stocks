#!/usr/bin/env python3
"""逐因子有效性回测：每个因子的子分能否预测前瞻收益（单因子 IC）。

复用 backtest_common 的取数与切片（严格 <= t），记录每个因子的 score_pct，
分别计算截面 Spearman IC，找出谁是真信号、谁在拖后腿。含幸存者偏差，看相对强弱。
"""
import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from backtest_common import (  # noqa: E402
    UNIVERSE, WARMUP, STEP, HORIZONS, load_panel, spearman, mean_t,
)
import scoring as S  # noqa: E402

# alpha 因子从 factor_breakdown 读；trend 为否决层，technical/volume/trend_quality 为 confirmation overlay
ALPHA_FACTORS = list(S.ALPHA_WEIGHTS)
RISK_FACTORS = ["trend"]
OVERLAY_FACTORS = ["technical", "volume_exec"]
CONFIRMATION_FACTORS = ["trend_quality"]
COLS = ALPHA_FACTORS + RISK_FACTORS + CONFIRMATION_FACTORS + OVERLAY_FACTORS + ["composite"]


def _pct(score_0_1):
    return round(score_0_1 * 100) if score_0_1 is not None else None


def _features(row):
    """从 score 块抽出各因子子分 + composite，便于逐列算 IC。"""
    score = row.get("score", {}) or {}
    fb = score.get("factor_breakdown", {})
    conf = score.get("confirmation", {}) or {}
    feats = {f: (fb.get(f, {}) or {}).get("score_pct") for f in ALPHA_FACTORS}
    feats["trend"] = _pct(S.FACTOR_FNS["trend"](row))
    feats["trend_quality"] = conf.get("trend_quality_pct")
    feats["technical"] = conf.get("technical_pct")
    feats["volume_exec"] = conf.get("volume_pct")
    feats["composite"] = score.get("composite")
    return feats


def run(feed, adjustment, timeout):
    panel = load_panel(feed, adjustment, timeout)
    rebal_idx = list(range(WARMUP, len(panel.calendar) - max(HORIZONS), STEP))
    logging.info(f"rebalance 时点数: {len(rebal_idx)}")

    ic_series = {h: {c: [] for c in COLS} for h in HORIZONS}
    for ti in rebal_idx:
        symbols = panel.scores_at(panel.calendar[ti])
        rows = []
        for s in UNIVERSE:
            row = symbols.get(s, {})
            if not row.get("score"):
                continue
            fwd = {h: panel.fwd_return_pct(s, ti, h) for h in HORIZONS}
            rows.append((_features(row), fwd))
        for h in HORIZONS:
            for c in COLS:
                xs = [(r[0][c], r[1][h]) for r in rows
                      if r[0].get(c) is not None and r[1][h] is not None]
                if len(xs) >= 5:
                    ic = spearman([a for a, _ in xs], [b for _, b in xs])
                    if ic is not None:
                        ic_series[h][c].append(ic)
    _report(ic_series)


def _report(ic_series):
    print("\n" + "=" * 70)
    print("逐因子单因子 IC（子分 vs 前瞻收益；IC>0 = 该因子分越高、未来收益越高）")
    print("=" * 70)
    for h in sorted(ic_series):
        print(f"\n── 前瞻 {h} 交易日 ──")
        print(f"   {'因子':<14}{'IC均值':>9}{'t':>8}{'IC>0%':>8}{'期数':>6}")
        ranked = []
        for c in COLS:
            st = mean_t(ic_series[h][c])
            if st:
                mic, t, n = st
                pos = sum(1 for x in ic_series[h][c] if x > 0) / n * 100
                ranked.append((c, mic, t, pos, n))
        for c, mic, t, pos, n in sorted(ranked, key=lambda x: x[1], reverse=True):
            tag = "  ← composite" if c == "composite" else ""
            print(f"   {c:<14}{mic:>+9.3f}{t:>+8.2f}{pos:>7.0f}%{n:>6}{tag}")
    print("\n注：63 日前瞻用 21 日步长 => 约 3 倍重叠，真实 t 约为表中值 ÷ √3；")
    print("    篮子有幸存者偏差。重点看因子间相对排序与 IC 符号，而非绝对显著性。")


def main():
    p = argparse.ArgumentParser(description="逐因子 IC 回测")
    p.add_argument("--feed", default="iex", help="Alpaca 数据源，默认 iex；无 SIP 权限不要用 sip")
    p.add_argument("--adjustment", default="split")
    p.add_argument("--timeout", type=int, default=60)
    args = p.parse_args()
    run(args.feed, args.adjustment, args.timeout)


if __name__ == "__main__":
    main()
