import subprocess
from datetime import date, datetime, time, timedelta

import pandas as pd
import pytest

from tradeloop.backtest import config_version, run_week, summary, trading_days
from tradeloop.core.ids import InstrumentId
from tradeloop.core.market_calendar import IST
from tradeloop.core.types import OrderStatus, OrderType, Product, Order, Side
from tradeloop.fill_model import FillModel
from tradeloop.sim_broker import SimBroker

EXP = date(2026, 10, 20)                     # NIFTY lot 65, F&O close 15:40
NIFTY = InstrumentId.index("NSE", "NIFTY")
CE = f"NSE|NIFTY|{EXP}|25000|CE"
PE = f"NSE|NIFTY|{EXP}|25000|PE"
FAR = f"NSE|NIFTY|{EXP}|25100|CE"            # only quoted as a call: never ATM for a straddle


MON = date(2026, 10, 19)
PREV = date(2026, 10, 13)                    # the previous weekly expiry


def ms(hh, mm, d=EXP):
    return int(datetime.combine(d, time(hh, mm), IST).timestamp() * 1000)


def expiry_candles(ce=lambda t: 100.0, pe=lambda t: 80.0, index=lambda t: 25010.0, days=(EXP,)):
    """Every minute 09:15-15:39 of each day: flat candles at the given prices, 6500 traded."""
    rows = []
    for d in days:
        for m in range(9 * 60 + 15, 15 * 60 + 40):
            ts = ms(m // 60, m % 60, d)
            rows.append(("NSE|NIFTY|IDX", ts, index(m), index(m), index(m), index(m), None, None))
            for inst, px in ((CE, ce(m)), (PE, pe(m)), (FAR, 60.0)):
                rows.append((inst, ts, px, px, px, px, 6500.0, 1000.0))
    return pd.DataFrame(rows, columns=["instrument", "ts_ms", "open", "high", "low", "close", "volume", "oi"])


CFG = {"strategy_id": "t", "strategy": "timed_straddle", "side": "BUY", "lots": 1,
       "entry_time": "09:20", "exit_time": "15:00"}
NO_SLIP = FillModel(slippage_ticks=0, slippage_pct=0)


def test_straddle_enters_at_0920_exits_at_1500_and_books_pnl():
    ce = lambda m: 100.0 if m < 15 * 60 else 130.0          # call rallies before the exit
    r = run_week(CFG, "v1", NIFTY, EXP, expiry_candles(ce=ce), 25010.0, NO_SLIP, PREV)[-1]
    assert r["fills"] == 4 and r["settled_at_expiry"] == 0
    assert r["gross"] == (130 - 100) * 65                   # put flat at 80
    assert r["net"] == pytest.approx(r["gross"] - r["charges"])
    # brokerage 80 + STT 13 + 8 (rupee-rounded) + exchange 8.88 + SEBI 0.04 + stamp 0.36 + GST 16.00
    assert r["charges"] == 126.28


def test_positions_left_open_settle_at_the_official_close():
    cfg = {**CFG, "exit_time": "23:00"}                     # never exits
    [r] = run_week(cfg, "v1", NIFTY, EXP, expiry_candles(), 25060.0, NO_SLIP, PREV)
    assert r["settled_at_expiry"] == 2
    # call worth 60 (25060 - 25000), put expires worthless; paid 100 + 80
    assert r["gross"] == (60 - 100 - 80) * 65


def test_entry_uses_the_first_candle_after_the_decision():
    seen = []
    ce = lambda m: 100.0 + (m - (9 * 60 + 15))              # call price rises 1 a minute: 09:20 open = 105
    import tradeloop.engine as engine
    orig = engine.Engine._on_fill
    engine.Engine._on_fill = lambda self, f: (seen.append(f), orig(self, f))
    try:
        run_week(CFG, "v1", NIFTY, EXP, expiry_candles(ce=ce), 25010.0, NO_SLIP, PREV)
    finally:
        engine.Engine._on_fill = orig
    entry = next(f for f in seen if str(f.instrument) == CE and f.side is Side.BUY)
    assert (entry.ts_ms, entry.price) == (ms(9, 20), 105.0)  # decided at 09:20 after the 09:19 close
    assert all(f.instrument != InstrumentId.parse(FAR) for f in seen)


def test_sim_broker_is_idempotent_and_reports_updates():
    b = SimBroker(NO_SLIP, lambda i: 65)
    updates = []
    b.on_order_update(updates.append)
    o = Order(InstrumentId.parse(CE), Side.BUY, 65, OrderType.MARKET, Product.INTRADAY, "t", "v1", client_ref="x")
    b.end_of_minute(ms(9, 20))
    b.place(o)
    b.place(o)                                              # retry with the same client_ref
    candle = type("C", (), dict(ts_ms=ms(9, 20), open=100.0, high=100.0, low=100.0, close=100.0, volume=6500.0))
    b.advance(ms(9, 20), {o.instrument: candle})
    assert [u.status for u in updates] == [OrderStatus.OPEN, OrderStatus.FILLED]
    assert b.positions()[0].qty == 65


def test_config_version_is_the_git_blob_hash(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("strategy_id: x\n")
    git = subprocess.run(["git", "hash-object", str(p)], capture_output=True, text=True)
    if git.returncode:
        pytest.skip("git not available")
    assert config_version(p) == git.stdout.strip()[:12]


def test_every_trading_day_of_the_week_is_replayed():
    c = expiry_candles(days=(MON, EXP))
    rows = run_week(CFG, "v1", NIFTY, EXP, c, 25010.0, NO_SLIP, PREV)
    assert [(r["day"], r["days_to_expiry"], r["fills"]) for r in rows] == [("2026-10-19", 1, 4), ("2026-10-20", 0, 4)]
    only_expiry = run_week({**CFG, "days": "expiry"}, "v1", NIFTY, EXP, c, 25010.0, NO_SLIP, PREV)
    assert [r["fills"] for r in only_expiry] == [0, 4]


def test_positions_can_be_held_overnight_into_expiry():
    c = expiry_candles(days=(MON, EXP))
    mon, tue = run_week({**CFG, "exit_time": "23:00"}, "v1", NIFTY, EXP, c, 25060.0, NO_SLIP, PREV)
    assert (mon["held_overnight"], mon["gross"]) == (2, 0)            # nothing realised on Monday
    # Monday's and Tuesday's straddles both settle: call worth 60, put 0, paid 180 each time
    assert (tue["settled_at_expiry"], tue["gross"]) == (2, 2 * (60 - 180) * 65)


def test_a_day_belongs_to_the_nearest_expiry_only():
    c = expiry_candles(days=(date(2026, 10, 13), MON, EXP))          # 13th was the previous expiry
    assert trading_days(c, PREV, EXP) == [MON, EXP]


def test_summary_reports_drawdown_and_days_to_expiry():
    report = pd.DataFrame({"index": ["X"] * 3, "expiry": ["2025-01-02"] * 3, "day": ["2024-12-31", "2025-01-01", "2025-01-02"],
                           "days_to_expiry": [2, 1, 0], "fills": [4, 4, 4], "settled_at_expiry": [0, 0, 0],
                           "gross": [0, 0, 0], "charges": [0, 0, 0], "net": [100.0, -300.0, 50.0]})
    text = summary(report)
    assert "max drawdown -300" in text and "0d 50" in text
