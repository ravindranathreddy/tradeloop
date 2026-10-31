# Research log

Every study, what was tried and what came out, including dead ends. Counting variants
matters: the more variants tried, the stronger a result must be to count (see the
research method in CLAUDE.md). Development years: 2020-2024. Held out: 2025-2026.

Money is per lot, before charges unless stated; a straddle round trip costs roughly
Rs 150-250 per lot in charges and slippage.

## Study 1: is the ATM straddle priced for bigger moves than happen? (27 Oct 2026)

`research/straddle_study.py`. Sell the ATM straddle at 09:20; value it at settlement
(expiry day) or 15:15 (other days). All years, all trading days: NIFTY 1,678 days,
SENSEX 779.

- Sellers win 55-72% of days, but the average is near zero: NIFTY expiry day +Rs 184/lot
  before costs, i.e. roughly nothing after them. Typical win +3,259, typical loss -4,451.
- Expiry-day straddle settles at 81% of its 09:20 price on the median day, 100% on
  average: frequent small wins, rare large losses.
- Actual move > 2x the straddle on 1 in 10 expiry days. Worst: -30,458 (NIFTY 17 Apr
  2025, expiry day); -63,191 (NIFTY 13 Mar 2020, lower-circuit day, 6 days to expiry).
- By year (expiry day, NIFTY): 2020-2023 about zero or negative, 2024-2026 positive.
  Not stable. (Study 1 showed the held-out years; later studies don't.)

Conclusion: blind straddle selling has no edge after costs and a severe tail.

## Study 2: does selling work better when options are expensive? (27 Oct 2026)

`research/iv_rich_study.py`. Development years only. Variants tried: 2 yardsticks x
5 fixed buckets x 2 indices x (expiry day, other days) = up to 40 cells.

- Yardstick 1 (IV / 10-day realised vol): fails on expiry day (215 of 259 NIFTY expiry
  days in the top bucket: final-morning IV is always high). Other days: no pattern.
- Yardstick 2, expiry day (straddle % / average 09:20-to-close move of the previous 10
  days): no pattern, slightly the wrong way. Correlation with the seller's result -0.10
  (NIFTY), -0.12 (SENSEX). On "expensive" days the actual move was 1.11x the straddle
  (NIFTY), i.e. bigger, not smaller.
- The best cell (NIFTY, ratio 0.9-1.1, +772/lot, 95% range 22..1,523) is one of ~40
  tried and sits between two negative neighbours: treated as noise.

Conclusion: rejected. Expensive straddles are expensive for a reason; the market
prices big days about right or too low. No selling filter from this yardstick.

## Study 3: opening range breakout with bought options (27 Oct 2026)

`research/orb_study.py`. Development years only. Breakout = first index close beyond the
first N minutes' high/low, before 12:00; buy the ATM option in that direction at the next
minute's open, sell at 15:15. Control: the opposite option, same strike and times.
Costs included (about Rs 108 per NIFTY trade). Variants: N {15, 30} x days to expiry
{0, 1-2, 3+} x 2 indices = 12 cells.

- Breakouts continue to 15:15 on 47-59% of days: close to a coin flip.
- Bought option wins on 29-43% of trades; average after costs between -354 and +290 per
  lot in every cell. No cell is profitable with any confidence.
- Breakout side beats the control in 9 of 12 cells, but every 95% range includes zero.
- Strongest hint: NIFTY expiry day, continuation 55-57%, trade beats control by ~610
  (both N). But the trade itself is -20 / +52 after costs, and by year it fades:
  2020 +497, 2021 +34, 2022 +277, 2023 -469, 2024 -459 (15m). Decaying, not stable.

Conclusion: rejected as a rule. Possible weak information in expiry-day breakouts that
doesn't pay for the option's time decay plus costs.

## Study 4: does OI build-up predict where the index settles? (27 Oct 2026)

`research/oi_walls_study.py`. Expiry days, development years only. The Sensibull reading:
put wall = strike at/below the index with the most put OI (change since the previous
close, or total), call wall = same for calls above; does the official close land between
them? Control: a band of the same width centred on the index at the check time.
Variants: {change, total} x {10:30, 12:30} x 2 indices = 8 cells.

- The OI band never beats the control. NIFTY: OI band catches the close 58-69% of days,
  the plain band 63-76%: worse by 5-8 points, and for total OI the 95% range excludes
  zero (-13..-2, -12..-3). SENSEX: -4 to 0 points, ranges include zero.
- The close is nearer the index at the check time than the OI walls' midpoint on 57-63%
  of days.

Conclusion: rejected, and informative: OI walls do not mark where the index settles;
the current price is a better centre for an expected range than OI is. "Support and
resistance from OI" should not drive strike choice.

## Study 5: which options are mispriced? (27 Oct 2026)

`research/mispricing_study.py`. Development years only. Every option within 3% of the
index at 09:20, held to expiry: payout ratio = total paid out / total premium (1.00 fair).
95% ranges over expiry weeks (days in a week share a settlement). Variants: 6 distance
buckets x 2 types x 3 day groups x 2 indices = 72 cells.

- Near the money: fairly priced (NIFTY 0.88-1.07, all ranges include 1).
- Far out of the money on expiry day: heavily overpriced, calls and puts alike (NIFTY
  2-3% out: 0.23 and 0.22). But they cost Rs 1-2, so after costs the seller makes
  Rs 8-72 per lot: the overpricing is real, the money isn't.
- 1-2 days before expiry, 2-3% out, NIFTY: calls 0.73 (0.54..0.93), puts 0.50
  (0.28..0.72), both ranges below 1. Seller after costs: calls +73, puts +356 per lot.
  Puts positive every development year: 2020 +270, 2021 +587, 2022 +168, 2023 +223,
  2024 +487. Worst single option: -39,626 per lot (11 Mar 2020), -34,796 (23 Feb 2022).
- Caveat: 2020-2024 rose strongly (NIFTY ~12,000 to ~23,600). In a rising market puts
  look overpriced and calls fair or cheap; SENSEX (2023-24 rally) shows calls paying
  out more than their price near the money. Trend explains part of the put result;
  the call result (also below 1 far out) does not depend on trend.

Conclusion: candidate, with a stated reason (buyers overpay for far crash insurance and
lottery tickets). Far OTM puts 1-2 days before expiry are the best risk/reward found,
but the tail is severe (one bad day = ~100 good ones). Next: backtest it as a put
credit spread (buying a further put caps the tail), then judge once on 2025-2026.

## Backtest 1: credit spreads and iron condors, 1-2 days before expiry (27 Oct 2026)

Engine backtests (real fills via the fill model, all charges, overnight hold, settlement
at the official close). NIFTY, development years 2020-2024, one trade per expiry week,
1 lot. Protection 2% further out than the sold strike. 14 variants, declared before
running: {put spread, call spread} x enter {1, 2} days before x sold strike {2, 2.5, 3}%
below/above, plus condors (2.5%) entering 1 or 2 days before.

| variant | trades | win | avg/trade | 95% range | avg win | avg loss | worst | years + |
|---|---|---|---|---|---|---|---|---|
| put, 2 days, 2.0% | 261 | 91% | +147 | -158..+451 | +728 | -5,870 | -16,805 | 4/5 |
| put, 2 days, 2.5% (pre-declared base) | 260 | 90% | +170 | -51..+391 | +512 | -3,041 | -13,489 | 5/5 |
| put, 2 days, 3.0% | 260 | 84% | +168 | -12..+348 | +414 | -1,112 | -13,367 | 5/5 |
| put, 1 day, 2.0 / 2.5 / 3.0% | ~246 | 89/79/66% | -46 / +9 / -21 | all include 0 | | | ~-17,000 | 2-3/5 |
| call, 2 days, 2.0 / 2.5 / 3.0% | ~257 | 85/72/55% | +45 / +22 / -46 | all include 0 | | | ~-14,000 | 2-4/5 |
| call, 1 day | ~241 | 33-69% | -116 to -145 | all include 0 | | | ~-14,000 | 1-2/5 |
| condor, 2 days, 2.5% | 261 | 81% | +191 | -80..+462 | +784 | -2,373 | -13,087 | 4/5 |
| condor, 1 day, 2.5% | 247 | 67% | -132 | -415..+152 | | | -16,865 | 2/5 |

- Entering 2 days before beats 1 day before in every structure. With 1 day left the
  premium is small and costs (~Rs 100/trade) eat it.
- Put spreads entering 2 days before: all three strikes positive (+147 to +170), every
  year positive at 2.5% and 3.0%, worst trade about -13,400 (capped as designed). The
  neighbours agree, which is what a real effect looks like.
- But: every 95% range includes or touches zero; 14 variants here on top of earlier
  studies; 2020-2024 was a strong bull market, which flatters put selling. Call spreads
  (the trend-neutral check) are only marginally positive.
- Risk shape of the base: wins +512 on average, about 26 wins to repay one worst trade.
  Max drawdown about -22,600 per lot.

Conclusion: first candidate with consistent signs (put credit spread, 2 days before,
2.5% below, 2% protection), but not proven. Before the one-time held-out test: finish
the development-side checks (volatility regime filter, event filter), fix the final
configuration, and only then open 2025-2026 once.

## Pre-registration: held-out test of the put credit spread (27 Oct 2026, before running)

Frozen configuration: strategies/put_credit_spread.yaml as committed (NIFTY, 1 lot,
09:20 entry on the first day with 1-2 calendar days to expiry, sell put 2.5% below,
buy put 4.5% below, hold to expiry). Data: NIFTY expiries 2025-01-01 to 2026-10-27,
opened once. Measured in index points per trade after all charges (lot sizes differ).

Development result for reference: 260 trades, +3.36 points/trade, 90% wins.

- PASS: average >= +1.7 points/trade (half the development average) and win rate >= 80%.
- FAIL: average <= 0.
- INCONCLUSIVE: anything between; then paper trading is the next unseen test.
Secondary (reported, not decisive): SENSEX over the same held-out period.

## Held-out result: put credit spread, 2025-2026 (27 Oct 2026)

Run once with the frozen config (commit 1e30e89, config 522ebac1e403).

- NIFTY: 87 trades, +6.67 points/trade after charges (95% range +4.10..+9.25), 86% wins,
  worst trade -0.4 points (charges only), total Rs 40,988 (1 lot). 2025 +7.03, 2026 +6.15
  points/trade. PASS against the pre-registered rule (>= +1.7 and >= 80% wins).
- SENSEX (secondary): 90 trades, +23.3 points/trade (SENSEX points are ~3.3x NIFTY's),
  94% wins, worst -0.6 points, total Rs 41,490.
- Caution: a favourable period. NIFTY fell more than 2% during the hold in only 2 of 87
  weeks (development years: 21 of 260, 8%), and both of those falls happened mostly
  before the 09:20 entry, so the spread was sold after the drop at richer premiums (Apr
  2025 tariff crash: +6,263). The capped-loss behaviour was not tested by this period;
  2020-2024 is the evidence for that (worst -13,489 per lot).
- 7 weeks had no trade: holiday-shifted expiries left no day with 1-2 days to go.

Conclusion: passes the held-out test. Expect long-run results nearer the development
average (+3.4 points/trade) than this calm period's +6.7. Next: a plain-language memo
for Ranga, then paper trading (needs step 7: paper broker on the live feed).

## Robustness: put credit spread, development years (27 Oct 2026)

Declared before running (base: 2 days before, 2.5% below, protection 2% further, 09:20):
change one setting at a time. Points per trade after charges.

| change | trades | avg | 95% range | wins | years + |
|---|---|---|---|---|---|
| base (for reference) | 260 | +3.36 | +0.58..+6.18 (bootstrap 90%) | 90% | 5/5 |
| entry 10:00 | 257 | +4.11 | +0.98..+7.25 | 90% | 5/5 |
| entry 11:00 | 253 | +2.53 | -1.05..+6.12 | 89% | 5/5 |
| protection 1.5% further | 261 | +2.39 | -0.95..+5.72 | 89% | 5/5 |
| protection 3% further | 256 | +4.81 | +0.52..+9.10 | 93% | 5/5 |
| SENSEX (Aug 2023-2024 only) | 56 | +3.72 SENSEX pts | -16.8..+24.2 | 80% | 1/2 |

Every NIFTY change stays positive in every year; size varies (+2.4 to +4.8). SENSEX's
development sample is too short to say much (its held-out result was strong). Supports
the effect being real rather than one lucky setting. Base config unchanged.

## Open ideas

- Capped-risk selling (iron condor) vs straddle: what does capping the tail cost?
- Expiry-day breakout via a spread instead of a bought option (less time decay to pay);
  only with a stated reason, and counting study 3's 12 cells against it.
- Exit timing: does closing a sold straddle earlier (e.g. 13:00) change the tail?
- Event days (RBI, budget): needs an event calendar.
- OI as a filter rather than a level (e.g. very lopsided PCR days): only with a reason.

## Pre-registration: volatility filter on the put credit spread (31 Oct 2026, before running)

Question: do the few big losses of the frozen spread (strategies/put_credit_spread.yaml,
NIFTY) cluster on days when volatility was already high at the 09:20 entry? Reason it
might: a sold put 2.5% below is ~1.9 expected-move standard deviations away at 15% IV
but only ~1.1 at 25% IV, so the spread is much more likely to be hit when IV is high.
Reason it might not: high-IV days also pay a richer premium.

Two filters, one threshold each, round numbers fixed now (variants tried: 2):
- A (primary): skip the week if ATM implied volatility at the 09:20 entry is above 20%.
  ATM IV = mean of call and put IV at the strike nearest the index, forward from put-call
  parity (same code as derive_greeks).
- B (alternative): skip if realised volatility of the previous 10 daily closes is above 20%
  (annualised). No option prices needed.

Develop on NIFTY 2020-2024 (the 260-trade backtest, charges and slippage included).
A filter PASSES development if all of:
  (a) it skips at least half of the weeks that lost more than Rs 5,000 per lot;
  (b) the weeks it keeps earn at least 90% of the unfiltered total net P&L;
  (c) it skips no more than 30% of the weeks.
Only a filter that passes is judged once on NIFTY 2025-26 (held out; already opened once
for the base rule, so this counts for less): it must keep at least 85% of that period's
total net P&L. That period was calm (2 falls over 2% in 87 weeks), so it can show the
filter does not cost much but cannot show it protects. With few big-loss weeks in
development, even a pass is weak evidence; adopt only with Ranga's approval, and paper
trading is the next unseen test. If neither passes, the answer is "no filter".

## Result: volatility filter, development 2020-2024 (31 Oct 2026)

`research/vol_filter_study.py`, NIFTY, 260 traded weeks, Rs per lot after charges and slippage.
Unfiltered: total Rs 44,217, 7 weeks lost more than Rs 5,000, worst Rs -13,489.

| filter | weeks skipped | kept total | big losses skipped | verdict |
|---|---|---|---|---|
| A: ATM IV at entry > 20% | 96 (37%) | Rs 16,796 (38%) | 6 of 7 | FAIL (b, c) |
| B: 10-day realised vol > 20% | 39 (15%) | Rs 28,993 (66%) | 3 of 7 | FAIL (a, b) |

Reading: the reason "it might not" held. High-IV weeks are where the spread earns most: the
96 weeks filter A skips made Rs 27,421, 62% of all profit. Filter A does catch 6 of the 7 big
losses (worst week drops from -13,489 to -7,007) but costs over 60% of the income, so it is
a different, much smaller strategy, not a free improvement. Filter B misses over half the
big losses (calm-looking markets before a fall) and still gives up a third of the profit.
Seven big-loss weeks is too few to learn a threshold from.

Conclusion: no volatility filter. The held-out period was not opened (variants tried: 2).
Not tried, and not to be tuned now: other thresholds, a sizing rule (smaller lots at high IV
instead of skipping). A sizing rule is the one idea this result points at; it needs its
own pre-registration. The big-loss risk stays as designed: capped at Rs 14-27k per lot.

## Pre-registration: two ways to cut big losses without skipping weeks (31 Oct 2026, before running)

Follows the volatility-filter result: high-IV weeks earn most of the profit, so skipping them
fails. Both ideas below keep trading every week. Both came from reasoning, before the loss
dates were looked at again; but the 7 worst weeks of 2020-24 had already been seen in the
filter study (Sep 2020, Mar 2020 x2, Mar 2021, Jan 2022, Feb 2022, May 2022), so treat this
as less clean than a first look. Base: the frozen spread, NIFTY 2020-2024, 260 weeks,
Rs per lot after charges and slippage.

Test 1, strike distance scaled to the expected move. Expected move EM% = ATM IV at the 09:20
entry x sqrt(time to expiry in years). Short put distance = k x EM%, never closer than 2.5%
and never further than 4.0%; protection stays 2.0% further out (same width, so the cap on
the loss is about the same). k is fixed from entry data alone, with no P&L: the median over
2020-24 of 2.5 / EM%, so a median week stays at 2.5% and jumpier weeks move out. One setting.
Strikes are the nearest listed, as the strategy already picks them.

Test 2, early exit. Base strikes. While holding, if the index is within 1.0% of the sold strike
(index <= strike x 1.01 for a put) at any minute of the session: buy back the short, then sell
the protection, at market with the usual slippage. No re-entry that week. One setting.

Each is judged alone against the base. PASS in development needs all of:
  (a) the loss in the base's big-loss weeks (net < -Rs 5,000) shrinks by at least 40%;
  (b) total net P&L over all weeks is at least 90% of the base's;
  (c) the worst single week is no worse than the base's.
Reported too, not decisive: weeks changed, weeks where the rule hurt, and for test 2 the
number of exits and how many were in weeks the base won ("false alarms").
A rule that passes is judged once on 2025-26 (already opened once; calm period): total net
at least 85% of the base's Rs 40,988. Then paper trading is the unseen test. Combining the
two is a third variant and needs a new pre-registration. Variants tried: 2.

Test 3, event filter (planned, not yet testable). Needs an event calendar built from published
schedules and checked by Ranga: RBI policy dates, Union Budget, US Fed meetings, election
result days. Rule to pre-register once it exists: skip a week when an event falls between
entry and expiry. Must also say how many of the weeks it skips are weeks the base won.

## Result: expected-move strikes and early exit, development 2020-2024 (31 Oct 2026)

`research/spread_variants_study.py`, NIFTY, 260 weeks, Rs per lot after charges and slippage.
Base: total Rs 44,217, worst week -13,489, the 7 big-loss weeks sum to -67,815.
Test 1 k = 1.804 (from entry data); 50% of weeks moved out, up to 4.0%.

| variant | total P&L | big-loss weeks | worst week | weeks worse / better | verdict |
|---|---|---|---|---|---|
| base | 44,217 | -67,815 | -13,489 | | |
| 1: strike = k x expected move | 37,279 (84%) | -33,288 (shrinks 51%) | -14,039 | 105 / 13 | FAIL (b, c) |
| 2: exit within 1% of sold strike | 30,191 (68%) | -20,958 (shrinks 69%) | -10,164 | 46 / 12 | FAIL (b) |

Reading: both do what they were meant to, and both pay for it. Test 1 halves the big losses
for 16% of the profit, but gives up Rs 50,094 of premium across 105 weeks to save Rs 43,157
in 13. Test 2 cuts them by 69% and the worst week by a quarter, but 46 exits in weeks the
base won cost Rs 64,649 against Rs 50,622 saved: early exits are triggered by dips that
recover. Neither is a free improvement. Test 1 fails (b) narrowly (84% vs the 90% line set
in advance); the line is not moved after seeing the result.
Held-out period not opened (variants tried: 2, plus the 2 in the filter study).
If wanted, test 1 could run in paper as a separate strategy id: that is unseen data, and
Ranga's call. Combining the two, a different width, or other thresholds are new variants.
Code kept: `exit_within_pct` in the credit spread (off by default; paper config unchanged).
