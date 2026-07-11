#!/usr/bin/env python3
"""Contract tests for the root Makefile command gateway."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("make"), "make is required")
class TestMakefileGateway(unittest.TestCase):
    def run_make(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["make", *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_default_help_is_safe_and_discovers_core_groups(self):
        result = self.run_make()
        self.assertEqual(result.returncode, 0, result.stderr)
        for target in (
            "analyze", "scan", "status", "sync-assets", "warm-market-plan",
            "reconcile-plan", "cache-purge-all", "backtest-factor", "backtest-visual",
            "backtest-lifecycle-validate", "check",
        ):
            self.assertIn(target, result.stdout)
        for variable in ("START", "END", "DATA_SOURCE", "COST_BPS", "HTML"):
            self.assertIn(variable, result.stdout)
        self.assertNotIn("Entering directory", result.stdout)

    def test_dry_run_forwards_market_job_variables_without_running(self):
        result = self.run_make(
            "-n", "reconcile-plan", "CACHE_DB=/tmp/make-gateway.sqlite3",
            "MAX_RPM=30", "WORKERS=2", "FORCE=1",
            "GLOBAL_ARGS=--log-level DEBUG",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('reconcile-market', result.stdout)
        self.assertIn('--cache-db "/tmp/make-gateway.sqlite3"', result.stdout)
        self.assertIn('--max-rpm "30"', result.stdout)
        self.assertIn('--workers "2"', result.stdout)
        self.assertIn('--force-assets', result.stdout)
        self.assertIn('--dry-run', result.stdout)
        self.assertLess(
            result.stdout.index("--log-level DEBUG"),
            result.stdout.index("reconcile-market"),
        )

    def test_research_targets_use_only_the_unified_backtest_cli(self):
        for target, command in (
            ("backtest-factor", "factor-ic"),
            ("backtest-score", "score"),
            ("backtest-robustness", "robustness"),
        ):
            with self.subTest(target=target):
                result = self.run_make(
                    "-n", target, "CACHE_DB=/tmp/research.sqlite3",
                    "GLOBAL_ARGS=--log-level DEBUG",
                    "ARGS=--cache refresh",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("scripts/backtest.py", result.stdout)
                self.assertIn(command, result.stdout)
                self.assertIn('--cache-db "/tmp/research.sqlite3"', result.stdout)
                self.assertNotRegex(
                    result.stdout,
                    r"backtest_(common|factor_ic|score|robustness)\.py",
                )
                self.assertLess(
                    result.stdout.index("--log-level DEBUG"),
                    result.stdout.index(command),
                )

    def test_diff_check_covers_worktree_and_staged_changes(self):
        result = self.run_make("-n", "diff-check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("git diff --check", result.stdout)
        self.assertIn("git diff --cached --check", result.stdout)

    def test_stateful_validation_uses_unified_cache_only_cli(self):
        result = self.run_make(
            "-n", "backtest-lifecycle-validate",
            "CACHE_DB=/tmp/research.sqlite3",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("scripts/backtest.py", result.stdout)
        self.assertIn("lifecycle-validate", result.stdout)
        self.assertIn('--cache-db "/tmp/research.sqlite3"', result.stdout)

    def test_status_preserves_json_only_stdout_and_missing_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "missing.sqlite3"
            result = self.run_make(
                "status", f"PYTHON={sys.executable}", f"CACHE_DB={db}"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["path"], str(db.resolve()))
            self.assertFalse(payload["exists"])
            self.assertFalse(db.exists())

    def test_symbol_purge_requires_explicit_scope_before_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "cache.sqlite3"
            result = self.run_make("cache-purge", f"CACHE_DB={db}")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("PURGE_SYMBOLS is required", result.stderr)
            self.assertFalse(db.exists())

    def test_full_purge_requires_exact_confirmation_before_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "cache.sqlite3"
            result = self.run_make(
                "cache-purge-all", "CONFIRM=yes", f"CACHE_DB={db}"
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("CONFIRM=purge-all is required", result.stderr)
            self.assertFalse(db.exists())

    def test_symbol_purge_requires_start_and_end_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "cache.sqlite3"
            result = self.run_make(
                "cache-purge", "PURGE_SYMBOLS=AAPL", "START=2026-07-01",
                f"CACHE_DB={db}",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("START and END must be provided together", result.stderr)
            self.assertFalse(db.exists())


if __name__ == "__main__":
    unittest.main()
