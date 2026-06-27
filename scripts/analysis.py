#!/usr/bin/env python3
"""单 symbol 指标分析与跨 symbol 聚合（相对 SPY/QQQ 强度）。

把 metrics.py 的原语组装成每个 symbol 的结构化结果，并计算相对强度。
离线模式从 multi-bars 形状 JSON 读入，便于测试与不触网回放。

build_result（跨票聚合 + 打分编排）已移至 pipeline.py；本模块只保留单票分析
与相对强度原语，不再依赖 scoring / finnhub / portfolio。
"""
from __future__ import annotations

import datetime
import json
import sys
from typing import Any

from metrics import (
    adx, annualized_return, annualized_vol, atr, efficiency_ratio, kdj, ma, macd,
    max_drawdown, momentum_12_1, obv, pct_return, rsi, to_weekly, trend_regression,
    up_day_volume_ratio, volume_ratio_ma, volume_trend_direction,
)

# 交易日窗口（约数）：1/3/6 个月、52 周
R1M, R3M, R6M = 21, 63, 126
TRADING_DAYS_52W = 252
STRUCT_WINDOW = 30          # 30 日结构（区间位置、高低点）
MIN_BARS = 30              # 计算核心指标所需的最少日线
MA60_SLOPE_LOOKBACK = 5    # MA60 方向：与 N 根前比较
MA200_SLOPE_LOOKBACK = 21  # MA200 方向：与约一月前比较


