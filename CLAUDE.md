# tradeloop — instructions for Claude Code

A research → trade → review loop for Indian index options. Plain code places orders;
AI agents propose rules and review results; Ranga (the owner) approves every change.
Full plan and architecture drawing: https://claude.ai/artifact/4Dxet6T6yWQfgTb7AhjBUd

Ranga is a senior Go backend engineer (fintech: UPI, ledgers, settlements). Explain
finance/market concepts when they come up; don't explain basic engineering.

## Non-negotiable rules

- **Rule #1 — the bot account is auto-only.** No manual trades ever in the Groww account.
  Every order carries `strategy_id`; any broker position or order without one halts all
  strategies and alerts. Manual trading stays on Zerodha.
- **No LLM in the order path.** Agents read records and write proposals or memos. Only
  approved config changes (versioned in git) change what trades.
- **Good enough, not perfect.** The money-moving parts must be simple and correct: risk
  limits, Rule #1 checks, order sanity checks, kill switch. Everything else ships rough
  and improves with feedback. Don't add sophistication that wasn't asked for.
- **Paper before money.** Real money only after 4+ weeks of paper trading and Ranga's
  manual review. Shadow trades: a live order blocked by capital/margin goes to the
  paper broker, tagged shadow, and notifies Ranga.
- **Never commit secrets.** `.env` holds credentials and is git-ignored.

## Architecture (design rules)

Structure
1. Brokers sit behind two interfaces in `tradeloop/core/interfaces.py`: `MarketData` and
   `Execution`. Groww implements both; the paper broker implements `Execution`; the replay
   feed implements `MarketData`. Build only adapters in use — no speculative ones.
2. Neutral instrument IDs everywhere (`tradeloop/core/ids.py`), e.g.
   `NSE|NIFTY|2026-10-27|25500|CE`. Adapters map them to broker tokens. Database tables,
   configs and agent output use neutral IDs only.
3. Core types (`tradeloop/core/types.py`) at the boundary; no broker field names inside
   the core. `tradeloop/core/` must never import a broker SDK (a test enforces this).
4. Market data has two paths. Live: the feed publishes ticks in memory (bus) to the engine
   and paper broker; nothing on the live path reads the database. History: the database
   serves backtests, research and review.
5. One engine for live, paper and backtest: same strategy code, different adapters.
6. One process, clear modules, in-memory pub/sub (`tradeloop/core/bus.py`). No
   microservices or message queues. SQLite until it hurts; Python until the feed needs Go.

Orders and state
7. Fills arrive asynchronously (broker order-update feed). The engine decides exits only
   from confirmed positions, never from orders it merely sent.
8. Every order has a `client_ref` (idempotency: a retry reuses it), `strategy_id` and
   `config_version` (git hash of the strategy config).
9. The ledger is double-entry and records charges (brokerage, STT, exchange fees, GST)
   on every fill, paper included.
10. Protective stops live at the broker (Groww GTT/OCO), placed right after each entry.

Risk and safety
11. One risk layer; capital buckets per track (F&O, swing, intraday) with separate limits.
    Pre-order checks: bucket limits, order sanity, margin, data freshness (no new entries
    on stale ticks).
12. A plain-code watchdog (not an LLM) triggers the kill switch on hard rules: max loss,
    untagged position, stale data, lost heartbeat. The monitor agent only explains.
13. The kill switch is a standalone command that talks straight to the broker adapter:
    cancel all, then flatten. Exits only. Works when the engine is dead.
14. Reconcile on startup (ledger vs broker positions) before any trading; same check
    intraday for Rule #1.
15. A market calendar drives the clock: trading days, session times, shifted expiries.

Agents
16. Agents never touch the order path.

## Tracks

F&O on weekly index options (building now; any day of the expiry week, expiry day
first) → swing trades → intraday equities. Long-term investing is
a separate research-memo track (no auto-trades; holdings stay outside the bot account).
Its research agent reads annual reports, concall transcripts, exchange filings and
brokerage reports, and writes memos that cite their sources.
Scalping is out.

## Current state

