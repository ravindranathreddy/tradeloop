"""Event filter on the put credit spread. Declared in research/LOG.md (31 Oct 2026).

    python research/event_filter_study.py              # development: NIFTY 2020-2024
    python research/event_filter_study.py --holdout    # 2025-26; once, only if the filter passed

Skip a week if an event (events.yaml) reacts after the 09:20 entry and by the expiry close.
Weeks and base P&L come from data/research/vol_filter_{dev,holdout}.csv.
"""
from __future__ import annotations

import argparse
from datetime import date

import pandas as pd

from tradeloop.core.events import default_events

BIG_LOSS = -5000


def counts(e, entry: date) -> bool:
    return e.effect > entry or (e.effect == entry and e.when == "in_session")


def flag(w: pd.DataFrame, verified_only: bool = False) -> pd.Series:
    events = [e for e in default_events().events if not (verified_only and e.source == "recalled")]
    out = []
    for day, expiry in zip(w["day"], w["expiry"]):
        entry, exp = date.fromisoformat(day), date.fromisoformat(expiry)
        out.append(any(counts(e, entry) and e.effect <= exp for e in events))
    return pd.Series(out, index=w.index)


def report(w: pd.DataFrame, skip: pd.Series, title: str) -> str:
    base_total, big = w["net"].sum(), w["net"] < BIG_LOSS
    keep = w[~skip]
    skipped_big = int((skip & big).sum())
    a, b, c = big.sum() > 0 and skipped_big >= big.sum() / 2, keep["net"].sum() >= 0.9 * base_total, skip.mean() <= 0.30
    won = int((skip & (w["net"] > 0)).sum())
    lines = [f"\n{title}: skips {int(skip.sum())} of {len(w)} weeks ({skip.mean():.0%}); kept total Rs {keep['net'].sum():,.0f} "
             f"({keep['net'].sum() / base_total:.0%} of Rs {base_total:,.0f}); kept worst Rs {keep['net'].min():,.0f} "
             f"(base Rs {w['net'].min():,.0f})",
             f"  big-loss weeks skipped {skipped_big} of {int(big.sum())}; skipped weeks the base won {won} "
             f"(they earned Rs {w.loc[skip & (w['net'] > 0), 'net'].sum():,.0f}); skipped weeks' total Rs {w.loc[skip, 'net'].sum():,.0f}",
             f"  (a) skips >= half the big losses: {a}   (b) keeps >= 90% of P&L: {b}   (c) skips <= 30%: {c}"
             f"   -> {'PASS' if a and b and c else 'FAIL'}"]
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--holdout", action="store_true")
    a = p.parse_args()
    tag = "holdout" if a.holdout else "dev"
    w = pd.read_csv(f"data/research/vol_filter_{tag}.csv")
    print(report(w, flag(w), "All events"))
    print(report(w, flag(w, verified_only=True), "Verified events only"))
    big = w[w["net"] < BIG_LOSS].copy()
    print("\nThe big-loss weeks and whether the rule would have caught them:")
    big["caught"] = flag(big)
    print(big[["expiry", "day", "net", "caught"]].sort_values("net").to_string(index=False))


if __name__ == "__main__":
    main()
