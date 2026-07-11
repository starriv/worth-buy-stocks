#!/usr/bin/env python3
"""Static guardrails for the skill-wide logging contract."""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FACADE = SCRIPTS / "skill_logging.py"
PUBLIC_CLIS = {
    "ai_opinion.py",
    "backtest.py",
    "cache_admin.py",
    "chart.py",
    "forward_log.py",
    "indicators.py",
    "notify_telegram.py",
    "scanner.py",
    "validate_agent_contract.py",
}
LEVEL_METHODS = {"debug", "info", "warning", "error", "exception", "critical"}
EVENT_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


def _name(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


class LoggingGuardTests(unittest.TestCase):
    def test_business_modules_do_not_bypass_facade(self):
        violations: list[str] = []
        for path in sorted(SCRIPTS.rglob("*.py")):
            if path == FACADE:
                continue
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = (
                        [alias.name for alias in node.names]
                        if isinstance(node, ast.Import)
                        else [node.module or ""]
                    )
                    if any(name in ("logging", "structlog") for name in names):
                        violations.append(f"{path.name}:{node.lineno}: direct {names}")
                if not isinstance(node, ast.Call):
                    continue
                call_name = _name(node.func)
                if call_name in {
                    "sys.stderr.write",
                    "logging.basicConfig",
                    *(f"logging.{level}" for level in LEVEL_METHODS),
                }:
                    violations.append(f"{path.name}:{node.lineno}: {call_name}")
                if call_name == "print":
                    for keyword in node.keywords:
                        if keyword.arg == "file" and _name(keyword.value) == "sys.stderr":
                            violations.append(f"{path.name}:{node.lineno}: print(file=sys.stderr)")
                if call_name.endswith(".stream.write") and node.args:
                    payload_name = _name(node.args[0].func) if isinstance(node.args[0], ast.Call) else ""
                    if payload_name == "json.dumps":
                        violations.append(f"{path.name}:{node.lineno}: independent JSON log writer")
        self.assertEqual(violations, [])

    def test_public_clis_use_common_configuration(self):
        missing: list[str] = []
        for name in sorted(PUBLIC_CLIS):
            source = (SCRIPTS / name).read_text(encoding="utf-8")
            if "add_logging_arguments(" not in source:
                missing.append(f"{name}: add_logging_arguments")
            if "configure_logging(" not in source:
                missing.append(f"{name}: configure_logging")
        self.assertEqual(missing, [])

    def test_literal_event_names_are_stable_snake_case(self):
        violations: list[str] = []
        for path in sorted(SCRIPTS.rglob("*.py")):
            if path == FACADE:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr not in LEVEL_METHODS or not node.args:
                    continue
                receiver = _name(node.func.value)
                if not receiver.endswith("logger"):
                    continue
                event = node.args[0]
                if isinstance(event, ast.Constant) and isinstance(event.value, str):
                    if not EVENT_NAME.fullmatch(event.value):
                        violations.append(f"{path.name}:{node.lineno}: {event.value!r}")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
