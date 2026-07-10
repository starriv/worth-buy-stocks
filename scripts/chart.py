#!/usr/bin/env python3
"""把日线 OHLC 渲染成终端蜡烛图（K 线），仅用 Python 标准库 + 本机 alpaca CLI。

两种取数方式：
  - 自取（默认，最省事）：给 --symbol，脚本用 fetching.fetch_bars 自行拉取日线
    （默认窗口足够画 --count 根，自动分页与 feed 回退），无需手动传日期。
  - 管道/离线：把 multi-bars 形状 JSON（{"bars": {...}}）从 stdin 或 --input 喂入。

用 Unicode 半块字符（▀▄█）画实体、│ 画上下影线，纵向分辨率翻倍；涨绿跌红（ANSI）。
默认开色（含管道/重定向，agent 执行时也带色）；--no-color 或 NO_COLOR 环境变量关闭，
--color 强制开（覆盖 NO_COLOR）。

ANSI 颜色只有输出直达终端才可见（经 agent 转写进 markdown 会被剥掉），所以另有
--html [路径] 生成自包含彩色 HTML 图（SVG，颜色是内容不怕转手；含悬浮 OHLC、
价位线标签、数据表），--open 生成后自动用浏览器打开。

可叠加入场/止盈/止损价位线（水平虚线 + 右侧标签，价格轴自动扩展到能容纳价位线）：
  - --plan：喂 indicators.py 的评分结果 JSON（或裸 trade_plan JSON），自动提取
    suggested_entry_price（缺失时回退 pullback_entry_price）、stop_loss_price、
    take_profit_price、take_profit_2_price。
  - --entry/--stop-loss/--take-profit/--take-profit-2：手动指定，覆盖 --plan 同名价位。
价位线配色按行业惯例：入场蓝、回踩入场（待触发）黄、止损红、止盈绿/亮绿。
价位线只做可视化参考，与 trade_plan 一样不是订单指令。

用法：
  chart.py --symbol AAPL                                  # 自取数据，最常用
  alpaca data multi-bars --symbols AAPL ... | chart.py --symbol AAPL --input -
  chart.py --input bars.json --symbol AAPL --count 30 --rows 14
  chart.py --symbol AAPL --plan result.json               # 叠加评分给出的计划价
  chart.py --symbol AAPL --entry 250 --stop-loss 238 --take-profit 274
  chart.py --symbol AAPL --plan result.json --html --open # 同时生成并打开彩色 HTML 图
"""
import argparse
import datetime
import html as html_mod
import json
import os
import sys

GREEN, RED, RESET = "\033[32m", "\033[31m", "\033[0m"
# 价位线配色按行业惯例（TradingView 风格）：开仓线蓝、待触发挂单黄、止损红、
# 止盈绿（第二档亮绿区分梯度）。蓝/绿用 bright 变体保证深色终端可读。
BLUE, YELLOW, BRIGHT_GREEN = "\033[94m", "\033[33m", "\033[92m"
COLOR_ENTRY, COLOR_PENDING, COLOR_STOP = BLUE, YELLOW, RED
COLOR_TP1, COLOR_TP2 = GREEN, BRIGHT_GREEN

# 价位线 = (标签, 价格, 颜色) 三元组；LEVEL_CHAR 只填蜡烛空白处，不覆盖 K 线
LEVEL_CHAR = "┄"

# —— HTML(SVG) 深色主题 ——
# 颜色作为 SVG 属性（内容）而非 ANSI 指令存在，经管道/agent 转手不丢色。
# 五个数据色已通过 dataviz 校验（亮度带/色度/CVD 分离/对深色底对比度≥3:1）；
# 价位线均带文字标签，识别不只靠颜色。
HTML_BG, HTML_PANEL = "#131722", "#1e222d"
HTML_INK, HTML_MUTED, HTML_GRID = "#d1d4dc", "#787b86", "#2a2e39"
HTML_UP, HTML_DOWN = "#26a69a", "#ef5350"
HTML_LEVEL = {COLOR_ENTRY: "#2962ff", COLOR_PENDING: "#c07f0c",
              COLOR_STOP: "#ef5350", COLOR_TP1: "#26a69a", COLOR_TP2: "#00a95c"}


