"""Backtest a strategy config on downloaded expiries (data/history).

    python -m tradeloop.backtest strategies/example_straddle.yaml [--from 2025-01-01] [--to ...]

For each expiry (status ok in expiries.csv, not excluded in data_quality.yaml), replays
every trading day on which it was the nearest expiry (the day after the previous expiry
up to expiry day), minute by minute from 09:15 to the F&O close: working orders are
matched against each minute's candle (SimBroker + FillModel), then the strategy sees
that minute's close, IV and Greeks and may place orders, which can fill from the next
minute. Unfilled orders lapse at each day's close; positions may be held overnight;
anything open at the expiry close settles at the official close. Each expiry week gets
a fresh strategy, ledger and broker.

Writes one row per trading day to data/backtests/<strategy_id>_<run time>.csv and
prints a summary. Money is rupees, net of all charges unless labelled gross; a day's
P&L is what was realised that day (overnight positions count when closed).
"""
from __future__ import annotations

import argparse
import hashlib
import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from tradeloop.core.ids import InstrumentId, Kind
from tradeloop.core.market_calendar import IST, default_calendar
from tradeloop.engine import Engine, Quote
from tradeloop.fill_model import FillModel
from tradeloop.ledger import Ledger
from tradeloop.lot_sizes import default_lot_sizes
from tradeloop.sim_broker import SimBroker
from tradeloop.strategies import REGISTRY

log = logging.getLogger("backtest")
REPO = Path(__file__).resolve().parents[1]
MINUTE_MS = 60_000


def config_version(path: Path) -> str:
    """The file's git blob hash: the same id `git hash-object` gives (design rule 8)."""
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()[:12]


def exclusions(path: Path = REPO / "data_quality.yaml") -> set[tuple[str, str]]:
    raw = yaml.safe_load(path.read_text()) or {}
    return {(index, str(d)) for index, days in (raw.get("exclude") or {}).items() for d in days}


def load_expiry(history: Path, index: InstrumentId, expiry: date) -> tuple[pd.DataFrame, float | None]:
    """1-minute candles joined with IV/Greeks, and the official close (daily candle)."""
    name, day = f"{index.exchange}_{index.underlying}", f"{expiry.isoformat()}.parquet"
    candles = pd.read_parquet(history / "candles_1m" / name / day)
    greeks_path = history / "greeks_1m" / name / day
    if greeks_path.exists():
        g = pd.read_parquet(greeks_path, columns=["instrument", "ts_ms", "iv", "delta", "gamma", "vega", "theta"])
        candles = candles.merge(g, on=["instrument", "ts_ms"], how="left")
    daily = pd.read_parquet(history / "candles_1d" / name / day)
    dd = pd.to_datetime(daily["ts_ms"], unit="ms", utc=True).dt.tz_convert(IST).dt.date
    official = daily.loc[dd == expiry, "close"]
    return candles, (float(official.iloc[0]) if len(official) else None)


def _num(v) -> float | None:
    return None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)


def trading_days(candles: pd.DataFrame, after: date, expiry: date) -> list[date]:
    """IST days with option candles in (after, expiry]: the days this expiry was the nearest one."""
    opts = candles[candles["instrument"].str.endswith(("CE", "PE"))]
    days = pd.to_datetime(opts["ts_ms"], unit="ms", utc=True).dt.tz_convert(IST).dt.date.unique()
    return sorted(d for d in days if after < d <= expiry)


def _session_ms(d: date) -> tuple[int, int]:
    at = lambda t: int(datetime.combine(d, t, IST).timestamp() * 1000)
    return at(time(9, 15)), at(default_calendar().close_time(d))


def run_week(cfg: dict, version: str, index: InstrumentId, expiry: date, candles: pd.DataFrame,
             official_close: float, fill_model: FillModel, previous_expiry: date) -> list[dict]:
    """Replay each trading day from the day after `previous_expiry` to `expiry`. One strategy,
    ledger and broker for the week; positions may be held overnight; unfilled orders lapse
    at each day's close; whatever is open at the expiry close settles. One row per day."""
    lots = default_lot_sizes()
    ledger = Ledger()
    broker = SimBroker(fill_model, lots.lot_size)
    strategy = REGISTRY[cfg["strategy"]](cfg)
    engine = Engine(strategy, broker, ledger, strategy_id=cfg["strategy_id"], config_version=version,
                    lot_size=lots.lot_size, working_orders=broker.working)
    ids = {s: InstrumentId.parse(s) for s in candles["instrument"].unique()}

    out = []
    for d in trading_days(candles, previous_expiry, expiry):
        before = (len(engine.fills), ledger.realised_pnl(), ledger.total_charges())
        open_ms, close_ms = _session_ms(d)
        day = candles[(candles["ts_ms"] >= open_ms) & (candles["ts_ms"] < close_ms)]
        quotes: dict[InstrumentId, Quote] = {}             # fresh each day: no stale quotes overnight
        index_price = None
        for ts, rows in day.groupby("ts_ms", sort=True):
            minute = {ids[r.instrument]: r for r in rows.itertuples(index=False)}
            broker.advance(int(ts), minute)                   # orders from earlier minutes fill first
            for iid, r in minute.items():
                if iid.kind is Kind.IDX:
                    index_price = float(r.close)
                else:
                    quotes[iid] = Quote(int(r.ts_ms), float(r.close), _num(r.volume), _num(r.oi),
                                        *(_num(getattr(r, k, None)) for k in ("iv", "delta", "gamma", "vega", "theta")))
            if index_price is None:
                continue
            broker.end_of_minute(int(ts) + MINUTE_MS)         # decisions now fill from the next candle
            engine.on_minute(int(ts) + MINUTE_MS, index, expiry, index_price, quotes)
        broker.cancel_all()                                   # day orders lapse at the close

        settled = 0
        if d == expiry:
            settled = len(ledger.positions())
            ledger.settle_expiry(expiry, index.underlying, official_close, close_ms)
        gross = (ledger.realised_pnl() - before[1]) / 100
        charges = (ledger.total_charges() - before[2]) / 100
        out.append({"index": str(index), "expiry": expiry.isoformat(), "day": d.isoformat(),
                    "days_to_expiry": (expiry - d).days, "fills": len(engine.fills) - before[0],
                    "held_overnight": 0 if d == expiry else len(ledger.positions()),
                    "settled_at_expiry": settled, "gross": gross, "charges": charges,
                    "net": round(gross - charges, 2)})
    return out


