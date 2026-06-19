#!/usr/bin/env python3
"""Stop hook 的 Telegram 消息抽取/格式化测试（_format_message）。不触网。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import stop_notify_hook as H  # noqa: E402

SAMPLE = """开场白。

**结论**

- 标的: `AVGO`（博通）
- 是否值得买: **观察**
- 建议: **观察等待**（不追高）
- 纪律评分: **72/100**（composite 72.4）
- 强制排除条件: **无**
- 一句话: 动量强但日线回撤，封顶观察。

**K 线走势**

```
AVGO 日K ┤█▀▄
```

**关键证据**

- 日线趋势：收 411 < MA20，跌破 MA5<MA10
- 相对强度：3 月 +18% 领先 SPY

**风控过滤条件**

| 过滤条件 | 状态 |
|---|---|
| MA60 | 满足 |
"""


class TestFormat(unittest.TestCase):
    def setUp(self):
        self.msg = H._format_message(SAMPLE)

    def test_has_ticker_header(self):
        self.assertIn("📈 <b>AVGO</b> 分析结论", self.msg)

    def test_keeps_conclusion_fields(self):
        for f in ("是否值得买: 观察", "建议: 观察等待", "纪律评分: 72/100", "一句话:"):
            self.assertIn(f, self.msg)

    def test_keeps_evidence_bullets(self):
        self.assertIn("• 日线趋势", self.msg)
        self.assertIn("• 相对强度", self.msg)

    def test_drops_chart_table_and_extras(self):
        # K 线 ASCII、代码围栏、表格、标的/强制排除行都不应出现
        for junk in ("█", "```", "|", "日K", "标的", "强制排除"):
            self.assertNotIn(junk, self.msg)

    def test_html_escapes_angle_brackets(self):
        self.assertIn("&lt;", self.msg)        # 411 < MA20 -> 411 &lt; MA20
        self.assertNotIn("< MA20", self.msg)

    def test_only_bold_tags_used(self):
        # 只用 Telegram 支持的 <b>，不残留 Markdown 星号
        self.assertNotIn("**", self.msg)
        self.assertIn("<b>结论</b>", self.msg)


class TestEdge(unittest.TestCase):
    def test_no_sections_returns_none(self):
        self.assertIsNone(H._format_message("就是一段普通聊天，没有结论也没有证据。"))

    def test_ticker_fallback_when_no_label(self):
        msg = H._format_message(
            "**结论**\n\n- 是否值得买: 否\n\n**关键证据**\n\n- NVDA 跌破 MA60")
        self.assertIn("NVDA", msg)


if __name__ == "__main__":
    unittest.main()
