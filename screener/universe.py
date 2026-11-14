"""Load the user's ticker list and map symbols to each provider's convention."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
UNIVERSE_XLSX = ROOT / "data" / "All_stocks_2026-11-07_clean.xlsx"

# Prompt universe: NYSE, NASDAQ, NYSE-MKT (NYSE American), LSE.
# The list (a TradingView export) labels NYSE American / Arca-listed common
# stocks "NYSE Arca" and Cboe Global Markets itself "CBOE".
US_EXCHANGES = {"NYSE", "NASDAQ", "NYSE Arca", "CBOE"}
UK_EXCHANGES = {"LSE"}

# TradingView exchange prefix for each list label.
TV_PREFIX = {"NYSE": "NYSE", "NASDAQ": "NASDAQ", "NYSE Arca": "AMEX", "CBOE": "CBOE", "LSE": "LSE"}


def load_universe() -> pd.DataFrame:
    df = pd.read_excel(UNIVERSE_XLSX, dtype=str)
    df.columns = ["symbol", "name", "exchange"]
    df = df.dropna(subset=["symbol"])
    df["symbol"] = df["symbol"].str.strip()
    df = df[df["exchange"].isin(US_EXCHANGES | UK_EXCHANGES)].copy()
    df["region"] = df["exchange"].map(lambda e: "UK" if e in UK_EXCHANGES else "US")
    df["tv_id"] = df["exchange"].map(TV_PREFIX) + ":" + df["symbol"]
    df["yahoo"] = [yahoo_symbol(s, r) for s, r in zip(df["symbol"], df["region"])]
    return df.reset_index(drop=True)


def yahoo_symbol(symbol: str, region: str) -> str:
    """BRK.B -> BRK-B ; LSE 'BT.A' -> 'BT-A.L' ; LSE 'RR.' -> 'RR.L'."""
    s = symbol.strip()
    if region == "UK":
        s = s.rstrip(".")
        return s.replace(".", "-") + ".L"
    return s.replace(".", "-").replace("/", "-")


def plain_us_symbol(symbol: str) -> str:
    """Class-share symbol as used by Zacks/Nasdaq/stockanalysis (BRK.B)."""
    return symbol.strip().replace("/", ".")
