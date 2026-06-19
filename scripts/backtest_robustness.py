#!/usr/bin/env python3
"""稳健性回测：修掉前两轮的三个统计硬伤，看 edge 是否还站得住。

三项硬化：
  1. 非重叠窗口：rebalance 步长 = 前瞻期，相邻样本独立，t 值不再被 ~3× 重叠虚高。
  2. regime 拆分：按 t 时点 SPY 相对其 MA200 分「risk-on / risk-off」，分别算 IC 与
     多空价差——动量最怕 risk-off 崩溃，这是最关键的稳健性检验。
  3. 幸存者偏差探针：尝试拉一批已退市/被并购的票，量化篮子缺了多少「死掉的票」。

复用 backtest_common 的取数与切片（严格 <= t）。仅用本机 alpaca CLI，输出纯统计。
"""
import argparse
import sys

sys.path.insert(0, __import__("os").path.dirname(__file__))
from fetching import _fetch_feed, FeedLimitError  # noqa: E402
from metrics import ma  # noqa: E402
from backtest_common import (  # noqa: E402
    UNIVERSE, WARMUP, HISTORY_DAYS, load_panel, spearman, mean_t, quintile_spread,
)
import datetime  # noqa: E402

# 2023–2026 间已退市 / 被并购 / 暴雷的票（用于幸存者偏差探针）
DELISTED_PROBE = ["FRC", "SIVB", "SBNY", "ATVI", "TWTR", "CREE", "ABMD", "PXD", "SPLK", "VMW"]


def _regime_map(spy_bars):
    """每个交易日 SPY 是否在其 MA200 上方（risk-on）。点时点、无未来函数。"""
    closes = [b["c"] for b in spy_bars]
    dates = [b["t"][:10] for b in spy_bars]
    return {dates[i]: ((closes[i] > m) if (m := ma(closes[:i + 1], 200)) is not None else None)
            for i in range(len(closes))}


def _ic_line(label, pairs, ic_list):
    if not pairs:
        print(f"   {label:16} 无样本")
        return
    qs = quintile_spread(pairs)
    spread = f"{qs[0]:+.2f}%" if qs else "n/a"
    st = mean_t(ic_list)
    icstr = f"IC {st[0]:+.3f} t≈{st[1]:+.2f} ({st[2]}期独立)" if st else "IC n/a"
    print(f"   {label:16} N={len(pairs):<5} 五分位多空 {spread:<9} {icstr}")


def run(feed, adjustment, timeout):
    panel = load_panel(feed, adjustment, timeout)
    regime = _regime_map(panel.bars.get("SPY", []))

    print("\n" + "=" * 72)
    print("硬化回测 1+2：非重叠窗口 + regime 拆分（composite vs 前瞻收益）")
    print("=" * 72)
    for H in (21, 63):
        rebal_idx = list(range(WARMUP, len(panel.calendar) - H, H))  # 非重叠：步长=前瞻期
        groups = {"all": ([], []), "on": ([], []), "off": ([], [])}  # (pairs, ic_list)
        for ti in rebal_idx:
            tdate = panel.calendar[ti]
            symbols = panel.scores_at(tdate)
            ron = regime.get(tdate)
            gkey = "on" if ron else "off"
            ds, df = [], []
            for s in UNIVERSE:
                comp = (symbols.get(s, {}).get("score") or {}).get("composite")
                fwd = panel.fwd_return_pct(s, ti, H)
                if comp is None or fwd is None:
                    continue
                groups["all"][0].append((comp, fwd))
                groups[gkey][0].append((comp, fwd))
                ds.append(comp)
                df.append(fwd)
            ic = spearman(ds, df)
            if ic is not None:
                groups["all"][1].append(ic)
                groups[gkey][1].append(ic)
        print(f"\n── 前瞻 {H} 交易日（独立 rebalance {len(rebal_idx)} 期）──")
        _ic_line("全样本", *groups["all"])
        _ic_line("risk-on", *groups["on"])
        _ic_line("risk-off", *groups["off"])

    _survivorship_probe(feed, adjustment, timeout)
    print("\n注：非重叠后 63 日独立样本很少（~7 期），显著性必然弱；regime 看 edge 是否在 "
          "risk-off 仍为正。幸存者探针量化篮子缺口方向。")


def _survivorship_probe(feed, adjustment, timeout):
    end = datetime.date.today().isoformat()
    start = (datetime.date.today() - datetime.timedelta(days=HISTORY_DAYS)).isoformat()
    print("\n" + "=" * 72)
    print("硬化 3：幸存者偏差探针（2023–26 已退市/并购票，篮子里缺席）")
    print("=" * 72)
    served, missing = [], []
    for sym in DELISTED_PROBE:
        try:
            n = len(_fetch_feed([sym], start, end, feed, adjustment, 10000, timeout).get(sym, []))
        except (FeedLimitError, RuntimeError):
            n = 0
        (served if n > 0 else missing).append((sym, n))
    print(f"   可拉到数据: {', '.join(f'{s}({n})' for s, n in served) or '无'}")
    print(f"   完全缺失  : {', '.join(s for s, _ in missing) or '无'}")
    print("   解读：这些票多数在期内暴跌/归零/被并购。它们不在评分篮子里，")
    print("        意味着回测只统计了「活下来的赢家」，真实 edge 被系统性高估。")


def main():
    p = argparse.ArgumentParser(description="稳健性回测：非重叠 + regime + 幸存者探针")
    p.add_argument("--feed", default="iex")
    p.add_argument("--adjustment", default="split")
    p.add_argument("--timeout", type=int, default=60)
    args = p.parse_args()
    run(args.feed, args.adjustment, args.timeout)


if __name__ == "__main__":
    main()
