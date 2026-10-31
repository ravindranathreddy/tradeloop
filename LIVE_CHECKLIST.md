# Live trading: what is still unverified

Things the code assumes but has not yet seen happen at Groww. Each one is checked on the first real
order (or read-only on the day named), and its line is updated with what was actually observed.
Nothing here is armed: `GrowwExecution` stays `armed=False` until Ranga says go.

What is already tested offline (fake Groww API, 10 live scenario drills): see the live_* drills in
`tests/test_scenarios.py`. This file is only about what a fake cannot tell us.

Status: `open` = not yet seen, `checked` = observed (add date and what happened).

## Orders

- **Market orders on options** (open). We don't know whether Groww accepts plain MARKET orders on
  index options. `LiveBroker` (tradeloop/live_broker.py) therefore sends every MARKET order as a
  marketable LIMIT: best ask + 4 ticks to buy, best bid - 4 ticks to sell, from the last order book
  (10% buffer on a stale book, so the kill switch can still exit; MARKET only if no book was ever
  seen). This differs from the paper broker, which fills at the book. Check on the first real order:
  does the limit fill at about the book price, and is 4 ticks the right buffer? If Groww accepts
  MARKET, decide whether to keep the limit anyway.
- **Order status sequence** (open). Statuses seen in the docs: NEW, ACKED, APPROVED, TRIGGER_PENDING,
  REJECTED, FAILED, EXECUTED, CANCELLED, CANCELLATION_REQUESTED, MODIFICATION_REQUESTED, COMPLETED,
  DELIVERY_AWAITED. There is no partial-fill status; fills come only from cumulative
  `filledQty` / `avgFillPrice`. Check the real sequence for a fill, a cancel and a rejection.
- **Duplicate order_reference_id** (open). Not documented. The adapter looks the reference up before
  sending and never resends after an unknown failure, so it doesn't rely on Groww rejecting a duplicate.
- **Rate limits** (open). `LiveBroker` polls a working order at most once a second.
- **Worker thread** (built; two things a fake cannot show, open). Groww calls run on one worker thread,
  so a slow Groww no longer stalls the feed (drill: a 90 s hang left a 7 s gap in the tick recording,
  97 s without the thread). Check live: (a) the Groww SDK tolerates the worker's calls overlapping with
  the watchdog thread's `get_positions_for_user` every 5 s; (b) because there is one worker, a kill's
  exit orders queue behind a hung entry call for up to the 10 s timeout. If (a) misbehaves, give the
  watchdog's position read its own lock or client; if (b) matters, lower `GrowwExecution(timeout=...)`.
- **Standalone flatten prices** (open). `kill_switch flatten-live` has no live book, so it prices exits
  from the last recorded price with a 10% buffer (MARKET if there is none). Check on a real exit that
  this fills, and that 10% is a sensible width.
- **Cancel-all scope** (open). `cancel_all` cancels every working order in the account, including ones
  that are not ours. That is intended for a kill; in a shared account it means manual orders go too.

## Margin

- **Buy-first margin** (open). The Rs 60,328 quote (spread) vs Rs 124,714 (short put alone) was for the
  two legs as one basket, on 31 Oct 2026. Check that selling the short put after the long put has
  filled is margined as the spread, not as a naked short.
- **Expiry-day margin** (open). Estimated about Rs 89k (SEBI extra 2% ELM on expiry day). Rerun
  `python -m tradeloop.groww_smoke` on Tue 3 Nov 2026.
- **Pledged liquid funds** (open). Whether Groww accepts the fund, the haircut (about 10% assumed),
  and whether liquid funds count as cash-equivalent for the minimum cash share of margin (about 50%
  assumed). Plan: Rs 2 lakh liquid fund pledged plus Rs 1 lakh cash. Test with a small pledge in the
  real account and read the margin credit.

- **Margin check inputs** (open). `margin_guard.py` quotes the whole book as one basket and reads
  `option_sell_balance_available` as the free margin. Check on the first real order that (a) the quote
  for the book with the long put held matches what Groww then actually blocks, (b) that balance field
  is what Groww enforces, and (c) pledged liquid-fund collateral shows up in it (after the haircut).
  If any of those differ, adjust `margin_buffer` / the field, not the guard's structure.
- **Daily / weekly loss limit** (open decision, not built). Today a loss is limited by the spread's
  structure (about Rs 31.5k per lot), the watchdog's open-loss kill (`max_open_loss`, Rs 20k) and one
  lot. A limit across weeks (stop entering after cumulative live loss of Rs X) needs Ranga's number.

## Positions and the account

- **Carried-forward positions** (open). How an open position is reported (`quantity`, `net_price`,
  credit/debit quantity). Run `python -m tradeloop.groww_smoke` while a position exists.
- **SEBI algo / strategy-ID rules** (open). Whether Groww needs a registered strategy ID or approval
  for API orders. Ask Groww support.
- **"Strategies is shutting down" banner** (open). Seen in the Groww app on 29 Oct 2026; API tokens
  were said to stay accessible. Get the exact wording and date; confirm API orders are unaffected.
- **Static IP** (open). Groww API orders need a registered static IP (the Mumbai VM, mid-Nov).
- **Account** (open). Real trading is in Ranga's mother's account (lower tax slab), after the CA and
  consent steps.

## Before arming (Ranga's switch)

1. Everything above that matters is `checked`, or consciously accepted.
2. `config.yaml`: `live.enabled: true` and `live.armed: true` (only `true`, exactly; anything else stays
   unarmed). `tests/test_live_wiring.py::test_the_committed_config_never_sends_real_orders` guards the
   committed file: arm on the trading machine, do not commit it armed.
3. The recorder announces `ALERT LIVE ARMED, REAL ORDERS WILL BE SENT` at start (phone alert).
4. Settle live expiries after the close: `python -m tradeloop.paper settle --live` (and `report --live`).
5. If the recorder is dead with live positions: `python -m tradeloop.kill_switch flatten-live --yes`.
