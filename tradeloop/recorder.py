"""Tick recorder: index values, option LTPs and top-of-book for the nearest weekly expiry.

Run once per trading day (cron at ~9:05 IST). It logs in, waits for spot,
picks strikes around ATM, then polls the feed and writes to SQLite until
the configured end time. Strikes are re-centred if spot drifts far.

    python -m tradeloop.recorder --config config.yaml
"""
import argparse
import logging
import signal
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import yaml
from growwapi import GrowwFeed

from tradeloop import feed_parse
from tradeloop.auth import login
from tradeloop.instruments import load_instruments, pick_contracts
from tradeloop.store import Store

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("recorder")


def now_ms() -> int:
    return int(time.time() * 1000)


class Recorder:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.store = Store(cfg["db_path"])
        self.groww = login()
        self.feed = GrowwFeed(self.groww)
        self.instruments = load_instruments()
        self.subscribed: set[tuple[str, str]] = set()   # (exchange, token)
        self.atm_spot: dict[str, float] = {}            # underlying -> spot used for selection
        self.stop = False

    # --- subscriptions ---------------------------------------------------
    def subscribe_indices(self):
        items = [{"exchange": u["exchange"], "segment": "CASH", "exchange_token": str(u["index_token"])}
                 for u in self.cfg["underlyings"]]
        self.feed.subscribe_index_value(items)

    def spot(self, u: dict) -> float | None:
        snap = self.feed.get_index_value() or {}
        d = snap.get(u["exchange"], {}).get("CASH", {}).get(str(u["index_token"]))
        return float(d["value"]) if d and d.get("value") else None

    def subscribe_contracts(self, u: dict, spot: float):
        today = datetime.now(IST).date()
        picked = pick_contracts(self.instruments, u["exchange"], u["name"], spot,
                                u["strikes_each_side"], today)
        if picked.empty:
            log.warning("No contracts found for %s on %s", u["name"], u["exchange"])
            return
        new = picked[[(u["exchange"], str(t)) not in self.subscribed for t in picked["exchange_token"]]]
        if new.empty:
            return
        items = [{"exchange": u["exchange"], "segment": "FNO", "exchange_token": str(t)}
                 for t in new["exchange_token"]]
        self.feed.subscribe_ltp(items)
        if self.cfg.get("record_depth", True):
            self.feed.subscribe_market_depth(items)
        self.store.save_session(today.isoformat(), new)
        self.subscribed.update((u["exchange"], str(t)) for t in new["exchange_token"])
        self.atm_spot[u["name"]] = spot
        log.info("%s: +%d contracts (expiry %s, spot %.1f), total %d",
                 u["name"], len(new), new["expiry_date"].iloc[0], spot, len(self.subscribed))

    def maybe_recenter(self):
        for u in self.cfg["underlyings"]:
            spot, last = self.spot(u), self.atm_spot.get(u["name"])
            if spot and last and abs(spot - last) >= u["recenter_points"]:
                self.subscribe_contracts(u, spot)

    # --- main loop ---------------------------------------------------------
    def run(self):
        end = datetime.strptime(self.cfg["end_time"], "%H:%M").time()
        poll = float(self.cfg.get("poll_seconds", 0.5))
        self.subscribe_indices()

        for u in self.cfg["underlyings"]:
            for _ in range(120):                      # wait up to ~60s for a first spot value
                spot = self.spot(u)
                if spot:
                    self.subscribe_contracts(u, spot)
                    break
                time.sleep(0.5)
            else:
                log.error("No spot for %s; skipping its options today", u["name"])

        last_recenter = time.time()
        while not self.stop and datetime.now(IST).time() < end:
            recv = now_ms()
            self.store.write_index(feed_parse.index_rows(self.feed.get_index_value(), recv))
            self.store.write_ltp(feed_parse.ltp_rows(self.feed.get_ltp(), recv))
            if self.cfg.get("record_depth", True):
                self.store.write_depth(feed_parse.depth_rows(self.feed.get_market_depth(), recv))
            if time.time() - last_recenter > 600:
                self.maybe_recenter()
                last_recenter = time.time()
            time.sleep(poll)

        log.info("Done. rows: index=%d ltp=%d depth=%d",
                 self.store.count("index_value"), self.store.count("ltp"), self.store.count("depth"))
        self.store.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.yaml")
    args = p.parse_args()
    with open(args.config) as f:
        cfg = yaml.safe_load(f)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    rec = Recorder(cfg)

    def handle(sig, _frame):
        log.info("Signal %s, stopping", sig)
        rec.stop = True

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)
    rec.run()


if __name__ == "__main__":
    main()
