"""Pre-trade margin check for live orders (design rule 11: capital buckets and margin).

Plugged into the risk gate as `gate.margin`. For an order that opens or adds to a position it asks
Groww (a quote, nothing is sent) what the whole book would need after the order, and refuses unless:

  free margin     Groww's option-selling balance covers the extra margin the order needs, times
                  `buffer` (1.5: expiry-day margin is higher than at entry, about Rs 89k vs Rs 60k)
  cap             the book's total margin stays under `max_margin`. A short sold without its
                  protection in the ledger costs about Rs 125k and trips this, so a naked short can't
                  slip through even if the strategy mis-sequenced
  entry floor     a new spread (nothing held) also needs `min_free` of free margin up front, so the
                  protection isn't bought only to be sold back when the short is refused

Orders that reduce a position are never refused here (closing must always be possible). Any failure to
ask or to understand the answer refuses the order: no margin figure, no new risk.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace

from tradeloop.brokers import groww_orders as g
from tradeloop.core.ids import InstrumentId
from tradeloop.core.types import Order, OrderType, Position, Side

log = logging.getLogger("margin")


class MarginGuard:
    def __init__(self, api, symbols: dict[InstrumentId, g.Symbol], *, positions: Callable[[], list[Position]],
                 price: Callable[[InstrumentId], float | None], buffer: float = 1.5, max_margin: float | None = 100_000,
                 min_free: float = 90_000, timeout: int = 10):
        self.api, self._symbols, self._positions, self._price = api, symbols, positions, price
        self.buffer, self.max_margin, self.min_free, self.timeout = buffer, max_margin, min_free, timeout

    def check(self, order: Order) -> str | None:
        """None to allow the order, else the reason it is refused."""
        held = {p.instrument: p.qty for p in self._positions() if p.qty}
        signed = order.qty if order.side is Side.BUY else -order.qty
        cur = held.get(order.instrument, 0)
        if cur and (cur > 0) != (signed > 0) and abs(signed) <= abs(cur):
            return None                                           # reduces a position: always allowed
        try:
            return self._check(order, held, signed)
        except Exception as e:
            return f"margin check failed ({type(e).__name__}: {e}); no new risk without a margin figure"

    def _check(self, order: Order, held: dict[InstrumentId, int], signed: int) -> str | None:
        after = dict(held)
        after[order.instrument] = after.get(order.instrument, 0) + signed
        required_after = self._margin(order, after)
        required_before = self._margin(order, held) if held else 0.0
        extra = max(required_after - required_before, 0.0)
        available = g.parse_available(self.api.get_available_margin_details(timeout=self.timeout)).option_sell
        need = extra * self.buffer
        log.info("margin: book after %s %d %s needs Rs %s (Rs %s more); free Rs %s; need Rs %s with the %.1fx buffer",
                 order.side.value, order.qty, order.instrument, f"{required_after:,.0f}", f"{extra:,.0f}",
                 f"{available:,.0f}", f"{need:,.0f}", self.buffer)
        if self.max_margin is not None and required_after > self.max_margin:
            return f"book margin Rs {required_after:,.0f} would exceed the live cap of Rs {self.max_margin:,.0f}"
        if not held and available < self.min_free:
            return f"free margin Rs {available:,.0f} is below the Rs {self.min_free:,.0f} needed to start a spread"
        if available < need:
            return f"free margin Rs {available:,.0f} is below the Rs {need:,.0f} needed (Rs {extra:,.0f} x {self.buffer:g})"
        return None

    def _margin(self, template: Order, book: dict[InstrumentId, int]) -> float:
        """Margin Groww quotes for the whole book as one basket (so the hedge benefit applies)."""
        legs, prices = [], {}
        for inst, qty in sorted(book.items(), key=lambda kv: str(kv[0])):
            if not qty:
                continue
            sym = self._symbols.get(inst)
            if sym is None:
                raise g.UnknownSymbol(f"{inst} is not a Groww contract")
            px = self._price(inst)
            if not px:
                raise ValueError(f"no reference price for {inst}")
            prices[inst] = px
            legs.append((replace(template, instrument=inst, side=Side.BUY if qty > 0 else Side.SELL, qty=abs(qty),
                                 order_type=OrderType.MARKET, price=None, trigger=None), sym))
        if not legs:
            return 0.0
        return g.parse_margin(self.api.get_order_margin_details(**g.margin_request(legs, prices), timeout=self.timeout)).total
