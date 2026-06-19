#!/usr/bin/env python3
"""把日线 OHLC 渲染成终端蜡烛图（K 线），仅用 Python 标准库 + 本机 alpaca CLI。

两种取数方式：
  - 自取（默认，最省事）：给 --symbol，脚本用 fetching.fetch_bars 自行拉取日线
    （默认窗口足够画 --count 根，自动分页与 feed 回退），无需手动传日期。
  - 管道/离线：把 multi-bars 形状 JSON（{"bars": {...}}）从 stdin 或 --input 喂入。

用 Unicode 半块字符（▀▄█）画实体、│ 画上下影线，纵向分辨率翻倍；涨绿跌红（ANSI）。
非 TTY（管道/重定向）默认关色，可用 --color 强制开、--no-color 强制关。

用法：
  chart.py --symbol AAPL                                  # 自取数据，最常用
  alpaca data multi-bars --symbols AAPL ... | chart.py --symbol AAPL --input -
  chart.py --input bars.json --symbol AAPL --count 30 --rows 14
"""
import argparse
import datetime
import json
import sys

GREEN, RED, RESET = "\033[32m", "\033[31m", "\033[0m"


def _default_window(count, end=None):
    """自取模式的默认日期窗口：留足日历日以覆盖 count 根交易日（约 1.5×+缓冲）。"""
    end_d = datetime.date.fromisoformat(end) if end else datetime.date.today()
    days = max(90, count * 2 + 10)  # 交易日≈日历日/1.4，再留缓冲
    return (end_d - datetime.timedelta(days=days)).isoformat(), end_d.isoformat()


def load_bars(path, symbol=None):
    """读 multi-bars JSON，返回某 symbol 的升序 bar 列表。"""
    raw = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    data = json.loads(raw)
    bars = data.get("bars") if isinstance(data, dict) else None
    if not bars:
        raise RuntimeError("输入 JSON 缺少 bars 字段")
    if symbol:
        sym = symbol.upper()
        if sym not in bars:
            raise RuntimeError(f"输入中没有 {sym}；可选: {','.join(bars)}")
    elif len(bars) == 1:
        sym = next(iter(bars))
    else:
        raise RuntimeError(f"多个 symbol，需用 --symbol 指定: {','.join(bars)}")
    blist = bars[sym]
    bymt = {b["t"]: b for b in blist}
    return sym, [bymt[t] for t in sorted(bymt)]


def _fill_subrows(lo, hi, max_p, min_p, sub):
    """价格区间 [lo,hi] 覆盖到哪些子行（0=顶）。sub=子行总数=2*rows。"""
    span = max_p - min_p
    if span <= 0:
        return {sub // 2}
    out = set()
    for s in range(sub):
        top = max_p - s / sub * span
        bot = max_p - (s + 1) / sub * span
        if hi >= bot and lo <= top:
            out.add(s)
    return out


def render(symbol, bars, count=30, rows=14, color=True):
    bars = bars[-count:]
    if not bars:
        return f"{symbol}: 无数据"
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    max_p, min_p = max(highs), min(lows)
    sub = rows * 2

    # 逐根算出每根蜡烛在各 char 行的字符与颜色
    cols = []
    for b in bars:
        o, c, h, low = b["o"], b["c"], b["h"], b["l"]
        up = c >= o
        body_lo, body_hi = min(o, c), max(o, c)
        body = _fill_subrows(body_lo, body_hi, max_p, min_p, sub)
        wick = _fill_subrows(low, h, max_p, min_p, sub)
        col = []
        for r in range(rows):
            us, ls = 2 * r, 2 * r + 1
            ub, lb = us in body, ls in body
            if ub and lb:
                ch = "█"
            elif ub:
                ch = "▀"
            elif lb:
                ch = "▄"
            elif us in wick or ls in wick:
                ch = "│"
            else:
                ch = " "
            col.append((ch, up))
        cols.append(col)

    # 左侧价格刻度：顶/中/底
    labels = {0: max_p, rows // 2: (max_p + min_p) / 2, rows - 1: min_p}
    width = max(len(f"{p:.2f}") for p in labels.values())
    lines = []
    for r in range(rows):
        axis = f"{labels[r]:>{width}.2f}" if r in labels else " " * width
        cells = []
        for col in cols:
            ch, up = col[r]
            if ch == " " or not color:
                cells.append(ch)
            else:
                cells.append(f"{GREEN if up else RED}{ch}{RESET}")
        lines.append(f"{axis} ┤{''.join(cells)}")

    first, last = bars[0]["t"][:10], bars[-1]["t"][:10]
    lastc = bars[-1]["c"]
    chg = (lastc / bars[0]["c"] - 1) * 100 if bars[0]["c"] else 0
    head = f"{symbol} 日K · 最近 {len(bars)} 根 · {first}→{last} · 收 {lastc:.2f} ({chg:+.2f}%)"
    foot = " " * (width + 2) + "└" + "─" * len(cols)
    return "\n".join([head, *lines, foot])


def _fetch(symbol, args):
    """自取模式：用 fetching.fetch_bars 拉日线（默认窗口足够画 count 根）。"""
    if not symbol:
        raise RuntimeError("自取模式需要 --symbol")
    from fetching import fetch_bars  # 延迟导入：仅自取时依赖 alpaca CLI
    dft_start, dft_end = _default_window(args.count, args.end)
    start = args.start or dft_start
    end = args.end or dft_end
    bars, _used_feed, _note = fetch_bars(
        [symbol.upper()], start, end, args.feed,
        args.adjustment, args.limit, args.timeout)
    blist = bars.get(symbol.upper())
    if not blist:
        raise RuntimeError(f"未取到 {symbol} 的日线数据")
    return symbol.upper(), blist


def main():
    p = argparse.ArgumentParser(description="终端蜡烛图（K 线）")
    p.add_argument("--input", help="multi-bars JSON 文件或 '-'(stdin)；缺省时自取数据")
    p.add_argument("--symbol", help="要画的标的；自取模式必填，管道输入只含一个时可省略")
    p.add_argument("--count", type=int, default=30, help="蜡烛根数，默认 30")
    p.add_argument("--rows", type=int, default=14, help="图高（字符行），默认 14")
    p.add_argument("--color", action="store_true", help="强制开启颜色")
    p.add_argument("--no-color", action="store_true", help="强制关闭颜色")
    # 自取模式参数（与 indicators.py 对齐）
    p.add_argument("--start", help="自取起始日；缺省按 --count 自动回推")
    p.add_argument("--end", help="自取结束日；缺省今天")
    p.add_argument("--feed", default="iex")
    p.add_argument("--adjustment", default="split")
    p.add_argument("--limit", type=int, default=10000)
    p.add_argument("--timeout", type=int, default=30)
    args = p.parse_args()

    # 取数来源（确定性，不依赖 isatty）：给了 --input 就读 JSON（文件或 '-' 走 stdin），
    # 否则自取。管道用法须显式 `--input -`，避免非交互 shell 下误判 stdin。
    if args.input is not None:
        sym, bars = load_bars(args.input, args.symbol)
    else:
        sym, bars = _fetch(args.symbol, args)

    color = sys.stdout.isatty()
    if args.color:
        color = True
    if args.no_color:
        color = False
    sys.stdout.write(render(sym, bars, args.count, args.rows, color) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"chart error: {e}\n")
        sys.exit(1)
