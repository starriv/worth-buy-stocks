#!/usr/bin/env python3
"""Tests for the skill-wide structlog facade."""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import skill_logging as L  # noqa: E402


class TtyBuffer(io.StringIO):
    def isatty(self):
        return True


class SecretObject:
    def __repr__(self):
        return "SecretObject(Authorization: Bearer object-secret)"


class SkillLoggingTests(unittest.TestCase):
    def tearDown(self):
        L.clear_log_context()
        logger = logging.getLogger(L.LOGGER_NAMESPACE)
        for handler in list(logger.handlers):
            if getattr(handler, L._HANDLER_MARKER, False):
                logger.removeHandler(handler)
                handler.close()

    def test_unconfigured_logger_is_silent_via_null_handler(self):
        stream = io.StringIO()
        root = logging.getLogger()
        probe = logging.StreamHandler(stream)
        root.addHandler(probe)
        self.addCleanup(root.removeHandler, probe)
        L.get_logger("unconfigured").warning("must_not_escape")
        self.assertEqual(stream.getvalue(), "")

    def test_json_contains_stable_minimum_fields_and_unicode(self):
        stream = io.StringIO()
        config = L.configure_logging(stream=stream, log_format="json")
        L.get_logger("unit").info("scan_started", symbol="腾讯", count=2)
        event = json.loads(stream.getvalue())
        self.assertEqual(config.log_format, "json")
        self.assertEqual(event["event"], "scan_started")
        self.assertEqual(event["level"], "info")
        self.assertEqual(event["logger"], "worth_buy_stocks.unit")
        self.assertEqual(event["symbol"], "腾讯")
        self.assertIn("timestamp", event)

    def test_console_is_human_readable_without_json_envelope(self):
        stream = TtyBuffer()
        with patch.dict(os.environ, {"NO_COLOR": "1"}, clear=False):
            config = L.configure_logging(stream=stream, log_format="auto")
            L.get_logger("unit").warning("cache_degraded", reason="fixture")
        output = stream.getvalue()
        self.assertEqual(config.log_format, "console")
        self.assertIn("cache_degraded", output)
        self.assertIn("fixture", output)
        self.assertFalse(output.lstrip().startswith("{"))
        self.assertNotIn("\x1b[", output)

    def test_level_and_environment_precedence(self):
        stream = io.StringIO()
        with patch.dict(
            os.environ,
            {L.LOG_LEVEL_ENV: "ERROR", L.LOG_FORMAT_ENV: "console", "NO_COLOR": "1"},
            clear=False,
        ):
            config = L.configure_logging(level="WARNING", stream=stream)
            logger = L.get_logger("unit")
            logger.info("hidden")
            logger.warning("visible")
        self.assertEqual((config.level, config.log_format), ("WARNING", "console"))
        self.assertNotIn("hidden", stream.getvalue())
        self.assertIn("visible", stream.getvalue())

    def test_invalid_environment_value_fails_clearly(self):
        with patch.dict(os.environ, {L.LOG_LEVEL_ENV: "VERBOSE"}, clear=False):
            with self.assertRaisesRegex(ValueError, L.LOG_LEVEL_ENV):
                L.configure_logging(stream=io.StringIO())

    def test_reconfigure_is_idempotent_and_uses_new_stream(self):
        first, second = io.StringIO(), io.StringIO()
        L.configure_logging(stream=first, log_format="json")
        logger = L.get_logger("unit")
        logger.info("first")
        L.configure_logging(stream=second, log_format="json")
        logger.info("second")
        self.assertEqual(len(first.getvalue().splitlines()), 1)
        self.assertEqual(len(second.getvalue().splitlines()), 1)
        self.assertNotIn("second", first.getvalue())

    def test_stdlib_bridge_and_bound_context(self):
        stream = io.StringIO()
        L.configure_logging(stream=stream, log_format="json")
        L.bind_log_context(command="scanner")
        logging.getLogger("worth_buy_stocks.legacy").warning(
            "legacy_warning", extra={"symbol": "AAPL"}
        )
        event = json.loads(stream.getvalue())
        self.assertEqual(event["event"], "legacy_warning")
        self.assertEqual(event["logger"], "worth_buy_stocks.legacy")
        self.assertEqual(event["symbol"], "AAPL")

    def test_non_serializable_values_and_concurrent_lines_are_safe(self):
        stream = io.StringIO()
        L.configure_logging(stream=stream, log_format="json")
        logger = L.get_logger("threads")
        workers = [
            threading.Thread(
                target=lambda index=index: logger.info(
                    "worker_event", worker=index, value=SecretObject()
                )
            )
            for index in range(20)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        events = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual(len(events), 20)
        self.assertEqual({event["worker"] for event in events}, set(range(20)))
        self.assertNotIn("object-secret", stream.getvalue())

    def test_sensitive_fields_urls_headers_and_exception_are_redacted(self):
        stream = io.StringIO()
        L.configure_logging(stream=stream, log_format="json")
        logger = L.get_logger("security")
        sentinel = "sentinel-super-secret"
        try:
            raise RuntimeError(
                f"request failed Authorization: Bearer {sentinel} "
                f"https://example.test/?token={sentinel}"
            )
        except RuntimeError:
            logger.exception(
                "request_failed",
                api_key=sentinel,
                nested={"TELEGRAM_BOT_TOKEN": sentinel},
                header=f"X-API-Key: {sentinel}",
                env=f"ALPACA_SECRET_KEY={sentinel}",
            )
        output = stream.getvalue()
        event = json.loads(output)
        self.assertNotIn(sentinel, output)
        self.assertEqual(event["api_key"], L.REDACTED)
        self.assertEqual(event["nested"]["TELEGRAM_BOT_TOKEN"], L.REDACTED)
        self.assertIn(L.REDACTED, event["exception"])

    def test_common_arguments_are_added_once(self):
        parser = argparse.ArgumentParser()
        self.assertIs(L.add_logging_arguments(parser), parser)
        L.add_logging_arguments(parser)
        args = parser.parse_args(["--log-level", "DEBUG", "--log-format", "json"])
        self.assertEqual((args.log_level, args.log_format), ("DEBUG", "json"))


if __name__ == "__main__":
    unittest.main()
