"""Cross-sectional factor, score and robustness studies."""
from __future__ import annotations

import datetime
from typing import Any

import scoring as S
from fetching import FeedLimitError, _fetch_feed
from metrics import ma
from skill_logging import get_logger

from .panel import load_panel
from .statistics import bootstrap_ci, bucket_mean, mean_t, quintile_spread, sector_demean, spearman
from .universe import DELISTED, HISTORY_DAYS, HORIZONS, SECTOR, STEP, UNIVERSE, WARMUP


logger = get_logger("backtesting.cross_sectional")
ALPHA_FACTORS = list(S.ALPHA_WEIGHTS)
RISK_FACTORS = ["trend"]
OVERLAY_FACTORS = ["technical", "volume_exec"]
CONFIRMATION_FACTORS = ["trend_quality"]
COLS = ALPHA_FACTORS + RISK_FACTORS + CONFIRMATION_FACTORS + OVERLAY_FACTORS + ["composite"]
RESIDUAL_PROBE = ["SBNY", "CREE", "BBBY", "WE", "RAD"]


def _pct(score_0_1):
    return round(score_0_1 * 100) if score_0_1 is not None else None


def factor_features(row: dict[str, Any]) -> dict[str, Any]:
    score = row.get("score", {}) or {}
    breakdown = score.get("factor_breakdown", {})
    confirmation = score.get("confirmation", {}) or {}
    features = {
        factor: (breakdown.get(factor, {}) or {}).get("score_pct")
        for factor in ALPHA_FACTORS
    }
    features["trend"] = _pct(S.FACTOR_FNS["trend"](row))
    features["trend_quality"] = confirmation.get("trend_quality_pct")
    features["technical"] = confirmation.get("technical_pct")
    features["volume_exec"] = confirmation.get("volume_pct")
    features["composite"] = score.get("composite")
    return features


def run_factor_ic(feed, adjustment, timeout, *, cache_mode="auto", cache_db=None):
    panel = load_panel(feed, adjustment, timeout, cache_mode=cache_mode, cache_db=cache_db)
    rebalance = list(range(WARMUP, len(panel.calendar) - max(HORIZONS), STEP))
    logger.info("backtest_rebalance_planned", rebalance_count=len(rebalance))
    series = {horizon: {column: [] for column in COLS} for horizon in HORIZONS}
    for time_index in rebalance:
        symbols = panel.scores_at(panel.calendar[time_index])
        rows = []
        for symbol in UNIVERSE:
            row = symbols.get(symbol, {})
            if not row.get("score"):
                continue
            future = {
                horizon: panel.fwd_return_pct(symbol, time_index, horizon)
                for horizon in HORIZONS
            }
            rows.append((factor_features(row), future))
        for horizon in HORIZONS:
            for column in COLS:
                values = [
                    (row[0][column], row[1][horizon]) for row in rows
                    if row[0].get(column) is not None and row[1][horizon] is not None
                ]
                if len(values) >= 5:
                    ic = spearman([a for a, _ in values], [b for _, b in values])
                    if ic is not None:
                        series[horizon][column].append(ic)
    report_factor_ic(series)


def report_factor_ic(series):
    print("\n" + "=" * 70)
    print("逐因子单因子 IC（子分 vs 前瞻收益；IC>0 = 该因子分越高、未来收益越高）")
    print("=" * 70)
    for horizon in sorted(series):
        print(f"\n── 前瞻 {horizon} 交易日 ──")
        print(f"   {'因子':<14}{'IC均值':>9}{'t':>8}{'IC>0%':>8}{'期数':>6}")
        ranked = []
        for column in COLS:
            stats = mean_t(series[horizon][column])
            if stats:
                mean_ic, t_value, count = stats
                positive = sum(value > 0 for value in series[horizon][column]) / count * 100
                ranked.append((column, mean_ic, t_value, positive, count))
        for column, mean_ic, t_value, positive, count in sorted(ranked, key=lambda row: row[1], reverse=True):
            tag = "  ← composite" if column == "composite" else ""
            print(f"   {column:<14}{mean_ic:>+9.3f}{t_value:>+8.2f}{positive:>7.0f}%{count:>6}{tag}")
    print("\n注：63 日前瞻用 21 日步长 => 约 3 倍重叠，真实 t 约为表中值 ÷ √3；")
    print("    篮子有幸存者偏差。重点看因子间相对排序与 IC 符号，而非绝对显著性。")


