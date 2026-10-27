"""Backtest a strategy config on downloaded expiries (data/history).

    python -m tradeloop.backtest strategies/example_straddle.yaml [--from 2025-01-01] [--to ...]

For each expiry (status ok in expiries.csv, not excluded in data_quality.yaml), replays
the expiry day minute by minute from 09:15 to the F&O close: working orders are matched
against each minute's candle (SimBroker + FillModel), then the strategy sees that
minute's close, IV and Greeks and may place orders, which can fill from the next
minute. Anything still open at the close settles at the official close. Each expiry
gets a fresh strategy, ledger and broker.

Writes one row per expiry to data/backtests/<strategy_id>_<run time>.csv and prints a
summary. Money in the report is rupees, net of all charges unless labelled gross.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
from datetime import date, datetime
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


def run_expiry(cfg: dict, version: str, index: InstrumentId, expiry: date, candles: pd.DataFrame,
               official_close: float, fill_model: FillModel) -> dict:
    session = default_calendar().close_time(expiry)
    open_ms = int(datetime.combine(expiry, datetime.strptime("09:15", "%H:%M").time(), IST).timestamp() * 1000)
    close_ms = int(datetime.combine(expiry, session, IST).timestamp() * 1000)
    day = candles[(candles["ts_ms"] >= open_ms) & (candles["ts_ms"] < close_ms)]
    ids = {s: InstrumentId.parse(s) for s in day["instrument"].unique()}
    lots = default_lot_sizes()

    ledger = Ledger()
    broker = SimBroker(fill_model, lots.lot_size)
    strategy = REGISTRY[cfg["strategy"]](cfg)
    engine = Engine(strategy, broker, ledger, strategy_id=cfg["strategy_id"], config_version=version,
                    lot_size=lots.lot_size, working_orders=broker.working)

    quotes: dict[InstrumentId, Quote] = {}
    index_price = None
    for ts, rows in day.groupby("ts_ms", sort=True):
        minute = {ids[r.instrument]: r for r in rows.itertuples(index=False)}
        broker.advance(int(ts), minute)                       # orders from earlier minutes fill first
        for iid, r in minute.items():
            if iid.kind is Kind.IDX:
                index_price = float(r.close)
            else:
                quotes[iid] = Quote(int(r.ts_ms), float(r.close), _num(r.volume), _num(r.oi),
                                    *(_num(getattr(r, k, None)) for k in ("iv", "delta", "gamma", "vega", "theta")))
        if index_price is None:
            continue
        broker.end_of_minute(int(ts) + MINUTE_MS)             # decisions now fill from the next candle
        engine.on_minute(int(ts) + MINUTE_MS, index, expiry, index_price, quotes)

    broker.cancel_all()
    open_before = len(ledger.positions())
    ledger.settle_expiry(expiry, index.underlying, official_close, close_ms)
    return {
        "index": str(index), "expiry": expiry.isoformat(), "fills": len(engine.fills),
        "settled_at_expiry": open_before,
        "gross": ledger.realised_pnl() / 100, "charges": ledger.total_charges() / 100, "net": ledger.net_pnl() / 100,
    }


def run(cfg_path: Path, history: Path, start: date, end: date, fill_model: FillModel | None = None) -> pd.DataFrame:
    cfg = yaml.safe_load(cfg_path.read_text())
    version = config_version(cfg_path)
    fill_model = fill_model or FillModel()
    skip = exclusions()
    manifest = pd.read_csv(history / "expiries.csv")
    rows = []
    for index_s in cfg["underlyings"]:
        index = InstrumentId.parse(index_s)
        todo = manifest[(manifest["index"] == index_s) & (manifest["status"] == "ok")]
        for exp_s in sorted(todo["expiry"]):
            expiry = date.fromisoformat(exp_s)
            if not (start <= expiry <= end) or (index_s, exp_s) in skip:
                continue
            candles, official = load_expiry(history, index, expiry)
            if official is None:
                log.warning("%s %s: no official close, skipped", index_s, exp_s)
                continue
            rows.append(run_expiry(cfg, version, index, expiry, candles, official, fill_model))
    out = pd.DataFrame(rows)
    out.attrs.update(strategy_id=cfg["strategy_id"], config_version=version)
    return out


def summary(report: pd.DataFrame) -> str:
    lines = []
    for name, g in report.groupby("index"):
        g = g.sort_values("expiry")
        cum = g["net"].cumsum()
        drawdown = (cum - cum.cummax()).min()
        traded = g[g["fills"] > 0]
        lines.append(f"{name}: {len(g)} expiries ({g['expiry'].min()} .. {g['expiry'].max()}), traded {len(traded)}")
        lines.append(f"  net Rs {g['net'].sum():,.0f} (gross {g['gross'].sum():,.0f}, charges {g['charges'].sum():,.0f})"
                     f"; per expiry avg {traded['net'].mean():,.0f}, win rate {(traded['net'] > 0).mean():.0%}")
        lines.append(f"  best {g['net'].max():,.0f}, worst {g['net'].min():,.0f}, max drawdown {drawdown:,.0f}")
        by_year = g.assign(year=g["expiry"].str[:4]).groupby("year")["net"].agg(["count", "sum"])
        lines.append("  by year: " + ", ".join(f"{y} {s:,.0f} ({c})" for y, (c, s) in by_year.iterrows()))
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
