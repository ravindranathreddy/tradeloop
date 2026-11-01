"""Expiry-day afternoon put spread. Declared in research/LOG.md (1 Nov 2026, "expiry-day afternoon put spread").

    python research/afternoon_spread_study.py              # development: NIFTY 2020-2024
    python research/afternoon_spread_study.py --holdout    # 2025-26; once, only if the development test passed

Enter at 13:30 on the expiry day: sell a put k% below the index, buy one 1.5% further below, hold to the
official close. The live strategy enters only before expiry day, so this study uses its own subclass.
Rs per lot, after charges and slippage.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from spread_variants_study import run_weeks
from tradeloop.strategies import REGISTRY
from tradeloop.strategies.credit_spreads import CreditSpread

KS = (0.75, 1.0, 1.5)
MAIN_K, WIDTH = 1.0, 1.5
MIN_AVG, WORST_FLOOR = 100.0, -20_000
rng = np.random.default_rng(11)


class ExpiryDaySpread(CreditSpread):
    def on_minute(self, ctx) -> None:
        if self.state == "waiting":
            if ctx.days_to_expiry == 0 and ctx.time >= self.entry:
                self._enter(ctx)
            return
        super().on_minute(ctx)


REGISTRY["expiry_day_spread"] = lambda cfg: ExpiryDaySpread({**cfg, "option_type": "PE"})


def boot(x: np.ndarray, n: int = 5000) -> tuple[float, float]:
    m = rng.choice(x, (n, len(x))).mean(axis=1)
    return float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--history", default="data/history")
    p.add_argument("--holdout", action="store_true")
    a = p.parse_args()
    tag = "holdout" if a.holdout else "dev"
    w = pd.read_csv(f"data/research/vol_filter_{tag}.csv")[["expiry"]]
    w["year"] = w["expiry"].str[:4]
    for k in KS:
        res = run_weeks(Path(a.history), list(w["expiry"]), lambda cfg, e, k=k: {
            **cfg, "strategy": "expiry_day_spread", "strategy_id": f"afternoon_{k}", "entry_time": "13:30",
            "enter_within_days": 1, "short_otm_pct": k, "long_otm_pct": k + WIDTH})
        w[f"k{k}"] = w["expiry"].map(res)
    w.to_csv(f"data/research/afternoon_spread_{tag}.csv", index=False)
    for k in KS:
        x = w[f"k{k}"]
        traded = x != 0
        lo, hi = boot(x.to_numpy())
        yrs = x.groupby(w["year"]).sum()
        print(f"\nsold {k}% below at 13:30{'  (named main variant)' if k == MAIN_K else ''}:")
        print(f"  weeks traded {int(traded.sum())} of {len(x)}; avg Rs {x.mean():,.0f} per week (95% {lo:,.0f}..{hi:,.0f}), "
              f"worst Rs {x.min():,.0f}, wins {(x > 0).sum() / max(int(traded.sum()), 1):.0%}")
        print("  years: " + ", ".join(f"{y} {v:,.0f}" for y, v in yrs.items()))
        a_, b_, c_, d_ = x.mean() >= MIN_AVG, lo > 0, x.min() >= WORST_FLOOR, int((yrs > 0).sum()) >= 4
        print(f"  (a) avg >= {MIN_AVG:.0f}: {a_}  (b) range lower bound > 0: {b_}  (c) worst >= {WORST_FLOOR:,}: {c_}  "
              f"(d) >= 4 years positive: {d_}  -> {'PASS' if a_ and b_ and c_ and d_ else 'FAIL'}")


if __name__ == "__main__":
    main()