def run_score(feed, adjustment, timeout, *, cache_mode="auto", cache_db=None):
    panel = load_panel(feed, adjustment, timeout, cache_mode=cache_mode, cache_db=cache_db)
    if len(panel.calendar) < WARMUP + max(HORIZONS) + STEP:
        logger.warning(
            "backtest_history_insufficient",
            sessions=len(panel.calendar),
            required=WARMUP + max(HORIZONS) + STEP,
        )
        return
    rebalance = list(range(WARMUP, len(panel.calendar) - max(HORIZONS), STEP))
    logger.info("backtest_rebalance_planned", rebalance_count=len(rebalance))
    observations = {horizon: [] for horizon in HORIZONS}
    ic_by_date = {horizon: [] for horizon in HORIZONS}
    gate_ab = {horizon: {"ok": [], "blocked": []} for horizon in HORIZONS}
    for time_index in rebalance:
        symbols = panel.scores_at(panel.calendar[time_index])
        per_date = {horizon: ([], []) for horizon in HORIZONS}
        for symbol in UNIVERSE:
            score = symbols.get(symbol, {}).get("score") or {}
            composite = score.get("composite")
            if composite is None:
                continue
            candidate = composite >= 75 and not score.get("risk_gates")
            bucket = "ok" if (score.get("confirmation") or {}).get("ok") else "blocked"
            for horizon in HORIZONS:
                future = panel.fwd_return_pct(symbol, time_index, horizon)
                if future is None:
                    continue
                observations[horizon].append((composite, future))
                per_date[horizon][0].append(composite)
                per_date[horizon][1].append(future)
                if candidate:
                    gate_ab[horizon][bucket].append(future)
        for horizon in HORIZONS:
            ic = spearman(*per_date[horizon])
            if ic is not None:
                ic_by_date[horizon].append(ic)
    report_score(observations, ic_by_date)
    report_gate_ab(gate_ab)


def report_score(observations, ic_by_date):
    print("\n" + "=" * 60)
    print("因子有效性回测结果（composite 分 vs 前瞻收益）")
    print("=" * 60)
    for horizon in sorted(observations):
        pairs = observations[horizon]
        print(f"\n── 前瞻 {horizon} 交易日 ──   样本数 N={len(pairs)}")
        for label, lower, upper in (("分数<60 (否)", 0, 60), ("60-75 (观察)", 60, 75), (">=75 (是)", 75, 1e9)):
            mean, count = bucket_mean(pairs, lower, upper)
            print(f"   {label:14} 平均前瞻收益 {mean:+.2f}%   (n={count})" if mean is not None else f"   {label:14} 无样本")
        spread = quintile_spread(pairs)
        if spread:
            gross, net, top, bottom = spread
            print(f"   五分位: 最高分组 {top:+.2f}%  最低分组 {bottom:+.2f}%  多空价差 毛 {gross:+.2f}% / 净 {net:+.2f}%（扣单边10bp×4）")
        stats = mean_t(ic_by_date[horizon])
        if stats:
            mean_ic, t_value, count = stats
            positive = sum(value > 0 for value in ic_by_date[horizon]) / count * 100
            print(f"   Spearman 截面 IC: 均值 {mean_ic:+.3f}  t≈{t_value:+.2f}  IC>0 占比 {positive:.0f}%  ({count} 期)")
    print("\n注：篮子含幸存者偏差，真实 edge 偏乐观；看方向与单调性而非绝对收益。")


def report_gate_ab(gate_ab):
    print("\n" + "=" * 60)
    print("confirmation gate A/B（composite≥75 且无否决的买入候选）")
    print("=" * 60)
    for horizon in sorted(gate_ab):
        ok, blocked = gate_ab[horizon]["ok"], gate_ab[horizon]["blocked"]
        mean_ok = sum(ok) / len(ok) if ok else None
        mean_blocked = sum(blocked) / len(blocked) if blocked else None
        print(f"\n── 前瞻 {horizon} 交易日 ──")
        print(f"   confirmation.ok=True  (放行) 平均前瞻收益 {mean_ok:+.2f}%  (n={len(ok)})" if mean_ok is not None else "   confirmation.ok=True  (放行) 无样本")
        print(f"   confirmation.ok=False (封顶) 平均前瞻收益 {mean_blocked:+.2f}%  (n={len(blocked)})" if mean_blocked is not None else "   confirmation.ok=False (封顶) 无样本")
        if mean_ok is not None and mean_blocked is not None:
            verdict = "gate 有效（放行组更优）" if mean_ok > mean_blocked else "gate 存疑（封顶组不差，可能在砍真买点）"
            print(f"   差值 {mean_ok - mean_blocked:+.2f}% → {verdict}")
    print("\n注：样本量通常很小（买入候选稀少），仅看方向参考；需多次/长窗口累积。")


