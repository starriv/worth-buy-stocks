"""Self-contained interactive HTML renderer for canonical backtest artifacts."""
from __future__ import annotations

import html
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .artifact import validate_artifact

_STATUS_NOTES = {
    "supports": "非重叠样本的置信区间支持「纪律信号」在该标的上有正向优势。",
    "contradicts": "非重叠样本的置信区间与「纪律信号」有效性相矛盾，请谨慎对待该信号。",
    "inconclusive": "非重叠样本不足或置信区间跨零，证据不足以下结论，仅供观察。",
    "invalid_run": "本次运行配置无效，结果不可用于任何决策。",
}

_SCENARIO_LABELS = {
    "disciplined_verdict": "纪律信号（结论=是）",
    "rank_only": "仅排名信号",
}


def _scenario_label(name: str) -> str:
    base, _, suffix = name.rpartition("_")
    if base in _SCENARIO_LABELS and suffix.isdigit():
        return f"{_SCENARIO_LABELS[base]} · {suffix} 日研究窗口（非策略出场）"
    if base in _SCENARIO_LABELS and suffix == "managed":
        return f"{_SCENARIO_LABELS[base]} · 计划仓位与持仓保护"
    if base in _SCENARIO_LABELS and suffix == "stateful":
        return f"{_SCENARIO_LABELS[base]} · 状态化持仓实验（已拒绝）"
    return name


def _embedded_json(artifact: Mapping[str, Any]) -> str:
    return json.dumps(
        artifact, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render_html(artifact: Mapping[str, Any]) -> str:
    """Render one portable report; all business values come from the artifact."""

    validate_artifact(artifact)
    config = artifact["config"]
    validation = artifact["validation"]
    status = str(validation["status"])
    title = f"{config['symbol']} 单标的评分回测"
    scenarios = sorted(artifact["simulations"])
    default_scenario = (
        "disciplined_verdict_managed"
        if "disciplined_verdict_managed" in scenarios
        else "disciplined_verdict_63"
        if "disciplined_verdict_63" in scenarios
        else scenarios[0] if scenarios else ""
    )
    options = "".join(
        f'<option value="{html.escape(name)}"'
        f'{" selected" if name == default_scenario else ""}>'
        f"{html.escape(_scenario_label(name))}</option>"
        for name in scenarios
    )
    warning_items = "".join(
        f"<li>{html.escape(str(item))}</li>" for item in artifact["warnings"]
    )
    warnings_block = (
        f'<section class="callout"><div class="callout-title">口径与边界</div>'
        f'<ul>{warning_items}</ul></section>'
        if warning_items else ""
    )
    generated = str(artifact["generated_at"])[:16].replace("T", " ")
    document = _TEMPLATE
    for token, value in {
        "@@TITLE@@": html.escape(title),
        "@@SYMBOL@@": html.escape(str(config["symbol"])),
        "@@RANGE@@": f"{html.escape(str(config['start']))} → {html.escape(str(config['end']))}",
        "@@STATUS@@": html.escape(status),
        "@@STATUS_NOTE@@": html.escape(_STATUS_NOTES.get(status, "")),
        "@@ARTIFACT_VERSION@@": html.escape(str(artifact["artifact_version"])),
        "@@MODEL_VERSION@@": html.escape(str(artifact["model_version"])),
        "@@GENERATED@@": html.escape(generated),
        "@@MODE@@": html.escape(str(artifact["provenance"]["mode"])),
        "@@FEED@@": html.escape(str(config["feed"])),
        "@@ADJUSTMENT@@": html.escape(str(config["adjustment"])),
        "@@COST_BPS@@": html.escape(str(config["cost_bps_oneway"])),
        "@@DATA_HASH@@": html.escape(str(artifact["provenance"]["data_hash"])[:16]),
        "@@SEMANTIC_HASH@@": html.escape(str(artifact["semantic_hash"])[:16]),
        "@@OPTIONS@@": options,
        "@@WARNINGS@@": warnings_block,
        "@@DATA@@": _embedded_json(artifact),
    }.items():
        document = document.replace(token, value)
    return document


_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>@@TITLE@@</title>
<style>
:root{
  --page:#f6f6f4;--surface:#fcfcfb;--surface2:#f0efec;
  --ink:#1b1b18;--ink2:#52514e;--muted:#898781;
  --grid:#e7e6e0;--axis:#c3c2b7;--border:rgba(11,11,11,.10);
  --s1:#2a78d6;--s2:#1baf7a;--s3:#eda100;--s4:#e34948;--s5:#4a3aa7;
  --up:#1baf7a;--down:#e34948;--pos:#006300;--neg:#c03636;
  --good:#0ca30c;--warnfill:#fab219;--warntext:#8a6100;--crit:#d03b3b;
  --shadow:0 1px 3px rgba(11,11,11,.06);
}
@media (prefers-color-scheme:dark){
  :root{
    --page:#0e1117;--surface:#161b26;--surface2:#1d2330;
    --ink:#f2f4f8;--ink2:#c3c2b7;--muted:#8b9099;
    --grid:#262c39;--axis:#3a4150;--border:rgba(255,255,255,.10);
    --s1:#3987e5;--s2:#199e70;--s3:#c98500;--s4:#e66767;--s5:#9085e9;
    --up:#22b07d;--down:#e66767;--pos:#2fbe5f;--neg:#ef7a7a;
    --warntext:#e9b03c;--shadow:0 1px 3px rgba(0,0,0,.4);
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);
  font:14px/1.55 system-ui,-apple-system,"Segoe UI","PingFang SC","Hiragino Sans GB",sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 20px 48px}
h1{margin:0;font-size:1.35rem;letter-spacing:.01em}
h2{margin:0 0 4px;font-size:1rem}
.sub,.hint{color:var(--ink2);font-size:13px}
.hint{color:var(--muted);font-size:12px}
section.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;
  padding:18px 20px;margin-bottom:16px;box-shadow:var(--shadow)}
