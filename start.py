"""Container entrypoint for Railway and other worker platforms."""
import os


REQUIRED_ENV_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "MEXC_API_KEY",
    "MEXC_API_SECRET",
    "DATABASE_URL",
)

missing = [name for name in REQUIRED_ENV_VARS if not os.getenv(name)]
if missing:
    raise SystemExit(
        "Missing required environment variables: " + ", ".join(missing)
    )

# Import only after validating the environment. database.py creates the SQLAlchemy
# engine at import time, so this keeps a missing DATABASE_URL error explicit.
from bot import main

if __name__ == "__main__":
    main()
