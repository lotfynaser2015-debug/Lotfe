import os
from dotenv import load_dotenv

load_dotenv()

# MEXC (مطلوب)
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")
DATABASE_URL = os.getenv("DATABASE_URL")

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# رقم الأدمن فقط يقدر يستخدم البوت (مستحسن جداً)
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0") or "0")

DEFAULT_THRESHOLD = 2.0
DEFAULT_MIN_TRADE_USDT = 5.0
# A sell order intentionally leaves a tiny safety buffer, which can remain
# as dust in the wallet. Balances below this market value are not treated as
# a real holding during the missing-coin scan.
BALANCE_PRESENCE_MIN_USDT = float(os.getenv("BALANCE_PRESENCE_MIN_USDT", "1.0"))
QUOTE_ASSET = "USDT"