.header{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;flex-wrap:wrap}
.status{font-weight:700;padding:6px 14px;border-radius:999px;font-size:13px;letter-spacing:.03em}
.supports{background:color-mix(in srgb,var(--good) 14%,transparent);color:var(--pos)}
.contradicts,.invalid_run{background:color-mix(in srgb,var(--crit) 12%,transparent);color:var(--neg)}
.inconclusive{background:color-mix(in srgb,var(--warnfill) 16%,transparent);color:var(--warntext)}
.status-note{max-width:340px;text-align:right;color:var(--ink2);font-size:12.5px;margin-top:6px}
.chips{display:flex;flex-wrap:wrap;gap:8px 18px;margin-top:14px;color:var(--ink2);font-size:12.5px}
.chips b{color:var(--ink);font-weight:600}
.chips code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;
  background:var(--surface2);border-radius:5px;padding:1px 6px}
.callout{background:color-mix(in srgb,var(--warnfill) 9%,var(--surface));
  border:1px solid color-mix(in srgb,var(--warnfill) 35%,transparent);
  border-radius:14px;padding:14px 18px;margin-bottom:16px}
.callout-title{font-weight:600;color:var(--warntext);margin-bottom:4px;font-size:13px}
.callout ul{margin:0;padding-left:18px;color:var(--ink2);font-size:12.5px}
.callout li{margin:2px 0}
.controls{display:flex;flex-wrap:wrap;gap:14px 24px;align-items:flex-end}
.controls .field{display:flex;flex-direction:column;gap:5px}
.controls label{color:var(--muted);font-size:12px}
select{background:var(--surface);color:var(--ink);border:1px solid var(--axis);
  border-radius:8px;padding:7px 10px;font-size:13.5px;min-width:230px}
.presets{display:flex;gap:6px}
.presets button{background:var(--surface);color:var(--ink2);border:1px solid var(--axis);
  border-radius:8px;padding:6px 12px;font-size:13px;cursor:pointer}
.presets button:hover{background:var(--surface2)}
.presets button.active{background:var(--s1);border-color:var(--s1);color:#fff}
.ranges{flex:1;min-width:260px;display:flex;flex-direction:column;gap:2px}
.ranges input{width:100%;accent-color:var(--s1);margin:0}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:16px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:14px;
  padding:14px 16px;box-shadow:var(--shadow)}
.tile .label{color:var(--muted);font-size:12px}
.tile .value{font-size:22px;font-weight:650;margin:2px 0;overflow-wrap:anywhere}
.tile .compare{color:var(--muted);font-size:12px}
.pos{color:var(--pos)}.neg{color:var(--neg)}
.legend{display:flex;flex-wrap:wrap;gap:6px 18px;color:var(--ink2);font-size:12.5px;margin:2px 0 10px}
.key{display:inline-block;width:14px;height:3px;border-radius:2px;vertical-align:middle;margin-right:6px}
.key.dot{width:9px;height:9px;border-radius:50%}
svg{display:block;width:100%;height:auto}
svg text{font:11px system-ui,-apple-system,sans-serif;font-variant-numeric:tabular-nums;fill:var(--muted)}
svg .gridline{stroke:var(--grid);stroke-width:1}
svg .threshold{stroke:var(--axis);stroke-width:1;stroke-dasharray:5 4}
svg .cross{stroke:var(--axis);stroke-width:1;pointer-events:none}
svg .marker{cursor:pointer}
svg .marker:focus{outline:none}
svg .marker:focus polygon{stroke:var(--ink);stroke-width:2}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
.grid2 section.card{margin-bottom:0}
.metrics{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px;margin-top:10px}
.metric{background:var(--surface2);border-radius:10px;padding:8px 12px}
.metric .label{display:block;color:var(--muted);font-size:11.5px}
.metric .value{font-weight:600;font-size:14.5px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border-bottom:1px solid var(--grid);padding:7px 9px;text-align:right;
  font-variant-numeric:tabular-nums;white-space:nowrap}
th{color:var(--muted);font-weight:600;font-size:12px;position:sticky;top:0;background:var(--surface)}
th:first-child,td:first-child,th.lt,td.lt{text-align:left}
tbody tr:hover{background:var(--surface2)}
.table-wrap{overflow:auto;max-height:430px;margin-top:8px;border-radius:8px}
.empty{color:var(--muted);padding:14px 0;text-align:center}
.conditions{display:flex;flex-direction:column;gap:8px;margin-top:10px}
.cond{display:flex;gap:10px;align-items:baseline;background:var(--surface2);border-radius:10px;padding:9px 12px}
.cond .mark{font-weight:700;width:16px;text-align:center}
.cond .mark.ok{color:var(--pos)}.cond .mark.fail{color:var(--neg)}.cond .mark.na{color:var(--muted)}
.cond .name{flex:1;color:var(--ink2)}
.cond .detail{font-variant-numeric:tabular-nums}
details{border-top:1px solid var(--grid);margin-top:12px;padding-top:10px;color:var(--ink2)}
summary{cursor:pointer;font-size:13px;color:var(--ink2)}
pre{overflow:auto;font-size:12px;background:var(--surface2);border-radius:8px;padding:10px;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.tooltip{position:fixed;display:none;z-index:9;pointer-events:none;background:var(--surface);
  border:1px solid var(--border);box-shadow:0 4px 16px rgba(0,0,0,.18);border-radius:10px;
  padding:10px 12px;min-width:220px;max-width:330px;font-size:12.5px}
.tooltip .tt-date{font-weight:650;margin-bottom:4px}
.tooltip .tt-ohlc{color:var(--ink2);font-variant-numeric:tabular-nums;margin-bottom:2px}
.tooltip .tt-score{color:var(--ink2);margin-bottom:4px}
.tooltip hr{border:none;border-top:1px solid var(--grid);margin:6px 0}
.tooltip .tt-row{display:flex;align-items:center;gap:7px;margin:2px 0;font-variant-numeric:tabular-nums}
.tooltip .tt-key{width:12px;height:3px;border-radius:2px;flex:none}
.tooltip .tt-val{font-weight:650;min-width:64px}
.tooltip .tt-name{color:var(--ink2)}
@media(max-width:880px){.grid2{grid-template-columns:1fr}.grid2 section.card{margin-bottom:16px}
  main{padding:14px 10px 40px}.status-note{text-align:left}}
@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important}}
</style>
</head>
<body><main>

