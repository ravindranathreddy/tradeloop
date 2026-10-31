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
   Scenario drills (the recorder, paper trader, watchdog, kill switch and reconciliation together,
   on simulated days; about 2 minutes): `RUN_SCENARIOS=1 pytest tests/test_scenarios.py`. Run them
   after changing anything in the safety layer.

Live trading (off): `live:` in config.yaml runs the same strategy through the Groww adapter into
`data/live.db`. It sends nothing until both `enabled` and `armed` are true; arming is Ranga's decision
(see LIVE_CHECKLIST.md). Settle and report the live ledger with `python -m tradeloop.paper settle --live`
and `report --live`. If the recorder is dead with live positions:
`python -m tradeloop.kill_switch flatten-live --yes`.

Phone alerts: every `ALERT` log line (kill switch, watchdog, reconciliation, Groww gave up) goes to
Telegram, with Gmail as the backup and for critical ones. Put the values from `.env.example` in
`.env` (Telegram bot token and chat id from @BotFather; a Gmail app password, never the real
password), then send a test with `python -m tradeloop.alerts test`. `alerts:` in config.yaml turns
it on and sets the repeat window.

No static IP is needed for recording; it's only required later for placing orders.

## Run

```bash
python -m tradeloop.recorder --config config.yaml   # runs until 15:41 IST (F&O closes 15:40), then exits
python -m tradeloop.chain_recorder                   # once a minute: full option chain (volume, OI, IV, Greeks)
python -m tradeloop.health --db data/ticks.db       # after close: rows, empty minutes, disk headroom
python -m tradeloop.archive                          # move earlier days to data/archive/<date>/*.parquet
python -m tradeloop.archive --db data/chain.db       # same for the option chain (13x smaller as Parquet)
```

Every recorded day is kept. The archive step moves each finished day out of SQLite into
compressed Parquet files (verified before anything is deleted), so the database holds only
about the current day. `tradeloop.archive.read_day()` reads a day wherever it lives.

Schedule it for every trading day (crontab on the machine that stays on, times in IST):

```cron
5 9 * * 1-5   cd ~/tradeloop && .venv/bin/python -m tradeloop.recorder >> logs/recorder.log 2>&1
6 9 * * 1-5   cd ~/tradeloop && .venv/bin/python -m tradeloop.chain_recorder >> logs/chain_recorder.log 2>&1
50 15 * * 1-5 cd ~/tradeloop && .venv/bin/python -m tradeloop.health >> logs/health.log 2>&1
55 15 * * 1-5 cd ~/tradeloop && .venv/bin/python -m tradeloop.archive >> logs/archive.log 2>&1
56 15 * * 1-5 cd ~/tradeloop && .venv/bin/python -m tradeloop.archive --db data/chain.db >> logs/archive_chain.log 2>&1
0 16 * * 1-5  cd ~/tradeloop && .venv/bin/python -m tradeloop.paper settle >> logs/paper.log 2>&1
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

## Ledger, charges and fills (for backtests and paper)

- `tradeloop/ledger.py`: double-entry, integer paise; every entry balances. Records fills
  with average cost and realised P&L, every charge per fill, and expiry settlement
  (options at intrinsic value against the official close; STT on long in-the-money ones).
- `charges.yaml` / `tradeloop/charges.py`: brokerage, STT, exchange, SEBI, IPFT, stamp
  duty and GST, each with the date it took effect and its source. Rates change (STT on
  option sales: 0.05% -> 0.0625% Apr 2023 -> 0.1% Oct 2024 -> 0.15% Apr 2026).
- `lot_sizes.yaml` / `tradeloop/lot_sizes.py`: lot size by expiry since 2020 (NIFTY
  75 -> 50 -> 25 -> 75 -> 65; SENSEX 10 -> 20), matching every downloaded expiry's volumes.
- `tradeloop/fill_model.py`: conservative fills from 1-minute candles (next minute's open
  plus slippage, nothing where nothing traded, at most 10% of a minute's volume).

Worked example (`tests/test_ledger.py`): buy 1 NIFTY lot (65) at 100, sell at 120 on
20 Oct 2026. Gross Rs 1,300.00, charges Rs 65.33 (buy 26.50: brokerage 20, exchange 2.28,
SEBI 0.01, stamp 0.20, GST 4.01; sell 38.83: brokerage 20, STT 12 (rounded to the rupee),
exchange 2.73, SEBI 0.01, GST 4.09), net Rs 1,234.67. The rates match Groww's charges
calculator to within 2 paise (it rounds on the round trip, we per fill).

## Backtest

```bash
python -m tradeloop.backtest strategies/example_straddle.yaml --from 2025-01-01
```

Replays every trading day, minute by minute, through the same engine paper and live
trading will use. Each day runs under the expiry that was nearest that day (the data
holds each weekly expiry's final week). The strategy sees each minute's prices, IV and
Greeks and how many days are left to expiry; orders fill from the next minute through
the fill model; unfilled orders lapse at the close; positions may be held overnight;
anything open at expiry settles at the official close. One row per trading day (gross,
charges, net) goes to `data/backtests/`, plus a summary: net, win rate, worst day, max
drawdown, by days to expiry and by year.
Expiries with known data problems are skipped (`data_quality.yaml`).

A strategy is a class with `on_minute(ctx)` (`tradeloop/strategies/`); its parameters live
in a config file in `strategies/`, identified by the file's git hash (`config_version`).
`example_straddle` exists only to exercise the engine.

## Paper trading

Strategies listed under `paper:` in config.yaml trade on the live feed inside the recorder:
same engine and strategy code as the backtest, orders through the risk gate (max lots, no
orders on stale data) to a paper broker that fills against the live order book. No real
orders. Fills, orders and strategy state go to `data/paper.db`; positions held overnight
survive the nightly restart. After the close on expiry days, settle at the official close:

```bash
python -m tradeloop.paper settle     # 16:00 on trading days (cron above); no-op if nothing expires
python -m tradeloop.paper report     # P&L, open positions, recent fills
```

Memo and drop rules for each paper strategy: `research/memos/`.

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
| `tradeloop/chain_recorder.py` | Option-chain recorder: volume, OI, IV, Greeks once a minute (`data/chain.db`) |
| `tradeloop/backfill.py`, `greeks.py`, `derive_greeks.py`, `check_history.py` | Historical candles, IV/Greeks, quality check |
| `tradeloop/ledger.py`, `charges.py`, `lot_sizes.py`, `fill_model.py` | Ledger, dated charges and lot sizes, backtest fills |
| `tradeloop/engine.py`, `sim_broker.py`, `backtest.py`, `strategies/` | Engine, backtest broker and runner, strategy code; configs in `strategies/` |
| `tradeloop/paper.py`, `paper_broker.py`, `paper_store.py`, `risk.py` | Paper trading on the live feed, risk gate |
| `research/` | Studies, research log, strategy backlog, memos |
| `CLAUDE.md` | Architecture rules and next steps, for Claude Code |

## Next

Paper trade the put credit spread for 30 trades (from 2 Nov 2026), review, then decide on real money. Meanwhile, research the next candidates in `research/STRATEGIES.md`. See `CLAUDE.md`.
