#!/usr/bin/env python3
"""纯指标计算原语：均线、EMA、MACD、RSI、KDJ、收益率、周线聚合。

只依赖 Python 标准库，不触网、不读写文件，便于单元测试与复用。
所有函数对输入做确定性计算；历史不足时返回 None（而非抛错或退化为 0）。

含两类原语：
  - 经典指标：均线、EMA、MACD、RSI、KDJ、收益率、周线聚合。
  - 量化因子：年化波动率、最大回撤、ATR、效率比、对数价格回归(斜率/R²)、12-1 动量。
"""
import datetime
import math

TRADING_DAYS = 252  # 年化用交易日数


def ema_series(values, n, seed="first"):
    """递归 EMA，返回与输入等长的序列。

    seed="first"（默认）：首值做种子，需约 3×n 根衰减偏置（历史行为）。
    seed="sma"：前 n 根 SMA 做种子，大幅缩短预热需求——MACD 用此模式。
    """
    if not values:
        return []
    k = 2.0 / (n + 1)
    if seed == "sma" and len(values) >= n:
        sma = sum(values[:n]) / n
        out = [sma] * n  # 前 n 根填 SMA 种子，对齐时间索引
        for v in values[n:]:
            out.append(v * k + out[-1] * (1 - k))
        return out
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def macd(closes, fast=12, slow=26, signal=9):
    """返回 DIF / DEA / 柱状(2*(DIF-DEA)) 的末值与最近交叉信息。

    三条 EMA 均用 SMA-seeded 初始化（前 N 根 SMA 做种子），将预热需求从
    ~3×slow 降到 ~slow+signal+5 根。
    """
    min_bars = slow + signal + 5  # SMA seed 后仅需 DIF → DEA 的递推长度
    if len(closes) < min_bars:
        return None
    ema_fast = ema_series(closes, fast, seed="sma")
    ema_slow = ema_series(closes, slow, seed="sma")
    dif = [f - s for f, s in zip(ema_fast, ema_slow)]
    dea = ema_series(dif, signal, seed="sma")
    hist = [2 * (d - e) for d, e in zip(dif, dea)]
    cross = _recent_cross(dif, dea, lookback=30)
    same_sign = len(hist) >= 2 and (hist[-1] >= 0) == (hist[-2] >= 0)
    return {
        "DIF": round(dif[-1], 4),
        "DEA": round(dea[-1], 4),
        "hist": round(hist[-1], 4),
        "above_zero": dif[-1] > 0 and dea[-1] > 0,
        "bull": dif[-1] > dea[-1],
        "hist_expanding": same_sign and abs(hist[-1]) > abs(hist[-2]),
        "recent_cross": cross,
    }


def _recent_cross(a, b, lookback=30):
    """检测序列 a 上穿/下穿 b 的最近一次交叉（金叉/死叉），在 lookback 根内。"""
    n = len(a)
    start = max(1, n - lookback)
    last = None
    for i in range(start, n):
        prev, cur = a[i - 1] - b[i - 1], a[i] - b[i]
        if prev <= 0 < cur:
            last = {"type": "golden", "bars_ago": n - 1 - i}
        elif prev >= 0 > cur:
            last = {"type": "death", "bars_ago": n - 1 - i}
    return last


def rsi(closes, n=14):
    """Wilder RSI。返回末值（0-100）。"""
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        avg_gain = (avg_gain * (n - 1) + gains[i]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i]) / n
    if avg_gain == 0 and avg_loss == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


def kdj(highs, lows, closes, n=9, k_smooth=3, d_smooth=3):
    """KDJ(n,k_smooth,d_smooth)。K/D 分别按各自平滑系数迭代，J=3K-2D。返回末值。"""
    if len(closes) < n:
        return None
    k_val, d_val = 50.0, 50.0
    ak, ad = 1.0 / k_smooth, 1.0 / d_smooth
    for i in range(n - 1, len(closes)):
        ll = min(lows[i - n + 1:i + 1])
        hh = max(highs[i - n + 1:i + 1])
        rsv = 50.0 if hh == ll else (closes[i] - ll) / (hh - ll) * 100
        k_val = (1 - ak) * k_val + ak * rsv
        d_val = (1 - ad) * d_val + ad * k_val
    j_val = 3 * k_val - 2 * d_val
    return {
        "K": round(k_val, 2),
        "D": round(d_val, 2),
        "J": round(j_val, 2),
        "bull": k_val > d_val,
        "above_50": k_val > 50 and d_val > 50,
    }


