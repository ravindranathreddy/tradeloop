from datetime import date

from tradeloop.chain_recorder import chain_rows
from tradeloop.core.ids import InstrumentId

# Shape of Groww's get_option_chain response (27 Oct 2026, trimmed)
CHAIN = {"underlying_ltp": 22691.8, "strikes": {
    "22700": {
        "CE": {"greeks": {"delta": 0.4819, "gamma": 0.0019, "theta": -80.5, "vega": 2.3755, "rho": 0.0749,
                          "iv": 36.0592},
               "trading_symbol": "NIFTY26OCT22700CE", "ltp": 82.25, "open_interest": 290144, "volume": 1107211},
        "PE": {"greeks": {}, "trading_symbol": "NIFTY26OCT22700PE", "ltp": 91.35},
    },
    "22750": {"CE": None},
}}


def test_chain_rows_use_neutral_ids_and_keep_missing_fields_empty():
    rows = chain_rows(InstrumentId.index("NSE", "NIFTY"), date(2026, 10, 27), CHAIN, 123)
    assert len(rows) == 2
    ce = next(r for r in rows if r[0].endswith("CE"))
    assert ce == ("NSE|NIFTY|2026-10-27|22700|CE", 123, 22691.8, 82.25, 1107211, 290144,
                  36.0592, 0.4819, 0.0019, -80.5, 2.3755, 0.0749)
    pe = next(r for r in rows if r[0].endswith("PE"))
    assert pe[3] == 91.35 and pe[4:] == (None,) * 8
