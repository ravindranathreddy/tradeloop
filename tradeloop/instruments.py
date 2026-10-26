"""Pick which option contracts to record: nearest expiry, strikes around ATM.

Expired contracts disappear from Groww's instrument CSV, so the recorder
also saves the day's selection to the database (see store.save_session).
"""
from datetime import date

import pandas as pd

INSTRUMENT_CSV_URL = "https://growwapi-assets.groww.in/instruments/instrument.csv"


def load_instruments(source: str = INSTRUMENT_CSV_URL) -> pd.DataFrame:
    df = pd.read_csv(
        source,
        dtype={"exchange_token": str, "underlying_exchange_token": str},
        low_memory=False,
    )
    df["expiry_date"] = pd.to_datetime(df["expiry_date"], errors="coerce").dt.date
    return df


def option_rows(df: pd.DataFrame, exchange: str, underlying: str) -> pd.DataFrame:
    mask = (
        (df["exchange"] == exchange)
        & (df["segment"] == "FNO")
        & (df["underlying_symbol"] == underlying)
        & (df["instrument_type"].isin(["CE", "PE"]))
    )
    return df[mask]


def nearest_expiry(options: pd.DataFrame, today: date) -> date | None:
    future = options.loc[options["expiry_date"] >= today, "expiry_date"]
    return None if future.empty else min(future)


def strike_step(chain: pd.DataFrame) -> float:
    strikes = sorted(chain["strike_price"].unique())
    gaps = pd.Series(strikes).diff().dropna()
    return float(gaps.mode().iloc[0]) if not gaps.empty else 0.0


def select_strikes(chain: pd.DataFrame, spot: float, each_side: int) -> pd.DataFrame:
    """CE and PE rows for the ATM strike and `each_side` strikes above and below."""
    step = strike_step(chain)
    if step <= 0:
        return chain
    atm = round(spot / step) * step
    lo, hi = atm - each_side * step, atm + each_side * step
    picked = chain[(chain["strike_price"] >= lo) & (chain["strike_price"] <= hi)]
    return picked.sort_values(["strike_price", "instrument_type"])


def pick_contracts(df: pd.DataFrame, exchange: str, underlying: str,
                   spot: float, each_side: int, today: date) -> pd.DataFrame:
    options = option_rows(df, exchange, underlying)
    expiry = nearest_expiry(options, today)
    if expiry is None:
        return options.iloc[0:0]
    chain = options[options["expiry_date"] == expiry]
    return select_strikes(chain, spot, each_side)
