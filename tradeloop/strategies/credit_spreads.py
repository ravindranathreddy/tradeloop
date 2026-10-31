"""Credit spreads, and the iron condor (both together). Hold to expiry.

  put side   Sensibull's "bull put spread": sell a put some % below the index, buy a
             further one as protection. Loses if the index falls past the sold strike.
  call side  "bear call spread": the mirror image above the index.
  condor     both sides at once (Sensibull's "iron condor"): collects both credits; only
             one side can lose, since the index can't finish below one and above the other.

The maximum loss of each side is fixed at entry: (strike gap - credit) x quantity.
They come from study 5 (research/LOG.md): far out-of-the-money options a day or two
before expiry paid out less than their price in 2020-2024 (puts 0.50, calls 0.73).

Safety: each side buys its protection first and sells only once the protection is
confirmed filled, and only for the filled quantity. If the protection doesn't fill,
nothing is sold; if the sale doesn't fill, the protection is sold back. Never a naked
short option.

Config keys: lots, entry_time ("HH:MM"), enter_within_days (enter on the first trading
day with 1..N calendar days to expiry, and only that day; e.g. 2 means Monday for a
Tuesday expiry, Tuesday for a Thursday one),
short_otm_pct, long_otm_pct (distance from the index at entry, in %), optional exit_within_pct
(close the spread, short first, once the index comes within this % of the sold strike; off by
default), and for
`credit_spread` option_type (PE or CE).
"""
from __future__ import annotations

from datetime import date, time

from tradeloop.core.ids import InstrumentId, Kind
from tradeloop.core.types import Side


class CreditSpread:
    def __init__(self, cfg: dict):
        self.kind = Kind(cfg.get("option_type", "PE"))
        if not self.kind.is_option:
            raise ValueError("option_type must be PE or CE")
        self.lots = int(cfg["lots"])
        self.entry = time.fromisoformat(cfg["entry_time"])
        self.within = int(cfg["enter_within_days"])
        self.short_pct = float(cfg["short_otm_pct"])
        self.long_pct = float(cfg["long_otm_pct"])
        if not 0 < self.short_pct < self.long_pct:
            raise ValueError("need 0 < short_otm_pct < long_otm_pct")
        self.exit_pct = float(cfg["exit_within_pct"]) if cfg.get("exit_within_pct") else None
        # waiting -> buying_long -> selling_short -> holding [-> closing_short -> closing_long] -> done
        self.state = "waiting"
        self.long: InstrumentId | None = None
        self.short: InstrumentId | None = None
        self.day: date | None = None

    def state_dict(self) -> dict:
        """What a restart needs (paper/live processes stop every evening)."""
        return {"state": self.state, "long": str(self.long) if self.long else None,
                "short": str(self.short) if self.short else None, "day": self.day.isoformat() if self.day else None}

    def restore(self, s: dict) -> None:
        self.state = s["state"]
        self.long = InstrumentId.parse(s["long"]) if s["long"] else None
        self.short = InstrumentId.parse(s["short"]) if s["short"] else None
        self.day = date.fromisoformat(s["day"]) if s["day"] else None

    def on_minute(self, ctx) -> None:
        if self.state == "waiting":
            if 0 < ctx.days_to_expiry <= self.within and ctx.time >= self.entry:
                if self._missed_first_day(ctx):
                    self.state = "done"
                    return
                self._enter(ctx)
        elif self.state == "buying_long":
            self._after_long(ctx)
        elif self.state == "selling_short":
            self._after_short(ctx)
        elif self.state == "holding":
            self._check_exit(ctx)
        elif self.state == "closing_short":
            self._after_close_short(ctx)
        elif self.state == "closing_long" and not self._working(ctx):
            self.state = "done"

    def _check_exit(self, ctx) -> None:
        if self.exit_pct is None:
            return
        k, sign = self.short.strike, (-1 if self.kind is Kind.PE else 1)
        if (ctx.index_price - k * (1 - sign * self.exit_pct / 100)) * sign < 0:
            return                                          # not near the sold strike yet
        self.state = "closing_short"
        self._after_close_short(ctx)

    def _after_close_short(self, ctx) -> None:
        """Buy the short back first (retrying if it lapsed); only then sell the protection."""
        if self._working(ctx):
            return
        positions = ctx.positions()
        sold = -positions.get(self.short, 0)
        if sold > 0:
            ctx.order(self.short, Side.BUY, sold // ctx.lot_size(self.short))
            return
        held = positions.get(self.long, 0)
        if held > 0:
            ctx.order(self.long, Side.SELL, held // ctx.lot_size(self.long))
        self.state = "closing_long"

    def _missed_first_day(self, ctx) -> bool:
        """Enter only on the first trading day inside the window, as the backtest does. A
        process that wasn't running that day (e.g. started on Wednesday for a Thursday
        expiry) would otherwise enter later, with less time left: a setup that tested badly."""
        prev = ctx.previous_trading_day()
        return prev is not None and 0 < (ctx.expiry - prev).days <= self.within

    def _working(self, ctx) -> bool:
        return any(o.instrument in (self.long, self.short) for o in ctx.working_orders())

    def _enter(self, ctx) -> None:
        self.state, self.day = "done", ctx.today          # one attempt per week
        sign = -1 if self.kind is Kind.PE else 1          # puts below the index, calls above
        quoted = {i.strike: i for i in ctx.options(self.kind)}
        out = [k for k in quoted if (k - ctx.index_price) * sign > 0]
        if not out:
            return
        short_k = min(out, key=lambda k: abs(k - ctx.index_price * (1 + sign * self.short_pct / 100)))
        further = [k for k in out if (k - short_k) * sign > 0]
        if not further:
            return
        long_k = min(further, key=lambda k: abs(k - ctx.index_price * (1 + sign * self.long_pct / 100)))
        self.short, self.long = quoted[short_k], quoted[long_k]
        ctx.order(self.long, Side.BUY, self.lots)
        self.state = "buying_long"

    def _after_long(self, ctx) -> None:
        if self._working(ctx):
            return                                          # still filling
        held = ctx.positions().get(self.long, 0)
        if held <= 0:
            self.state = "done"                             # protection never filled: do nothing
            return
        ctx.order(self.short, Side.SELL, held // ctx.lot_size(self.long))
        self.state = "selling_short"

    def _after_short(self, ctx) -> None:
        if self._working(ctx):
            return
        positions = ctx.positions()
        sold = -positions.get(self.short, 0)
        held = positions.get(self.long, 0)
        if sold <= 0 or ctx.today != self.day:
            # sale never filled: sell the protection back rather than hold a lone option
            if held > 0:
                ctx.order(self.long, Side.SELL, held // ctx.lot_size(self.long))
            self.state = "done"
            return
        if held > sold:                                     # partly filled sale: trim the extra protection
            ctx.order(self.long, Side.SELL, (held - sold) // ctx.lot_size(self.long))
        self.state = "holding"


def PutCreditSpread(cfg: dict) -> CreditSpread:
    return CreditSpread({**cfg, "option_type": "PE"})


class IronCondor:
    """A put credit spread and a call credit spread with the same settings, run together."""

    def __init__(self, cfg: dict):
        self.sides = [CreditSpread({**cfg, "option_type": "PE"}), CreditSpread({**cfg, "option_type": "CE"})]

    def on_minute(self, ctx) -> None:
        for side in self.sides:
            side.on_minute(ctx)

    def state_dict(self) -> dict:
        return {"sides": [s.state_dict() for s in self.sides]}

    def restore(self, s: dict) -> None:
        for side, st in zip(self.sides, s["sides"]):
            side.restore(st)