- `tradeloop/brokers/groww.py`: Groww `MarketData` adapter. Maps the instrument CSV and
  feed tokens to neutral IDs, polls feed snapshots on its own loop (SDK callbacks run on the
  SDK's network thread), and rebuilds the feed if no data arrives for `stale_seconds`.
- `tradeloop/recorder.py`: wiring only. Adapter -> bus -> `TickWriter` (store.py); index ticks
  drive strike selection and re-centring. Tables hold neutral IDs.
- `tradeloop/core/`: neutral IDs, types, interfaces, event bus, market calendar. Tested, no
  broker code.
- `tradeloop/core/market_calendar.py` + `market_calendar.yaml`: trading days, sessions,
  expiries (holiday-shifted). Unknown years raise. Broker-listed expiries win; the
  recorder cross-checks them against the calendar at startup and skips holidays.
  Update the yaml yearly when NSE publishes the holiday list. Session times can change by
  date (`session_changes`): the F&O close moved from 15:30 to 15:40 on 3 Aug 2026.
  Code asks `close_time(d)`; never hard-code 15:30.
- `tradeloop/backfill.py` + `derive_greeks.py` + `check_history.py`: historical candles in
  `data/history/` (git-ignored), IV/Greeks derived, quality checked. See README.
- Groww's live feed runs over NATS; only the official Python SDK (`growwapi`) is used.
  Auth: TOTP (`GROWW_API_KEY` + `GROWW_TOTP_SECRET`); tokens expire 06:00 IST daily.
  The live LTP stream rarely carries volume/OI (the SDK prints missing fields as 0), so
  the recorder stores them as NULL. `chain_recorder.py` fills the gap live: Groww's option
  chain (LTP, volume, OI, IV, Greeks per strike) once a minute into data/chain.db. Strategies
  use our own IV/Greeks (same code as backtest); Groww's are a cross-check (its theta is
  capped at the premium, ours is the per-day rate).

## Next steps (in order) — backtest first, then paper

