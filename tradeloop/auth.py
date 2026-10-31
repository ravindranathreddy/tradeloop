"""Groww login via TOTP, so the recorder can start unattended every morning.

Access tokens expire daily at 6:00 AM IST, so log in fresh on each run.

A TOTP code works once: two processes logging in within the same 30 s window (the
recorder and the chain recorder) get the same code and the second is refused. So a
failed login waits for the next code and tries again.
"""
import logging
import os
import time

import pyotp
from dotenv import load_dotenv
from growwapi import GrowwAPI

log = logging.getLogger("auth")


def login(attempts: int = 3) -> GrowwAPI:
    load_dotenv()
    api_key = os.environ["GROWW_API_KEY"]        # the TOTP token (long string starting eyJ...)
    totp_secret = os.environ["GROWW_TOTP_SECRET"]  # the base32 secret shown with the QR code
    totp = pyotp.TOTP(totp_secret)
    for attempt in range(attempts):
        try:
            return GrowwAPI(GrowwAPI.get_access_token(api_key, totp=totp.now()))
        except Exception as e:
            if attempt == attempts - 1:
                raise
            wait = totp.interval - time.time() % totp.interval + 1     # until the next code
            log.warning("Login failed (%s); retrying with the next code in %.0f s", type(e).__name__, wait)
            time.sleep(wait)
