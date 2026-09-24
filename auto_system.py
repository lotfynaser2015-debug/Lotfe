# -*- coding: utf-8 -*-
"""
نظام تلقائي لمحفظتين × 15 عملة
+ حسّ السوق (BTC) + متابعة أرباح + دفاع/خروج عند انهيار عام
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ========== الـ 30 عملة — محفظتين 15 + 15 ==========
PORTFOLIO_CORE_NAME = "نواة-تلقائي"
PORTFOLIO_CORE_COINS = [
    "BTC", "ETH", "SOL", "BNB", "XRP",
    "LINK", "NEAR", "AVAX", "SUI", "ADA",
    "DOT", "UNI", "AAVE", "TAO", "XMR",
]

PORTFOLIO_GROWTH_NAME = "نمو-تلقائي"
PORTFOLIO_GROWTH_COINS = [
    "HYPE", "HBAR", "ICP", "BCH", "FIL",
    "ARB", "INJ", "APT", "FET", "RENDER",
    "KAS", "TIA", "AR", "VET", "XLM",
]

ALL_AUTO_COINS = PORTFOLIO_CORE_COINS + PORTFOLIO_GROWTH_COINS
AUTO_PORTFOLIO_NAMES = {PORTFOLIO_CORE_NAME, PORTFOLIO_GROWTH_NAME}

# ----- حسّ السوق -----
BTC_WEAK_1H = -2.0
BTC_BLACK_1H = -3.5
BTC_BLACK_4H = -5.0
BTC_CRASH_4H = -7.0          # انهيار حاد → خروج طارئ
DAILY_LOSS_LIMIT_PCT = -8.0  # خسارة من قمة اليوم

# دفاع
DEFENSE_TRAIL_PCT = 2.0      # تضييق الـ trail في الضعف


@dataclass
class MarketRegime:
    regime: str          # bull | neutral | weak | black | crash
    btc_change_1h: float
    btc_change_4h: float
    allow_new_entries: bool
    defense_level: int   # 0 عادي | 1 حماية ربح | 2 خروج طارئ
    message: str
    ts: float


_last_regime: Optional[MarketRegime] = None
_system_enabled: Dict[int, bool] = {}
# peak equity per telegram_id per UTC day
_daily_peak: Dict[str, float] = {}
_last_defense_alert: Dict[int, float] = {}  # tid -> ts


def set_system_enabled(telegram_id: int, enabled: bool) -> None:
    _system_enabled[int(telegram_id)] = bool(enabled)


def is_system_enabled(telegram_id: int) -> bool:
    return bool(_system_enabled.get(int(telegram_id), False))


def _pct_change(candles: List, bars: int) -> float:
    if not candles or len(candles) < bars + 1:
        return 0.0
    try:
        now = float(candles[-1][4])
        prev = float(candles[-(bars + 1)][4])
        if prev <= 0:
            return 0.0
        return (now / prev - 1.0) * 100.0
    except Exception:
        return 0.0


def detect_market_regime(client) -> MarketRegime:
    """قراءة خفيفة من شموع BTC فقط."""
    global _last_regime
    ch1, ch4 = 0.0, 0.0
    try:
        candles = client.exchange.fetch_ohlcv("BTC/USDT", timeframe="1h", limit=10) or []
        ch1 = _pct_change(candles, 1)
        ch4 = _pct_change(candles, 4)
    except Exception as e:
        logger.warning("BTC regime fetch failed: %s", e)
        if _last_regime:
            return _last_regime
        return MarketRegime(
            "neutral", 0, 0, True, 0,
            "تعذر قراءة BTC — دخول بحذر", time.time(),
        )

    if ch4 <= BTC_CRASH_4H or ch1 <= BTC_BLACK_1H and ch4 <= BTC_BLACK_4H:
        regime, allow, defense = "crash", False, 2
        msg = f"🔴 انهيار عام محتمل: BTC 1h={ch1:+.2f}% | 4h={ch4:+.2f}% — خروج دفاعي"
    elif ch1 <= BTC_BLACK_1H or ch4 <= BTC_BLACK_4H:
        regime, allow, defense = "black", False, 2
        msg = f"🔴 سوق أسود: BTC 1h={ch1:+.2f}% | 4h={ch4:+.2f}% — حماية مشددة"
    elif ch1 <= BTC_WEAK_1H:
        regime, allow, defense = "weak", False, 1
        msg = f"⚠️ سوق ضعيف: BTC 1h={ch1:+.2f}% | 4h={ch4:+.2f}% — إيقاف دخول + حماية أرباح"
    elif ch1 >= 0.8 and ch4 >= 1.5:
        regime, allow, defense = "bull", True, 0
        msg = f"🚀 سوق صاعد: BTC 1h={ch1:+.2f}% | 4h={ch4:+.2f}%"
    else:
        regime, allow, defense = "neutral", True, 0
        msg = f"سوق متوازن: BTC 1h={ch1:+.2f}% | 4h={ch4:+.2f}%"

    _last_regime = MarketRegime(regime, ch1, ch4, allow, defense, msg, time.time())
    return _last_regime


def get_cached_regime() -> Optional[MarketRegime]:
    return _last_regime


def should_allow_entry(telegram_id: int, client=None) -> Tuple[bool, str]:
    if not is_system_enabled(telegram_id):
        return False, "النظام التلقائي متوقف"
    reg = detect_market_regime(client) if client is not None else _last_regime
    if reg is None:
        return True, "لا يوجد تقييم سوق بعد — مسموح"
    if not reg.allow_new_entries:
        return False, reg.message
    return True, reg.message


def update_daily_peak(telegram_id: int, equity_usdt: float) -> Tuple[float, float]:
    """يرجع (القمة اليومية، التغير % من القمة)."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = f"{telegram_id}:{day}"
    peak = _daily_peak.get(key, 0.0)
    if equity_usdt > peak:
        peak = equity_usdt
        _daily_peak[key] = peak
    if peak <= 0:
        return peak, 0.0
    dd = (equity_usdt / peak - 1.0) * 100.0
    return peak, dd


