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
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from fetching import fetch_bars  # noqa: E402
from pipeline import build_result  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)

# 规则化篮子（2026-07-09 由 45 只手挑名单扩至 132 只）。选票规则（可复述，非手挑品味）：
# 2021 年前上市、今日仍在交易的美股大盘流动性票，按 GICS 11 个行业分组、每行业 ≥7 只、
# 行业内按大致市值/流动性取头部。目的：降低「今天著名的名字」的选择偏差、给行业中性
# 检验提供标签。仍非时点成分库（缺 CRSP 类数据），幸存者缓解依赖 DELISTED/BANKRUPT。
SECTOR = {
    # Information Technology
    "AAPL": "tech", "MSFT": "tech", "NVDA": "tech", "AVGO": "tech", "AMD": "tech",
    "ADBE": "tech", "CRM": "tech", "ORCL": "tech", "CSCO": "tech", "QCOM": "tech",
    "TXN": "tech", "INTC": "tech", "AMAT": "tech", "MU": "tech", "NOW": "tech",
    "INTU": "tech", "IBM": "tech", "ACN": "tech", "LRCX": "tech", "KLAC": "tech",
    # Communication Services
    "GOOGL": "comm", "META": "comm", "NFLX": "comm", "DIS": "comm", "CMCSA": "comm",
    "TMUS": "comm", "VZ": "comm", "T": "comm", "EA": "comm", "TTWO": "comm",
    # Consumer Discretionary
    "AMZN": "cons_disc", "TSLA": "cons_disc", "HD": "cons_disc", "MCD": "cons_disc",
    "NKE": "cons_disc", "SBUX": "cons_disc", "LOW": "cons_disc", "TJX": "cons_disc",
    "BKNG": "cons_disc", "MAR": "cons_disc", "GM": "cons_disc", "F": "cons_disc",
    "ORLY": "cons_disc", "CMG": "cons_disc",
    # Consumer Staples
    "WMT": "cons_staples", "COST": "cons_staples", "PG": "cons_staples", "KO": "cons_staples",
    "PEP": "cons_staples", "PM": "cons_staples", "MO": "cons_staples", "MDLZ": "cons_staples",
    "CL": "cons_staples", "KMB": "cons_staples",
    # Health Care
    "UNH": "health", "JNJ": "health", "LLY": "health", "ABBV": "health", "MRK": "health",
    "PFE": "health", "TMO": "health", "ABT": "health", "DHR": "health", "BMY": "health",
    "AMGN": "health", "GILD": "health", "ISRG": "health", "VRTX": "health",
    "REGN": "health", "CVS": "health",
    # Financials（V/MA 按 2023 GICS 重分类归金融）
    "JPM": "fin", "BAC": "fin", "WFC": "fin", "GS": "fin", "MS": "fin", "C": "fin",
    "BLK": "fin", "SCHW": "fin", "AXP": "fin", "V": "fin", "MA": "fin", "SPGI": "fin",
    "CME": "fin", "ICE": "fin", "COF": "fin", "PGR": "fin",
    # Industrials
    "CAT": "industrials", "BA": "industrials", "GE": "industrials", "HON": "industrials",
    "UPS": "industrials", "RTX": "industrials", "LMT": "industrials", "DE": "industrials",
    "UNP": "industrials", "CSX": "industrials", "FDX": "industrials", "ETN": "industrials",
    "EMR": "industrials", "ITW": "industrials",
    # Energy
    "XOM": "energy", "CVX": "energy", "COP": "energy", "SLB": "energy", "EOG": "energy",
    "MPC": "energy", "PSX": "energy", "VLO": "energy", "OXY": "energy", "KMI": "energy",
    # Materials
    "LIN": "materials", "APD": "materials", "SHW": "materials", "FCX": "materials",
    "NEM": "materials", "ECL": "materials", "NUE": "materials", "DOW": "materials",
    # Utilities
    "NEE": "utilities", "DUK": "utilities", "SO": "utilities", "D": "utilities",
    "AEP": "utilities", "EXC": "utilities", "SRE": "utilities",
    # Real Estate
    "PLD": "real_estate", "AMT": "real_estate", "EQIX": "real_estate", "CCI": "real_estate",
    "SPG": "real_estate", "O": "real_estate", "PSA": "real_estate",
}
ALIVE = list(SECTOR)
# 2021–24 间离开市场、但有死前历史的大盘票（降幸存者偏差）。
# 真暴雷（跑高后归零，最关键）：SIVB(SVB,2023-03)、FRC(First Republic,2023-04)。
# 被并购（溢价退出）：ATVI(MSFT)、TWTR(Musk)、PXD(XOM)、SPLK(Cisco)、VMW(Broadcom)、ABMD(JNJ)。
# 排除 CREE（更名 WOLF，仅 87 根）与 SBNY（末根 2025-03 与其 2023 倒闭不符，数据存疑）。
DELISTED = ["SIVB", "FRC", "ATVI", "TWTR", "PXD", "SPLK", "VMW", "ABMD"]
# 终值口径分两类：破产票停牌后按 -100% 计（最后成交价远高于清零后的真实终值，
# 用最后价会把 SIVB 的 -100% 记成 -60% 量级、低估模型崩盘前给高分的代价）；
# 被并购票维持「最后可得收盘价」（溢价退出，最后价即真实终值）。
BANKRUPT = {"SIVB", "FRC"}
# 退市票的行业标签（供行业中性检验；ATVI/TWTR 按 GICS 归 Communication Services）
SECTOR.update({
    "SIVB": "fin", "FRC": "fin", "ATVI": "comm", "TWTR": "comm",
    "PXD": "energy", "SPLK": "tech", "VMW": "tech", "ABMD": "health",
})
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
        self.syms = list(dict.fromkeys(UNIVERSE + BENCH + list(bars)))
        self.closes_by_date = {
            s: {b["t"][:10]: b["c"] for b in bars.get(s, [])} for s in self.syms
        }
        self.dates_by_symbol = {
            s: sorted(self.closes_by_date.get(s, {})) for s in self.syms
        }
        self.calendar = [b["t"][:10] for b in bars.get("SPY", [])]

    def close(self, sym, date):
        return self.closes_by_date.get(sym, {}).get(date)

    def close_on_or_before(self, sym, date, after_date=None):
        """Return last available close <= date, optionally requiring it after entry."""
        by_date = self.closes_by_date.get(sym, {})
        for d in reversed(self.dates_by_symbol.get(sym, [])):
            if d <= date and (after_date is None or d > after_date):
                return by_date.get(d)
        return None

    def fwd_return_pct(self, sym, ti, horizon):
        """sym 从 calendar[ti] 到 calendar[ti+horizon] 的收益（%）；缺价返回 None。"""
        if ti + horizon >= len(self.calendar):
            return None
        entry_date = self.calendar[ti]
        target_date = self.calendar[ti + horizon]
        c0 = self.close(sym, entry_date)
        c1 = self.close(sym, target_date)
        if c1 is None:
            dates = self.dates_by_symbol.get(sym, [])
            if sym in BANKRUPT and dates and dates[-1] < target_date:
                # 破产退市：股权清零，按 -100% 计（见 BANKRUPT 注释）
                return -100.0 if c0 else None
            # Delisted/merged names often stop before the benchmark target date.
            # Use the last available post-entry close so the observation remains
            # in the panel instead of silently reintroducing survivorship bias.
            c1 = self.close_on_or_before(sym, target_date, after_date=entry_date)
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
    logging.info(f"拉取 {len(syms)} 只标的日线 {start}→{end} …")
    bars, used_feed, note = fetch_bars(syms, start, end, feed, adjustment, 10000, timeout)
    if note:
        logging.info(note)
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


