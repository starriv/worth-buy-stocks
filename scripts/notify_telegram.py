#!/usr/bin/env python3
"""把分析结论推送到 Telegram（仅 Python 标准库 urllib，不依赖第三方）。

读取 skill 根目录 `.env` 或环境变量 TELEGRAM_BOT_TOKEN 与 TELEGRAM_CHAT_ID：
  - 两者都配置 -> 发送消息，输出 {"ok": true, "message_id": ...}
  - 任一缺失   -> stderr 打印配置指引，stdout 输出 {"ok": false, "error": "telegram_not_configured", ...}，退出码 2

不打印 token、不在错误信息里回显含 token 的 URL。消息文本从 --text / --file / stdin 读取。

用法：
  echo "结论..." | notify_telegram.py
  notify_telegram.py --text "结论..."
  notify_telegram.py --file result.md --parse-mode Markdown
"""
import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

from local_env import DEFAULT_ENV_PATH, get_env

API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_LEN = 4096  # Telegram 单条消息字符上限
TRUNCATED_MARK = "\n…(已截断)"

CONFIG_HINT = (
    "未配置 Telegram 凭证，无法发送通知。请在 skill 根目录 .env 写入，或设置同名环境变量：\n"
    f"  {DEFAULT_ENV_PATH}\n"
    "  TELEGRAM_BOT_TOKEN=<你的 bot token>   # 向 @BotFather 创建 bot 获取\n"
    "  TELEGRAM_CHAT_ID=<目标 chat id>        # 给 bot 发条消息后用 getUpdates 查 chat.id\n"
)


def _truncate(text, max_len=MAX_LEN):
    """超过 Telegram 单条上限时截断并加标记，确保结果不超过 max_len。"""
    if len(text) <= max_len:
        return text
    return text[:max_len - len(TRUNCATED_MARK)] + TRUNCATED_MARK


def _read_text(args):
    if args.text is not None:
        return args.text
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            return f.read()
    return sys.stdin.read()


def send(token, chat_id, text, parse_mode=None, timeout=15):
    payload = {
        "chat_id": chat_id,
        "text": _truncate(text),
        "disable_web_page_preview": "true",
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(API.format(token=token), data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _emit(obj, code):
    json.dump(obj, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    sys.exit(code)


def main():
    p = argparse.ArgumentParser(description="发送 Telegram 通知")
    p.add_argument("--text", help="消息文本；缺省则从 --file 或 stdin 读")
    p.add_argument("--file", help="从文件读消息文本")
    p.add_argument("--parse-mode", choices=["Markdown", "MarkdownV2", "HTML"],
                   help="Telegram 解析模式；缺省为纯文本")
    p.add_argument("--timeout", type=int, default=15)
    args = p.parse_args()

    token = get_env("TELEGRAM_BOT_TOKEN").strip()
    chat_id = get_env("TELEGRAM_CHAT_ID").strip()
    if not token or not chat_id:
        missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", token),
                                  ("TELEGRAM_CHAT_ID", chat_id)) if not v]
        sys.stderr.write(CONFIG_HINT)
        _emit({"ok": False, "error": "telegram_not_configured", "missing": missing}, 2)

    text = _read_text(args).strip()
    if not text:
        _emit({"ok": False, "error": "empty_message"}, 1)

    try:
        body = send(token, chat_id, text, args.parse_mode, args.timeout)
    except urllib.error.HTTPError as e:
        # 只回显状态码与响应体（不含 token），不暴露请求 URL
        detail = e.read().decode("utf-8", "replace")[:300]
        _emit({"ok": False, "error": f"http_{e.code}", "detail": detail}, 1)
    except Exception as e:  # noqa: BLE001
        _emit({"ok": False, "error": str(e)}, 1)

    if body.get("ok"):
        _emit({"ok": True, "message_id": body.get("result", {}).get("message_id")}, 0)
    _emit({"ok": False, "error": body.get("description", "unknown")}, 1)


if __name__ == "__main__":
    main()
