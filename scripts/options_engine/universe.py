"""Conservative reviewed universe, not a live option-liquidity measurement.

Core equities match the high-activity single-stock group described by Cboe:
https://www.cboe.com/insights/posts/state-of-the-options-industry-options-market-continued-to-break-records-in-q-2-2026
GOOG is an alternative Alphabet share class, not additional diversification.
Broad-index ETFs remain explicit exceptions to the large-company requirement.
Review membership quarterly; never expand from stock volume or has_options alone.
"""
REVIEWED_ON = "2026-10-02"
CORE_EQUITIES = frozenset({
    "AAPL", "AMZN", "AVGO", "GOOG", "GOOGL", "META", "MSFT", "NVDA", "TSLA",
})
INDEX_ETFS = frozenset({"SPY", "QQQ", "DIA", "IWM"})
APPROVED_UNIVERSE = CORE_EQUITIES | INDEX_ETFS


def restrict_universe(symbols):
    """Return approved symbols and exclusions, preserving input order."""
    return ([s for s in symbols if s in APPROVED_UNIVERSE],
            [s for s in symbols if s not in APPROVED_UNIVERSE])
