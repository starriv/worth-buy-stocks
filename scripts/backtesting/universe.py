"""Frozen cross-sectional research universe, separate from single-symbol APIs."""
from __future__ import annotations


SECTOR = {
    "AAPL": "tech", "MSFT": "tech", "NVDA": "tech", "AVGO": "tech", "AMD": "tech",
    "ADBE": "tech", "CRM": "tech", "ORCL": "tech", "CSCO": "tech", "QCOM": "tech",
    "TXN": "tech", "INTC": "tech", "AMAT": "tech", "MU": "tech", "NOW": "tech",
    "INTU": "tech", "IBM": "tech", "ACN": "tech", "LRCX": "tech", "KLAC": "tech",
    "GOOGL": "comm", "META": "comm", "NFLX": "comm", "DIS": "comm", "CMCSA": "comm",
    "TMUS": "comm", "VZ": "comm", "T": "comm", "EA": "comm", "TTWO": "comm",
    "AMZN": "cons_disc", "TSLA": "cons_disc", "HD": "cons_disc", "MCD": "cons_disc",
    "NKE": "cons_disc", "SBUX": "cons_disc", "LOW": "cons_disc", "TJX": "cons_disc",
    "BKNG": "cons_disc", "MAR": "cons_disc", "GM": "cons_disc", "F": "cons_disc",
    "ORLY": "cons_disc", "CMG": "cons_disc",
    "WMT": "cons_staples", "COST": "cons_staples", "PG": "cons_staples", "KO": "cons_staples",
    "PEP": "cons_staples", "PM": "cons_staples", "MO": "cons_staples", "MDLZ": "cons_staples",
    "CL": "cons_staples", "KMB": "cons_staples",
    "UNH": "health", "JNJ": "health", "LLY": "health", "ABBV": "health", "MRK": "health",
    "PFE": "health", "TMO": "health", "ABT": "health", "DHR": "health", "BMY": "health",
    "AMGN": "health", "GILD": "health", "ISRG": "health", "VRTX": "health",
    "REGN": "health", "CVS": "health",
    "JPM": "fin", "BAC": "fin", "WFC": "fin", "GS": "fin", "MS": "fin", "C": "fin",
    "BLK": "fin", "SCHW": "fin", "AXP": "fin", "V": "fin", "MA": "fin", "SPGI": "fin",
    "CME": "fin", "ICE": "fin", "COF": "fin", "PGR": "fin",
    "CAT": "industrials", "BA": "industrials", "GE": "industrials", "HON": "industrials",
    "UPS": "industrials", "RTX": "industrials", "LMT": "industrials", "DE": "industrials",
    "UNP": "industrials", "CSX": "industrials", "FDX": "industrials", "ETN": "industrials",
    "EMR": "industrials", "ITW": "industrials",
    "XOM": "energy", "CVX": "energy", "COP": "energy", "SLB": "energy", "EOG": "energy",
    "MPC": "energy", "PSX": "energy", "VLO": "energy", "OXY": "energy", "KMI": "energy",
    "LIN": "materials", "APD": "materials", "SHW": "materials", "FCX": "materials",
    "NEM": "materials", "ECL": "materials", "NUE": "materials", "DOW": "materials",
    "NEE": "utilities", "DUK": "utilities", "SO": "utilities", "D": "utilities",
    "AEP": "utilities", "EXC": "utilities", "SRE": "utilities",
    "PLD": "real_estate", "AMT": "real_estate", "EQIX": "real_estate", "CCI": "real_estate",
    "SPG": "real_estate", "O": "real_estate", "PSA": "real_estate",
}

ALIVE = list(SECTOR)
DELISTED = ["SIVB", "FRC", "ATVI", "TWTR", "PXD", "SPLK", "VMW", "ABMD"]
BANKRUPT = {"SIVB", "FRC"}
SECTOR.update({
    "SIVB": "fin", "FRC": "fin", "ATVI": "comm", "TWTR": "comm",
    "PXD": "energy", "SPLK": "tech", "VMW": "tech", "ABMD": "health",
})
UNIVERSE = ALIVE + DELISTED
BENCH = ["SPY", "QQQ"]
# Matches contracts.WARMUP_SESSIONS: 12-1 momentum needs 253 closes before
# the first rebalance, otherwise early dates score with a different factor mix.
WARMUP = 260
STEP = 21
HORIZONS = [21, 63]
HISTORY_DAYS = 6 * 365
