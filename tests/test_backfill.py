from datetime import date, datetime, time, timedelta

import pandas as pd
import pytest

from tradeloop.backfill import IST, add_wings, backfill, clean, read_manifest, strikes_to_fetch, week_window
from tradeloop.brokers.groww import GrowwHistory, candle_rows, derivative_from_groww_symbol, groww_symbol
from tradeloop.core.ids import InstrumentId, Kind

NIFTY = InstrumentId.index("NSE", "NIFTY")
EXP = date(2025, 6, 26)
GHOST = date(2025, 7, 4)


def opt(strike, kind, expiry=EXP):
    return InstrumentId.option("NSE", "NIFTY", expiry, strike, kind)


class FakeHistory:
    """Index drifts 25000 -> 25200 over the week; only GHOST's options never trade."""

    def __init__(self, fail_on=None):
        self.calls, self.fail_on = [], fail_on

    def expiries(self, index, year):
        return [EXP, GHOST, date(2025, 7, 10)] if year == 2025 else []

    def contracts(self, index, expiry):
        """Like Groww's: incomplete. 25100 CE exists but isn't listed."""
        fut = [InstrumentId("NSE", "NIFTY", Kind.FUT, expiry)] if expiry == EXP else []
        listed = [opt(k, t, expiry) for k in range(24000, 26050, 50) for t in ("CE", "PE")]
        return [o for o in listed if o != opt(25100, "CE", expiry)] + fut

    def candles(self, iid, start, end, interval="1minute"):
        self.calls.append(iid)
        if self.fail_on and iid == self.fail_on:
            raise RuntimeError("boom")
        t = datetime.combine(end.date(), time(9, 15), IST)
        if interval == "1day":
            return [(str(iid), int(datetime.combine(end.date(), time(0), IST).timestamp() * 1000),
                     25150, 25210, 25140, 25190.0, None, None)]
        if iid.kind is Kind.IDX:
            return [(str(iid), int((t - timedelta(days=1)).timestamp() * 1000), 25000, 25010, 24990, 25000, None, None),
                    (str(iid), int(t.timestamp() * 1000), 25150, 25210, 25140, 25200, None, None)]
        volume = 0 if iid.expiry == GHOST else 650
        return [(str(iid), int(t.timestamp() * 1000), 10, 11, 9, 10.5, volume, 1000)]


# --- pure pieces -----------------------------------------------------------------

def test_strikes_cover_the_week_range_plus_extra():
    picked = strikes_to_fetch(NIFTY, EXP, low=24990, high=25210, extra=2, step=50)
    assert sorted({o.strike for o in picked}) == [24900, 24950, 25000, 25050, 25100, 25150, 25200, 25250, 25300]
    assert len(picked) == 18


def test_week_window():
    start, end = week_window(EXP)
    assert (start.date(), start.time(), end.date(), end.time()) == (date(2025, 6, 20), time(9, 0), EXP, time(15, 30))
    assert week_window(date(2026, 10, 20))[1].time() == time(15, 40)       # F&O close moved


def test_groww_symbols_round_trip():
    for s in ("NSE-NIFTY-26Jun25-25000-CE", "BSE-SENSEX-03Jan25-79300-PE", "NSE-NIFTY-02Jan20-12100-PE",
              "NSE-NIFTY-26Jun25-FUT"):
        assert groww_symbol(derivative_from_groww_symbol(s)) == s
    assert str(derivative_from_groww_symbol("BSE-SENSEX-03Jan25-79300-PE")) == "BSE|SENSEX|2025-01-03|79300|PE"
    assert str(derivative_from_groww_symbol("NSE-NIFTY-26Jun25-FUT")) == "NSE|NIFTY|2025-06-26|FUT"
    assert groww_symbol(NIFTY) == "NSE-NIFTY"


def test_candle_rows_are_ist_and_keep_missing_oi():
    rows = candle_rows(opt(25000, "CE"), [["2025-06-26T09:15:00", 290.0, 302.55, 278.3, 299.05, 297675, 108369],
                                          ["2025-06-26T09:16:00", 298.75, 307.6, 297.1, 305.2, 196350, None]])
    assert rows[0][1] == int(datetime(2025, 6, 26, 9, 15, tzinfo=IST).timestamp() * 1000)
    assert rows[0][0] == "NSE|NIFTY|2025-06-26|25000|CE" and rows[1][7] is None


def test_clean_keeps_the_fullest_candle_per_minute_and_drops_zero_prices():
    i = "NSE|NIFTY|2025-11-04|25000|PE"
    df = pd.DataFrame([
        (i, 1, 2.0, 2.0, 2.0, 2.0, 1875.0, None),          # partial copy of minute 1
        (i, 1, 2.0, 2.05, 1.95, 2.05, 217800.0, 9.0),
        (i, 1, 2.0, 2.05, 1.95, 2.05, 217800.0, 9.0),       # exact copy
        (i, 2, 1.7, 1.7, 1.7, 1.7, 100.0, None),
        (i, 3, 0.0, 0.0, 0.0, 0.0, 0.0, None),              # not a trade
        ("NSE|NIFTY|IDX", 1, 25000.0, 25010.0, 24990.0, 25005.0, None, None),
        ("NSE|NIFTY|IDX", 1, 25000.0, 25010.0, 24990.0, 25005.0, None, None),
    ], columns=["instrument", "ts_ms", "open", "high", "low", "close", "volume", "oi"])
    out = clean(df)
    assert len(out) == 3 and not out.duplicated(["instrument", "ts_ms"]).any()
    assert out.loc[(out["instrument"] == i) & (out["ts_ms"] == 1), "volume"].item() == 217800.0
    assert (out["close"] > 0).all()


