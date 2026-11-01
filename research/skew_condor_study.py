"""Skew-weighted condor: the put credit spread plus a call credit spread sold farther out.
Declared in research/LOG.md (1 Nov 2026, "skew-weighted condor").

    python research/skew_condor_study.py              # development: NIFTY 2020-2024
    python research/skew_condor_study.py --holdout    # 2025-26; once, only if the development test passed

The two sides never share a position, so each runs as its own week and the condor is their sum.
Base put P&L comes from data/research/vol_filter_{dev,holdout}.csv. Rs per lot, after charges and slippage.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from spread_variants_study import run_weeks

CALL_SHORT = (3.0, 3.5)
WIDTH = 2.0
PUT_AVG, WORST_FLOOR = 170.0, -17_000
rng = np.random.default_rng(7)


def boot(x: np.ndarray, n: int = 5000) -> tuple[float, float]:
    m = rng.choice(x, (n, len(x))).mean(axis=1)
    return float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--history", default="data/history")
    p.add_argument("--holdout", action="store_true")
    a = p.parse_args()
    tag = "holdout" if a.holdout else "dev"
    w = pd.read_csv(f"data/research/vol_filter_{tag}.csv").rename(columns={"net": "put"})
    w["year"] = w["expiry"].str[:4]
    print(f"{len(w)} weeks, put spread total Rs {w['put'].sum():,.0f}, avg Rs {w['put'].mean():,.0f}")
    for k in CALL_SHORT:
        calls = run_weeks(Path(a.history), list(w["expiry"]), lambda cfg, e, k=k: {
            **cfg, "strategy": "credit_spread", "strategy_id": f"call_spread_{k}", "option_type": "CE",
            "short_otm_pct": k, "long_otm_pct": k + WIDTH})
        w[f"call{k}"] = w["expiry"].map(calls)
        w[f"condor{k}"] = w["put"] + w[f"call{k}"]
    w.to_csv(f"data/research/skew_condor_{tag}.csv", index=False)
    for k in CALL_SHORT:
        c, d = w[f"call{k}"], w[f"condor{k}"]
        lo, hi = boot(d.to_numpy())
        yrs = d.groupby(w["year"]).sum()
        print(f"\ncall short {k}% above:")
        print(f"  call side alone: avg Rs {c.mean():,.0f} (worst Rs {c.min():,.0f}), wins {(c > 0).mean():.0%}")
        print(f"  condor: avg Rs {d.mean():,.0f} (95% {lo:,.0f}..{hi:,.0f}), worst Rs {d.min():,.0f}, "
              f"wins {(d > 0).mean():.0%}; years: " + ", ".join(f"{y} {v:,.0f}" for y, v in yrs.items()))
        a_, b_, c_, d_ = d.mean() >= PUT_AVG, c.mean() > 0, d.min() >= WORST_FLOOR, int((yrs > 0).sum()) >= 4
        print(f"  (a) avg >= {PUT_AVG:.0f}: {a_}  (b) call side positive: {b_}  (c) worst >= {WORST_FLOOR:,}: {c_}  "
              f"(d) >= 4 years positive: {d_}  -> {'PASS' if a_ and b_ and c_ and d_ else 'FAIL'}")


if __name__ == "__main__":
    main()