<section class="card">
  <div class="header">
    <div>
      <h1>@@TITLE@@</h1>
      <div class="sub">@@RANGE@@ · artifact @@ARTIFACT_VERSION@@ · model @@MODEL_VERSION@@ · 生成于 @@GENERATED@@ UTC</div>
    </div>
    <div>
      <div class="status @@STATUS@@">@@STATUS@@</div>
      <div class="status-note">@@STATUS_NOTE@@</div>
    </div>
  </div>
  <div class="chips">
    <span>数据源 <b>@@MODE@@</b></span>
    <span>Feed / 复权 <b>@@FEED@@ / @@ADJUSTMENT@@</b></span>
    <span>单边成本 <b>@@COST_BPS@@ bps</b></span>
    <span>主窗口样本（重叠 / 非重叠）<b id="sampleCount">—</b></span>
    <span>数据 hash <code>@@DATA_HASH@@</code></span>
    <span>语义 hash <code>@@SEMANTIC_HASH@@</code></span>
  </div>
</section>

@@WARNINGS@@

<section class="card">
  <div class="controls">
    <div class="field">
      <label for="scenario">策略场景（指标、交易与曲线随之切换）</label>
      <select id="scenario">@@OPTIONS@@</select>
    </div>
    <div class="field">
      <label>显示范围（仅缩放图表，指标始终为全区间）</label>
      <div class="presets" id="presets">
        <button data-n="0" class="active">全部</button>
        <button data-n="756">3年</button>
        <button data-n="252">1年</button>
        <button data-n="126">6月</button>
        <button data-n="63">3月</button>
      </div>
    </div>
    <div class="ranges">
      <input id="rangeStart" type="range" min="0" value="0" aria-label="起始位置">
      <input id="rangeEnd" type="range" min="0" value="0" aria-label="结束位置">
      <div class="hint" id="rangeLabel"></div>
    </div>
  </div>
</section>

<div class="tiles" id="tiles"></div>

<section class="card">
  <h2>价格、持仓与交易</h2>
  <div class="legend">
    <span><i class="key dot" style="background:var(--up)"></i>上涨 K</span>
    <span><i class="key dot" style="background:var(--down)"></i>下跌 K</span>
    <span><i class="key dot" style="background:var(--s1)"></i>▲ <span id="entryMarkerLabel">入场</span></span>
    <span><i class="key dot" style="background:var(--s5)"></i>▼ <span id="exitMarkerLabel">策略退出</span></span>
    <span><i class="key" style="background:color-mix(in srgb,var(--s1) 30%,transparent);height:9px"></i><span id="positionSpanLabel">持仓区间</span></span>
  </div>
  <svg id="priceChart" viewBox="0 0 1200 430" role="img" aria-label="K线、持仓区间和交易标记"></svg>
</section>

<section class="card">
  <h2>评分时间线</h2>
  <div class="legend">
    <span><i class="key" style="background:var(--s3)"></i>原始综合分</span>
    <span><i class="key" style="background:var(--s1)"></i>封顶后综合分</span>
    <span>虚线为 60 / 75 阈值</span>
    <span><i class="key dot" style="background:var(--good)"></i>结论=是</span>
    <span><i class="key dot" style="background:var(--warnfill)"></i>观察</span>
  </div>
  <svg id="scoreChart" viewBox="0 0 1200 260" role="img" aria-label="评分时间线与每日结论"></svg>
</section>

<section class="card">
  <h2>策略与基准净值</h2>
  <div class="legend" id="equityLegend"></div>
  <svg id="equityChart" viewBox="0 0 1200 300" role="img" aria-label="净值曲线"></svg>
</section>

<section class="card">
  <h2>回撤</h2>
  <div class="legend" id="drawdownLegend"></div>
  <svg id="drawdownChart" viewBox="0 0 1200 200" role="img" aria-label="回撤曲线"></svg>
</section>

<section class="card">
  <h2>场景指标</h2>
  <div class="hint">全区间统计，不随显示范围缩放。收益均为扣除双边成本后的净值口径。</div>
  <div class="metrics" id="metrics"></div>
</section>

<div class="grid2">
  <section class="card">
    <h2>事件研究（信号后前瞻收益）</h2>
    <div class="hint">「样本 N」为逐日重叠样本；「非重叠 N」为推断样本，置信区间只来自后者。</div>
    <div class="table-wrap"><table>
      <thead><tr><th class="lt">窗口 · 分组</th><th>样本 N</th><th>非重叠 N</th>
      <th>平均收益</th><th>中位数</th><th>胜率</th><th>均值 95% CI（非重叠）</th></tr></thead>
      <tbody id="evidenceRows"></tbody>
    </table></div>
    <div class="hint" id="evidenceNotes" style="margin-top:8px"></div>
  </section>
  <section class="card">
    <h2>自动结论条件</h2>
    <div class="hint">全部条件满足才会给出 supports / contradicts。</div>
    <div class="conditions" id="validationConditions"></div>
  </section>
</div>

<section class="card">
  <h2>交易明细</h2>
  <div class="table-wrap"><table>
    <thead><tr><th class="lt">#</th><th class="lt">信号日</th><th class="lt" id="entryDateHeader">入场日</th><th id="entryPriceHeader">入场价</th>
    <th class="lt" id="exitDateHeader">退出日</th><th id="exitPriceHeader">退出价</th><th>持有</th><th>仓位</th><th class="lt">退出原因 / 状态</th><th>毛收益</th><th>净收益</th></tr></thead>
    <tbody id="tradeRows"></tbody>
  </table></div>
  <div class="empty" id="tradeEmpty" hidden>该场景在回测区间内没有成交。</div>
</section>

<section class="card">
  <details id="exclusionDetails">
    <summary id="exclusionSummary">跳过的信号与事件研究排除</summary>
    <div class="table-wrap"><table>
      <thead><tr><th class="lt">日期</th><th class="lt">类型</th><th class="lt">原因</th></tr></thead>
      <tbody id="exclusionRows"></tbody>
    </table></div>
  </details>
  <details>
    <summary>完整 provenance</summary>
    <pre id="provenance"></pre>
  </details>
