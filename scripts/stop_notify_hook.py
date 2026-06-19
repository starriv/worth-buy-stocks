#!/usr/bin/env python3
"""Claude Code Stop hook：分析结束后自动把 worth-buy-stocks 结论推送到 Telegram。

注册在 settings.json 的 Stop 事件上。它从 stdin 读取 hook 负载（含 transcript_path），
解析出最后一条 assistant 文本，**仅当**该文本是一次 worth-buy-stocks 分析
（同时包含「是否值得买」与「纪律评分」标记）时，才把它转发给 notify_telegram.py。

设计约束（务必满足，否则会污染每一次会话）：
  - 内容守卫：非分析输出一律跳过，普通聊天不发消息。
  - 去重：按 session 记录上次已推送的 assistant uuid，Stop 重复触发不重发。
  - 非阻塞：发送动作 detach 到后台子进程，hook 立即返回。
  - 永不报错：任何异常都吞掉并 exit 0，绝不让通知逻辑打断会话。
"""
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
NOTIFY = os.path.join(HERE, "notify_telegram.py")

# 同时命中这些标记才认定为一次股票分析结论（降低误报）
MARKERS = ("是否值得买", "纪律评分")

# 优先取结论里显式写的「标的: AAPL」；兜底再从全文找独立的大写代码 token
_TICKER_LABELED = re.compile(r"标的[:：]\s*\*{0,2}([A-Z][A-Z.\-]{0,9})")
_TICKER_BARE = re.compile(r"(?<![A-Za-z])([A-Z]{1,5}(?:[.\-][A-Z]{1,4})?)(?![A-Za-z])")
# 兜底时排除的常见非代码大写词，避免把缩写误当 ticker
_TICKER_STOP = {"MA", "MACD", "RSI", "KDJ", "SPY", "QQQ", "DIF", "DEA",
                "OBV", "ATR", "ETF", "USD", "API", "JSON", "OK"}

# 只从「结论」段保留这几条核心字段（标的另起标题，强制排除/评分拆解等略去）
_CONCL_FIELDS = ("是否值得买", "建议", "纪律评分", "一句话")
# 匹配多种标题形式：**结论** / **结论：** / ## 结论 / ### 结论
_HEADER_RE = re.compile(r"^\s*(?:\*{2}(.+?)\*{2}|#{1,4}\s*\*{0,2}(.+?)\*{0,2})\s*$")
_BULLET_RE = re.compile(r"^\s*[-*•]\s+")


def _extract_ticker(text):
    """从结论文本提取股票代码：先认显式「标的:」，再兜底扫裸代码（排除 SPY/QQQ 等）。"""
    m = _TICKER_LABELED.search(text)
    if m:
        return m.group(1)
    for cand in _TICKER_BARE.findall(text):
        if cand not in _TICKER_STOP and not cand.isdigit():
            return cand
    return None


def _sections(text):
    """按 **标题** 或 ## 标题 切段；先剔除代码块（K 线 ASCII，Telegram 无法渲染）。"""
    # 剔除代码块：先处理闭合的，再处理未闭合的（从 ``` 到文末）
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"```.*", "", text, flags=re.S)
    # 切段
    sections, cur = {}, None
    for line in text.splitlines():
        m = _HEADER_RE.match(line.strip())
        if m:
            # regex 有两个捕获组（**...** 或 ## ...），取非空的那个
            raw = (m.group(1) or m.group(2) or "").strip()
            # 规范化：去掉尾部冒号，统一为无格式名
            cur = raw.rstrip("：:")
            sections.setdefault(cur, [])
        elif cur is not None:
            sections[cur].append(line)
    return sections


def _clean(s):
    """去掉 bullet 标记与 Markdown 强调/反引号，留纯文本。"""
    s = _BULLET_RE.sub("", s.strip())
    return s.replace("**", "").replace("`", "").strip()


def _esc(s):
    # 只转义 & < >（Telegram HTML 必需）；保留引号等，避免 &#x27; 渲染异常
    return html.escape(s, quote=False)


def _format_message(text):
    """从分析全文抽取「股票代码 + 结论 + 证据列表」，组装为 Telegram HTML。

    丢弃 K 线 ASCII、表格、评分拆解等 Telegram 无法渲染或冗余的内容。
    解析不到结论与证据时返回 None（交由调用方走纯文本兜底）。
    """
    sec = _sections(text)
    concl = {}
    for ln in sec.get("结论", []):
        c = _clean(ln)
        for f in _CONCL_FIELDS:
            if c.startswith(f):
                concl[f] = c
    evidence = [_clean(ln) for ln in sec.get("关键证据", []) if _BULLET_RE.match(ln)]
    evidence = [e for e in evidence if e]
    if not concl and not evidence:
        return None

    ticker = _extract_ticker(text) or "股票"
    out = [f"📈 <b>{_esc(ticker)}</b> 分析结论"]
    if concl:
        out.append("")
        out.append("<b>结论</b>")
        out += [_esc(concl[f]) for f in _CONCL_FIELDS if f in concl]
    if evidence:
        out.append("")
        out.append("<b>关键证据</b>")
        out += [f"• {_esc(e)}" for e in evidence]
    return "\n".join(out)


def _last_assistant(transcript_path):
    """从 Claude Code transcript JSONL 取最后一条 assistant 的纯文本与 uuid。"""
    last_text, last_uuid = None, None
    with open(transcript_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") != "assistant":
                continue
            content = (ev.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            parts = [c.get("text", "") for c in content
                     if isinstance(c, dict) and c.get("type") == "text"]
            text = "".join(parts).strip()
            if text:
                last_text, last_uuid = text, ev.get("uuid")
    return last_text, last_uuid


def _state_path(session_id):
    key = hashlib.sha256((session_id or "default").encode()).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), f"wbs_notify_{key}.txt")


def _already_sent(session_id, marker):
    path = _state_path(session_id)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() == marker
    except OSError:
        return False


def _record_sent(session_id, marker):
    try:
        with open(_state_path(session_id), "w", encoding="utf-8") as f:
            f.write(marker)
    except OSError:
        pass


def main():
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return  # 无有效负载，静默退出

    transcript = payload.get("transcript_path")
    session_id = payload.get("session_id", "")
    if not transcript or not os.path.exists(transcript):
        return

    try:
        text, uuid = _last_assistant(transcript)
    except OSError:
        return
    if not text or not all(m in text for m in MARKERS):
        return  # 不是分析结论，守卫拦下

    # 去重：同一条结论（uuid 或内容哈希）只推一次
    marker = uuid or hashlib.sha256(text.encode()).hexdigest()
    if _already_sent(session_id, marker):
        return

    # 抽取精简内容；HTML 渲染，解析失败则纯文本兜底
    msg = _format_message(text)
    if msg:
        args = [sys.executable, NOTIFY, "--parse-mode", "HTML", "--text", msg]
    else:
        ticker = _extract_ticker(text) or "股票"
        args = [sys.executable, NOTIFY, "--text", f"📈 {ticker} 分析完成（结论解析失败，请查看会话）"]

    # detach 后台发送，hook 立即返回；凭证未配置时 notify 自身会处理并退出
    try:
        subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        _record_sent(session_id, marker)
    except OSError:
        return


if __name__ == "__main__":
    try:
        main()
    finally:
        sys.exit(0)  # 永不以非零退出影响会话
