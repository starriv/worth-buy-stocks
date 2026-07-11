#!/usr/bin/env python3
"""Canonical artifact, CLI and self-contained HTML end-to-end tests."""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from backtesting.artifact import (  # noqa: E402
    build_artifact,
    load_artifact,
    semantic_hash,
    write_artifact,
)
from backtesting.contracts import BacktestConfig  # noqa: E402
from backtesting.data import load_backtest_data  # noqa: E402
from backtesting.visualization import render_html  # noqa: E402
from scoring import MODEL_VERSION  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "backtest.py"


def _dates(count: int = 340) -> list[str]:
    values = []
    day = dt.date(2023, 1, 3)
    while len(values) < count:
        if day.isoweekday() <= 5:
            values.append(day.isoformat())
        day += dt.timedelta(days=1)
    return values


def _bars(dates: list[str], base: float, slope: float) -> list[dict]:
    rows = []
    for index, date in enumerate(dates):
        cycle = ((index % 21) - 10) * 0.08
        close = base + slope * index + cycle
        rows.append({
            "t": f"{date}T04:00:00Z", "o": close - 0.25,
            "h": close + 0.9, "l": close - 0.9, "c": close,
            "v": 1_000_000 + (index % 15) * 50_000,
        })
    return rows


def _fixture(path: Path):
    dates = _dates()
    payload = {"bars": {
        "AAPL": _bars(dates, 100, 0.22),
        "SPY": _bars(dates, 400, 0.10),
        "QQQ": _bars(dates, 350, 0.12),
    }}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return dates


