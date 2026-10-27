"""EXAMPLE ONLY: trade the at-the-money straddle at a fixed time, close it at another.

Exists to run the engine end to end (orders, fills, charges, expiry settlement). It is
not a trading idea and nothing about it has been tested for profitability.

Config keys: side (BUY or SELL), lots, entry_time, exit_time ("HH:MM" IST).
"""
from __future__ import annotations

from datetime import time

from tradeloop.core.ids import Kind
from tradeloop.core.types import Side


class TimedStraddle:
    def __init__(self, cfg: dict):
        self.side = Side(cfg["side"])
        self.lots = int(cfg["lots"])
        self.entry = time.fromisoformat(cfg["entry_time"])
        self.exit = time.fromisoformat(cfg["exit_time"])
        self.entered = self.exited = False

    def on_minute(self, ctx) -> None:
        if not self.entered and ctx.time >= self.entry:
            self.entered = True
            calls, puts = ctx.options(Kind.CE), ctx.options(Kind.PE)
            strikes = {i.strike for i in calls} & {i.strike for i in puts}
            if not strikes:
                return                                      # nothing quoted on both sides: skip the day
            atm = min(strikes, key=lambda k: abs(k - ctx.index_price))
            for leg in (calls, puts):
                ctx.order(next(i for i in leg if i.strike == atm), self.side, self.lots)
        elif self.entered and not self.exited and ctx.time >= self.exit:
            self.exited = True
            for o in ctx.working_orders():                  # don't leave an unfilled entry behind
                ctx.cancel(o.client_ref)
            for instrument, qty in ctx.positions().items():
                ctx.order(instrument, Side.SELL if qty > 0 else Side.BUY, abs(qty) // ctx.lot_size(instrument))