def bootstrap_ci(values, n_boot=2000, seed=42, alpha=0.05):
    """对均值做百分位 bootstrap，返回 (lo, hi) 置信区间；n<3 返回 None。

    固定 seed 保证确定性（同输入同输出）。非重叠 IC 序列近独立，普通
    bootstrap 足够；不做 block bootstrap。
    """
    import random
    n = len(values)
    if n < 3:
        return None
    rng = random.Random(seed)
    means = sorted(
        sum(values[rng.randrange(n)] for _ in range(n)) / n
        for _ in range(n_boot)
    )
    lo_idx = int(alpha / 2 * n_boot)
    hi_idx = int((1 - alpha / 2) * n_boot) - 1
    return means[lo_idx], means[hi_idx]


def sector_demean(triples):
    """[(score, fwd, sector)] → [(score, fwd − 同期同行业均值)]。

    用于行业中性 IC：把「买对行业」的收益成分去掉，只留行业内的个股区分度。
    行业在当期只有 1 只票时剔除该票（无行业内对照，去均值后恒为 0 会稀释信号）。
    """
    by_sector = {}
    for _, f, sec in triples:
        by_sector.setdefault(sec, []).append(f)
    means = {sec: sum(fs) / len(fs) for sec, fs in by_sector.items()}
    counts = {sec: len(fs) for sec, fs in by_sector.items()}
    return [(s, f - means[sec]) for s, f, sec in triples if counts[sec] > 1]


def bucket_mean(pairs, lo, hi):
    """pairs:[(score,fwd)]，取 score∈[lo,hi) 的前瞻收益均值与个数。"""
    sel = [f for s, f in pairs if lo <= s < hi]
    return (sum(sel) / len(sel), len(sel)) if sel else (None, 0)


COST_BPS_ONEWAY = 10.0  # 大盘股保守单边成本（佣金+滑点），用于净价差口径


def quintile_spread(pairs, cost_bps_oneway=COST_BPS_ONEWAY):
    """最高五分位均值 − 最低五分位均值；返回 (spread_gross, spread_net, top, bot)。

    净价差 = 毛价差 − 4×单边成本：多空两腿、每腿每期一买一卖共 4 次单边。
    样本 <10 返回 None。
    """
    if len(pairs) < 10:
        return None
    ss = sorted(pairs, key=lambda x: x[0])
    q = len(ss) // 5
    bot = sum(f for _, f in ss[:q]) / q
    top = sum(f for _, f in ss[-q:]) / q
    gross = top - bot
    net = gross - 4 * cost_bps_oneway / 100.0
    return gross, net, top, bot
