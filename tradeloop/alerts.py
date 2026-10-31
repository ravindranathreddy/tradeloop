"""Phone alerts for every `ALERT ...` log line (kill switch, watchdog, reconciliation, Groww gave up).

Telegram is the main channel. Gmail is the backup: it is used when Telegram fails or isn't set up,
and always for critical alerts (ERROR level, or a kill switch). Secrets come from .env.

    python -m tradeloop.alerts test       # sends one test message through each channel

Alerts must never get in the way of trading or the kill switch: sending happens on a background
thread, every failure is logged and swallowed, and a repeat of the same alert inside
`dedupe_seconds` is held back (counted, and reported with the next one). The dedupe state is a file,
so the cron watchdog, which is a new process every minute, doesn't send the same thing every minute.
"""
from __future__ import annotations

import json
import logging
import os
import re
import smtplib
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from tradeloop.core.market_calendar import IST

log = logging.getLogger("alerts")
STATE_FILE = Path("data/alert_state.json")
MAX_TEXT = 1000


def _wall_ms() -> int:
    return int(time.time() * 1000)


def send_telegram(token: str, chat_id: str, text: str, timeout: float = 10) -> None:
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body)
    urllib.request.urlopen(req, timeout=timeout).read()


def send_gmail(user: str, app_password: str, to: str, subject: str, text: str, timeout: float = 15) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, to, subject
    msg.set_content(text)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=timeout) as s:
        s.login(user, app_password)
        s.send_message(msg)


def _why(e: Exception) -> str:
    """Name the failure without the URL (it carries the bot token) or the server's reply."""
    code = getattr(e, "code", None)
    return f"{type(e).__name__}" + (f" {code}" if code else "")


class Notifier:
    def __init__(self, *, telegram: tuple[str, str] | None = None, gmail: tuple[str, str, str] | None = None,
                 state_file: Path = STATE_FILE, dedupe_seconds: float = 300, max_per_minute: int = 10,
                 clock_ms: Callable[[], int] = _wall_ms, telegram_sender=send_telegram, gmail_sender=send_gmail):
        self.telegram, self.gmail, self.state_file = telegram, gmail, Path(state_file)
        self.dedupe_ms, self.max_per_minute, self.clock = int(dedupe_seconds * 1000), max_per_minute, clock_ms
        self._send_telegram, self._send_gmail = telegram_sender, gmail_sender
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    @classmethod
    def from_env(cls, cfg: dict | None = None, **kw) -> Notifier:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        env = os.environ.get
        a = (cfg or {}).get("alerts", {})
        telegram = (env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID"))
        gmail = (env("GMAIL_USER"), env("GMAIL_APP_PASSWORD"), env("ALERT_EMAIL_TO"))
        return cls(telegram=telegram if all(telegram) else None, gmail=gmail if all(gmail) else None,
                   state_file=Path(a.get("state_file", STATE_FILE)), dedupe_seconds=float(a.get("dedupe_seconds", 300)),
                   max_per_minute=int(a.get("max_per_minute", 10)), **kw)

    # --- deciding whether to send ------------------------------------------------------

    def _load(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def _admit(self, text: str, now: int) -> tuple[bool, int]:
        """(send it?, how many identical ones were held back since the last send)."""
        key = re.sub(r"\d+", "#", text)[:120]          # "no tick for 343s" and "...344s" are one alert
        with self._lock:
            state = self._load()
            entry = state.get(key, {"ms": 0, "held": 0})
            recent = [t for t in state.get("_sent", []) if now - t < 60_000]
            if now - entry["ms"] < self.dedupe_ms or len(recent) >= self.max_per_minute:
                entry["held"] += 1
                state[key] = entry
                send, held = False, 0
            else:
                held = entry["held"]
                state[key] = {"ms": now, "held": 0}
                state["_sent"] = recent + [now]
                send = True
            state = {k: v for k, v in state.items() if k == "_sent" or now - v["ms"] < 86_400_000 or v["held"]}
            try:
                self.state_file.parent.mkdir(parents=True, exist_ok=True)
                self.state_file.write_text(json.dumps(state))
            except (OSError, ValueError) as e:      # still send: a lost dedupe is better than a lost alert
                log.error("could not save alert state: %s", _why(e))
        return send, held

    # --- sending -------------------------------------------------------------------------

    def notify(self, text: str, *, critical: bool = False, force: bool = False) -> threading.Thread | None:
        """Queue an alert; returns the sender thread (or None if held back). Never raises."""
        try:
            now = self.clock()
            held = 0
            if not force:
                send, held = self._admit(text, now)
                if not send:
                    return None
            when = datetime.fromtimestamp(now / 1000, IST).strftime("%H:%M:%S")
            body = f"tradeloop {when} {text}" + (f" (repeated {held}x since the last message)" if held else "")
            t = threading.Thread(target=self._deliver, args=(body[:MAX_TEXT], critical), name="alert", daemon=True)
            self._threads.append(t)
            t.start()
            return t
        except Exception as e:                      # an alert problem must never stop trading or a kill
            log.error("alert not sent: %s", _why(e))
            return None

    def _deliver(self, body: str, critical: bool) -> None:
        delivered = False
        if self.telegram:
            try:
                self._send_telegram(*self.telegram, body)
                delivered = True
            except Exception as e:
                log.error("Telegram alert failed: %s", _why(e))
        if self.gmail and (critical or not delivered):
            try:
                self._send_gmail(self.gmail[0], self.gmail[1], self.gmail[2], "tradeloop alert: " + body[19:80], body)
                delivered = True
            except Exception as e:
                log.error("Gmail alert failed: %s", _why(e))
        if not delivered:
            log.error("alert reached no channel: %s", body)

    def flush(self, timeout: float = 20) -> None:
        """Wait for sends in flight; call before a short-lived process exits."""
        for t in self._threads:
            t.join(timeout)


class AlertHandler(logging.Handler):
    """Forwards log records that start with ALERT."""

    def __init__(self, notifier: Notifier):
        super().__init__(level=logging.WARNING)
        self.notifier = notifier

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if msg.startswith("ALERT"):
                self.notifier.notify(msg[5:].strip(), critical=record.levelno >= logging.ERROR or "kill switch" in msg)
        except Exception:
            pass


def install(cfg: dict | None = None, notifier: Notifier | None = None) -> Notifier:
    """Attach the alert handler to the root logger. Call once, after logging.basicConfig."""
    notifier = notifier or Notifier.from_env(cfg)
    logging.getLogger().addHandler(AlertHandler(notifier))
    if not (notifier.telegram or notifier.gmail):
        log.warning("No alert channel configured (.env): alerts go to the log only")
    return notifier


def main():
    import argparse

    import yaml

    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["test"])
    p.add_argument("--config", default="config.yaml")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = yaml.safe_load(Path(a.config).read_text())
    n = Notifier.from_env(cfg)
    print("Telegram:", "configured" if n.telegram else "NOT configured", "| Gmail:", "configured" if n.gmail else "NOT configured")
    for name, channel in (("Telegram", Notifier(telegram=n.telegram)), ("Gmail", Notifier(gmail=n.gmail))):
        if channel.telegram or channel.gmail:
            channel.notify(f"TEST via {name}: if you can read this, {name} alerts work", force=True)
            channel.flush()


if __name__ == "__main__":
    main()