def run(cfg_path: Path, history: Path, start: date, end: date, fill_model: FillModel | None = None) -> pd.DataFrame:
    cfg = yaml.safe_load(cfg_path.read_text())
    version = config_version(cfg_path)
    fill_model = fill_model or FillModel()
    skip = exclusions()
    manifest = pd.read_csv(history / "expiries.csv")
    rows = []
    for index_s in cfg["underlyings"]:
        index = InstrumentId.parse(index_s)
        mine = manifest[manifest["index"] == index_s]
        # every expiry that really traded, so each day goes to the expiry nearest to it
        real = sorted(date.fromisoformat(e) for e in mine.loc[mine["status"].isin(["ok", "failed"]), "expiry"])
        for exp_s in sorted(mine.loc[mine["status"] == "ok", "expiry"]):
            expiry = date.fromisoformat(exp_s)
            if not (start <= expiry <= end) or (index_s, exp_s) in skip:
                continue
            candles, official = load_expiry(history, index, expiry)
            if official is None:
                log.warning("%s %s: no official close, skipped", index_s, exp_s)
                continue
            earlier = [e for e in real if e < expiry]
            previous = earlier[-1] if earlier else expiry - timedelta(days=7)
            rows += run_week(cfg, version, index, expiry, candles, official, fill_model, previous)
    out = pd.DataFrame(rows)
    out.attrs.update(strategy_id=cfg["strategy_id"], config_version=version)
    return out


def summary(report: pd.DataFrame) -> str:
    lines = []
    for name, g in report.groupby("index"):
        g = g.sort_values("day")
        cum = g["net"].cumsum()
        drawdown = (cum - cum.cummax()).min()
        traded = g[(g["fills"] > 0) | (g["settled_at_expiry"] > 0)]
        lines.append(f"{name}: {len(g)} trading days ({g['day'].min()} .. {g['day'].max()}, "
                     f"{g['expiry'].nunique()} expiries), traded on {len(traded)}")
        lines.append(f"  net Rs {g['net'].sum():,.0f} (gross {g['gross'].sum():,.0f}, charges {g['charges'].sum():,.0f})"
                     f"; per traded day avg {traded['net'].mean():,.0f}, win rate {(traded['net'] > 0).mean():.0%}")
        lines.append(f"  best day {g['net'].max():,.0f}, worst day {g['net'].min():,.0f}, max drawdown {drawdown:,.0f}")
        by_dte = traded.groupby("days_to_expiry")["net"].agg(["count", "sum", "mean"])
        lines.append("  by days to expiry: " + ", ".join(f"{d}d {s:,.0f} ({c:.0f} days, avg {m:,.0f})"
                                                       for d, (c, s, m) in by_dte.iterrows()))
        by_year = g.assign(year=g["day"].str[:4]).groupby("year")["net"].agg(["count", "sum"])
        lines.append("  by year: " + ", ".join(f"{y} {s:,.0f}" for y, (c, s) in by_year.iterrows()))
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("config")
    p.add_argument("--history", default="data/history")
    p.add_argument("--from", dest="start", default="2020-01-01")
    p.add_argument("--to", dest="end", default=datetime.now(IST).date().isoformat())
    p.add_argument("--out", default="data/backtests")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    report = run(Path(a.config), Path(a.history), date.fromisoformat(a.start), date.fromisoformat(a.end))
    if report.empty:
        print("No expiries in range")
        return
    out = Path(a.out) / f"{report.attrs['strategy_id']}_{datetime.now(IST):%Y%m%d_%H%M%S}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    report.to_csv(out, index=False)
    print(f"{report.attrs['strategy_id']} (config {report.attrs['config_version']}) -> {out}")
    print(summary(report))


if __name__ == "__main__":
    main()
