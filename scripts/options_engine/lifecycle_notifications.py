"""Optional Telegram delivery for lifecycle summaries."""
from __future__ import annotations

from typing import Any, Callable

from local_env import get_env
from notify_telegram import send

from .lifecycle_reporting import build_lifecycle_alerts, render_telegram_summary


def notify_lifecycle(
    result: dict[str, Any], *, mode: str = "off", timeout: int = 15,
    sender: Callable[..., dict[str, Any]] = send,
    env_getter: Callable[[str], str] = get_env,
) -> dict[str, Any]:
    """Send only when explicitly enabled; notification failure never hides review output."""
    if mode not in {"off", "on", "alerts"}:
        raise ValueError("notification mode must be off, on or alerts")
    if mode == "off":
        return {"status": "disabled"}
    alerts = build_lifecycle_alerts(result)
    if mode == "alerts" and not alerts:
        return {"status": "skipped", "reason": "no_lifecycle_alerts"}
    token = (env_getter("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (env_getter("TELEGRAM_CHAT_ID") or "").strip()
    missing = [name for name, value in (
        ("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chat_id)
    ) if not value]
    if missing:
        return {"status": "unavailable", "reason": "telegram_not_configured", "missing": missing}
    try:
        body = sender(token, chat_id, render_telegram_summary(result), None, timeout)
    except Exception as exc:  # noqa: BLE001 - notification is best-effort
        # Exception text from HTTP clients can contain the credential-bearing URL.
        return {"status": "failed", "reason": "telegram_send_failed", "error_type": type(exc).__name__}
    if body.get("ok"):
        return {"status": "sent", "message_id": (body.get("result") or {}).get("message_id")}
    return {"status": "failed", "reason": "telegram_response_failed"}