def regime_map(spy_bars):
    closes = [bar["c"] for bar in spy_bars]
    dates = [bar["t"][:10] for bar in spy_bars]
    return {
        dates[index]: ((closes[index] > average) if (average := ma(closes[:index + 1], 200)) is not None else None)
        for index in range(len(closes))
    }


def ic_line(label, pairs, values):
    if not pairs:
        print(f"   {label:16} 无样本")
        return
    spread_result = quintile_spread(pairs)
    spread = f"毛{spread_result[0]:+.2f}%/净{spread_result[1]:+.2f}%" if spread_result else "n/a"
    stats = mean_t(values)
    ci = bootstrap_ci(values)
    ci_text = f" 95%CI[{ci[0]:+.3f},{ci[1]:+.3f}]" if ci else ""
    ic_text = f"IC {stats[0]:+.3f} t≈{stats[1]:+.2f}{ci_text} ({stats[2]}期独立)" if stats else "IC n/a"
    print(f"   {label:16} N={len(pairs):<5} 五分位多空 {spread:<9} {ic_text}")


def run_robustness(feed, adjustment, timeout, *, cache_mode="auto", cache_db=None):
    panel = load_panel(feed, adjustment, timeout, cache_mode=cache_mode, cache_db=cache_db)
    regimes = regime_map(panel.bars.get("SPY", []))
    print("\n" + "=" * 72)
    print("硬化回测 1+2：非重叠窗口 + regime 拆分（composite vs 前瞻收益）")
    print("=" * 72)
    for horizon in (21, 63):
        rebalance = list(range(WARMUP, len(panel.calendar) - horizon, horizon))
        groups = {"all": ([], []), "on": ([], []), "off": ([], [])}
        neutral = {"all": ([], []), "on": ([], []), "off": ([], [])}
        for time_index in rebalance:
            date = panel.calendar[time_index]
            symbols = panel.scores_at(date)
            regime = "on" if regimes.get(date) else "off"
            triples = []
            for symbol in UNIVERSE:
                composite = (symbols.get(symbol, {}).get("score") or {}).get("composite")
                future = panel.fwd_return_pct(symbol, time_index, horizon)
                if composite is not None and future is not None:
                    triples.append((composite, future, SECTOR.get(symbol, "unknown")))
            pairs = [(score, future) for score, future, _ in triples]
            neutral_pairs = sector_demean(triples)
            for target, values in ((groups, pairs), (neutral, neutral_pairs)):
                target["all"][0].extend(values)
                target[regime][0].extend(values)
                ic = spearman([pair[0] for pair in values], [pair[1] for pair in values])
                if ic is not None:
                    target["all"][1].append(ic)
                    target[regime][1].append(ic)
        print(f"\n── 前瞻 {horizon} 交易日（独立 rebalance {len(rebalance)} 期）──")
        ic_line("全样本", *groups["all"])
        ic_line("全样本·行业中性", *neutral["all"])
        ic_line("risk-on", *groups["on"])
        ic_line("risk-on·行业中性", *neutral["on"])
        ic_line("risk-off", *groups["off"])
        ic_line("risk-off·行业中性", *neutral["off"])
    survivorship_probe(feed, adjustment, timeout)
    print("\n注：非重叠后 63 日独立样本很少（~7 期），显著性必然弱；regime 看 edge 是否在 risk-off 仍为正。幸存者探针量化篮子缺口方向。")


def survivorship_probe(feed, adjustment, timeout):
    end = datetime.date.today().isoformat()
    start = (datetime.date.today() - datetime.timedelta(days=HISTORY_DAYS)).isoformat()
    print("\n" + "=" * 72)
    print("硬化 3：幸存者偏差探针（篮子已纳入死亡名字 + 残余缺口）")
    print("=" * 72)
    print(f"   已纳入篮子的死亡票（降偏差）: {', '.join(DELISTED)}")
    served, missing = [], []
    for symbol in RESIDUAL_PROBE:
        try:
            count = len(_fetch_feed([symbol], start, end, feed, adjustment, 10000, timeout).get(symbol, []))
        except (FeedLimitError, RuntimeError):
            count = 0
        (served if count > 0 else missing).append((symbol, count))
    print(f"   残余缺口·可拉到: {', '.join(f'{symbol}({count})' for symbol, count in served) or '无'}")
    print(f"   残余缺口·全缺失: {', '.join(symbol for symbol, _ in missing) or '无'}")
    print("   解读：DELISTED 已让篮子接近「窗口起点的大盘」，存活期会被打分、前瞻收益含")
    print("        暴跌/收购退出。残余缺口（数据存疑/未列出的破产票）说明偏差降低但未根除。")
