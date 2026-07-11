#!/usr/bin/env python3
"""Unified structured logging for worth-buy-stocks command-line tools.

Application payloads belong on stdout.  This module owns the skill's stderr
diagnostics and keeps configuration at CLI boundaries rather than import time.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars, merge_contextvars


LOGGER_NAMESPACE = "worth_buy_stocks"
LOG_LEVEL_ENV = "WORTH_BUY_STOCKS_LOG_LEVEL"
LOG_FORMAT_ENV = "WORTH_BUY_STOCKS_LOG_FORMAT"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
LOG_FORMATS = ("auto", "console", "json")
REDACTED = "[REDACTED]"
_HANDLER_MARKER = "_worth_buy_stocks_handler"
_NULL_HANDLER_MARKER = "_worth_buy_stocks_null_handler"

_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|secret|token|authorization|cookie|password|credential)",
    re.IGNORECASE,
)
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:api[_-]?key|secret|token|password|authorization)=)([^&\s]+)"
)
_ENV_SECRET = re.compile(
    r"(?i)(\b[A-Z0-9_]*(?:API_KEY|SECRET_KEY|TOKEN|PASSWORD|CREDENTIAL)"
    r"[A-Z0-9_]*\s*[=:]\s*)([^\s,;&]+)"
)
_HEADER_SECRET = re.compile(
    r"(?i)(\b(?:authorization|x-api-key|api-key|cookie)\s*[:=]\s*)"
    r"(?:(?:Bearer|Basic)\s+)?([^\s,;]+)"
)
_AUTH_SCHEME_SECRET = re.compile(r"(?i)\b(Bearer|Basic)\s+[^\s,;]+")


def _install_null_handler() -> None:
    """Prevent logging.lastResort output when modules are used without a CLI."""

    namespace_logger = logging.getLogger(LOGGER_NAMESPACE)
    if not any(
        getattr(handler, _NULL_HANDLER_MARKER, False)
        for handler in namespace_logger.handlers
    ):
        handler = logging.NullHandler()
        setattr(handler, _NULL_HANDLER_MARKER, True)
        namespace_logger.addHandler(handler)
    namespace_logger.propagate = False


_install_null_handler()


@dataclass(frozen=True)
class LoggingConfig:
    """Resolved logging settings, returned for tests and diagnostics."""

    level: str
    log_format: str
    stream: TextIO


def _logger_name(module_name: str | None) -> str:
    raw = str(module_name or "unknown").strip().replace("/", ".")
    if raw.startswith(f"{LOGGER_NAMESPACE}.") or raw == LOGGER_NAMESPACE:
        return raw
    if raw in ("", "__main__"):
        raw = "cli"
    return f"{LOGGER_NAMESPACE}.{raw.strip('.')}"


def get_logger(module_name: str | None = None):
    """Return a lazily configured structlog logger in the skill namespace."""

    return structlog.get_logger(_logger_name(module_name))


def add_logging_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Add the common logging switches to an argparse parser once."""

    if any(action.dest == "log_level" for action in parser._actions):
        return parser
    group = parser.add_argument_group("日志")
    group.add_argument(
        "--log-level",
        choices=LOG_LEVELS,
        default=None,
        help=f"stderr 日志级别；默认 ${LOG_LEVEL_ENV} 或 INFO",
    )
    group.add_argument(
        "--log-format",
        choices=LOG_FORMATS,
        default=None,
        help=(
            "stderr 格式；auto=TTY console/非 TTY JSONL；"
            f"默认 ${LOG_FORMAT_ENV} 或 auto"
        ),
    )
    return parser


def _resolve_choice(value: str | None, env_name: str, allowed: Sequence[str], default: str) -> str:
    raw = value if value is not None else os.environ.get(env_name, default)
    normalized = str(raw).strip()
    if env_name == LOG_LEVEL_ENV:
        normalized = normalized.upper()
    else:
        normalized = normalized.lower()
    if normalized not in allowed:
        choices = ", ".join(allowed)
        raise ValueError(f"{env_name} 必须是 {choices} 之一")
    return normalized