def _default_window(count, end=None):
    """自取模式的默认日期窗口：留足日历日以覆盖 count 根交易日（约 1.5×+缓冲）。"""
    end_d = datetime.date.fromisoformat(end) if end else datetime.date.today()
    days = max(90, count * 2 + 10)  # 交易日≈日历日/1.4，再留缓冲
    return (end_d - datetime.timedelta(days=days)).isoformat(), end_d.isoformat()


def load_bars(path, symbol=None):
    """读 multi-bars JSON，返回某 symbol 的升序 bar 列表。"""
    if path == "-":
        raw = sys.stdin.read()
    else:
        with open(path, encoding="utf-8") as plan_file:
            raw = plan_file.read()
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


def load_plan(path, symbol=None):
    """读交易计划 JSON，返回 trade_plan dict（可能为空）。

    兼容三种形状：indicators.py 评分结果（symbols.{SYM}.score.trade_plan）、
    {"trade_plan": {...}} 包裹、裸 trade_plan 对象。
    """
    if path == "-":
        raw = sys.stdin.read()
    else:
        with open(path, encoding="utf-8") as plan_file:
            raw = plan_file.read()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise RuntimeError("计划 JSON 不是对象")
    syms = data.get("symbols")
    if isinstance(syms, dict) and syms:
        if symbol:
            sym = symbol.upper()
            if sym not in syms:
                raise RuntimeError(f"计划 JSON 中没有 {sym}；可选: {','.join(syms)}")
        elif len(syms) == 1:
            sym = next(iter(syms))
        else:
            raise RuntimeError(f"计划 JSON 含多个 symbol，需用 --symbol 指定: {','.join(syms)}")
        plan = ((syms[sym] or {}).get("score") or {}).get("trade_plan") or {}
    elif isinstance(data.get("trade_plan"), dict):
        plan = data["trade_plan"]
    else:
        plan = data
    if not isinstance(plan, dict) or plan.get("status") == "unavailable":
        return {}
    return plan


def plan_levels(plan):
    """从 trade_plan 提取价位线 [(标签, 价格, 颜色)]；缺失字段直接跳过。

    入场价优先 suggested_entry_price；为 null（不建议按现价开）时回退
    pullback_entry_price 并标注为回踩入场，保持与 trade_plan 语义一致。
    """
    if not isinstance(plan, dict):
        return []
    fields = [("suggested_entry_price", "入场", COLOR_ENTRY),
              ("stop_loss_price", "止损", COLOR_STOP),
              ("take_profit_price", "止盈1", COLOR_TP1),
              ("take_profit_2_price", "止盈2", COLOR_TP2)]
    if plan.get("suggested_entry_price") is None:
        # 回踩入场是待触发的等待价，不是当下开仓线，按挂单惯例用黄色
        fields[0] = ("pullback_entry_price", "回踩入场", COLOR_PENDING)
    out = []
    for key, label, color_code in fields:
        v = plan.get(key)
        if isinstance(v, (int, float)) and v > 0:
            out.append((label, float(v), color_code))
    return out


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


def _price_row(price, max_p, min_p, rows):
    """价格映射到 char 行号（0=顶），越界钳到边界行。"""
    span = max_p - min_p
    if span <= 0:
        return rows // 2
    return min(max(int((max_p - price) / span * rows), 0), rows - 1)


