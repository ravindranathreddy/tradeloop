# tradeloop

A research → trade → review loop for Indian index options, built in the open.
Plain code places orders; AI agents propose rules and review results; a human approves every change.

This repo is step 1: the **tick recorder**. It records what the market did each day so the
engine, the paper broker and the agents have real data to work with.

## What it records

For the nearest weekly expiry of each configured index (NIFTY on NSE, SENSEX on BSE):

Every row uses neutral instrument IDs such as `NSE|NIFTY|2026-10-27|25500|CE` or `NSE|NIFTY|IDX`.

| Table | What | Why |
| --- | --- | --- |
| `ltp` | Option last traded price, day volume and open interest; spot index value | Strike selection, signals, P&L marking |
| `depth` | Best bid/ask plus top-5 book | Realistic paper fills and slippage |
| `contracts` | Which contracts were recorded that day, with lot size | Expired contracts vanish from Groww's instrument list |

Strikes: ATM ± 15 by default, re-centred if spot moves far. About 124 contracts, well under Groww's 1,000-subscription limit.

## Setup (once)

1. **Groww Trade API subscription.** Live data needs the paid plan (₹499/month + GST). Subscribe from the Groww Trade API page.
2. **TOTP credentials.** On Groww's API keys page, generate a TOTP token. Copy the token (long string starting `eyJ`) and the base32 secret shown with the QR code.
3. **Install:**
   ```bash
   git clone <this repo> && cd tradeloop
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env    # paste your token and secret
   ```
4. **Check the logic:** `pytest -q` (runs offline).

No static IP is needed for recording; it's only required later for placing orders.

## Run

```bash
python -m tradeloop.recorder --config config.yaml   # runs until 15:41 IST (F&O closes 15:40), then exits
python -m tradeloop.health --db data/ticks.db       # after close: rows, empty minutes, disk headroom
python -m tradeloop.archive                          # move earlier days to data/archive/<date>/*.parquet
```

Every recorded day is kept. The archive step moves each finished day out of SQLite into
compressed Parquet files (verified before anything is deleted), so the database holds only
about the current day. `tradeloop.archive.read_day()` reads a day wherever it lives.

Schedule it for every trading day (crontab on the machine that stays on, times in IST):

```cron
5 9 * * 1-5   cd ~/tradeloop && .venv/bin/python -m tradeloop.recorder >> logs/recorder.log 2>&1
50 15 * * 1-5 cd ~/tradeloop && .venv/bin/python -m tradeloop.health >> logs/health.log 2>&1
55 15 * * 1-5 cd ~/tradeloop && .venv/bin/python -m tradeloop.archive >> logs/archive.log 2>&1
```

Create `logs/` first. On weekends and exchange holidays (from `market_calendar.yaml`) the
recorder logs the reason and exits. Update that file each December when NSE publishes the
next year's holiday list; asking about a year that isn't there raises an error on purpose.

## Historical data (for backtests)

```bash
python -m tradeloop.backfill                 # every past NIFTY/SENSEX weekly expiry Groww has (~6 h first time)
python -m tradeloop.derive_greeks            # IV and Greeks per option per minute (~2 min)
python -m tradeloop.check_history            # quality report; exit 1 if any expiry has problems
```

Per expiry, the final week (expiry - 6 days to the close) of the index, every strike from
the week's low to high plus 10 either side, and the future:
`data/history/candles_1m/<EX>_<INDEX>/<expiry>.parquet`, daily index candles in
`candles_1d/` (the daily close is the official close options settle on), IV/Greeks in
`greeks_1m/`. `data/history/expiries.csv` lists every expiry and its status (ok, ghost =
listed but never traded, no_index_data = holiday-shifted, failed). The backfill resumes
where it stopped and retries failed expiries; `--refetch` downloads a range again.

- Groww has NIFTY from Jan 2020 and SENSEX from Aug 2023, OI included, no IV/Greeks
  (we derive them: Black-76 on the synthetic forward from put-call parity).
- Groww returns some candles twice and some futures minutes with no prices; the backfill
  cleans both before writing.
- The token expires at 06:00 IST: a run past that fails the remaining expiries; run it again.
- Run it under `caffeinate -i` on a Mac so it doesn't sleep.
- Data is git-ignored and must stay so (exchange data, public repo). Back up
  `candles_1m/` and `candles_1d/` privately; `greeks_1m/` can be recomputed.

## First run checklist

- Log shows `NIFTY: +62 contracts (expiry ...)` and the same for SENSEX within a minute of starting.
- `health` after close shows ~375 index minutes with **no gaps**.
- If SENSEX finds no contracts, check `underlying_symbol` and `exchange` for SENSEX options in the instrument CSV and fix `config.yaml`.

## Design notes

- **Python, for now.** Groww's live feed runs over NATS and only the Python SDK is official. A Go recorder is a later improvement.
- **Polling the SDK's snapshot** every 0.5s; duplicate updates are dropped by the table keys.
- **SQLite with WAL.** Good enough for one machine; a time-series DB can come later.

## Layout

| Path | What |
| --- | --- |
| `tradeloop/core/` | Broker-neutral core: instrument IDs, types, `MarketData`/`Execution` interfaces, event bus |
| `tradeloop/recorder.py`, `instruments.py`, `feed_parse.py`, `store.py`, `health.py` | Tick recorder (Groww) |
| `tradeloop/backfill.py`, `greeks.py`, `derive_greeks.py`, `check_history.py` | Historical candles, IV/Greeks, quality check |
| `CLAUDE.md` | Architecture rules and next steps, for Claude Code |

## Next

Ledger (with charges) and a fill model, then a minimal engine running one expiry-day rule as a backtest on the downloaded history, then paper trading on the live feed. See `CLAUDE.md`.
