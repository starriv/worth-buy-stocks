#!/usr/bin/env python3
"""Display-only AI second opinion overlay (the fifth overlay).

Sends the already-computed evidence (indicators, relative strength, structure,
news red flags) to Claude and asks for an independent stance. The script — not
the model — compares that stance against the script verdict to produce an
``agree``/``diverge`` marker.

Hard boundaries (see docs/ai-opinion-overlay-design.md):

- Zero scoring power: output goes to ``supplemental.ai_opinion`` only and must
  never change ``score.verdict``/``score.composite``, cap, or downgrade.
- Anti-anchoring: the evidence payload is built from an explicit whitelist so
  the model never sees verdict, composite, or trade plan.
- Graceful degradation: no API key, missing SDK, or any API failure returns a
  ``status=unavailable`` artifact and never blocks scoring.
"""
from __future__ import annotations

import datetime
import json
import sys
from typing import Any

from agent_contracts import CONTRACT_VERSION, KIND_AI_OPINION, ContractError, validate_payload
from local_env import get_env

try:  # anthropic SDK 是可选依赖：未安装时整体降级为 unavailable，不影响评分
    import anthropic as _anthropic
except ImportError:  # pragma: no cover - 取决于运行环境
    _anthropic = None

API_KEY_ENV = "ANTHROPIC_API_KEY"
MODEL = "claude-opus-4-8"
REQUEST_TIMEOUT_SECONDS = 60.0
MAX_TOKENS = 8192
BENCHMARK_SYMBOLS = {"SPY", "QQQ"}

# 证据白名单：只从单票分析结果拷贝这些键。verdict/composite/trade_plan 等含结论
# 倾向的字段不在名单内，从结构上保证防锚定（另有测试断言保护）。
EVIDENCE_KEYS = (
    "last_close", "last_date", "bars_count",
    "ma", "weekly", "structure_30d",
    "returns_pct", "relative_strength_pct",
    "momentum", "volatility", "risk", "trend_quality",
    "macd", "rsi", "kdj", "volume",
)

# 脚本 verdict → 等价 AI stance；用于 agreement 计算（由脚本算，不由模型自述）
_VERDICT_TO_STANCE = {
    "是": "buy",
    "观察": "watch",
    "否": "avoid",
    "持仓需减风险": "avoid",
}

