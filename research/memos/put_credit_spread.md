# Memo: put credit spread (paper trading from 2 Nov 2026)

Status: paper trading only. No real money until Ranga reviews the paper results.
Config: strategies/put_credit_spread.yaml (config version 522ebac1e403, frozen in 1e30e89).

## What it does
Every week, on the first trading day with 1-2 days left before NIFTY's expiry (Monday
for a Tuesday expiry), at 09:20: buy a put ~4.5% below NIFTY as protection, then sell a
put ~2.5% below. Hold to expiry; both settle at the official close. It earns the
premium if NIFTY doesn't fall more than ~2.5% in those two days.

Why it should work: far out-of-the-money puts are overpriced; buyers pay for crash
insurance (study 5: they paid back 0.50 of their price, 2020-2024).

## Evidence
- 2020-2024 (development): 260 trades, +3.36 points/trade after charges, 90% wins,
  every year positive, worst trade -13,489 per lot.
- 2025-2026 (held out, opened once): 87 trades, +6.67 points/trade, 86% wins. Passed.
  A calm period: only 2 weeks with a >2% fall during the hold.
- Loses in sharp falls during the hold (about 1 week in 12 in 2020-2024), capped.
- Robust to its settings: entry at 10:00 or 11:00, protection 1.5% or 3% further out:
  +2.4 to +4.8 points/trade, every year positive in every case.

## Money (1 lot, lot size 65)
- Margin: Rs 59,803 (Groww's calculator, 27 Oct 2026). Keep ~Rs 1 lakh per lot.
- Expected: ~Rs 218 per trade, ~Rs 11,000 a year (about 11% on Rs 1 lakh).
- Worst seen: -Rs 14,176 in a week; -Rs 19,531 peak to trough. Worst possible in one
  week: about -Rs 27,000 (the strike gap minus the credit).

## Paper-trading rules, fixed before the first trade
- Runs inside the recorder; fills at the live order book; 1 lot; no real orders.
- Review after 8 trades and after 30 trades, comparing with the backtest.
- Stop and investigate at once if: any order fills without its protection, any loss
  exceeds the cap, or fills are worse than the backtest's slippage by more than half
  the credit on average.
- Drop if, after 30 trades, the average per trade after charges is below zero.
- Real money is a separate decision by Ranga after the 30-trade review (at least 4
  weeks, per the plan), starting with 1 lot.

## SENSEX: second paper trial (lower confidence)

Same rule on SENSEX (strategies/put_credit_spread_sensex.yaml), paper only, at Ranga's
request: no money at risk, and it doubles the paper trades. Evidence is thinner: SENSEX
history starts Aug 2023 (2023: 12 trades, -0.8 points/trade; 2024: 44 trades, +5.0);
held out 2025-2026: 90 trades, +23 points/trade, 94% wins, in the same calm period.
Enters Tuesdays for Thursday expiries; first paper trade Tue 3 Nov 2026. Judged
separately from NIFTY with the same review and drop rules; a good SENSEX paper result
alone would not justify real money given the short history.