def render(symbol, bars, count=30, rows=14, color=True, levels=None):
    bars = bars[-count:]
    if not bars:
        return f"{symbol}: 无数据"
    levels = [lv for lv in (levels or []) if lv[1] and lv[1] > 0]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    # 价格轴扩展到能容纳所有价位线，止损/止盈在图外时依然可见
    max_p = max(highs + [lv[1] for lv in levels])
    min_p = min(lows + [lv[1] for lv in levels])
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

    # 价位线按行分组；同一行多条时合并右侧标签，线色取价格最高的一条
    row_levels = {}
    for lv in sorted(levels, key=lambda x: -x[1]):
        row_levels.setdefault(_price_row(lv[1], max_p, min_p, rows), []).append(lv)

    # 左侧价格刻度：顶/中/底
    labels = {0: max_p, rows // 2: (max_p + min_p) / 2, rows - 1: min_p}
    width = max(len(f"{p:.2f}") for p in labels.values())
    lines = []
    for r in range(rows):
        axis = f"{labels[r]:>{width}.2f}" if r in labels else " " * width
        lvs = row_levels.get(r)
        lv_color = lvs[0][2] if lvs else ""
        cells = []
        for col in cols:
            ch, up = col[r]
            if ch == " " and lvs:
                cells.append(f"{lv_color}{LEVEL_CHAR}{RESET}" if color else LEVEL_CHAR)
            elif ch == " " or not color:
                cells.append(ch)
            else:
                cells.append(f"{GREEN if up else RED}{ch}{RESET}")
        suffix = ""
        if lvs:
            tags = " ".join(f"{label} {price:.2f}" for label, price, _ in lvs)
            suffix = f" {lv_color}◂ {tags}{RESET}" if color else f" ◂ {tags}"
        lines.append(f"{axis} ┤{''.join(cells)}{suffix}")

    first, last = bars[0]["t"][:10], bars[-1]["t"][:10]
    lastc = bars[-1]["c"]
    chg = (lastc / bars[0]["c"] - 1) * 100 if bars[0]["c"] else 0
    head = f"{symbol} 日K · 最近 {len(bars)} 根 · {first}→{last} · 收 {lastc:.2f} ({chg:+.2f}%)"
    foot = " " * (width + 2) + "└" + "─" * len(cols)
    return "\n".join([head, *lines, foot])


def render_html(symbol, bars, count=30, levels=None):
    """渲染自包含 HTML（内联 SVG）K 线，返回完整文档字符串。

    深色金融风格；含右侧价格轴、日期刻度、悬浮 OHLC 提示、价位线（虚线+标签）
    与折叠数据表。无外部依赖，可直接用浏览器打开。
    """
    bars = bars[-count:]
    if not bars:
        raise RuntimeError(f"{symbol}: 无数据")
    levels = [lv for lv in (levels or []) if lv[1] and lv[1] > 0]
    esc = html_mod.escape

    top, bottom, left, right = 16, 32, 12, 168
    step, cw, plot_h = 16, 10, 380
    plot_w = step * len(bars)
    w, h = left + plot_w + right, top + plot_h + bottom
    hi = max([b["h"] for b in bars] + [lv[1] for lv in levels])
    lo = min([b["l"] for b in bars] + [lv[1] for lv in levels])
    pad = (hi - lo) * 0.04 or max(hi * 0.01, 1)
    hi, lo = hi + pad, lo - pad

    def y(p):
        return top + (hi - p) / (hi - lo) * plot_h

    parts = []
    # 网格线 + 右侧价格刻度（recessive，5 档）
    for k in range(5):
        p = hi - (hi - lo) * k / 4
        gy = y(p)
        parts.append(f'<line x1="{left}" y1="{gy:.1f}" x2="{left + plot_w}" y2="{gy:.1f}" '
                     f'stroke="{HTML_GRID}" stroke-width="1"/>')
        parts.append(f'<text x="{left + plot_w + 8}" y="{gy + 4:.1f}" fill="{HTML_MUTED}" '
                     f'font-size="11">{p:.2f}</text>')
    # 日期刻度：首/中/尾（首尾分别左/右对齐，避免溢出画布被裁）
    for i in sorted({0, len(bars) // 2, len(bars) - 1}):
        cx = left + i * step + step / 2
        anchor = "start" if i == 0 else ("end" if i == len(bars) - 1 else "middle")
        parts.append(f'<text x="{cx:.1f}" y="{h - 10}" fill="{HTML_MUTED}" font-size="11" '
                     f'text-anchor="{anchor}">{esc(bars[i]["t"][:10])}</text>')
    # 蜡烛（影线+实体）与悬浮命中区（整列，比蜡烛本身好命中）
    hits = []
    prev_c = None
    for i, b in enumerate(bars):
        o, c, hh, ll = b["o"], b["c"], b["h"], b["l"]
        col = HTML_UP if c >= o else HTML_DOWN
        cx = left + i * step + step / 2
        parts.append(f'<line x1="{cx:.1f}" y1="{y(hh):.1f}" x2="{cx:.1f}" y2="{y(ll):.1f}" '
                     f'stroke="{col}" stroke-width="1"/>')
        by = y(max(o, c))
        bh = max(y(min(o, c)) - by, 1)
        parts.append(f'<rect x="{cx - cw / 2:.1f}" y="{by:.1f}" width="{cw}" height="{bh:.1f}" '
                     f'fill="{col}" rx="1"/>')
        chg = f" ({(c / prev_c - 1) * 100:+.2f}%)" if prev_c else ""
        tip = esc(f'{b["t"][:10]}<br>开 {o:.2f} 高 {hh:.2f}<br>低 {ll:.2f} 收 {c:.2f}{chg}', quote=True)
        hits.append(f'<rect class="hit" x="{left + i * step}" y="{top}" width="{step}" '
                    f'height="{plot_h}" fill="transparent" data-tip="{tip}"/>')
        prev_c = c
    # 价位线：虚线贯穿 + 右侧标签芯片（文字用正文色，颜色由描边承载，双编码）
    for label, price, ansi in levels:
        col = HTML_LEVEL.get(ansi, HTML_MUTED)
        ly = y(price)
        text = f"{label} {price:.2f}"
        parts.append(f'<line x1="{left}" y1="{ly:.1f}" x2="{left + plot_w}" y2="{ly:.1f}" '
                     f'stroke="{col}" stroke-width="1.5" stroke-dasharray="5 4"/>')
        parts.append(f'<rect x="{left + plot_w + 4}" y="{ly - 9:.1f}" width="{right - 12}" height="18" '
                     f'fill="{HTML_PANEL}" stroke="{col}" rx="4"/>')
        parts.append(f'<text x="{left + plot_w + 12}" y="{ly + 4:.1f}" fill="{HTML_INK}" '
                     f'font-size="11">{esc(text)}</text>')

    first, last = bars[0]["t"][:10], bars[-1]["t"][:10]
    lastc = bars[-1]["c"]
    chg = (lastc / bars[0]["c"] - 1) * 100 if bars[0]["c"] else 0
    chg_col = HTML_UP if chg >= 0 else HTML_DOWN
    rows = "".join(
        f'<tr><td>{esc(b["t"][:10])}</td><td>{b["o"]:.2f}</td><td>{b["h"]:.2f}</td>'
        f'<td>{b["l"]:.2f}</td><td>{b["c"]:.2f}</td></tr>' for b in bars)
    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(symbol)} 日K</title>
<style>
  body {{ margin: 0; background: {HTML_BG}; color: {HTML_INK};
         font: 14px/1.5 -apple-system, "PingFang SC", "Helvetica Neue", sans-serif; }}
  .wrap {{ max-width: {w + 48}px; margin: 0 auto; padding: 20px 24px; }}
  .head {{ margin-bottom: 12px; }}
  .sym {{ font-size: 18px; font-weight: 700; }}
  .sub {{ color: {HTML_MUTED}; }}
  .scroll {{ overflow-x: auto; }}
  #tip {{ display: none; position: absolute; pointer-events: none; z-index: 9;
          background: {HTML_PANEL}; color: {HTML_INK}; border: 1px solid {HTML_GRID};
          border-radius: 6px; padding: 6px 10px; font-size: 12px; white-space: nowrap; }}
  details {{ margin-top: 16px; color: {HTML_MUTED}; }}
  table {{ border-collapse: collapse; font-size: 12px; margin-top: 8px; }}
  td, th {{ padding: 2px 12px 2px 0; text-align: right; }}
  td:first-child, th:first-child {{ text-align: left; }}
