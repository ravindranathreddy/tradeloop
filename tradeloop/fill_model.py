"""Backtest fills from 1-minute candles. Conservative on purpose: history has no bid/ask.

An order decided at time t (after seeing the candle that closed at t) can fill only in
candles that start at or after t, and only in minutes where the contract traded:

  MARKET  the candle's open, plus slippage against us
  LIMIT   only if the price traded through the limit (a touch isn't enough); at the
          better of open + slippage and the limit
  SL_M    a resting stop: triggers when the candle reaches the trigger, then fills at
          the worse of open and trigger, plus slippage; remaining quantity fills in
          later minutes as a market order

Each minute fills at most `max_volume_share` of that minute's traded volume, in whole
lots, so a big order spreads over several minutes. MARKET and LIMIT orders give up
after `max_wait_minutes`; stops stay until the session closes. Nothing fills after
the F&O close.

Slippage is the larger of `slippage_ticks` ticks and `slippage_pct` of the price. The
defaults are a guess; the recorder's order-book data is what will calibrate them.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from tradeloop.core.market_calendar import IST, default_calendar
from tradeloop.core.types import Fill, Order, OrderType, Side

TICK = 0.05
MINUTE_MS = 60_000


@dataclass(frozen=True, slots=True)
class FillModel:
    slippage_ticks: int = 2
    slippage_pct: float = 0.5
    max_volume_share: float = 0.1
    max_wait_minutes: int = 5

    def slippage(self, price: float) -> float:
        return max(self.slippage_ticks * TICK, price * self.slippage_pct / 100)

    def fill(self, order: Order, decided_ms: int, candles: pd.DataFrame, lot_size: int) -> list[Fill]:
        """Fills for `order`, given that instrument's 1-minute candles (columns ts_ms, open,
        high, low, close, volume). Returns [] if nothing fills; a partial fill is normal."""
        if order.order_type is OrderType.SL:
            raise NotImplementedError("stop-limit orders: use SL_M or LIMIT")
        if order.qty % lot_size:
            raise ValueError(f"qty {order.qty} is not a whole number of lots of {lot_size}")
        day = datetime.fromtimestamp(decided_ms / 1000, IST).date()
        close = datetime.combine(day, default_calendar().close_time(day), IST)
        close_ms = int(close.timestamp() * 1000)
        c = candles[(candles["ts_ms"] >= decided_ms) & (candles["ts_ms"] < close_ms)].sort_values("ts_ms")
        if order.order_type is not OrderType.SL_M:
            c = c[c["ts_ms"] < decided_ms + self.max_wait_minutes * MINUTE_MS]

        fills: list[Fill] = []
        remaining, triggered = order.qty, False
        buy = order.side is Side.BUY
        for row in c.itertuples(index=False):
            if remaining == 0:
                break
            if not (row.volume and row.volume > 0):
                continue
            price = self._price(order, row, buy, triggered)
            if price is None:
                continue
            triggered = True
            room = int(row.volume * self.max_volume_share) // lot_size * lot_size
            qty = min(remaining, room)
            if qty == 0:
                continue
            remaining -= qty
            fills.append(Fill(f"{order.client_ref}-{len(fills) + 1}", order.client_ref, order.instrument,
                              order.side, qty, price, int(row.ts_ms)))
        return fills

    def _price(self, order: Order, row, buy: bool, triggered: bool) -> float | None:
        """Fill price in this candle, or None if the order can't fill in it."""
        if order.order_type is OrderType.LIMIT:
            through = row.low < order.price if buy else row.high > order.price
            if not through:
                return None
            raw = min(row.open + self.slippage(row.open), order.price) if buy else \
                max(row.open - self.slippage(row.open), order.price)
            return _to_tick(raw, up=buy)
        base = row.open
        if order.order_type is OrderType.SL_M and not triggered:
            hit = row.high >= order.trigger if buy else row.low <= order.trigger
            if not hit:
                return None
            base = max(row.open, order.trigger) if buy else min(row.open, order.trigger)
        raw = base + self.slippage(base) if buy else base - self.slippage(base)
        return _to_tick(raw, up=buy)


def _to_tick(price: float, up: bool) -> float:
    """Round to the 0.05 tick against us; never below one tick."""
    ticks = price / TICK
    ticks = math.ceil(ticks - 1e-9) if up else math.floor(ticks + 1e-9)
    return round(max(ticks, 1) * TICK, 2)