# --- backfill ---------------------------------------------------------------------

def test_backfill_writes_files_marks_ghosts_and_skips_future(tmp_path):
    h = FakeHistory()
    m = backfill(h, [NIFTY], date(2025, 1, 1), date(2025, 12, 31), tmp_path, extra=2, today=date(2025, 7, 8))
    assert {k[1]: r["status"] for k, r in m.items()} == {"2025-06-26": "ok", "2025-07-04": "ghost"}  # 10 Jul is future
    df = pd.read_parquet(tmp_path / "candles_1m" / "NSE_NIFTY" / "2025-06-26.parquet")
    assert list(df.columns) == ["instrument", "ts_ms", "open", "high", "low", "close", "volume", "oi"]
    assert df["instrument"].str.endswith("|IDX").sum() == 2
    assert sorted({InstrumentId.parse(i).strike for i in df["instrument"] if i.endswith(("CE", "PE"))}) == \
        [24900, 24950, 25000, 25050, 25100, 25150, 25200, 25250, 25300]
    assert "NSE|NIFTY|2025-06-26|FUT" in set(df["instrument"])
    assert "NSE|NIFTY|2025-06-26|25100|CE" in set(df["instrument"])     # unlisted, fetched from the grid
    assert "1 contracts missing from Groww's list had data" in m[("NSE|NIFTY|IDX", "2025-06-26")]["note"]
    daily = pd.read_parquet(tmp_path / "candles_1d" / "NSE_NIFTY" / "2025-06-26.parquet")
    assert daily["close"].tolist() == [25190.0]                 # official close for settlement
    assert not (tmp_path / "candles_1m" / "NSE_NIFTY" / "2025-07-04.parquet").exists()
    ghost_calls = [c for c in h.calls if c.kind.is_option and c.expiry == GHOST]
    assert len(ghost_calls) == 2                       # only the ATM call and put were tried


def test_rerun_skips_finished_expiries(tmp_path):
    backfill(FakeHistory(), [NIFTY], date(2025, 1, 1), date(2025, 12, 31), tmp_path, extra=2, today=date(2025, 7, 8))
    h = FakeHistory()
    backfill(h, [NIFTY], date(2025, 1, 1), date(2025, 12, 31), tmp_path, extra=2, today=date(2025, 7, 8))
    assert h.calls == []


def test_failure_is_recorded_and_retried(tmp_path):
    bad = opt(25200, "CE")
    m = backfill(FakeHistory(fail_on=bad), [NIFTY], date(2025, 6, 1), date(2025, 6, 30), tmp_path, extra=2,
                 today=date(2025, 7, 8))
    assert m[(str(NIFTY), "2025-06-26")]["status"] == "failed"
    assert not (tmp_path / "candles_1m" / "NSE_NIFTY" / "2025-06-26.parquet").exists()
    backfill(FakeHistory(), [NIFTY], date(2025, 6, 1), date(2025, 6, 30), tmp_path, extra=2, today=date(2025, 7, 8))
    assert read_manifest(tmp_path / "expiries.csv")[(str(NIFTY), "2025-06-26")]["status"] == "ok"


# --- throttle and rate-limit retry ----------------------------------------------------

class FakeApi:
    def __init__(self, failures):
        self.failures = failures

    def get_expiries(self, exchange, underlying, year=None, month=None, timeout=None):
        assert timeout == 30                       # never the SDK's default of no timeout
        if self.failures:
            self.failures -= 1
            raise Exception("Rate limit has breached for your request. Please try again later.")
        return {"expiries": ["2025-06-26"]}


def test_history_spaces_calls_and_backs_off_on_rate_limit():
    now, slept = [0.0], []

    def sleep(s):
        slept.append(s)
        now[0] += s

    h = GrowwHistory(FakeApi(failures=2), min_interval=0.5, sleep=sleep, clock=lambda: now[0])
    assert h.expiries(NIFTY, 2025) == [EXP]
    assert h.expiries(NIFTY, 2025) == [EXP]
    assert [s for s in slept if s >= 2] == [2.0, 4.0]          # backoff after each rate-limit error
    assert h.calls == 4


def test_history_retries_network_blips():
    import requests
    errors = [requests.ConnectionError("('Connection aborted.', ConnectionResetError(54))"),
              requests.Timeout("read timed out")]

    class Flaky:
        def get_expiries(self, *a, **k):
            if errors:
                raise errors.pop(0)
            return {"expiries": ["2025-06-26"]}

    assert GrowwHistory(Flaky(), sleep=lambda s: None).expiries(NIFTY, 2025) == [EXP]


def test_history_gives_up_on_other_errors():
    class Broken:
        def get_expiries(self, *a, **k):
            raise Exception("Access forbidden for this request.")

    with pytest.raises(Exception, match="forbidden"):
        GrowwHistory(Broken(), sleep=lambda s: None).expiries(NIFTY, 2025)


def test_wings_add_far_puts_below_and_calls_above(tmp_path):
    backfill(FakeHistory(), [NIFTY], date(2025, 1, 1), date(2025, 12, 31), tmp_path, extra=2, today=date(2025, 7, 8))
    path = tmp_path / "candles_1m" / "NSE_NIFTY" / "2025-06-26.parquet"
    before = set(pd.read_parquet(path)["instrument"])
    got = add_wings(FakeHistory(), NIFTY, EXP, tmp_path, 3)
    added = set(pd.read_parquet(path)["instrument"]) - before
    assert got == 6
    assert added == {str(opt(k, "PE")) for k in (24850, 24800, 24750)} | {str(opt(k, "CE")) for k in (25350, 25400, 25450)}
