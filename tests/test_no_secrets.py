"""Never commit secrets: .env.example must only ever hold placeholders."""
from pathlib import Path

PLACEHOLDERS = {"GROWW_API_KEY": "eyJ...your_totp_token...", "GROWW_TOTP_SECRET": "ABCDEF123456"}


def test_env_example_holds_only_placeholders():
    example = Path(__file__).resolve().parents[1] / ".env.example"
    for line in example.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            assert PLACEHOLDERS.get(key.strip()) == value.strip(), \
                f"{key.strip()} in .env.example is not the placeholder; real values belong in .env"
