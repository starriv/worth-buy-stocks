#!/usr/bin/env python3
"""因子有效性回测：composite 分能否预测未来收益（information coefficient 研究）。

方法（严格避免未来函数）：
  - 取一篮子流动性大盘股 + SPY/QQQ 的多年日线（backtest_common.load_panel）。
  - 在历史上按月（每 STEP=21 交易日）设若干 rebalance 时点 t。
  - 在每个 t：只用 <= t 的日线，对每只票跑 build_result → composite 分。
  - 前瞻收益 = 该票在 t+H 交易日的收盘 / t 收盘 - 1（H=21、63）。
  - 汇总：分数分桶的平均前瞻收益、Spearman 截面 IC、top-bottom 多空价差。

输出纯统计，不下单。仅用本机 alpaca CLI 拉数。含幸存者偏差，看方向与量级。
"""
import argparse
import sys

sys.path.insert(0, __import__("os").path.dirname(__file__))
from backtest_common import (  # noqa: E402
    UNIVERSE, WARMUP, STEP, HORIZONS,
    load_panel, spearman, mean_t, bucket_mean, quintile_spread,
)


def run(feed, adjustment, timeout):
    panel = load_panel(feed, adjustment, timeout)
    if len(panel.calendar) < WARMUP + max(HORIZONS) + STEP:
        print("历史不足，无法回测", file=sys.stderr)
        return
    rebal_idx = list(range(WARMUP, len(panel.calendar) - max(HORIZONS), STEP))
    print(f"rebalance 时点数: {len(rebal_idx)}", file=sys.stderr)

    obs = {h: [] for h in HORIZONS}        # (composite, fwd_ret)
    ic_by_date = {h: [] for h in HORIZONS}
    # confirmation gate A/B：买入候选（composite≥75 且无风险否决）按 confirmation.ok 拆分
    gate_ab = {h: {"ok": [], "blocked": []} for h in HORIZONS}
    for ti in rebal_idx:
        symbols = panel.scores_at(panel.calendar[ti])
        per_date = {h: ([], []) for h in HORIZONS}
        for s in UNIVERSE:
            sc = symbols.get(s, {}).get("score") or {}
            comp = sc.get("composite")
            if comp is None:
                continue
            buy_candidate = comp >= 75 and not sc.get("risk_gates")
            bucket = "ok" if (sc.get("confirmation") or {}).get("ok") else "blocked"
            for h in HORIZONS:
                fwd = panel.fwd_return_pct(s, ti, h)
                if fwd is None:
                    continue
                obs[h].append((comp, fwd))
                per_date[h][0].append(comp)
                per_date[h][1].append(fwd)
                if buy_candidate:
                    gate_ab[h][bucket].append(fwd)
        for h in HORIZONS:
            ic = spearman(*per_date[h])
            if ic is not None:
                ic_by_date[h].append(ic)
    _report(obs, ic_by_date)
    _report_gate_ab(gate_ab)


def _report(obs, ic_by_date):
    print("\n" + "=" * 60)
    print("因子有效性回测结果（composite 分 vs 前瞻收益）")
    print("=" * 60)
    for h in sorted(obs):
        pairs = obs[h]
        print(f"\n── 前瞻 {h} 交易日 ──   样本数 N={len(pairs)}")
        for label, lo, hi in (("分数<60 (否)", 0, 60),
                              ("60-75 (观察)", 60, 75),
                              (">=75 (是)", 75, 1e9)):
            m, n = bucket_mean(pairs, lo, hi)
            print(f"   {label:14} 平均前瞻收益 {m:+.2f}%   (n={n})" if m is not None
                  else f"   {label:14} 无样本")
        qs = quintile_spread(pairs)
        if qs:
            spread, top, bot = qs
            print(f"   五分位: 最高分组 {top:+.2f}%  最低分组 {bot:+.2f}%  多空价差 {spread:+.2f}%")
        st = mean_t(ic_by_date[h])
        if st:
            mic, t, ndays = st
            pos = sum(1 for x in ic_by_date[h] if x > 0) / ndays * 100
            print(f"   Spearman 截面 IC: 均值 {mic:+.3f}  t≈{t:+.2f}  "
                  f"IC>0 占比 {pos:.0f}%  ({ndays} 期)")
    print("\n注：篮子含幸存者偏差，真实 edge 偏乐观；看方向与单调性而非绝对收益。")


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _report_gate_ab(gate_ab):
    """检验 confirmation gate 是否真有用：买入候选里 ok 组前瞻收益应 ≥ blocked 组。

    若 blocked（技术未确认、被封顶为「观察」）组收益反而不低于 ok 组，说明 gate
    在砍真买点而非过滤差买点——这正是 scoring.py:_f_technical 那层 overlay 的实战检验。
    """
    print("\n" + "=" * 60)
    print("confirmation gate A/B（composite≥75 且无否决的买入候选）")
    print("=" * 60)
    for h in sorted(gate_ab):
        ok, blk = gate_ab[h]["ok"], gate_ab[h]["blocked"]
        m_ok, m_blk = _mean(ok), _mean(blk)
        print(f"\n── 前瞻 {h} 交易日 ──")
        print(f"   confirmation.ok=True  (放行) 平均前瞻收益 "
              f"{m_ok:+.2f}%  (n={len(ok)})" if m_ok is not None else
              "   confirmation.ok=True  (放行) 无样本")
        print(f"   confirmation.ok=False (封顶) 平均前瞻收益 "
              f"{m_blk:+.2f}%  (n={len(blk)})" if m_blk is not None else
              "   confirmation.ok=False (封顶) 无样本")
        if m_ok is not None and m_blk is not None:
            verdict = ("gate 有效（放行组更优）" if m_ok > m_blk
                       else "gate 存疑（封顶组不差，可能在砍真买点）")
            print(f"   差值 {m_ok - m_blk:+.2f}% → {verdict}")
    print("\n注：样本量通常很小（买入候选稀少），仅看方向参考；需多次/长窗口累积。")


def main():
    p = argparse.ArgumentParser(description="composite 分因子有效性回测")
    p.add_argument("--feed", default="iex")
    p.add_argument("--adjustment", default="split")
    p.add_argument("--timeout", type=int, default=60)
    args = p.parse_args()
    run(args.feed, args.adjustment, args.timeout)


if __name__ == "__main__":
    main()