</section>

<div id="tooltip" class="tooltip" role="status" aria-live="polite"></div>
<script id="artifact-data" type="application/json">@@DATA@@</script>
<script>
'use strict';
const A = JSON.parse(document.getElementById('artifact-data').textContent);
const bars = A.bars || [];
const dates = bars.map(b => b.t.slice(0, 10));
const dateIndex = new Map(dates.map((d, i) => [d, i]));
const timeline = new Map((A.timeline || []).map(r => [r.signal_date, r]));
const $ = id => document.getElementById(id);
const startInput = $('rangeStart'), endInput = $('rangeEnd'), scenarioSel = $('scenario'), tip = $('tooltip');
const W = 1200, PL = 56, PR = 20, PT = 14, XAXIS = 26;
const NS = 'http://www.w3.org/2000/svg';
const lastIdx = Math.max(bars.length - 1, 0);
startInput.max = endInput.max = lastIdx;
startInput.value = 0; endInput.value = lastIdx;
let range = [0, lastIdx];

const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const E = (name, attrs = {}, text = '') => {
  const n = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (text) n.textContent = text;
  return n;
};
const clear = n => { while (n.firstChild) n.removeChild(n.firstChild); };
const esc = v => String(v ?? '').replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const isNum = v => typeof v === 'number' && isFinite(v);
const fmtNum = (v, d = 2) => isNum(v) ? v.toLocaleString('en-US',
  {minimumFractionDigits: d, maximumFractionDigits: d}) : '—';
const fmtPct = (v, signed = true) => !isNum(v) ? '—'
  : (signed && v > 0 ? '+' : '') + fmtNum(v, 2) + '%';
const pctClass = v => !isNum(v) || v === 0 ? '' : v > 0 ? 'pos' : 'neg';

const TIMING_LABELS = {trend_continuation:'趋势延续', pullback_no_trigger:'回调未触发',
  pullback_reversal:'回调反转', recovery_reversal:'超跌反转', overextended:'过度延伸',
  trend_broken:'趋势破位', unknown:'未知'};
const REASON_LABELS = {position_or_order_active:'已有持仓或挂单，跳过新信号',
  horizon_immature:'观察期未满，前瞻收益尚不可得', no_verdict:'当日无法评分'};
const STATUS_LABELS = {closed:'已平仓', open:'持仓中'};
const EXIT_REASON_LABELS = {stop_loss:'初始止损', gap_open_stop_loss:'跳空触发初始止损',
  break_even_stop:'保本保护', gap_open_break_even_stop:'跳空触发保本保护',
  trailing_stop:'移动止损', gap_open_trailing_stop:'跳空触发移动止损',
  signal_overextended_reversal:'高位反转信号', signal_trend_break:'趋势破位信号',
  bankrupt_terminal:'破产终值'};

// ---------- range & filters ----------
function activeRange() {
  let a = +startInput.value, b = +endInput.value;
  if (a > b) [a, b] = [b, a];
  range = [a, b];
  const n = b - a + 1;
  $('rangeLabel').textContent = bars.length
    ? `${dates[a]} → ${dates[b]} · ${n} 个交易日` : '无行情数据';
  return range;
}
$('presets').addEventListener('click', e => {
  const btn = e.target.closest('button'); if (!btn) return;
  const n = +btn.dataset.n;
  startInput.value = n ? Math.max(0, lastIdx - n + 1) : 0;
  endInput.value = lastIdx;
  for (const b of $('presets').children) b.classList.toggle('active', b === btn);
  redraw();
});
[startInput, endInput].forEach(el => el.addEventListener('input', () => {
  for (const b of $('presets').children) b.classList.remove('active');
  redraw();
}));
scenarioSel.addEventListener('change', () => { renderScenario(); redraw(); });

// ---------- axes ----------
function niceTicks(lo, hi, count = 5) {
  const span = (hi - lo) || Math.abs(hi) || 1;
  const step0 = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const err = step0 / mag;
  const step = (err >= 5 ? 10 : err >= 2 ? 5 : err >= 1 ? 2 : 1) * mag;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step / 1e6; v += step)
    out.push(Math.abs(v) < step * 1e-9 ? 0 : v);
  out.step = step;
  return out;
}
const tickDecimals = ticks => {
  const step = ticks.step ?? 1;
  return step >= 1 ? 0 : step >= 0.1 ? 1 : 2;
};
function logTicks(lo, hi) {
  const out = [];
  for (let k = Math.floor(Math.log10(lo)); k <= Math.ceil(Math.log10(hi)); k++)
    for (const m of [1, 2, 5]) { const v = m * Math.pow(10, k); if (v >= lo && v <= hi) out.push(v); }
  return out.length >= 2 ? out : niceTicks(lo, hi);
}
function drawYAxis(svg, ticks, y, fmt) {
  for (const t of ticks) {
    const yy = y(t);
    svg.append(E('line', {class: 'gridline', x1: PL, x2: W - PR, y1: yy, y2: yy}));
    svg.append(E('text', {x: PL - 7, y: yy + 4, 'text-anchor': 'end'}, fmt(t)));
  }
}
function drawXAxis(svg, a, b, x, yBase) {
  const n = b - a, step = Math.max(1, Math.ceil(n / 6));
  const long = n > 400;
  for (let i = a; i <= b; i += step) {
    if (b - i < step / 2 && i !== a) break;
    svg.append(E('text', {x: x(i), y: yBase + 17, 'text-anchor': 'middle'},
      long ? dates[i].slice(0, 7) : dates[i].slice(5)));
  }
  svg.append(E('text', {x: x(b), y: yBase + 17, 'text-anchor': 'end'},
    long ? dates[b].slice(0, 7) : dates[b].slice(5)));
}

