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
python -m tradeloop.recorder --config config.yaml   # runs until 15:31 IST, then exits
python -m tradeloop.health --db data/ticks.db       # after close: rows and any empty minutes
```

Schedule it for every trading day (crontab on the machine that stays on, times in IST):

```cron
5 9 * * 1-5   cd ~/tradeloop && .venv/bin/python -m tradeloop.recorder >> logs/recorder.log 2>&1
40 15 * * 1-5 cd ~/tradeloop && .venv/bin/python -m tradeloop.health >> logs/health.log 2>&1
```

Create `logs/` first. On holidays the recorder just records nothing and exits.

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
| `CLAUDE.md` | Architecture rules and next steps, for Claude Code |

## Next

Move the Groww code behind the core interfaces, then a market calendar, ledger and paper broker, and a minimal engine running one expiry-day rule in paper mode. See `CLAUDE.md`.