Groww's historical API serves 1-minute candles for indices and F&O back to 2020, so a
rule can be tested on past expiries before paper trading. Candles have no bid/ask: the
recorder's order-book data is what calibrates fill costs. Only ~2 years match today's
rules (SEBI's Nov 2024 F&O changes, expiry weekday moves); older years are a robustness
check, not the main evidence.

2. Groww adapter — done in code, not yet run against the live feed.
   Archive (decided, built: `tradeloop/archive.py`): keep every recorded day forever,
   compressed. Finished days move from SQLite to Parquet after verification. Expiry days
   are the most valuable data the system produces.
3. Market calendar — done for 2026 onward. Expiry history before Sep 2025 (weekday
   changes, transition exceptions) and pre-2026 trading days come from broker data in
   step 4, not hand-written rules.
4. Candle downloader — done. 523 expiries: NIFTY Jan 2020-Oct 2026, SENSEX Aug 2023-Oct
   2026, ~680 MB. Expired contracts and OI available; no IV/Greeks from Groww, so we derive
   them. Options settle within ~0.5 pts of the official close. Strikes come from the strike
   grid, not Groww's contract list (it omits contracts that exist: 1,930 recovered). Known
   gaps (backtests skip, data_quality.yaml): NIFTY 2026-09-29 (data stops ~14:00), SENSEX
   2023-10-20 (a few strikes settle 6-13 pts off).
   Lot sizes aren't in Groww's history: they're in lot_sizes.yaml (step 5).
5. Ledger and fill model — done (`ledger.py`, `charges.py` + `charges.yaml`, `lot_sizes.py`
   + `lot_sizes.yaml`, `fill_model.py`). Money in integer paise; charges dated with
   sources; lot sizes verified against every expiry's volumes; charges match Groww's
   calculator (Rs 20/order, STT rounded to the rupee, no IPFT). Open: pre-Oct-2024
   exchange slabs are approximations; compare with a real contract note once live. Slippage defaults are a guess until recorded depth
   calibrates them. Margin isn't modelled yet (risk layer, step 7).
6. Engine — built (`engine.py`, `sim_broker.py`, `backtest.py`), tested with an EXAMPLE
   rule (`strategies/example_straddle.yaml`, not a trading idea). Replays every trading
   day (not only expiry day), nearest weekly expiry's contracts, overnight holds allowed. Next: Ranga's first real
   rule, found and tested by the research method below.
7. Paper trading — built (`paper.py`, `paper_broker.py`, `paper_store.py`, `risk.py`), runs
   inside the recorder. Safety layer (all built, paper-only until the Groww `Execution`
   adapter exists): `kill_switch.py` (data/KILL: halt, cancel, flatten), `watchdog.py` (stale
   data, max open loss, untagged position, heartbeat), `reconcile.py` (startup check, halts
   without flattening), `alerts.py` (every `ALERT` log line to Telegram, Gmail as backup and for
   critical ones; secrets in .env; `python -m tradeloop.alerts test`; verified on Ranga's phone and
   inbox 31 Oct 2026; never let an alert failure block an order or a kill). Five ways of cutting the spread's big losses were tested and all
   failed (research/LOG.md): the lever left is lot size.
   Live wiring (built, OFF): `live_broker.py` (GrowwExecution behind the paper trader: marketable limits,
   polling, `tend()` so a kill works with the feed dead), `live_traders()` in recorder.py (config `live:`
   enabled+armed both false), `live_kill.py` (`kill_switch flatten-live --yes`, refuses while the recorder
   is alive), reconciliation also checks Groww positions and untagged orders, watchdog trips on 3 failed
   position reads and its cron heartbeat check covers live positions.
   Worker thread: every blocking Groww call (place, cancel, poll, cancel-all) runs on one thread in
   LiveBroker (`live.worker_thread`, default on); events reach the ledger only via `drain()` on the
   caller's thread, and the broker's own `working()` changes in the same step as the ledger (else a kill
   double-exits: tested, with a mutation check). `GrowwExecution.working()` is lock-free for the same reason.
   Pre-trade margin check (`margin_guard.py`, plugged into RiskGate as `gate.margin`): quotes the whole
   book as one basket, refuses without enough free margin (extra x 1.5 for expiry day), above a
   Rs 100k book cap (so a naked short can't pass), or without Rs 90k free to start a spread; closing
   orders are never refused; no margin figure means no new risk. Numbers are `live:` in config.yaml. 10 live scenario drills against a
   fake Groww exchange (`tests/scenarios`). The drills found two real bugs (a failing cancel_all lost the
   kill; the heartbeat check ignored live positions): both fixed with regression tests. Never set
   `live.armed: true` in a commit.
   Groww `Execution` adapter (mid-Nov plan): stages 1-3 done (`brokers/groww_orders.py`,
   `brokers/groww_execution.py`, `groww_smoke.py`). Unarmed by default (`armed=False` refuses every
   order); never place a real order without Ranga's explicit go-ahead. Verified read-only on Ranga's
   account 31 Oct 2026: list keys are `positions` and `order_list`; the SDK ignores `page_size`
   and needs `price` on every margin leg; Groww margins the NIFTY spread at ~Rs 60k (short put
   alone ~Rs 125k). Still unchecked: how an open or carried-forward position is reported
   (run `python -m tradeloop.groww_smoke` while a position exists), the expiry-day margin jump,
   and the order/fill status sequences (needs a real order). Everything unverified before live money, with how to check it, is listed in
   LIVE_CHECKLIST.md: keep it current (mark items checked, with date and what was seen). First strategy: put credit spread (passed its held-out test; memo
   and drop rules in research/memos/put_credit_spread.md). First paper trade Mon 2 Nov
   2026. 30 trades / 4+ weeks, then Ranga's review before any real money.

## Decisions pending (Ranga's; do not decide them for him)

- **Open-loss kill: `watchdog.max_open_loss` is Rs 20,000 for now (30 Oct 2026 decision: keep it).** It is an
  absolute rupee amount, one value for paper and live. One NIFTY lot caps at about Rs 29k at index 22,900
  (the cap is 2% of the index x 65, so it moves ~Rs 1,300 per 1,000 index points), so 20k is ~69% of the cap
  today. Revisit after the paper data shows how often it triggers: keep fixed, make it a percent of the cap,
  or give live its own `live.max_open_loss`.
- **Cumulative live loss limit across weeks** (e.g. stop entering after Rs X lost): not built, needs his number.
  Suggested ~Rs 40k, about 1.4 max-loss trades.
- **Lot size for the first live trades:** 1 lot (`live.max_lots: 1`) unless he says otherwise. The Rs 100k margin
  cap also blocks a second lot.
- Archive long-term home, the 26 unverified event dates, and when to arm live (see LIVE_CHECKLIST.md).

## Research method (how rules are found)

Rules should come from data (IV, Greeks, OI, price), but 523 expiries will always contain
patterns that worked by luck. Every candidate rule:
- starts from a stated reason it should work, then is tested; no blind parameter scans;
- is developed on 2020-2024 and judged once on 2025-2026 (held out; also today's market
  rules). A rule that only works where it was tuned is dropped;
- is judged after charges and slippage (the backtest always includes both);
- needs hundreds of trades and must hold across years and conditions, not one stretch;
- comes with a count of variants tried: the more tried, the stronger the result must be.
Agents propose rules with this evidence; Ranga approves. Nothing trades unapproved.

## Working conventions

- Python 3.10+. Run `pytest -q` before every commit; keep tests offline (no network).
- Small commits, one step at a time. Explain what changed and why in plain terms.
- Verify Groww behaviour against the real API or docs (groww.in/trade-api/docs) rather
  than guessing field names.
- Infra: laptop now (recorder via cron, health check daily); a Mumbai VM with static IP
  before live trading.