// ---------- shared cursor & tooltip ----------
const chartIds = ['priceChart', 'scoreChart', 'equityChart', 'drawdownChart'];
function positionSpans(curve, a, b) {
  const spans = []; let open = null;
  for (const row of curve) {
    const i = dateIndex.get(row.date);
    if (i === undefined || i < a || i > b) continue;
    if (row.in_position) { if (open === null) open = i; }
    else if (open !== null) { spans.push([open, i - 1]); open = null; }
  }
  if (open !== null) spans.push([open, b]);
  return spans;
}
function seriesDefs() {
  const sym = A.config.symbol, bench = A.benchmarks || {};
  return [
    {name: '策略净值', color: css('--s1'), map: curveMap(currentSim().equity_curve)},
    {name: sym + ' 买入持有', color: css('--s2'), map: curveMap((bench[sym] || {}).curve || [])},
    {name: 'SPY 买入持有', color: css('--s3'), map: curveMap((bench.SPY || {}).curve || [])},
  ];
}
let tipSeries = [];
const curveCache = new Map();
function curveMap(curve) {
  if (!curveCache.has(curve)) curveCache.set(curve, new Map(curve.map(r => [r.date, r])));
  return curveCache.get(curve);
}
function showCursor(idx, clientX, clientY) {
  const [a, b] = range, span = Math.max(b - a, 1);
  const px = PL + (idx - a) / span * (W - PL - PR);
  for (const id of chartIds) {
    const svg = $(id);
    svg.querySelectorAll('.cross').forEach(n => n.remove());
    svg.append(E('line', {class: 'cross', x1: px, x2: px, y1: PT, y2: svg.viewBox.baseVal.height - XAXIS}));
  }
  clear(tip);
  const d = dates[idx], bar = bars[idx], r = timeline.get(d);
  tip.append(Object.assign(document.createElement('div'), {className: 'tt-date', textContent: d}));
  if (bar) {
    const prev = idx > 0 ? bars[idx - 1].c : null;
    const chg = isNum(prev) && prev ? (bar.c / prev - 1) * 100 : null;
    const line = document.createElement('div');
    line.className = 'tt-ohlc';
    line.textContent = `开 ${fmtNum(bar.o)} 高 ${fmtNum(bar.h)} 低 ${fmtNum(bar.l)} 收 ${fmtNum(bar.c)}`;
    tip.append(line);
    if (chg !== null) {
      const cd = document.createElement('div');
      cd.className = 'tt-ohlc ' + pctClass(chg);
      cd.textContent = '日涨跌 ' + fmtPct(chg);
      tip.append(cd);
    }
  }
  if (r && r.status === 'ok') {
    const sc = document.createElement('div');
    sc.className = 'tt-score';
    const timing = TIMING_LABELS[(r.entry_timing || {}).classification] || '—';
    sc.textContent = `评分 ${fmtNum(r.raw_composite, 1)} → ${fmtNum(r.composite, 1)} · ${r.verdict || '—'} · ${timing}`;
    tip.append(sc);
  }
  tip.append(document.createElement('hr'));
  for (const s of tipSeries) {
    const row = s.map.get(d); if (!row) continue;
    const div = document.createElement('div'); div.className = 'tt-row';
    const key = document.createElement('i'); key.className = 'tt-key'; key.style.background = s.color;
    const val = document.createElement('span');
    val.className = 'tt-val ' + pctClass((row.equity - 1) * 100);
    val.textContent = fmtPct((row.equity - 1) * 100);
    const name = document.createElement('span'); name.className = 'tt-name';
    name.textContent = s.name + ` · 回撤 ${fmtNum(row.drawdown_pct, 1)}%`;
    div.append(key, val, name); tip.append(div);
  }
  tip.style.display = 'block';
  const rect = tip.getBoundingClientRect();
  let left = clientX + 14, top = clientY + 14;
  if (left + rect.width > innerWidth - 8) left = clientX - rect.width - 14;
  if (top + rect.height > innerHeight - 8) top = clientY - rect.height - 14;
  tip.style.left = Math.max(4, left) + 'px';
  tip.style.top = Math.max(4, top) + 'px';
}
function hideCursor() {
  tip.style.display = 'none';
  for (const id of chartIds) $(id).querySelectorAll('.cross').forEach(n => n.remove());
}
function bindCursor(svg) {
  svg.addEventListener('pointermove', e => {
    if (!bars.length) return;
    const [a, b] = range;
    const rect = svg.getBoundingClientRect();
    const px = (e.clientX - rect.left) / rect.width * W;
    const frac = (px - PL) / Math.max(W - PL - PR, 1);
    const idx = Math.min(b, Math.max(a, Math.round(a + frac * (b - a))));
    showCursor(idx, e.clientX, e.clientY);
  });
  svg.addEventListener('pointerleave', hideCursor);
}

// ---------- charts ----------
function currentSim() { return A.simulations[scenarioSel.value] || {equity_curve: [], trades: [], skipped_signals: [], metrics: {}}; }
function managedScenario() { return scenarioSel.value.endsWith('_managed') || scenarioSel.value.endsWith('_stateful'); }