def daily_loss_triggered(telegram_id: int, equity_usdt: float) -> Tuple[bool, str]:
    peak, dd = update_daily_peak(telegram_id, equity_usdt)
    if peak > 0 and dd <= DAILY_LOSS_LIMIT_PCT:
        return True, f"حد خسارة يومي: {dd:.1f}% من قمة اليوم ({peak:.1f}$)"
    return False, f"من قمة اليوم: {dd:+.1f}%"


def portfolio_specs() -> List[Dict[str, Any]]:
    return [
        {"name": PORTFOLIO_CORE_NAME, "coins": list(PORTFOLIO_CORE_COINS), "label": "نواة (15)"},
        {"name": PORTFOLIO_GROWTH_NAME, "coins": list(PORTFOLIO_GROWTH_COINS), "label": "نمو (15)"},
    ]


def format_coins_message() -> str:
    core = " · ".join(f"`{c}`" for c in PORTFOLIO_CORE_COINS)
    growth = " · ".join(f"`{c}`" for c in PORTFOLIO_GROWTH_COINS)
    return (
        "📋 *المحفظتان التلقائيتان (30 عملة)*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"*{PORTFOLIO_CORE_NAME}* — 15\n{core}\n\n"
        f"*{PORTFOLIO_GROWTH_NAME}* — 15\n{growth}\n\n"
        "النظام يحس بالسوق من BTC ويتابع الأرباح ويدافع عند الانهيار."
    )


def should_alert_defense(telegram_id: int, min_interval_sec: float = 900) -> bool:
    """منع سبام تنبيهات الدفاع (كل 15 دقيقة كحد أقصى)."""
    now = time.time()
    last = _last_defense_alert.get(int(telegram_id), 0)
    if now - last < min_interval_sec:
        return False
    _last_defense_alert[int(telegram_id)] = now
    return True