def _redact_text(value: str) -> str:
    value = _QUERY_SECRET.sub(lambda m: f"{m.group(1)}{REDACTED}", value)
    value = _ENV_SECRET.sub(lambda m: f"{m.group(1)}{REDACTED}", value)
    value = _HEADER_SECRET.sub(lambda m: f"{m.group(1)}{REDACTED}", value)
    return _AUTH_SCHEME_SECRET.sub(lambda m: f"{m.group(1)} {REDACTED}", value)


def _json_safe(value: Any, seen: set[int] | None = None) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)

    seen = seen if seen is not None else set()
    marker = id(value)
    if marker in seen:
        return "<recursive>"
    seen.add(marker)
    try:
        if isinstance(value, Mapping):
            return {str(key): _json_safe(item, seen) for key, item in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [_json_safe(item, seen) for item in value]
        return repr(value)
    finally:
        seen.discard(marker)


def _redact_value(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _SENSITIVE_KEY.search(key):
        return REDACTED
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {
            item_key: _redact_value(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


def _json_safe_processor(_logger, _method_name: str, event_dict: dict[str, Any]):
    return _json_safe(event_dict)


def _redact_processor(_logger, _method_name: str, event_dict: dict[str, Any]):
    return _redact_value(event_dict)


def _shared_processors() -> list[Any]:
    return [
        merge_contextvars,
        structlog.stdlib.ExtraAdder(),
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _json_safe_processor,
        _redact_processor,
    ]


def _is_tty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError):
        return False


def _console_colors(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return _is_tty(stream)


def _json_dumps(value: Any, **kwargs: Any) -> str:
    kwargs.setdefault("ensure_ascii", False)
    kwargs.setdefault("sort_keys", True)
    return json.dumps(value, **kwargs)


def configure_logging(
    *,
    level: str | None = None,
    log_format: str | None = None,
    stream: TextIO | None = None,
) -> LoggingConfig:
    """Configure the skill logger namespace and return resolved settings.

    Reconfiguration replaces only handlers created by this module.  Root and
    third-party handlers are left untouched, which keeps imports/tests safe.
    """

    target = stream if stream is not None else sys.stderr
    resolved_level = _resolve_choice(level, LOG_LEVEL_ENV, LOG_LEVELS, "INFO")
    requested_format = _resolve_choice(log_format, LOG_FORMAT_ENV, LOG_FORMATS, "auto")
    resolved_format = (
        "console" if requested_format == "auto" and _is_tty(target)
        else "json" if requested_format == "auto"
        else requested_format
    )

    if resolved_format == "json":
        renderer = structlog.processors.JSONRenderer(serializer=_json_dumps)
    else:
        renderer = structlog.dev.ConsoleRenderer(
            colors=_console_colors(target),
            exception_formatter=structlog.dev.plain_traceback,
        )

    shared = _shared_processors()
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=shared,
    )
    handler = logging.StreamHandler(target)
    setattr(handler, _HANDLER_MARKER, True)
    handler.setFormatter(formatter)
    handler.setLevel(getattr(logging, resolved_level))

    namespace_logger = logging.getLogger(LOGGER_NAMESPACE)
    for current in list(namespace_logger.handlers):
        if getattr(current, _HANDLER_MARKER, False):
            namespace_logger.removeHandler(current)
            current.close()
    namespace_logger.addHandler(handler)
    namespace_logger.setLevel(getattr(logging, resolved_level))
    namespace_logger.propagate = False

    structlog.configure(
        processors=[
            merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _json_safe_processor,
            _redact_processor,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )
    return LoggingConfig(resolved_level, resolved_format, target)


def bind_log_context(**fields: Any):
    """Bind context-local fields; workers must explicitly re-bind as needed."""

    return bind_contextvars(**fields)


def clear_log_context() -> None:
    """Clear context-local fields at the end/start of a CLI invocation."""

    clear_contextvars()