def ma(values, n):
    return round(sum(values[-n:]) / n, 4) if len(values) >= n else None


def pct_return(closes, bars_ago):
    if len(closes) <= bars_ago or closes[-1 - bars_ago] == 0:
        return None
    return round((closes[-1] / closes[-1 - bars_ago] - 1) * 100, 2)


def to_weekly(bars):
    """按 ISO 周聚合日线：周开=首根，周收=末根，周高=max，周低=min，量=sum。"""
    weeks = {}
    order = []
    for b in bars:
        # t 形如 2026-06-12T04:00:00Z；用日期算 (iso_year, iso_week)
        y, m, d = (int(x) for x in b["t"][:10].split("-"))
        key = datetime.date(y, m, d).isocalendar()[:2]
        if key not in weeks:
            weeks[key] = {"o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]}
            order.append(key)
        else:
            w = weeks[key]
            w["h"] = max(w["h"], b["h"])
            w["l"] = min(w["l"], b["l"])
            w["c"] = b["c"]
            w["v"] += b["v"]
    return [weeks[k] for k in order]


# ---- 量化因子原语 ----

def daily_returns(closes):
    """简单日收益率序列（剔除基准为 0 的点）。"""
    return [closes[i] / closes[i - 1] - 1
            for i in range(1, len(closes)) if closes[i - 1]]


def _sample_std(xs):
    """样本标准差（n-1）；样本数 < 2 返回 None。"""
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def annualized_vol(closes, window=63):
    """窗口内日收益率的年化波动率（百分比）。需要 ≥ window+1 根。"""
    if len(closes) < window + 1:
        return None
    s = _sample_std(daily_returns(closes[-(window + 1):]))
    return round(s * math.sqrt(TRADING_DAYS) * 100, 2) if s is not None else None


def annualized_return(closes, window=126):
    """窗口内复合年化收益率（百分比）。需要 ≥ window+1 根。"""
    if len(closes) < window + 1 or closes[-(window + 1)] <= 0:
        return None
    total = closes[-1] / closes[-(window + 1)]
    return round((total ** (TRADING_DAYS / window) - 1) * 100, 2)


def max_drawdown(closes, window=126):
    """窗口内最大回撤（负百分比；峰值到谷值的最深跌幅）。"""
    if len(closes) < window:
        return None
    seg = closes[-window:]
    if len(seg) < 2:
        return None
    peak, mdd = seg[0], 0.0
    for c in seg:
        if c > peak:
            peak = c
        if peak:
            mdd = min(mdd, c / peak - 1)
    return round(mdd * 100, 2)


def atr(highs, lows, closes, n=14):
    """Wilder ATR（绝对价格单位）。需要 ≥ n+1 根。"""
    if len(closes) < n + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    a = sum(trs[:n]) / n
    for i in range(n, len(trs)):
        a = (a * (n - 1) + trs[i]) / n
    return round(a, 4)


def efficiency_ratio(closes, n=30):
    """Kaufman 效率比：净位移/路径总长，∈[0,1]。1=单边干净趋势，0=纯震荡。"""
    if len(closes) < n + 1:
        return None
    seg = closes[-(n + 1):]
    net = abs(seg[-1] - seg[0])
    path = sum(abs(seg[i] - seg[i - 1]) for i in range(1, len(seg)))
    return round(net / path, 3) if path else None


def _linreg(values):
    """对 values 关于索引 0..n-1 做最小二乘，返回 (slope, r2)；退化时 None。"""
    n = len(values)
    if n < 3:
        return None
    xs = range(n)
    mx = (n - 1) / 2.0
    my = sum(values) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (values[x] - my) for x in xs)
    syy = sum((v - my) ** 2 for v in values)
    # 用相对阈值判退化：近乎零方差的序列趋势无定义（浮点噪声会让 syy 是 1e-28 量级
    # 而非精确 0，绝对 ==0 判定挡不住），返回 None 而非给出虚假的 r2=0。
    scale = max((abs(v) for v in values), default=0.0)
    if sxx == 0 or syy <= (scale * scale + 1.0) * 1e-18 * n:
        return None
    slope = sxy / sxx
    r2 = (sxy * sxy) / (sxx * syy)
    return slope, r2


