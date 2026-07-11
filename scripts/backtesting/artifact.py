"""Canonical artifact composition, hashing, validation and atomic persistence."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from .contracts import (
    ARTIFACT_VERSION,
    CONTRACT_VERSION,
    SUPPORTED_ARTIFACT_VERSIONS,
    SUPPORTED_CONTRACT_VERSIONS,
    BacktestConfig,
)

if TYPE_CHECKING:
    from .data import DataBundle


WARNINGS = (
    "历史回测只包含价量主链路；新闻、账户、Finnhub、snapshot 与 AI opinion 未回填。",
    "行情为 split-adjusted 日线，不含现金股息，不等同于 total return。",
    "事件研究的逐日样本存在重叠；置信区间和自动结论只使用非重叠推断样本。",
    "固定 21/63 日场景是标准化研究窗口，窗口终点不是策略高位出场；managed 场景按日线近似执行建议仓位、初始止损、1R 保本、移动止损与收盘信号的下一开盘退出，两族场景不可合并统计。",
    "stateful 场景是 contract 1.4 的已拒绝实验生命周期；多标的 holdout 未通过，不得解释为已升级策略。",
)


def _semantic_payload(artifact: Mapping[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(dict(artifact))
    payload.pop("semantic_hash", None)
    payload.pop("generated_at", None)
    provenance = payload.get("provenance")
    if isinstance(provenance, dict):
        provenance.pop("db_path", None)
    return payload


def semantic_hash(artifact: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _semantic_payload(artifact),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_artifact(artifact: Mapping[str, Any]) -> None:
    required = {
        "artifact_version", "contract_version", "model_version", "semantic_hash",
        "generated_at", "config", "provenance", "bars", "timeline",
        "event_study", "simulations", "benchmarks", "warnings", "validation",
    }
    missing = sorted(required - set(artifact))
    if missing:
        raise ValueError(f"artifact missing fields: {', '.join(missing)}")
    for field in ("config", "provenance", "event_study", "simulations", "benchmarks", "validation"):
        if not isinstance(artifact[field], Mapping):
            raise ValueError(f"artifact {field} must be an object")
    for field in ("bars", "timeline", "warnings"):
        if not isinstance(artifact[field], list):
            raise ValueError(f"artifact {field} must be an array")
    if artifact["artifact_version"] not in SUPPORTED_ARTIFACT_VERSIONS:
        raise ValueError("unsupported artifact_version")
    if artifact["contract_version"] not in SUPPORTED_CONTRACT_VERSIONS:
        raise ValueError("unsupported contract_version")
    if not isinstance(artifact["model_version"], str) or not artifact["model_version"].strip():
        raise ValueError("artifact model_version must be a non-empty string")
    if artifact["validation"].get("status") not in {
        "supports", "inconclusive", "contradicts", "invalid_run",
    }:
        raise ValueError("artifact validation status is unsupported")
    expected = semantic_hash(artifact)
    if artifact["semantic_hash"] != expected:
        raise ValueError("artifact semantic_hash mismatch")
    json.dumps(artifact, ensure_ascii=False, allow_nan=False)


def build_artifact(config: BacktestConfig, bundle: DataBundle) -> dict[str, Any]:
    # Keep scoring out of the artifact loader/renderer import graph. Only a new
    # run needs to bind itself to the currently installed production model.
    from scoring import MODEL_VERSION
    from .event_study import build_event_study
    from .replay import replay_scores
    from .simulation import run_simulations

    expected = {
        "data_source": (config.data_source, bundle.mode),
        "feed": (config.feed, bundle.feed),
        "adjustment": (config.adjustment, bundle.adjustment),
        "start": (config.start, bundle.requested_start),
        "end": (config.end, bundle.requested_end),
        "warmup_sessions": (config.warmup_sessions, bundle.warmup_sessions),
    }
    mismatches = [
        name for name, (configured, observed) in expected.items()
        if configured != observed
    ]
    if mismatches:
        raise ValueError(
            "config and data provenance mismatch: " + ", ".join(mismatches)
        )

    replay = replay_scores(bundle, config.symbol, signal_step=config.signal_step)
    event_study = build_event_study(
        replay, bundle.bars, horizons=config.horizons
    )
    simulations, benchmarks = run_simulations(
        replay, bundle.bars, horizons=config.horizons, cost_bps=config.cost_bps
    )
    evaluation_bars = [
        dict(bar) for bar in bundle.bars[config.symbol]
        if bundle.actual_start <= bar["t"][:10] <= bundle.actual_end
    ]
    validation = copy.deepcopy(event_study["validation"])
    warnings = list(WARNINGS)
    warnings.extend(bundle.quality_flags)
    if config.signal_step != 1:
        validation = {
            **validation,
            "underlying_status": validation["status"],
            "status": "invalid_run",
            "invalid_reasons": [
                "signal_step must be 1 for a scientific validation run"
            ],
        }
        warnings.append(
            "signal_step 不为 1；本产物仅用于性能/流程验收，不得解释为评分有效性证据。"
        )
    artifact: dict[str, Any] = {
        "artifact_version": ARTIFACT_VERSION,
        "contract_version": CONTRACT_VERSION,
        "model_version": MODEL_VERSION,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "config": config.as_dict(),
        "provenance": bundle.provenance(),
        "bars": evaluation_bars,
        "timeline": replay["timeline"],
        "replay_exclusions": replay["exclusions"],
        "event_study": event_study,
        "simulations": simulations,
        "benchmarks": benchmarks,
        "warnings": warnings,
        "validation": validation,
    }
    artifact["semantic_hash"] = semantic_hash(artifact)
    validate_artifact(artifact)
    return artifact


def write_artifact(path: str | os.PathLike[str], artifact: Mapping[str, Any]) -> Path:
    validate_artifact(artifact)
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(
        artifact,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ) + "\n"
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=target.parent,
        prefix=f".{target.name}.", suffix=".tmp", delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target


def load_artifact(path: str | os.PathLike[str]) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("artifact must be a JSON object")
    validate_artifact(payload)
    return payload
