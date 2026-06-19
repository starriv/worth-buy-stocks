#!/usr/bin/env python3
"""回测公共件：篮子常量、取数面板、按时点切片打分、统计原语。

三个回测脚本（backtest_score / backtest_factor_ic / backtest_robustness）共用此模块，
避免重复取数与切片逻辑，也避免互相 import 带 __main__ 的脚本。纯件，无 __main__。

幸存者偏差：篮子若只取今日仍在交易的大盘股，退市/暴雷者缺席，真实 edge 被高估。
本版做了**部分缓解**——把 2021–24 间退市/被并购/暴雷、但 Alpaca 仍可拉到死前历史的
名字加进篮子（DELISTED），让篮子接近「窗口起点时的大盘」而非「今日幸存者」。这些票
在存活期会被正常打分，其前瞻收益涵盖暴跌/收购退出，检验模型是否在崩盘前给了高分。
仍非完全无偏：缺点时点指数成分库（需 CRSP 类数据），且漏掉未列出的死亡名字——
方向是「显著缓解、未根除」。
"""
import datetime
import sys

sys.path.insert(0, __import__("os").path.dirname(__file__))
from fetching import fetch_bars  # noqa: E402
from analysis import build_result  # noqa: E402

# 今日仍在交易的大盘流动性票
ALIVE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "AMD", "NFLX",
    "ADBE", "CRM", "ORCL", "CSCO", "QCOM", "TXN", "INTC", "AMAT", "MU",
    "JPM", "BAC", "WFC", "GS", "V", "MA",
    "UNH", "JNJ", "LLY", "ABBV", "MRK", "PFE",
    "WMT", "COST", "HD", "MCD", "NKE", "SBUX",
    "XOM", "CVX", "CAT", "BA", "GE", "PG", "KO", "PEP", "DIS",
]
# 2021–24 间离开市场、但有死前历史的大盘票（降幸存者偏差）。
# 真暴雷（跑高后归零，最关键）：SIVB(SVB,2023-03)、FRC(First Republic,2023-04)。
# 被并购（溢价退出）：ATVI(MSFT)、TWTR(Musk)、PXD(XOM)、SPLK(Cisco)、VMW(Broadcom)、ABMD(JNJ)。
# 排除 CREE（更名 WOLF，仅 87 根）与 SBNY（末根 2025-03 与其 2023 倒闭不符，数据存疑）。
DELISTED = ["SIVB", "FRC", "ATVI", "TWTR", "PXD", "SPLK", "VMW", "ABMD"]
UNIVERSE = ALIVE + DELISTED
BENCH = ["SPY", "QQQ"]
WARMUP = 230         # 起评所需的最少历史根数（>200 让 MA200 可算）
STEP = 21            # rebalance 间隔（交易日）
HORIZONS = [21, 63]  # 前瞻收益窗口
# IEX 日线最早约 2020-07；取约 6 年覆盖 2022 全程熊市（warmup 后约 2021 年中起评）。
HISTORY_DAYS = 6 * 365


# ---- 取数面板 ----

class Panel:
    """一次性取数后的只读面板：原始 bars、每票 date->close、SPY 交易日历。"""

    def __init__(self, bars, used_feed, adjustment):
        self.bars = bars
        self.used_feed = used_feed
        self.adjustment = adjustment
        self.syms = UNIVERSE + BENCH
        self.closes_by_date = {
            s: {b["t"][:10]: b["c"] for b in bars.get(s, [])} for s in self.syms
        }
        self.calendar = [b["t"][:10] for b in bars.get("SPY", [])]

    def close(self, sym, date):
        return self.closes_by_date.get(sym, {}).get(date)

    def fwd_return_pct(self, sym, ti, horizon):
        """sym 从 calendar[ti] 到 calendar[ti+horizon] 的收益（%）；缺价返回 None。"""
        if ti + horizon >= len(self.calendar):
            return None
        c0 = self.close(sym, self.calendar[ti])
        c1 = self.close(sym, self.calendar[ti + horizon])
        return (c1 / c0 - 1) * 100 if (c0 and c1) else None

    def scores_at(self, tdate):
        """只用 <= tdate 的日线对全篮子打分，返回 {sym: analyze+score 字典}。"""
        sliced = {s: [b for b in self.bars.get(s, []) if b["t"][:10] <= tdate]
                  for s in self.syms}
        return build_result(self.syms, sliced, self.used_feed, self.adjustment)["symbols"]


def load_panel(feed, adjustment, timeout, history_days=HISTORY_DAYS):
    end = datetime.date.today().isoformat()
    start = (datetime.date.today() - datetime.timedelta(days=history_days)).isoformat()
    syms = UNIVERSE + BENCH
    print(f"拉取 {len(syms)} 只标的日线 {start}→{end} …", file=sys.stderr)
    bars, used_feed, note = fetch_bars(syms, start, end, feed, adjustment, 10000, timeout)
    if note:
        print(note, file=sys.stderr)
    return Panel(bars, used_feed, adjustment)


# ---- 统计原语 ----

def avg_ranks(xs):
    """平均秩（处理并列），用于 Spearman。"""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        r = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = r
        i = j + 1
    return ranks


def spearman(a, b):
    """Spearman 秩相关；样本 < 3 或零方差返回 None。"""
    if len(a) < 3:
        return None
    ra, rb = avg_ranks(a), avg_ranks(b)
    n = len(a)
    ma, mb = sum(ra) / n, sum(rb) / n
    cov = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    va = sum((x - ma) ** 2 for x in ra) ** 0.5
    vb = sum((x - mb) ** 2 for x in rb) ** 0.5
    return cov / (va * vb) if va and vb else None


def mean_t(values):
    """返回 (均值, t 值, n)；t = 均值 / (样本标准差 / √n)。空则 None。"""
    n = len(values)
    if n == 0:
        return None
    m = sum(values) / n
    sd = (sum((x - m) ** 2 for x in values) / (n - 1)) ** 0.5 if n > 1 else 0.0
    t = m / (sd / n ** 0.5) if sd else float("inf")
    return m, t, n


def bucket_mean(pairs, lo, hi):
    """pairs:[(score,fwd)]，取 score∈[lo,hi) 的前瞻收益均值与个数。"""
    sel = [f for s, f in pairs if lo <= s < hi]
    return (sum(sel) / len(sel), len(sel)) if sel else (None, 0)


def quintile_spread(pairs):
    """最高五分位均值 − 最低五分位均值；返回 (spread, top, bot)，样本<10 返回 None。"""
    if len(pairs) < 10:
        return None
    ss = sorted(pairs, key=lambda x: x[0])
    q = len(ss) // 5
    bot = sum(f for _, f in ss[:q]) / q
    top = sum(f for _, f in ss[-q:]) / q
    return top - bot, top, bot
