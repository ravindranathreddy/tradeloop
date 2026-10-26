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

F&O expiry-day (building now) → swing trades → intraday equities. Long-term investing is
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
- `tradeloop/core/`: neutral IDs, types, interfaces, event bus. Tested, no broker code.
- Groww's live feed runs over NATS; only the official Python SDK (`growwapi`) is used.
  Auth: TOTP (`GROWW_API_KEY` + `GROWW_TOTP_SECRET`); tokens expire 06:00 IST daily.

## Next steps (in order)

2. Groww adapter — done in code, not yet run against the live feed.
   Archive (decided): keep every recorded day forever, compressed. After each session,
   move finished days from SQLite to Parquet; verify row counts before deleting from
   SQLite. Expiry days are the most valuable data the system produces.
3. Market calendar.
4. Ledger (double-entry, charges) and paper broker (fills at recorded bid/ask).
5. Risk layer (basic limits, stale-data check) and minimal engine running Ranga's first
   expiry-day rule from config in paper mode.

## Working conventions

- Python 3.10+. Run `pytest -q` before every commit; keep tests offline (no network).
- Small commits, one step at a time. Explain what changed and why in plain terms.
- Verify Groww behaviour against the real API or docs (groww.in/trade-api/docs) rather
  than guessing field names.
- Infra: laptop now (recorder via cron, health check daily); a Mumbai VM with static IP
  before live trading.
