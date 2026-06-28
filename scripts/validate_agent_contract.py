#!/usr/bin/env python3
"""Validate a multi-agent artifact JSON file."""
from __future__ import annotations

import argparse
import json
import sys

from agent_contracts import KINDS, ContractError, validate_payload


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
    args = parser.parse_args(argv)

    try:
        payload = _load_json(args.path)
        result = validate_payload(args.kind, payload)
    except json.JSONDecodeError as e:
        result = {
            "status": "error",
            "kind": args.kind,
            "error_code": "json_invalid",
            "message": str(e),
        }
        rc = 1
    except (OSError, ContractError) as e:
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
