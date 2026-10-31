"""Never commit secrets: .env.example must only ever hold placeholders."""
from pathlib import Path

PLACEHOLDERS = {"GROWW_API_KEY": "eyJ...your_totp_token...", "GROWW_TOTP_SECRET": "ABCDEF123456"}
EMPTY = {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GMAIL_USER", "GMAIL_APP_PASSWORD", "ALERT_EMAIL_TO"}


def test_env_example_holds_only_placeholders():
    example = Path(__file__).resolve().parents[1] / ".env.example"
    for line in example.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            expected = "" if key.strip() in EMPTY else PLACEHOLDERS.get(key.strip())
            assert expected == value.strip(), \
                f"{key.strip()} in .env.example is not the placeholder; real values belong in .env"