function drawPrice() {
  const svg = $('priceChart'), H = 430; clear(svg);
  const [a, b] = range, rows = bars.slice(a, b + 1);
  if (!rows.length) return;
  const hi = Math.max(...rows.map(r => r.h)), lo = Math.min(...rows.map(r => r.l));
  const pad = (hi - lo) * 0.05 || 1;
  const x = i => PL + (i - a) * (W - PL - PR) / Math.max(b - a, 1);
  const y = v => PT + (hi + pad - v) / (hi - lo + 2 * pad) * (H - PT - XAXIS - 10);
  const yBase = H - XAXIS;
  const ticks = niceTicks(lo - pad, hi + pad);
  drawYAxis(svg, ticks, y, t => fmtNum(t, tickDecimals(ticks)));
  drawXAxis(svg, a, b, x, yBase);
  for (const [s, e2] of positionSpans(currentSim().equity_curve, a, b))
    svg.append(E('rect', {x: x(s), y: PT, width: Math.max(x(e2) - x(s), 1.5), height: yBase - PT,
      fill: css('--s1'), opacity: '0.08'}));
  const cw = Math.max(1, Math.min(9, (W - PL - PR) / rows.length * 0.62));
  const up = css('--up'), down = css('--down');
  rows.forEach((r, k) => {
    const col = r.c >= r.o ? up : down, xx = x(a + k);
    svg.append(E('line', {x1: xx, x2: xx, y1: y(r.h), y2: y(r.l), stroke: col}));
    svg.append(E('rect', {x: xx - cw / 2, y: y(Math.max(r.o, r.c)), width: cw,
      height: Math.max(1, Math.abs(y(r.o) - y(r.c))), fill: col, rx: cw > 2.5 ? 1 : 0}));
  });
  const surface = css('--surface');
  for (const t of currentSim().trades || []) {
    for (const side of ['entry', 'exit']) {
      const d = t[side + '_date'];
      const i = dateIndex.get(d);
      if (i === undefined || i < a || i > b) continue;
      const entry = side === 'entry', xx = x(i);
      const yy = entry ? y(bars[i].l) + 10 : y(bars[i].h) - 10;
      const pts = entry
        ? `${xx},${yy} ${xx - 6.5},${yy + 11} ${xx + 6.5},${yy + 11}`
        : `${xx},${yy} ${xx - 6.5},${yy - 11} ${xx + 6.5},${yy - 11}`;
      const g = E('g', {class: 'marker', tabindex: 0});
      g.append(E('circle', {cx: xx, cy: entry ? yy + 6 : yy - 6, r: 13, fill: 'transparent'}));
      g.append(E('polygon', {points: pts, fill: entry ? css('--s1') : css('--s5'),
        stroke: surface, 'stroke-width': 2}));
      const managed = managedScenario();
      const marker = managed
        ? (entry ? '入场' : '策略退出')
        : (entry ? '研究起点' : '窗口终点');
      const ret = entry ? '' : ` · 净收益 ${fmtPct(t.net_return_pct)}`;
      g.append(E('title', {}, `${marker} ${d} @ ${fmtNum(t[side + '_price'])}${ret}`));
      svg.append(g);
    }
  }
  bindCursor(svg);
}