class ArtifactTests(unittest.TestCase):
    def _build(self, directory: str):
        source = Path(directory) / "bars.json"
        dates = _fixture(source)
        bundle = load_backtest_data(
            ["AAPL", "SPY", "QQQ"], dates[265], dates[-1],
            mode="input", input_path=str(source),
        )
        config = BacktestConfig(
            symbol="AAPL", start=dates[265], end=dates[-1],
            data_source="input", signal_step=1,
        )
        return build_artifact(config, bundle), source

    def test_semantic_hash_ignores_generated_time_and_path(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            one, _ = self._build(first)
            two, _ = self._build(second)
        self.assertEqual(one["semantic_hash"], two["semantic_hash"])
        self.assertEqual(one["model_version"], MODEL_VERSION)
        self.assertEqual(one["artifact_version"], "1.4")
        self.assertEqual(one["contract_version"], "1.4")
        self.assertNotEqual(one["artifact_version"], one["model_version"])

    def test_contract_v1_rejects_unfrozen_feed_or_adjustment(self):
        for values in ({"feed": "sip"}, {"adjustment": "raw"}):
            with self.subTest(values=values), self.assertRaisesRegex(
                ValueError, "requires feed=iex and adjustment=split"
            ):
                BacktestConfig(
                    symbol="AAPL", start="2021-01-01", end="2022-01-01", **values
                )

        for values in ({"horizons": (21,)}, {"warmup_sessions": 229}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                BacktestConfig(
                    symbol="AAPL", start="2021-01-01", end="2022-01-01", **values
                )
        with self.assertRaisesRegex(ValueError, "cost_bps"):
            BacktestConfig(
                symbol="AAPL", start="2021-01-01", end="2022-01-01",
                cost_bps=float("nan"),
            )

    def test_config_is_canonical_and_cannot_mislabel_data_provenance(self):
        config = BacktestConfig(
            symbol=" aapl ", start="2021-01-01", end="2022-01-01",
            horizons=(63, 21), data_source="INPUT", feed="IEX", adjustment="SPLIT",
        )
        self.assertEqual(config.symbol, "AAPL")
        self.assertEqual(config.horizons, (21, 63))
        self.assertEqual(config.data_source, "input")
        with tempfile.TemporaryDirectory() as tmp:
            artifact, source = self._build(tmp)
            dates = _dates()
            bundle = load_backtest_data(
                ["AAPL", "SPY", "QQQ"], dates[265], dates[-1],
                mode="input", input_path=str(source),
            )
            mismatched = BacktestConfig(
                symbol="AAPL", start=dates[236], end=dates[-1], data_source="input"
            )
            with self.assertRaisesRegex(ValueError, "provenance mismatch: start"):
                build_artifact(mismatched, bundle)
        self.assertIn("semantic_hash", artifact)

    def test_atomic_write_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact, _ = self._build(tmp)
            path = write_artifact(Path(tmp) / "out" / "artifact.json", artifact)
            loaded = load_artifact(path)
        self.assertEqual(artifact, loaded)
        self.assertEqual(loaded["semantic_hash"], semantic_hash(loaded))

    def test_loader_preserves_old_model_artifacts_but_rejects_contract_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact, _ = self._build(tmp)
            artifact["model_version"] = "v0.9"
            artifact["semantic_hash"] = semantic_hash(artifact)
            old_model = write_artifact(Path(tmp) / "old-model.json", artifact)
            self.assertEqual(load_artifact(old_model)["model_version"], "v0.9")

            artifact["artifact_version"] = "1.2"
            artifact["contract_version"] = "1.2"
            artifact["semantic_hash"] = semantic_hash(artifact)
            old_contract = write_artifact(Path(tmp) / "old-contract.json", artifact)
            self.assertEqual(load_artifact(old_contract)["artifact_version"], "1.2")

            artifact["contract_version"] = "999"
            artifact["semantic_hash"] = semantic_hash(artifact)
            with self.assertRaisesRegex(ValueError, "contract_version"):
                write_artifact(Path(tmp) / "unknown-contract.json", artifact)

    def test_loader_rejects_malformed_artifact_shape_with_contract_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact, _ = self._build(tmp)
            artifact["validation"] = []
            artifact["semantic_hash"] = semantic_hash(artifact)
            malformed = Path(tmp) / "malformed.json"
            malformed.write_text(json.dumps(artifact), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "validation must be an object"):
                load_artifact(malformed)

    def test_html_is_self_contained_traceable_and_escapes_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact, _ = self._build(tmp)
        artifact["event_study"]["exclusions"].append({
            "signal_date": "2026-01-01", "reason": "<script>alert('x')</script>"
        })
        artifact["semantic_hash"] = semantic_hash(artifact)
        document = render_html(artifact)
        self.assertIn("价格、持仓与交易", document)
        self.assertIn("评分时间线", document)
        self.assertIn("自动结论条件", document)
        self.assertIn("artifact-data", document)
        self.assertIn('value="disciplined_verdict_managed" selected', document)
        self.assertIn("63 日研究窗口（非策略出场）", document)
        self.assertIn("研究起点", document)
        self.assertIn("窗口终点", document)
        self.assertIn("计划仓位与持仓保护", document)
        self.assertIn("状态化持仓实验（已拒绝）", document)
        self.assertIn("stateful 场景是 contract 1.4 的已拒绝", document)
        self.assertNotIn("<script>alert('x')</script>", document)
        self.assertNotIn("<script src=", document)
        self.assertNotIn("<link rel=", document)
        self.assertNotIn("fetch(", document)
        self.assertIn(artifact["semantic_hash"][:16], document)

    def test_html_surfaces_invalid_run_as_a_failure_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact, _ = self._build(tmp)
        artifact["validation"]["status"] = "invalid_run"
        artifact["semantic_hash"] = semantic_hash(artifact)
        document = render_html(artifact)
        self.assertIn('<div class="status invalid_run">invalid_run</div>', document)


class BacktestCliTests(unittest.TestCase):
    def run_cli(self, *args: str, input_text: str | None = None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=ROOT,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_help_exposes_index_etf_validation_without_compatibility_entrypoint(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("index-lifecycle-validate", result.stdout)

    def test_input_mode_generates_json_and_html_then_rerenders(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "bars.json"
            dates = _fixture(source)
            output = Path(tmp) / "output"
            result = self.run_cli(
                "--log-format", "json", "single",
                "--symbol", "AAPL", "--start", dates[265], "--end", dates[-1],
                "--data-source", "input", "--input", str(source),
                "--signal-step", "1", "--output-dir", str(output),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            json_path, html_path = Path(summary["json"]), Path(summary["html"])
            self.assertTrue(json_path.is_file())
            self.assertTrue(html_path.is_file())
            source.unlink()
            rerender = self.run_cli(
                "--log-format", "json", "render", "--artifact", str(json_path),
                "--html", str(output / "rerender.html"),
            )
            self.assertEqual(rerender.returncode, 0, rerender.stderr)
            self.assertTrue((output / "rerender.html").is_file())
            self.assertEqual(
                html_path.read_bytes(),
                (output / "rerender.html").read_bytes(),
            )
            log_events = [json.loads(line) for line in result.stderr.splitlines() if line]
        self.assertEqual(log_events[-1]["event"], "backtest_completed")

    def test_json_dash_keeps_stdout_as_one_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "bars.json"
            dates = _fixture(source)
            result = self.run_cli(
                "--log-format", "json", "single",
                "--symbol", "AAPL", "--start", dates[265], "--end", dates[-1],
                "--data-source", "input", "--input", str(source),
                "--signal-step", "1", "--json", "-", "--html", "off",
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        artifact = json.loads(result.stdout)
        self.assertIn("semantic_hash", artifact)
        self.assertIn(artifact["validation"]["status"], {"supports", "inconclusive", "contradicts"})

    def test_cache_only_missing_db_uses_data_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.sqlite3"
            result = self.run_cli(
                "--log-format", "json", "single", "--symbol", "AAPL",
                "--start", "2024-01-01", "--end", "2025-01-01",
                "--cache-db", str(missing), "--html", "off",
            )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(json.loads(result.stdout)["error"], "cache_missing")
        self.assertFalse(missing.exists())

    def test_renderer_import_graph_does_not_load_scoring_or_data_layers(self):
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; sys.path.insert(0, 'scripts'); "
                    "import backtesting.visualization; "
                    "blocked={'scoring','pipeline','backtesting.data','backtesting.replay',"
                    "'backtesting.event_study','backtesting.simulation'}; "
                    "loaded=sorted(blocked.intersection(sys.modules)); "
                    "assert not loaded, loaded"
                ),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)

    def test_research_subcommands_are_unified_and_legacy_files_are_absent(self):
        for command in ("factor-ic", "score", "robustness"):
            with self.subTest(command=command):
                result = self.run_cli(command, "--help")
                self.assertEqual(result.returncode, 0, result.stderr)
        for name in (
            "backtest_common.py",
            "backtest_factor_ic.py",
            "backtest_score.py",
            "backtest_robustness.py",
        ):
            self.assertFalse((ROOT / "scripts" / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