</style></head><body>
<div class="wrap">
  <div class="head"><span class="sym">{esc(symbol)}</span>
    <span class="sub">日K · 最近 {len(bars)} 根 · {esc(first)} → {esc(last)} · 收</span>
    <b>{lastc:.2f}</b> <b style="color:{chg_col}">{chg:+.2f}%</b></div>
  <div class="scroll"><svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img"
    aria-label="{esc(symbol)} 蜡烛图">{"".join(parts)}{"".join(hits)}</svg></div>
  <details><summary>数据表（OHLC）</summary>
    <table><tr><th>日期</th><th>开</th><th>高</th><th>低</th><th>收</th></tr>{rows}</table>
  </details>
</div>
<div id="tip"></div>
<script>
  const tip = document.getElementById('tip');
  document.querySelectorAll('.hit').forEach(r => {{
    r.addEventListener('mousemove', e => {{
      tip.style.display = 'block'; tip.innerHTML = r.dataset.tip;
      tip.style.left = (e.pageX + 14) + 'px'; tip.style.top = (e.pageY - 12) + 'px';
    }});
    r.addEventListener('mouseleave', () => {{ tip.style.display = 'none'; }});
  }});
</script>
</body></html>
"""


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
    p.add_argument("--color", action="store_true", help="强制开启颜色（覆盖 NO_COLOR）")
    p.add_argument("--no-color", action="store_true", help="强制关闭颜色（默认开启）")
    p.add_argument("--html", nargs="?", const="auto",
                   help="另写彩色 HTML 图到该路径；不带值时默认 chart_{SYMBOL}.html")
    p.add_argument("--open", action="store_true", help="生成后用浏览器打开 HTML 图（需 --html）")
    # 价位线叠加
    p.add_argument("--plan", help="评分结果/trade_plan JSON 文件或 '-'(stdin)，叠加入场/止盈/止损价位线")
    p.add_argument("--entry", type=float, help="手动入场价位线，覆盖 --plan 的入场价")
    p.add_argument("--stop-loss", type=float, help="手动止损价位线")
    p.add_argument("--take-profit", type=float, help="手动止盈价位线")
    p.add_argument("--take-profit-2", type=float, help="手动第二止盈价位线")
    # 自取模式参数（与 indicators.py 对齐）
    p.add_argument("--start", help="自取起始日；缺省按 --count 自动回推")
    p.add_argument("--end", help="自取结束日；缺省今天")
    p.add_argument("--feed", default="iex", help="Alpaca 数据源，默认 iex；无 SIP 权限不要用 sip")
    p.add_argument("--adjustment", default="split")
    p.add_argument("--limit", type=int, default=10000)
    p.add_argument("--timeout", type=int, default=30)
    args = p.parse_args()

    if args.input == "-" and args.plan == "-":
        raise RuntimeError("--input 与 --plan 不能同时从 stdin 读取")

    # 取数来源（确定性，不依赖 isatty）：给了 --input 就读 JSON（文件或 '-' 走 stdin），
    # 否则自取。管道用法须显式 `--input -`，避免非交互 shell 下误判 stdin。
    if args.input is not None:
        sym, bars = load_bars(args.input, args.symbol)
    else:
        sym, bars = _fetch(args.symbol, args)

    levels = plan_levels(load_plan(args.plan, args.symbol or sym)) if args.plan else []
    manual = [("入场", args.entry, COLOR_ENTRY, {"入场", "回踩入场"}),
              ("止损", args.stop_loss, COLOR_STOP, {"止损"}),
              ("止盈1", args.take_profit, COLOR_TP1, {"止盈1"}),
              ("止盈2", args.take_profit_2, COLOR_TP2, {"止盈2"})]
    for label, price, color_code, replaces in manual:
        if price is not None:
            levels = [lv for lv in levels if lv[0] not in replaces]
            levels.append((label, price, color_code))

    # 默认开色（不探测 isatty：skill/agent 场景 stdout 是管道，探测会误关色）
    color = not os.environ.get("NO_COLOR")
    if args.color:
        color = True
    if args.no_color:
        color = False
    sys.stdout.write(render(sym, bars, args.count, args.rows, color, levels) + "\n")

    # HTML 图走 stderr 报路径，保持 stdout 只有终端图（agent 原样贴图不混入杂行）
    if args.html:
        out = os.path.abspath(f"chart_{sym}.html" if args.html == "auto" else args.html)
        with open(out, "w", encoding="utf-8") as f:
            f.write(render_html(sym, bars, args.count, levels))
        sys.stderr.write(f"HTML 图已写入: {out}\n")
        if args.open:
            import webbrowser
            webbrowser.open("file://" + out)
    elif args.open:
        raise RuntimeError("--open 需与 --html 同用")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"chart error: {e}\n")
        sys.exit(1)
