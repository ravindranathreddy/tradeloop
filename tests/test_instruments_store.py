from datetime import date

import pandas as pd

from tradeloop.instruments import pick_contracts, strike_step
from tradeloop.store import Store


def fake_instruments():
    rows = []
    for expiry in ("2026-10-27", "2026-11-03"):
        for strike in range(25000, 26050, 50):
            for kind in ("CE", "PE"):
                rows.append({
                    "exchange": "NSE", "exchange_token": f"{expiry[-2:]}{strike}{kind}",
                    "trading_symbol": f"NIFTY{strike}{kind}", "segment": "FNO",
                    "instrument_type": kind, "underlying_symbol": "NIFTY",
                    "expiry_date": date.fromisoformat(expiry), "strike_price": float(strike),
                    "lot_size": 75,
                })
    rows.append({"exchange": "NSE", "exchange_token": "2885", "trading_symbol": "RELIANCE",
                 "segment": "CASH", "instrument_type": "EQ", "underlying_symbol": None,
                 "expiry_date": None, "strike_price": None, "lot_size": 1})
    return pd.DataFrame(rows)


def test_strike_step():
    df = fake_instruments()
    assert strike_step(df[df["segment"] == "FNO"]) == 50


def test_pick_nearest_expiry_around_atm():
    picked = pick_contracts(fake_instruments(), "NSE", "NIFTY", spot=25512, each_side=2,
                            today=date(2026, 10, 26))
    assert set(picked["expiry_date"]) == {date(2026, 10, 27)}
    assert sorted(picked["strike_price"].unique()) == [25400, 25450, 25500, 25550, 25600]
    assert len(picked) == 10  # CE + PE per strike


def test_expiry_day_still_picks_today():
    picked = pick_contracts(fake_instruments(), "NSE", "NIFTY", 25500, 1, today=date(2026, 10, 27))
    assert set(picked["expiry_date"]) == {date(2026, 10, 27)}


def test_store_dedupes_and_saves_session(tmp_path):
    s = Store(tmp_path / "t.db")
    row = (1, 2, "NSE", "FNO", "35241", 100.0)
    s.write_ltp([row, row])
    s.write_ltp([row])
    assert s.count("ltp") == 1
    picked = pick_contracts(fake_instruments(), "NSE", "NIFTY", 25500, 1, date(2026, 10, 26))
    s.save_session("2026-10-26", picked)
    assert s.count("session_contracts") == 6