def trend_regression(closes, window=63):
    """对数价格线性回归：年化斜率(%)与 R²（趋势平滑度）。需要 ≥3 个正价。"""
    seg = [c for c in closes[-window:] if c > 0]
    if len(seg) < 3:
        return None
    res = _linreg([math.log(c) for c in seg])
    if not res:
        return None
    slope, r2 = res
    ann = (math.exp(slope * TRADING_DAYS) - 1) * 100
    return {"ann_slope_pct": round(ann, 2), "r2": round(r2, 3)}


def momentum_12_1(closes):
    """学术口径 12-1 动量：t-252 到 t-21 的收益（跳过最近一月，避开短期反转）。

    closes[-1] = P(t)，则 P(t-21) = closes[-22]、P(t-252) = closes[-253]。
    历史不足 253 根时返回 None，而非用 closes[0] 近似——后者会把窗口悄悄
    变成「全历史-1」，得到与真 12-1 动量不可比的读数，污染跨股截面排序/IC。
    历史短的标的应缺这一读数（_f_momentum 会按可用权重重归一），不应给假值。
    """
    if len(closes) < 253:
        return None
    recent, base = closes[-22], closes[-253]
    if base <= 0:
        return None
    return round((recent / base - 1) * 100, 2)


def obv(closes, volumes, n=30):
    """On-Balance Volume（能量潮）与背离检测。

    计算累积 OBV 序列，对最近 n 根做线性回归，比较价格与 OBV 的趋势方向：
      - bearish divergence：价格趋势向上但 OBV 趋势向下（顶背离 = 派发预警）
      - bullish divergence：价格趋势向下但 OBV 趋势向上（底背离 = 累积信号）

    需要 ≥ n+1 根。返回 dict 或 None。
    """
    if len(closes) < n + 1:
        return None
    # 累积 OBV
    vals = [0.0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            vals.append(vals[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            vals.append(vals[-1] - volumes[i])
        else:
            vals.append(vals[-1])

    # OBV 最近 n 根的趋势方向
    obv_reg = _linreg(vals[-n:])
    obv_dir = None
    if obv_reg is not None:
        obv_dir = "rising" if obv_reg[0] > 0 else "falling"

    # 价格最近 n 根的趋势方向（对数回归）
    price_reg = _linreg([math.log(c) for c in closes[-n:] if c > 0])
    price_dir = None
    if price_reg is not None:
        price_dir = "rising" if price_reg[0] > 0 else "falling"

    divergence = None
    if price_dir == "rising" and obv_dir == "falling":
        divergence = "bearish"
    elif price_dir == "falling" and obv_dir == "rising":
        divergence = "bullish"

    return {
        "value": round(vals[-1], 1),
        "trend_30d": obv_dir,
        "divergence": divergence,
    }


def volume_ratio_ma(volumes, short_window=5, long_window=20):
    """近 short_window 天平均量能比（vs long_window 日均量）。

    比单日 ratio_vs_ma20 更稳健，消除单日噪声。
    需要 ≥ long_window+short_window 根。
    """
    total = long_window + short_window
    if len(volumes) < total:
        return None
    ma_long = sum(volumes[-(total):-short_window]) / long_window
    if not ma_long:
        return None
    recent = volumes[-short_window:]
    return round(sum(recent) / len(recent) / ma_long, 2)


def volume_trend_direction(volumes, window=10):
    """量能线性趋势方向。返回 "rising" / "falling" / None。"""
    if len(volumes) < window:
        return None
    reg = _linreg(volumes[-window:])
    if reg is None:
        return None
    return "rising" if reg[0] > 0 else "falling"


def up_day_volume_ratio(closes, volumes, window=10):
    """上涨日 vs 下跌日的平均量比。

    >1 = 上涨放量（健康），<1 = 下跌放量（派发）。
    需要 ≥ window+1 根且 window 内有涨有跌。
    """
    if len(closes) < window + 1:
        return None
    seg_c = closes[-(window + 1):]
    seg_v = volumes[-(window + 1):]
    up_vols, dn_vols = [], []
    for i in range(1, len(seg_c)):
        if seg_c[i] > seg_c[i - 1]:
            up_vols.append(seg_v[i])
        elif seg_c[i] < seg_c[i - 1]:
            dn_vols.append(seg_v[i])
    if not dn_vols or not up_vols:
        return None  # 全部上涨或全部下跌 → 无法比较
    avg_up = sum(up_vols) / len(up_vols)
    avg_dn = sum(dn_vols) / len(dn_vols)
    return round(avg_up / avg_dn, 2) if avg_dn else None


# ---- 入场时机（过热度 / 趋势内回调拐点）----

# 过热判定：距 MA20 的 ATR 倍数与 RSI14 超买（硬条件二选一，软条件需同时满足）
ENTRY_EXT_ATR_HARD = 3.0
ENTRY_EXT_RSI_HARD = 80.0
ENTRY_EXT_ATR_SOFT = 2.2
ENTRY_EXT_RSI_SOFT = 75.0
# 回调拐点：近 10 日低点较 30 日高点的最小回撤深度（%），或 5 日内触及 MA20
ENTRY_PULLBACK_MIN_DEPTH_PCT = -4.0
# 反弹确认：反转日之外还需的独立触发信号数
ENTRY_BOUNCE_MIN_SIGNALS = 2


def _ma_reclaim(closes, n, lookback=2):
    """近 lookback 根内收盘价由 n 日均线下方收复到上方，且当前仍在上方。"""
    total = len(closes)
    if total < n + lookback + 1:
        return None
    m_now = ma(closes, n)
    if m_now is None or closes[-1] <= m_now:
        return False
    for back in range(lookback):
        c_prev = closes[-2 - back]
        m_prev = ma(closes[:total - 1 - back], n)
        if m_prev is not None and c_prev <= m_prev:
            return True
    return False


def entry_timing(closes, highs, lows, volumes):
    """入场时机画像：过热度 + 趋势内回调拐点识别。历史 <60 根返回 None。

    只做时机分类，不判断趋势资格（趋势/相对强度由 ALPHA 因子与闸门负责）：
      - overextended：价格显著超出短期均值/RSI 超买 → 追高风险
      - pullback_reversal：趋势完好 + 近期回调 + 最后一根出现放量反转触发
      - pullback_no_trigger：回调中但反转未确认
      - trend_continuation：趋势正常推进、未过热
      - trend_broken：跌破中期均线（资格由闸门另行否决）
    """
    n = len(closes)
    if n < 60:
        return None
    last = closes[-1]
    ma20 = ma(closes, 20)
    ma60 = ma(closes, 60)
    ma200 = ma(closes, 200)
    atr14 = atr(highs, lows, closes, 14)
    rsi14 = rsi(closes, 14)

    # 过热度：距 MA20 的 ATR 倍数（均值回归引力的量纲化读数）
    ext_atr = round((last - ma20) / atr14, 2) if (ma20 and atr14) else None
    ext_pct = round((last / ma20 - 1) * 100, 2) if ma20 else None
    overextended = bool(
        (ext_atr is not None and ext_atr >= ENTRY_EXT_ATR_HARD)
        or (rsi14 is not None and rsi14 >= ENTRY_EXT_RSI_HARD)
        or (ext_atr is not None and rsi14 is not None
            and ext_atr >= ENTRY_EXT_ATR_SOFT and rsi14 >= ENTRY_EXT_RSI_SOFT)
    )

    # 回调结构必须满足「先高点、后低点、再到当前反转」的时间顺序。旧实现直接拿
    # 近 10 日低点除以近 30 日最高点；当最高点就是今天时，会把从旧低点上涨到
    # 新高的幅度误当成回撤。这里先定位近 10 日最低点，再只在它之前找高点。
    win = min(30, n)
    window_start = n - win
    recent_start = n - min(10, n)
    recent_low = min(lows[recent_start:])
    # 并列低点取最后一个，优先解释离当前反转最近的回调低点。
    recent_low_idx = recent_start + max(
        i for i, value in enumerate(lows[recent_start:]) if value == recent_low
    )
    prior_highs = highs[window_start:recent_low_idx]
    if prior_highs:
        hi = max(prior_highs)
        # 并列高点取最后一个，得到距离当前最近且仍早于低点的结构高点。
        hi_idx = window_start + max(i for i, v in enumerate(prior_highs) if v == hi)
        days_since_high = n - 1 - hi_idx
        depth = round((recent_low / hi - 1) * 100, 2) if hi else None
    else:
        hi, hi_idx, days_since_high, depth = None, None, None, None

    # 过去五日的低点必须与「当时」的 MA20 比较，不能用今天的 MA20 回看历史。
    touched_ma20_days = []
    for idx in range(max(19, n - 5), n):
        ma20_at_idx = ma(closes[:idx + 1], 20)
        if ma20_at_idx is not None and lows[idx] <= ma20_at_idx * 1.01:
            touched_ma20_days.append(idx)
    touched_ma20 = bool(touched_ma20_days)
    had_pullback = bool(
        (depth is not None and depth <= ENTRY_PULLBACK_MIN_DEPTH_PCT) or touched_ma20
    )

    # 反转日：收阳且收在日内区间上半部，或直接收在前一根最高价上方
    prev_close = closes[-2]
    up_day = last > prev_close
    day_range = highs[-1] - lows[-1]
    close_strength = round((last - lows[-1]) / day_range, 2) if day_range else None
    bounce_day = bool(up_day and (
        (close_strength is not None and close_strength >= 0.55) or last > highs[-2]
    ))

    # 独立触发信号
    vol_ma20 = ma(volumes, 20)
    vol_ratio = round(volumes[-1] / vol_ma20, 2) if vol_ma20 else None
    vol_expand = bool(vol_ratio is not None and vol_ratio >= 1.2)
    reclaim10 = _ma_reclaim(closes, 10)
    reclaim20 = _ma_reclaim(closes, 20)
    reclaim_ma = bool(reclaim10 or reclaim20)

    kdj_now = kdj(highs, lows, closes)
    kdj_p1 = kdj(highs[:-1], lows[:-1], closes[:-1])
    kdj_p2 = kdj(highs[:-2], lows[:-2], closes[:-2]) if n > 2 else None
    kdj_golden = bool(
        kdj_now and kdj_now.get("bull") and (
            (kdj_p1 and not kdj_p1.get("bull"))
            or (kdj_p1 and kdj_p1.get("bull") and kdj_p2 and not kdj_p2.get("bull"))
        )
    )

    macd_now = macd(closes)
    macd_p1 = macd(closes[:-1])
    macd_rising = bool(
        macd_now and macd_p1
        and macd_now.get("hist") is not None and macd_p1.get("hist") is not None
        and macd_now["hist"] > macd_p1["hist"]
    )

    rsi6_now = rsi(closes, 6)
    rsi6_p1 = rsi(closes[:-1], 6)
    rsi6_rebound = bool(
        rsi6_now is not None and rsi6_p1 is not None
        and rsi6_now > rsi6_p1 and rsi6_p1 < 50
    )

    signal_count = sum((vol_expand, reclaim_ma, kdj_golden, macd_rising, rsi6_rebound))

    # 趋势是否完好（时机分类的前提；不足以单独构成买入资格）
    trend_intact = bool(
        ma60 is not None and last >= ma60 * 0.97
        and (ma200 is None or last > ma200 * 0.99)
    )

    if not trend_intact:
        cls = "trend_broken"
        timing_score = 10
    elif overextended:
        cls = "overextended"
        timing_score = 20
    elif had_pullback and bounce_day and signal_count >= ENTRY_BOUNCE_MIN_SIGNALS:
        cls = "pullback_reversal"
        timing_score = min(75 + 5 * signal_count, 95)
    elif had_pullback and (ext_atr is None or ext_atr < 1.0):
        # 回调中但反转未确认（含反弹日信号不足）；已收复到 MA20 上方 1 ATR 以上的
        # 视为回调结束、回到趋势推进
        cls = "pullback_no_trigger"
        timing_score = 45
    else:
        cls = "trend_continuation"
        timing_score = 65
        if ext_atr is not None and ext_atr >= 2.0:
            timing_score = 45
        elif ext_atr is not None and ext_atr >= 1.5:
            timing_score = 55

    return {
        "ext_atr_above_ma20": ext_atr,
        "ext_pct_above_ma20": ext_pct,
        "rsi14": rsi14,
        "overextended": overextended,
        "trend_intact": trend_intact,
        "pullback": {
            "high_30d": round(hi, 4) if hi is not None else None,
            "days_since_30d_high": days_since_high,
            "recent_low_10d": round(recent_low, 4),
            "bars_since_recent_low": n - 1 - recent_low_idx,
            "depth_from_30d_high_pct": depth,
            "touched_ma20_5d": touched_ma20,
            "had_pullback": had_pullback,
        },
        "bounce": {
            "bounce_day": bounce_day,
            "up_day": up_day,
            "close_strength": close_strength,
            "vol_ratio_vs_ma20": vol_ratio,
            "vol_expand": vol_expand,
            "reclaim_ma10": reclaim10,
            "reclaim_ma20": reclaim20,
            "kdj_golden_3d": kdj_golden,
            "macd_hist_rising": macd_rising,
            "rsi6_rebound": rsi6_rebound,
            "signal_count": signal_count,
        },
        "classification": cls,
        "timing_score_pct": timing_score,
    }


def adx(highs, lows, closes, n=14):
    """Average Directional Index（Wilder 平滑）。

    返回 ADX 值、+DI、-DI 与趋势判定：
      - ADX > 25：强趋势市，> 40：极强趋势
      - ADX < 20：震荡/无趋势
      - +DI > -DI：上涨趋势，反之下跌趋势

    需要 ≥ n*2+1 根。与 efficiency_ratio 互补——
    efficiency_ratio 衡量趋势"干净度"，ADX 衡量趋势"力度"。
    """
    if len(closes) < n * 2 + 1:
        return None

    # True Range
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))

    # Directional Movements
    plus_dm, minus_dm = [], []
    for i in range(1, len(highs)):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)

    # Wilder 平滑：alpha = 1/n，首 n 根用 SMA
    def _wilder(values, period):
        if len(values) < period:
            return None
        k = 1.0 / period
        out = [sum(values[:period]) / period]
        for v in values[period:]:
            out.append(v * k + out[-1] * (1 - k))
        return out

    atr14 = _wilder(trs, n)
    pdi14 = _wilder(plus_dm, n)
    mdi14 = _wilder(minus_dm, n)

    if not all((atr14, pdi14, mdi14)):
        return None

    # +DI / -DI（百分比）与 DX
    pdi = [round(p / t * 100, 2) if t else 0.0 for p, t in zip(pdi14, atr14)]
    mdi = [round(m / t * 100, 2) if t else 0.0 for m, t in zip(mdi14, atr14)]
    dx = [abs(p - m) / (p + m) * 100 if (p + m) > 0 else 0.0
          for p, m in zip(pdi, mdi)]

    # ADX = Wilder 平滑的 DX
    adx14 = _wilder(dx, n)
    if not adx14:
        return None

    return {
        "ADX": round(adx14[-1], 2),
        "plus_DI": pdi[-1],
        "minus_DI": mdi[-1],
        "trend_strong": adx14[-1] >= 25,
        "bull_trend": adx14[-1] >= 20 and pdi[-1] > mdi[-1],
    }