function drawScore() {
  const svg = $('scoreChart'), H = 260; clear(svg);
  const [a, b] = range;
  if (!bars.length) return;
  const ribbonH = 8, yBase = H - XAXIS, plotBottom = yBase - ribbonH - 6;
  const x = i => PL + (i - a) * (W - PL - PR) / Math.max(b - a, 1);
  const y = v => PT + (100 - v) / 100 * (plotBottom - PT);
  for (const t of [0, 25, 50, 100]) {
    svg.append(E('line', {class: 'gridline', x1: PL, x2: W - PR, y1: y(t), y2: y(t)}));
    svg.append(E('text', {x: PL - 7, y: y(t) + 4, 'text-anchor': 'end'}, String(t)));
  }
  for (const t of [60, 75]) {
    svg.append(E('line', {class: 'threshold', x1: PL, x2: W - PR, y1: y(t), y2: y(t)}));
    svg.append(E('text', {x: PL - 7, y: y(t) + 4, 'text-anchor': 'end'}, String(t)));
  }
  drawXAxis(svg, a, b, x, yBase);
  const rows = [];
  for (let i = a; i <= b; i++) {
    const r = timeline.get(dates[i]);
    if (r && r.status === 'ok') rows.push([i, r]);
  }
  for (const [key, color] of [['raw_composite', css('--s3')], ['composite', css('--s1')]]) {
    let d = '', pen = false;
    for (const [i, r] of rows) {
      if (!isNum(r[key])) { pen = false; continue; }
      d += (pen ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(r[key]).toFixed(1);
      pen = true;
    }
    if (d) svg.append(E('path', {d, fill: 'none', stroke: color,
      'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round'}));
  }
  const good = css('--good'), warn = css('--warnfill'), axis = css('--axis');
  const cw = Math.max((W - PL - PR) / Math.max(b - a + 1, 1), 0.8);
  for (const [i, r] of rows) {
    const v = r.verdict;
    const col = v === '是' ? good : v === '观察' ? warn : axis;
    const op = v === '否' ? '0.35' : '0.95';
    svg.append(E('rect', {x: x(i) - cw / 2, y: plotBottom + 4, width: cw, height: ribbonH,
      fill: col, opacity: op}));
  }
  bindCursor(svg);
}

function drawCurves(id, H, series, opts = {}) {
  const svg = $(id); clear(svg);
  const [a, b] = range;
  const sliced = series.map(s => {
    const rows = [];
    for (let i = a; i <= b; i++) {
      const row = s.map.get(dates[i]);
      if (row) rows.push([i, opts.drawdown ? row.drawdown_pct : row.equity]);
    }
    return {...s, rows};
  }).filter(s => s.rows.length > 1);
  if (!sliced.length) return;
  const vals = sliced.flatMap(s => s.rows.map(r => r[1]));
  let hi = Math.max(...vals), lo = Math.min(...vals);
  if (opts.drawdown) hi = Math.max(hi, 0);
  const useLog = !opts.drawdown && lo > 0 && hi / lo > 4;
  const T = useLog ? Math.log : (v => v);
  const pad = (T(hi) - T(lo)) * 0.07 || 0.02;
  const yBase = H - XAXIS;
  const x = i => PL + (i - a) * (W - PL - PR) / Math.max(b - a, 1);
  const y = v => PT + (T(hi) + pad - T(v)) / (T(hi) - T(lo) + 2 * pad) * (yBase - PT - 6);
  const ticks = useLog ? logTicks(lo, hi) : niceTicks(lo, hi);
  drawYAxis(svg, ticks, y, opts.drawdown ? (t => fmtNum(t, 0) + '%')
    : (t => fmtNum(t, Number.isInteger(t) ? 0 : t >= 1 ? 1 : 2) + 'x'));
  drawXAxis(svg, a, b, x, yBase);
  sliced.forEach((s, si) => {
    const d = s.rows.map((r, k) => (k ? 'L' : 'M') + x(r[0]).toFixed(1) + ' ' + y(r[1]).toFixed(1)).join('');
    if (si === 0 && opts.drawdown) {
      const first = s.rows[0], last = s.rows[s.rows.length - 1];
      const base = y(0);
      svg.append(E('path', {d: d + `L${x(last[0]).toFixed(1)} ${base}L${x(first[0]).toFixed(1)} ${base}Z`,
        fill: s.color, opacity: '0.09', stroke: 'none'}));
    }
    svg.append(E('path', {d, fill: 'none', stroke: s.color, 'stroke-width': 2,
      'stroke-linejoin': 'round', 'stroke-linecap': 'round'}));
  });
  if (useLog) svg.append(E('text', {x: W - PR, y: PT + 2, 'text-anchor': 'end'}, '对数刻度'));
  bindCursor(svg);
}

function legendHtml(series) {
  return series.map(s =>
    `<span><i class="key" style="background:${esc(s.color)}"></i>${esc(s.name)}</span>`).join('');
}

// ---------- scenario-scoped panels ----------
const METRIC_DEFS = [
  ['total_return_pct', '总收益', 'spct'], ['cagr_pct', '年化收益 CAGR', 'spct'],
  ['max_drawdown_pct', '最大回撤', 'spct'], ['sharpe_rf0', '夏普比率（rf=0）', 'n2'],
  ['annualized_volatility_pct', '年化波动率', 'pct'], ['win_rate_pct', '胜率', 'pct'],
  ['profit_factor', '盈亏比', 'n2'], ['trade_count', '交易次数', 'int'],
  ['average_trade_pct', '平均单笔收益', 'spct'], ['median_trade_pct', '单笔收益中位数', 'spct'],
  ['exposure_pct', '持仓时间占比', 'pct'], ['turnover_sides', '换手（单边次数）', 'int'],
  ['skipped_signals', '跳过信号数', 'int'], ['open_trade_count', '未平仓交易', 'int'],
  ['total_cost_equity', '累计交易成本（净值）', 'n4'],
];
function fmtMetric(v, kind) {
  if (!isNum(v)) return {text: v == null ? '—' : String(v), cls: ''};
  if (kind === 'spct') return {text: fmtPct(v), cls: pctClass(v)};
  if (kind === 'pct') return {text: fmtNum(v, 2) + '%', cls: ''};
  if (kind === 'int') return {text: v.toLocaleString('en-US'), cls: ''};
  if (kind === 'n4') return {text: fmtNum(v, 4), cls: ''};
  return {text: fmtNum(v, 2), cls: ''};
}
function renderTiles() {
  const m = currentSim().metrics || {};
  const bench = ((A.benchmarks || {})[A.config.symbol] || {}).metrics || {};
  const tiles = [
    ['总收益（净）', fmtPct(m.total_return_pct), pctClass(m.total_return_pct),
      `买入持有 ${fmtPct(bench.total_return_pct)}`],
    ['年化收益 CAGR', fmtPct(m.cagr_pct), pctClass(m.cagr_pct),
      `买入持有 ${fmtPct(bench.cagr_pct)}`],
    ['最大回撤', fmtPct(m.max_drawdown_pct, false), pctClass(m.max_drawdown_pct),
      `买入持有 ${fmtPct(bench.max_drawdown_pct, false)}`],
    ['夏普比率', fmtNum(m.sharpe_rf0), '',
      `买入持有 ${fmtNum(bench.sharpe_rf0)}`],
    ['胜率', isNum(m.win_rate_pct) ? fmtNum(m.win_rate_pct, 1) + '%' : '—', '',
      `${m.trade_count ?? 0} 笔交易`],
    ['持仓时间占比', isNum(m.exposure_pct) ? fmtNum(m.exposure_pct, 1) + '%' : '—', '',
      `跳过信号 ${m.skipped_signals ?? 0} 个`],
  ];
  $('tiles').innerHTML = tiles.map(([label, value, cls, compare]) =>
    `<div class="tile"><div class="label">${esc(label)}</div>` +
    `<div class="value ${cls}">${esc(value)}</div>` +
    `<div class="compare">${esc(compare)}</div></div>`).join('');
}
function renderMetrics() {
  const m = currentSim().metrics || {};
  const known = new Set(METRIC_DEFS.map(d => d[0]));
  const items = METRIC_DEFS.filter(([k]) => k in m)
    .map(([k, label, kind]) => ({label, ...fmtMetric(m[k], kind)}))
    .concat(Object.keys(m).filter(k => !known.has(k)).sort()
      .map(k => ({label: k, ...fmtMetric(m[k], 'n2')})));
  $('metrics').innerHTML = items.map(it =>
    `<div class="metric"><span class="label">${esc(it.label)}</span>` +
    `<span class="value ${it.cls}">${esc(it.text)}</span></div>`).join('');
}
function renderTrades() {
  const trades = currentSim().trades || [];
  const managed = managedScenario();
  $('tradeEmpty').hidden = trades.length > 0;
  $('entryDateHeader').textContent = managed ? '入场日' : '研究起点日';
  $('entryPriceHeader').textContent = managed ? '入场价' : '起点价';
  $('exitDateHeader').textContent = managed ? '退出日' : '窗口终点日';
  $('exitPriceHeader').textContent = managed ? '退出价' : '终点价';
  $('tradeRows').innerHTML = trades.map((t, i) =>
    `<tr title="${esc(t.trade_id)}"><td class="lt">${i + 1}</td>` +
    `<td class="lt">${esc(t.signal_date)}</td>` +
    `<td class="lt">${esc(t.entry_date)}</td><td>${esc(fmtNum(t.entry_price))}</td>` +
    `<td class="lt">${esc(t.exit_date || '—')}</td><td>${esc(fmtNum(t.exit_price))}</td>` +
    `<td>${esc(isNum(t.holding_sessions) ? t.holding_sessions + '日' : '—')}</td>` +
    `<td>${esc(isNum(t.entry_allocation_pct) ? fmtNum(t.entry_allocation_pct, 1) + '%' : managed ? '—' : '100.0%')}</td>` +
    `<td class="lt">${esc(managed && t.exit_reason ? EXIT_REASON_LABELS[t.exit_reason] || t.exit_reason : STATUS_LABELS[t.status] || t.status)}</td>` +
    `<td class="${pctClass(t.gross_return_pct)}">${esc(fmtPct(t.gross_return_pct))}</td>` +
    `<td class="${pctClass(t.net_return_pct)}">${esc(fmtPct(t.net_return_pct))}</td></tr>`).join('');
}
function renderExclusions() {
  const skipped = (currentSim().skipped_signals || []).map(s =>
    ({date: s.signal_date, kind: '信号跳过', reason: REASON_LABELS[s.reason] || s.reason}));
  const transitions = (currentSim().state_transitions || []).map(s =>
    ({date: s.date, kind: '状态转换', reason: `${s.from_state} → ${s.to_state} · ${s.reason}`}));
  const excluded = ((A.event_study || {}).exclusions || []).slice(0, 500).map(x =>
    ({date: x.signal_date || '—', kind: `事件排除（${x.horizon ?? '—'}日）`,
      reason: REASON_LABELS[x.reason] || x.reason}));
  const rows = transitions.concat(skipped, excluded);
  $('exclusionSummary').textContent =
    `状态转换、跳过信号与事件排除（${transitions.length} + ${skipped.length} + ${excluded.length} 条）`;
  $('exclusionRows').innerHTML = rows.map(r =>
    `<tr><td class="lt">${esc(r.date)}</td><td class="lt">${esc(r.kind)}</td>` +
    `<td class="lt">${esc(r.reason)}</td></tr>`).join('');
}
function renderScenario() {
  tipSeries = seriesDefs();
  const managed = managedScenario();
  $('entryMarkerLabel').textContent = managed ? '入场' : '研究起点';
  $('exitMarkerLabel').textContent = managed ? '策略退出' : '窗口终点';
  $('positionSpanLabel').textContent = managed ? '持仓区间' : '研究持有窗口';
  renderTiles(); renderMetrics(); renderTrades(); renderExclusions();
  $('equityLegend').innerHTML = legendHtml(tipSeries);
  $('drawdownLegend').innerHTML = legendHtml(tipSeries.slice(0, 2));
}

// ---------- evidence & validation (scenario independent) ----------
function ciText(ci) {
  return Array.isArray(ci) && ci.length === 2 && ci.every(isNum)
    ? `[${fmtNum(ci[0], 2)}, ${fmtNum(ci[1], 2)}]` : '样本不足';
}
function renderEvidence() {
  const horizons = (A.event_study || {}).horizons || {};
  const rows = [], notes = [];
  for (const h of Object.keys(horizons).sort((p, q) => +p - +q)) {
    const v = horizons[h], inf = v.inference || {};
    const desc = ((v.descriptive || {}).disciplined_signal) || {};
    for (const [group, label] of [['yes', '结论=是'], ['not_yes', '非「是」']]) {
      const d = desc[group] || {}, nd = inf[group === 'yes' ? 'disciplined_yes' : 'not_yes'] || {};
      rows.push(`<tr><td class="lt">${esc(h)}日 · ${label}</td>` +
        `<td>${esc(d.n ?? '—')}</td><td>${esc(nd.n ?? '—')}</td>` +
        `<td class="${pctClass(d.mean_pct)}">${esc(fmtPct(d.mean_pct))}</td>` +
        `<td class="${pctClass(d.median_pct)}">${esc(fmtPct(d.median_pct))}</td>` +
        `<td>${esc(isNum(d.win_rate_pct) ? fmtNum(d.win_rate_pct, 1) + '%' : '—')}</td>` +
        `<td>${esc(group === 'yes' ? ciText(inf.yes_mean_ci_95) : '—')}</td></tr>`);
    }
    notes.push(`${h}日：Spearman IC ${isNum(inf.spearman_ic) ? fmtNum(inf.spearman_ic, 3) : '—'}` +
      ` · 「是」−「非是」均值差 ${fmtPct(inf.yes_minus_not_yes_mean_pct)}` +
      `（95% CI ${ciText(inf.yes_minus_not_yes_ci_95)}）`);
  }
  $('evidenceRows').innerHTML = rows.join('');
  $('evidenceNotes').innerHTML = notes.map(esc).join('<br>');
  const primary = String((A.validation || {}).primary_horizon ?? '');
  const hp = horizons[primary];
  $('sampleCount').textContent = hp
    ? `${primary}日 ${hp.descriptive_n ?? '—'} / ${hp.inference_n ?? '—'}` : '—';
}
const CONDITION_LABELS = {
  disciplined_yes_n: '「是」信号非重叠样本数',
  not_yes_n: '对照组（非「是」）非重叠样本数',
  yes_minus_not_yes_ci: '「是」−「非是」差值 95% CI',
  yes_spy_excess_ci: '「是」组 SPY 超额 95% CI',
};
function renderValidation() {
  const conditions = (A.validation || {}).conditions || {};
  $('validationConditions').innerHTML = Object.entries(conditions).map(([key, v]) => {
    const label = CONDITION_LABELS[key] || key;
    let mark = 'na', icon = '—', detail;
    if (typeof v.pass === 'boolean') {
      mark = v.pass ? 'ok' : 'fail'; icon = v.pass ? '✓' : '✗';
      detail = `${v.actual ?? '—'} / 要求 ≥ ${v.required ?? '—'}`;
    } else {
      const ci = ciText(v.actual);
      if (v.supports) { mark = 'ok'; icon = '✓'; }
      else if (v.contradicts) { mark = 'fail'; icon = '✗'; }
      detail = ci + (v.supports ? ' · 支持' : v.contradicts ? ' · 矛盾' : ' · 不判定');
    }
    return `<div class="cond"><span class="mark ${mark}">${icon}</span>` +
      `<span class="name">${esc(label)}</span>` +
      `<span class="detail">${esc(detail)}</span></div>`;
  }).join('');
  $('provenance').textContent = JSON.stringify(A.provenance, null, 2);
}

// ---------- boot ----------
function redraw() {
  activeRange();
  drawPrice(); drawScore();
  drawCurves('equityChart', 300, tipSeries);
  drawCurves('drawdownChart', 200, tipSeries.slice(0, 2), {drawdown: true});
}
if (scenarioSel.value) renderScenario(); else tipSeries = [];
renderEvidence(); renderValidation();
redraw();
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { renderScenario(); redraw(); });
</script>
</main></body></html>"""


def write_html(path: str | os.PathLike[str], artifact: Mapping[str, Any]) -> Path:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    document = render_html(artifact)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=target.parent,
        prefix=f".{target.name}.", suffix=".tmp", delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(document)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target
