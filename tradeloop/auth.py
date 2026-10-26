"""Groww login via TOTP, so the recorder can start unattended every morning.

Access tokens expire daily at 6:00 AM IST, so log in fresh on each run.
"""
import os

import pyotp
from dotenv import load_dotenv
from growwapi import GrowwAPI


def login() -> GrowwAPI:
    load_dotenv()
    api_key = os.environ["GROWW_API_KEY"]        # the TOTP token (long string starting eyJ...)
    totp_secret = os.environ["GROWW_TOTP_SECRET"]  # the base32 secret shown with the QR code
    totp = pyotp.TOTP(totp_secret).now()
    access_token = GrowwAPI.get_access_token(api_key, totp=totp)
    return GrowwAPI(access_token)