_STANCE_ZH = {"buy": "买入", "watch": "观察", "avoid": "回避"}

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "stance": {"type": "string", "enum": ["buy", "watch", "avoid"]},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "view": {"type": "string", "description": "中文一句话核心理由，不超过 60 字"},
        "key_risks": {
            "type": "array",
            "items": {"type": "string"},
            "description": "0-3 条主要风险，中文短语",
        },
    },
    "required": ["stance", "confidence", "view", "key_risks"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = """你是一名独立的美股技术分析顾问，基于趋势跟随与相对强度纪律框架给出第二意见。

规则：
- 只依据用户提供的结构化证据判断，不引入证据之外的公司知识、新闻记忆或行情记忆。
- 框架取向：只认可趋势延续与相对强度领先；弱势修复、均值回归反弹和估值叙事不构成买入理由。
- stance 含义：buy=值得新开仓；watch=观察等待更好的入场确认；avoid=回避或不参与。
- view 用中文一句话（不超过 60 字）给出最关键依据；key_risks 列出 0-3 条主要风险。
- 这是纪律对照参考，不是投资建议；不要输出免责声明或客套话。"""


def has_api_key() -> bool:
    return bool(get_env(API_KEY_ENV).strip())


def sdk_available() -> bool:
    return _anthropic is not None


def build_evidence(
    symbol_analysis: dict[str, Any],
    market_risk_off: Any = None,
    news_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Whitelist-copy evidence for the model; conclusion-bearing fields never enter."""
    evidence = {k: symbol_analysis[k] for k in EVIDENCE_KEYS if k in symbol_analysis}
    evidence["market_risk_off"] = bool(market_risk_off) if market_risk_off is not None else None
    if isinstance(news_context, dict):
        news: dict[str, Any] = {}
        if news_context.get("red_flags"):
            news["red_flags"] = news_context["red_flags"]
        if news_context.get("catalyst"):
            news["catalyst"] = news_context["catalyst"]
        if news:
            evidence["news"] = news
    # data_flags（历史不足/低流动性等）是事实性提示，不含结论，允许进入证据
    data_flags = (symbol_analysis.get("score") or {}).get("data_flags")
    if data_flags:
        evidence["data_flags"] = data_flags
    return evidence


def compute_agreement(verdict: Any, stance: str) -> str:
    expected = _VERDICT_TO_STANCE.get(str(verdict or "").strip())
    if expected is None:
        return "not_applicable"
    return "agree" if expected == stance else "diverge"


def stance_zh(stance: str) -> str:
    return _STANCE_ZH.get(str(stance or "").strip(), str(stance or ""))


def unavailable_opinions(
    artifact: dict[str, Any] | None,
    symbols: list[str] | None = None,
) -> dict[str, str]:
    """Return requested symbols without a usable opinion.

    Top-level ``status=ok`` means the batch contract itself is usable; individual
    entries may still have failed. Explicit ``--ai-opinion on`` callers use this
    helper to enforce all-requested-symbol success instead of checking only the
    top-level status.
    """
    if not isinstance(artifact, dict):
        return {"*": "AI 意见 artifact 缺失"}
    if artifact.get("status") != "ok":
        return {"*": str(artifact.get("reason") or "AI 意见整体不可用")}
    entries = artifact.get("symbols") if isinstance(artifact.get("symbols"), dict) else {}
    wanted = [str(s).strip().upper() for s in symbols] if symbols is not None else list(entries)
    failures: dict[str, str] = {}
    for sym in wanted:
        entry = entries.get(sym)
        if not isinstance(entry, dict) or entry.get("status") != "ok":
            reason = entry.get("reason") if isinstance(entry, dict) else "AI 意见条目缺失"
            failures[sym] = str(reason or "AI 意见不可用")
    return failures


def _unavailable(reason: str) -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "kind": KIND_AI_OPINION,
        "status": "unavailable",
        "reason": reason,
    }


def _call_model(symbol: str, evidence: dict[str, Any]) -> dict[str, Any]:
    """One structured-output API call; raises on any failure (caller degrades)."""
    client = _anthropic.Anthropic(
        api_key=get_env(API_KEY_ENV),
        timeout=REQUEST_TIMEOUT_SECONDS,
        max_retries=1,
    )
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        thinking={"type": "adaptive"},
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": _RESPONSE_SCHEMA},
        },
        system=_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                f"请根据以下结构化证据，对 {symbol} 给出独立判断：\n"
                + json.dumps(evidence, ensure_ascii=False)
            ),
        }],
    )
    if getattr(response, "stop_reason", None) == "refusal":
        raise RuntimeError("model refused the request")
    text = next(
        (block.text for block in response.content if getattr(block, "type", "") == "text"),
        None,
    )
    if not text:
        raise RuntimeError("no text block in model response")
    return json.loads(text)


def _opinion_entry(symbol: str, analysis: dict[str, Any], market_risk_off: Any,
                   news_context: dict[str, Any] | None) -> dict[str, Any]:
    verdict = (analysis.get("score") or {}).get("verdict")
    parsed = _call_model(symbol, build_evidence(analysis, market_risk_off, news_context))
    stance = str(parsed.get("stance") or "").lower()
    confidence = str(parsed.get("confidence") or "").lower()
    if stance not in _VERDICT_TO_STANCE.values() or confidence not in {"low", "medium", "high"}:
        raise RuntimeError(f"model returned invalid stance/confidence: {stance}/{confidence}")
    return {
        "status": "ok",
        "stance": stance,
        "confidence": confidence,
        "view": str(parsed.get("view") or "").strip(),
        "key_risks": [str(r) for r in (parsed.get("key_risks") or [])][:3],
        "agreement": compute_agreement(verdict, stance),
        "model": MODEL,
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


def opinions_for_result(
    result: dict[str, Any],
    llm_context: dict[str, dict[str, Any]] | None = None,
    symbols: list[str] | None = None,
) -> dict[str, Any]:
    """Build an ai_opinion contract for the scored, non-benchmark symbols of a result.

    Never raises: every failure mode degrades to top-level or per-symbol
    ``status=unavailable``. The returned artifact is self-checked against the
    contract before being handed back.
    """
    if not sdk_available():
        return _unavailable("anthropic SDK 未安装（pip install anthropic）")
    if not has_api_key():
        return _unavailable(f"{API_KEY_ENV} 未配置")
    llm_context = llm_context or {}
    wanted = {s.strip().upper() for s in symbols} if symbols else None
    entries: dict[str, Any] = {}
    for sym, analysis in (result.get("symbols") or {}).items():
        sym = str(sym).strip().upper()
        if sym in BENCHMARK_SYMBOLS or (wanted is not None and sym not in wanted):
            continue
        if not isinstance(analysis, dict) or "error" in analysis:
            entries[sym] = {"status": "unavailable", "reason": "该标的无分析结果"}
            continue
        verdict = str((analysis.get("score") or {}).get("verdict") or "").strip()
        if verdict not in _VERDICT_TO_STANCE:
            # 无法评分（或缺 score）：约定不触网，直接标记不可用（设计文档 4.5）
            entries[sym] = {"status": "unavailable", "reason": "脚本无法评分，未调用 AI"}
            continue
        try:
            entries[sym] = _opinion_entry(sym, analysis, result.get("market_risk_off"), llm_context.get(sym))
        except Exception as e:  # 单票失败只降级该票，不影响其他票与评分
            entries[sym] = {"status": "unavailable", "reason": f"AI 调用失败：{type(e).__name__}"}
    artifact = {
        "contract_version": CONTRACT_VERSION,
        "kind": KIND_AI_OPINION,
        "status": "ok",
        "symbols": entries,
    }
    try:
        validate_payload(KIND_AI_OPINION, artifact)
    except ContractError as e:  # 自检兜底：不合契约宁可整体不可用，也不外泄坏数据
        return _unavailable(f"ai_opinion 自检失败：{e}")
    return artifact


def main(argv: list[str] | None = None) -> int:
    """CLI: read a result JSON from stdin, print the ai_opinion artifact."""
    import argparse

    parser = argparse.ArgumentParser(description="Generate display-only AI opinions for a result JSON")
    parser.add_argument("--llm-context-file", help="optional news_context JSON for evidence enrichment")
    parser.add_argument("--symbols", help="comma-separated symbol filter")
    args = parser.parse_args(argv)

    result = json.load(sys.stdin)
    llm_context = None
    if args.llm_context_file:
        with open(args.llm_context_file, encoding="utf-8") as f:
            raw = json.load(f)
        llm_context = raw.get("symbols") if isinstance(raw.get("symbols"), dict) else raw
    symbols = [s for s in (args.symbols or "").split(",") if s.strip()] or None
    artifact = opinions_for_result(result, llm_context=llm_context, symbols=symbols)
    json.dump(artifact, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
