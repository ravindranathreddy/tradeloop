# Strategy backlog

Candidate strategies to research, with the reason each might work, its risk shape, and
whether our data can test it. Findings go in LOG.md; this file is the plan. Every test
counts toward the variants tried, and nothing trades without passing the held-out
2025-2026 test, paper trading and Ranga's approval.

## What the evidence says before we start

- SEBI: over 91% of individual F&O traders lost money in FY24 and FY25 (FY25 net loss
  about Rs 1.05 lakh crore); FY24 profits went mostly to algorithmic FPIs and prop
  desks, and the average individual spent Rs 26,000 a year on transaction costs.
  Popular retail setups are what most of those traders run.
- Index option sellers earn a volatility risk premium on average (CBOE PUT index
  research; NIFTY studies report the same), but the losses cluster in volatility
  shocks. The premium is largest in far out-of-the-money, short-dated puts (SPX 0-5
  day study). Our study 5 agrees for NIFTY: far puts 1-2 days out paid back 0.50.
- Popular signals (OI walls, opening range breakouts, "sell when IV is high") failed
  our studies 2-4. Edges here are in pricing and risk shape, not prediction.

## The trade-off to keep in mind

High win rate and good risk:reward pull against each other: a fairly priced option has
zero expected value at any win rate. What matters is average profit per trade after
costs, the worst trades, and stability across years. A strategy is worth having only
where the price is wrong (positive average) and the loss is capped (survivable).

## Backlog

Status: testing / ready (data in hand) / needs data / parked.

| # | Strategy | Why it might work (who overpays) | Risk shape | Status |
|---|---|---|---|---|
| 1 | Credit spreads 1-2 days out (bull put, bear call) | Far options overpriced: crash insurance and lottery demand (study 5) | ~95% win, capped loss | testing |
| 2 | Iron condor 1-2 days out | Both wings overpriced; only one side can lose | ~93% win, capped | testing |
| 3 | Skew-weighted condor: put side wider or closer than call side | Puts more overpriced than calls (study 5: 0.50 vs 0.73) | capped | ready |
| 4 | Broken-wing butterfly (put side) | Collect skew premium with little or no cost if the market rises | capped, asymmetric | ready |
| 5 | Volatility regime filter on 1-3 (skip or size down when IV is very high or rising) | Seller losses cluster in shocks; IV regimes persist | overlay | ready (our own ATM IV series) |
| 6 | Day after a big move (>1.5%): condor or spreads | IV overshoots after shocks, then mean-reverts | capped | ready |
| 7 | Event filter: skip selling before RBI policy, budget, election results, US Fed | Removes known tail days; tests whether events cause the losses | overlay | needs event calendar |
| 8 | Event IV crush: sell spreads right after the event | IV drops once uncertainty resolves | capped | needs event calendar |
| 9 | Expiry-day afternoon spreads (enter 13:00-14:00) | Decay is fastest late on expiry day; less time for big moves | capped, small | ready |
| 10 | NIFTY vs SENSEX: sell the richer one (different expiry days) | Same market, prices can differ | capped | ready (SENSEX from Aug 2023) |
| 11 | Calendar spread: sell this week, buy next week | Near-week decays faster; the long leg is tail protection | capped-ish, vega risk | needs next-week contracts |
| 12 | Calendar ratio strangle (published NIFTY bot) | Theta harvest with next-week tail protection | complex | needs next-week contracts |
| 13 | Debit spreads in a trend (e.g. above the 20-day average) | Momentum; cheaper than buying options | capped, ~35-45% win | parked: prediction, low prior after study 3 |
| 14 | Gap fade with debit spreads | Overreaction at the open | capped | parked: same family as study 3 |
| 15 | Short straddle / strangle, naked | VRP | unlimited | rejected as a live rule (study 1 tail); only as a component with wings |

Overlays that improve any surviving strategy rather than being strategies:
- position sizing by volatility (smaller when IV is high; published put-writing work
  compares Kelly and VIX-based sizing);
- stop-loss / early exit rules (does closing at 2x credit lose more than it saves?);
- spreading entries across days instead of one entry per week.

## Order of work

1. Results of 1-2 (running).
2. 3 and 4 (skew), then 5 and 6 (regimes), then 9 and 10: all data-ready.
3. Build an event calendar 2020-2026 from official sources (RBI MPC decision dates,
   Union Budget, general election results, FOMC) for 7 and 8.
4. Download next-week contracts for 11 and 12 if earlier results justify the cost.

## Sources

- CBOE PUT index and the volatility premium: https://www.cboe.com/insights/posts/white-paper-shows-volatility-risk-premium-facilitated-higher-risk-adjusted-returns-for-put-index/
- Put-writing sizing (Kelly vs VIX): https://arxiv.org/pdf/2508.16598
- Noisy option prices and the volatility premium (OTM put puzzle): http://faculty.marshall.usc.edu/Christopher-Jones/pdf/duarte_jones_wang_2022.pdf
- Selling volatility, the backtest trap: https://www.quantt.co.uk/resources/selling-volatility-premium
- SEBI FY25 study coverage: https://www.business-standard.com/amp/markets/news/net-losses-of-traders-in-fo-widens-in-fy25-sebi-study-125070701221_1.html
- SEBI FY25 study (91% lost money): https://moneylife.in/article/91-percentage-of-retail-traders-lost-money-in-derivatives-losses-in-fo-surged-41-percentage-to-rs105-lakh-crore-in-fy25-sebi-study/77613.html
- NIFTY calendar ratio strangle bot: https://github.com/kunalrbhatia/nifty-weekly-calendar-ratio-strangle
- NIFTY option-selling backtester (author's claims, unverified): https://github.com/shivam61/nifty-options-backtester
