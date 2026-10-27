"""Double-entry ledger (design rule 9). Same ledger for backtest, paper and live.

Money is integer paise. Every entry's postings sum to zero (debits positive, credits
negative), so the books always balance. Accounts:

    cash                    money at the broker
    capital                 money paid in (credit)
    position:<instrument>   cost of the open position: debit for a long (what we paid),
                            credit for a short (what we received)
    pnl:realised            credit = profit
    charges:<name>          brokerage, stt, exchange, sebi, ipft, stamp, gst

A fill that reduces a position moves the closed part's average cost out of the position
account and the difference to pnl:realised; a fill that crosses zero is split into a
close and an open. Options still open at expiry settle at their intrinsic value against
the official close (settle_expiry); long in-the-money ones pay STT on that value.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from tradeloop.charges import ChargeSchedule, default_charges
from tradeloop.core.ids import InstrumentId, Kind
from tradeloop.core.types import Fill, Position, Side

IST = ZoneInfo("Asia/Kolkata")
CASH, CAPITAL, REALISED = "cash", "capital", "pnl:realised"


class Unbalanced(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Posting:
    account: str
    amount: int              # paise; debit positive, credit negative


@dataclass(frozen=True, slots=True)
class Entry:
    ts_ms: int
    memo: str
    postings: tuple[Posting, ...]
    ref: str = ""            # fill_id, or "" for deposits / settlements


@dataclass(slots=True)
class _Open:
    qty: int = 0             # signed: + long, - short
    cost: int = 0            # paise, same sign as qty (the position account's balance)


def paise(rupees: float) -> int:
    """Prices are on a 0.05 tick, so rounding to the paisa is exact."""
    return round(rupees * 100)


class Ledger:
    def __init__(self, charges: ChargeSchedule | None = None):
        self.charges = charges or default_charges()
        self.entries: list[Entry] = []
        self._balances: dict[str, int] = {}
        self._open: dict[InstrumentId, _Open] = {}
        self._orders_charged: set[str] = set()
        self._fill_ids: set[str] = set()

    # --- posting ---------------------------------------------------------------

    def post(self, ts_ms: int, memo: str, postings: list[Posting], ref: str = "") -> Entry:
        postings = [p for p in postings if p.amount != 0]
        if sum(p.amount for p in postings) != 0:
            raise Unbalanced(f"{memo}: postings sum to {sum(p.amount for p in postings)}")
        entry = Entry(ts_ms, memo, tuple(postings), ref)
        self.entries.append(entry)
        for p in postings:
            self._balances[p.account] = self._balances.get(p.account, 0) + p.amount
        return entry

    def deposit(self, rupees: float, ts_ms: int = 0) -> Entry:
        amount = paise(rupees)
        return self.post(ts_ms, "deposit", [Posting(CASH, amount), Posting(CAPITAL, -amount)])

    # --- trading -----------------------------------------------------------------

    def record_fill(self, fill: Fill) -> list[Entry]:
        """Post a fill and its charges. A repeated fill_id is ignored (idempotent)."""
        if fill.fill_id in self._fill_ids:
            return []
        self._fill_ids.add(fill.fill_id)
        value = paise(fill.price) * fill.qty
        signed = fill.qty * fill.side.sign
        memo = f"{fill.side.value} {fill.qty} {fill.instrument} @ {fill.price}"
        entries = [self.post(fill.ts_ms, memo, self._trade_postings(fill.instrument, signed, value), fill.fill_id)]

        first = fill.client_ref not in self._orders_charged
        self._orders_charged.add(fill.client_ref)
        c = self.charges.trade(_ist_date(fill.ts_ms), fill.instrument, fill.side, value, first)
        entries.append(self._post_charges(fill.ts_ms, f"charges: {memo}", c, fill.fill_id))
        return entries

    def _trade_postings(self, instrument: InstrumentId, signed_qty: int, value: int) -> list[Posting]:
        """Cash moves by the trade value; the position account and realised P&L absorb it."""
        pos = self._open.setdefault(instrument, _Open())
        account = f"position:{instrument}"
        cash = -value if signed_qty > 0 else value                  # buy pays, sell receives
        postings = [Posting(CASH, cash)]
        closing = min(abs(signed_qty), abs(pos.qty)) if pos.qty * signed_qty < 0 else 0
        if closing:
            # average cost of the part being closed; the last unit takes the rounding remainder
            cost_out = pos.cost if closing == abs(pos.qty) else pos.cost * closing // abs(pos.qty)
            trade_part = value * closing // abs(signed_qty) if closing < abs(signed_qty) else value
            postings.append(Posting(account, -cost_out))
            # long close: receive trade_part for cost_out; short close: pay trade_part, had received -cost_out
            pnl = (trade_part - cost_out) if pos.qty > 0 else (-cost_out - trade_part)
            postings.append(Posting(REALISED, -pnl))
            pos.qty += closing * (1 if signed_qty > 0 else -1)
            pos.cost -= cost_out
            value -= trade_part
            signed_qty += closing * (-1 if signed_qty > 0 else 1)
        if signed_qty:                                              # opening (or the flipped remainder)
            cost_in = value if signed_qty > 0 else -value
            postings.append(Posting(account, cost_in))
            pos.qty += signed_qty
            pos.cost += cost_in
        if pos.qty == 0:
            del self._open[instrument]
        return postings

    def _post_charges(self, ts_ms: int, memo: str, charges: dict[str, int], ref: str) -> Entry:
        total = sum(charges.values())
        postings = [Posting(f"charges:{k}", v) for k, v in charges.items()] + [Posting(CASH, -total)]
        return self.post(ts_ms, memo, postings, ref)

    # --- expiry ----------------------------------------------------------------

    def settle_expiry(self, expiry: date, underlying: str, settlement_price: float, ts_ms: int) -> list[Entry]:
        """Close every open position in `underlying` expiring on `expiry` at its settlement
        value: intrinsic value for options, the settlement price for futures."""
        entries = []
        for instrument in sorted((i for i in self._open if i.underlying == underlying and i.expiry == expiry), key=str):
            pos = self._open[instrument]
            if instrument.kind is Kind.FUT:
                price = settlement_price
            elif instrument.kind is Kind.CE:
                price = max(0.0, settlement_price - instrument.strike)
            else:
                price = max(0.0, instrument.strike - settlement_price)
            qty, long = abs(pos.qty), pos.qty > 0
            value = paise(price) * qty
            memo = f"expiry settlement {qty} {instrument} @ {price:.2f}"
            entries.append(self.post(ts_ms, memo, self._trade_postings(instrument, -pos.qty, value)))
            if long and value and instrument.kind.is_option:
                entries.append(self._post_charges(ts_ms, f"charges: {memo}", self.charges.exercise(expiry, value), ""))
        return entries

    # --- reading ---------------------------------------------------------------

    def balance(self, account: str) -> int:
        return self._balances.get(account, 0)

    def balances(self) -> dict[str, int]:
        return dict(self._balances)

    def positions(self) -> list[Position]:
        return [Position(i, p.qty, p.cost / p.qty / 100) for i, p in sorted(self._open.items(), key=lambda kv: str(kv[0]))]

    def realised_pnl(self) -> int:
        """Before charges, paise."""
        return -self.balance(REALISED)

    def total_charges(self) -> int:
        return sum(v for k, v in self._balances.items() if k.startswith("charges:"))

    def net_pnl(self) -> int:
        """Realised P&L after charges, paise."""
        return self.realised_pnl() - self.total_charges()


def _ist_date(ts_ms: int) -> date:
    return datetime.fromtimestamp(ts_ms / 1000, IST).date()