def analyze_symbol(bars: list[dict[str, Any]]) -> dict[str, Any]:
    closes = [b["c"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    vols = [b["v"] for b in bars]
    if len(closes) < MIN_BARS:
        return {"error": f"日线不足（<{MIN_BARS} 根），无法计算核心指标"}

    last = closes[-1]
    ma60 = ma(closes, 60)
    have_ma60 = ma60 is not None
    high_52w = max(highs[-TRADING_DAYS_52W:])
    high_52w_bars = min(len(highs), TRADING_DAYS_52W)
    low_w = min(lows[-STRUCT_WINDOW:])
    high_w = max(highs[-STRUCT_WINDOW:])
    rng = high_w - low_w
    pos = round((last - low_w) / rng * 100, 1) if rng else None
    dist_to_30d_high = round((last / high_w - 1) * 100, 2) if high_w else None

    vol_ma20 = ma(vols, 20)
    vol_ma50 = ma(vols, 50)
    # 20 日均成交额（美元）：流动性/可交易性的稳健代理，进 data_flags 用于低流动性警示
    dollar_vol_ma20 = ma([c * v for c, v in zip(closes, vols)], 20)

    wcloses = [w["c"] for w in to_weekly(bars)]
    # 最后一根日线非周五 => 当前周线尚未收盘，周线指标含半根
    ly, lm, ld = (int(x) for x in bars[-1]["t"][:10].split("-"))
    last_week_partial = datetime.date(ly, lm, ld).isoweekday() < 5

    # MA60 不可算时这两个字段必须为 None（无法确认），不能退化为 False，
    # 否则下游会把"缺历史"误读成"跌破 MA60"而触发强制排除。
    if have_ma60 and len(closes) >= 60 + MA60_SLOPE_LOOKBACK:
        ma60_rising = ma60 > ma(closes[:-MA60_SLOPE_LOOKBACK], 60)
    else:
        ma60_rising = None

    # MA200：长期趋势/regime 滤网（同样：不可算时为 None，不得退化为 False）
    ma200 = ma(closes, 200)
    have_ma200 = ma200 is not None
    if have_ma200 and len(closes) >= 200 + MA200_SLOPE_LOOKBACK:
        ma200_rising = ma200 > ma(closes[:-MA200_SLOPE_LOOKBACK], 200)
    else:
        ma200_rising = None

    # 量化因子：波动率、风险调整动量、回撤、ATR、趋势质量
    ann_vol_3m = annualized_vol(closes, R3M)
    ann_vol_6m = annualized_vol(closes, R6M)
    ann_ret_6m = annualized_return(closes, R6M)
    # 风险调整动量（Sharpe-like）：6 个月年化收益 / 年化波动
    risk_adj_6m = (round(ann_ret_6m / ann_vol_6m, 3)
                   if (ann_ret_6m is not None and ann_vol_6m) else None)
    atr14 = atr(highs, lows, closes, 14)

    return {
        "last_close": round(last, 4),
        "last_date": bars[-1]["t"][:10],
        "bars_count": len(closes),
        "ma": {
            "MA10": ma(closes, 10), "MA20": ma(closes, 20),
            "MA30": ma(closes, 30), "MA60": ma60, "MA200": ma200,
            "above_MA60": (last > ma60) if have_ma60 else None,
            "MA60_rising": ma60_rising,
            "above_MA200": (last > ma200) if have_ma200 else None,
            "MA200_rising": ma200_rising,
        },
        "volatility": {
            "ann_vol_3m_pct": ann_vol_3m,
            "ann_vol_6m_pct": ann_vol_6m,
        },
        "momentum": {
            "m12_1_pct": momentum_12_1(closes),
            "ann_return_6m_pct": ann_ret_6m,
            "risk_adj_6m": risk_adj_6m,
        },
        "risk": {
            "max_drawdown_6m_pct": max_drawdown(closes, R6M),
            "atr14": atr14,
            "atr_pct": round(atr14 / last * 100, 2) if (atr14 and last) else None,
        },
        "trend_quality": {
            "efficiency_30": efficiency_ratio(closes, STRUCT_WINDOW),
            "regression_3m": trend_regression(closes, R3M),
            "adx": adx(highs, lows, closes, 14),
        },
        "returns_pct": {
            "r1m_21d": pct_return(closes, R1M),
            "r3m_63d": pct_return(closes, R3M),
            "r6m_126d": pct_return(closes, R6M),
        },
        "structure_30d": {
            "ret_30bars": pct_return(closes, STRUCT_WINDOW),
            "range_position_pct": pos,
            "dist_to_30d_high_pct": dist_to_30d_high,
            "dist_to_52w_high_pct": round((last / high_52w - 1) * 100, 2) if high_52w else None,
            # 高点回看实际根数；<252 说明历史不足一年，52w 口径名不副实，需谨慎解读
            "high_lookback_bars": high_52w_bars,
        },
        "macd": macd(closes),
        "rsi": {"RSI14": rsi(closes, 14), "RSI6": rsi(closes, 6)},
        "kdj": kdj(highs, lows, closes),
        "volume": {
            "last": vols[-1],
            "ma20": vol_ma20,
            "ma50": vol_ma50,
            "ratio_vs_ma20": round(vols[-1] / vol_ma20, 2) if vol_ma20 else None,
            "ratio_vs_ma50": round(vols[-1] / vol_ma50, 2) if vol_ma50 else None,
            "dollar_vol_ma20": round(dollar_vol_ma20, 0) if dollar_vol_ma20 else None,
            "avg_ratio_5d": volume_ratio_ma(vols, 5, 20),
            "up_down_vol_ratio": up_day_volume_ratio(closes, vols, 10),
            "vol_trend_10d": volume_trend_direction(vols, 10),
            "obv": obv(closes, vols, 30),
        },
        "weekly": {
            "MA5": ma(wcloses, 5), "MA10": ma(wcloses, 10),
            "MA20": ma(wcloses, 20), "MA30": ma(wcloses, 30),
            "macd": macd(wcloses),
            "bearish_alignment": _weekly_bear(wcloses),
            # True 表示最后一根周线尚未收盘（含半根），周线交叉/排列判定需谨慎
            "last_week_partial": last_week_partial,
        },
    }


def _weekly_bear(wcloses: list[float]) -> bool | None:
    """周线均线空头排列：MA5<MA10<MA20<MA30 且有足够间距。

    用严格 `<` 而非 `<=`：横盘时四条均线近乎相等，`<=` 会被浮点相等/微噪触发，
    误判空头排列并触发否决（封顶 50）。再加间距门槛——MA5→MA30 跨度须 ≥ MA30 的
    1%，否则视为均线缠绕的横盘，而非真正的空头排列。
    """
    m5, m10, m20, m30 = ma(wcloses, 5), ma(wcloses, 10), ma(wcloses, 20), ma(wcloses, 30)
    if None in (m5, m10, m20, m30):
        return None
    strictly_bear = m5 < m10 < m20 < m30
    meaningful = m30 > 0 and (m30 - m5) / m30 >= 0.01
    return strictly_bear and meaningful


def relative_strength(sym_ret: dict[str, Any], bench_ret: dict[str, Any]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for k in ("r1m_21d", "r3m_63d", "r6m_126d"):
        a, b = sym_ret.get(k), bench_ret.get(k)
        out[k] = round(a - b, 2) if (a is not None and b is not None) else None
    return out


def _load_input(path: str) -> dict[str, list[dict[str, Any]]]:
    """从文件/stdin 读取 multi-bars 形状 JSON（{"bars": {...}}），返回 {symbol: [bar,...]}。"""
    if path == "-":
        raw = sys.stdin.read()
    else:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    data = json.loads(raw)
    bars = data.get("bars") if isinstance(data, dict) else None
    if not bars:
        raise RuntimeError("输入 JSON 缺少 bars 字段")
    out: dict[str, list[dict[str, Any]]] = {}
    for sym, blist in bars.items():
        bymt = {b["t"]: b for b in blist}
        out[sym] = [bymt[t] for t in sorted(bymt)]
    return out

