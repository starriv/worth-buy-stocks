#!/usr/bin/env python3
"""Validate a multi-agent artifact JSON file."""
from __future__ import annotations

import argparse
import json
import sys

from agent_contracts import KINDS, ContractError, validate_payload
from skill_logging import (
    add_logging_arguments,
    bind_log_context,
    clear_log_context,
    configure_logging,
    get_logger,
)

logger = get_logger("validate_agent_contract")


def _load_json(path: str):
    if path == "-":
        raw = sys.stdin.read()
    else:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    return json.loads(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate worth-buy-stocks agent artifact JSON")
    parser.add_argument("--kind", required=True, choices=KINDS)
    parser.add_argument("path", help="JSON file path, or '-' for stdin")
    add_logging_arguments(parser)
    args = parser.parse_args(argv)
    configure_logging(level=args.log_level, log_format=args.log_format)
    clear_log_context()
    bind_log_context(command="validate_agent_contract", artifact_kind=args.kind)

    try:
        payload = _load_json(args.path)
        result = validate_payload(args.kind, payload)
    except json.JSONDecodeError as e:
        logger.warning(
            "artifact_json_invalid",
            error_type=type(e).__name__,
            reason=str(e),
        )
        result = {
            "status": "error",
            "kind": args.kind,
            "error_code": "json_invalid",
            "message": str(e),
        }
        rc = 1
    except (OSError, ContractError) as e:
        logger.warning(
            "artifact_contract_invalid",
            error_type=type(e).__name__,
            reason=str(e),
        )
        result = {
            "status": "error",
            "kind": args.kind,
            "error_code": "contract_invalid",
            "message": str(e),
        }
        rc = 1
    else:
        rc = 0
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
