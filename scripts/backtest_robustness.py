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
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from fetching import _fetch_feed, FeedLimitError  # noqa: E402
from metrics import ma  # noqa: E402
from backtest_common import (  # noqa: E402
    UNIVERSE, DELISTED, WARMUP, HISTORY_DAYS, load_panel, spearman, mean_t, quintile_spread,
)

# 仍未纳入篮子的死亡名字（残余幸存者缺口）：SBNY 数据存疑、CREE 更名 WOLF，
# 另含本轮未加的破产票（Bed Bath、WeWork、Rite Aid）。DELISTED 已纳入的不再探针。
RESIDUAL_PROBE = ["SBNY", "CREE", "BBBY", "WE", "RAD"]


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
    print("硬化 3：幸存者偏差探针（篮子已纳入死亡名字 + 残余缺口）")
    print("=" * 72)
    print(f"   已纳入篮子的死亡票（降偏差）: {', '.join(DELISTED)}")
    served, missing = [], []
    for sym in RESIDUAL_PROBE:
        try:
            n = len(_fetch_feed([sym], start, end, feed, adjustment, 10000, timeout).get(sym, []))
        except (FeedLimitError, RuntimeError):
            n = 0
        (served if n > 0 else missing).append((sym, n))
    print(f"   残余缺口·可拉到: {', '.join(f'{s}({n})' for s, n in served) or '无'}")
    print(f"   残余缺口·全缺失: {', '.join(s for s, _ in missing) or '无'}")
    print("   解读：DELISTED 已让篮子接近「窗口起点的大盘」，存活期会被打分、前瞻收益含")
    print("        暴跌/收购退出。残余缺口（数据存疑/未列出的破产票）说明偏差降低但未根除。")


def main():
    p = argparse.ArgumentParser(description="稳健性回测：非重叠 + regime + 幸存者探针")
    p.add_argument("--feed", default="iex", help="Alpaca 数据源，默认 iex；无 SIP 权限不要用 sip")
    p.add_argument("--adjustment", default="split")
    p.add_argument("--timeout", type=int, default=60)
    args = p.parse_args()
    run(args.feed, args.adjustment, args.timeout)


if __name__ == "__main__":
    main()
