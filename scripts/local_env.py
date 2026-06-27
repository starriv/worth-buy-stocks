#!/usr/bin/env python3
"""Local .env reader for optional provider credentials.

The skill keeps credentials out of source and output. This module reads the
skill-root `.env` file when present, without printing values. Values in `.env`
take precedence over process environment variables so hooks do not accidentally
inherit unrelated global credentials.
"""
import os

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ENV_PATH = os.path.join(SKILL_ROOT, ".env")
ENV_PATH_OVERRIDE = "WORTH_BUY_STOCKS_ENV_FILE"


def env_path():
    return os.environ.get(ENV_PATH_OVERRIDE, "").strip() or DEFAULT_ENV_PATH


def _strip_unquoted_comment(value):
    in_single = False
    in_double = False
    for i, ch in enumerate(value):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if i == 0 or value[i - 1].isspace():
                return value[:i].rstrip()
    return value.strip()


def _unquote(value):
    value = _strip_unquoted_comment(value.strip())
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        quote = value[0]
        value = value[1:-1]
        if quote == '"':
            value = (
                value
                .replace("\\n", "\n")
                .replace('\\"', '"')
                .replace("\\\\", "\\")
            )
    return value


def parse_env_file(path=None):
    """Parse a minimal dotenv file into a dict.

    Supports KEY=value, `export KEY=value`, quotes, blank lines, and comments.
    Invalid lines are ignored intentionally; credentials are optional.
    """
    path = path or env_path()
    if not path or not os.path.isfile(path):
        return {}
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].strip()
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
                    continue
                out[key] = _unquote(value)
    except OSError:
        return {}
    return out


def get_env(name, default="", path=None, prefer_file=True):
    """Return a local credential value without exposing it.

    `.env` wins by default; process environment is a fallback. Set prefer_file
    false only for callers that explicitly want shell env precedence.
    """
    file_values = parse_env_file(path)
    if prefer_file and name in file_values:
        return str(file_values.get(name) or "").strip()
    if name in os.environ:
        return os.environ.get(name, "").strip()
    if not prefer_file and name in file_values:
        return str(file_values.get(name) or "").strip()
    return default
