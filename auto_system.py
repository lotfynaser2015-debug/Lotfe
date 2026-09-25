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
DAILY_LOSS_LIMIT_PCT = -8.0  # خسارة حقيقية % من سعر الدخول (مش قيمة الباقي بعد البيع)

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


def update_daily_peak_pnl(telegram_id: int, pnl_pct: float) -> Tuple[float, float]:
    """يتتبع أفضل نسبة ربح/خسارة غير محققة خلال اليوم.

    يرجع (أفضل_نسبة_اليوم، الهبوط_بالنقاط_عن_القمة).
    مثال: القمة كانت +12% والآن +3% → dd = -9 نقاط.
    """
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = f"{telegram_id}:{day}:pnl"
    peak = _daily_peak.get(key)
    if peak is None or pnl_pct > peak:
        peak = pnl_pct
        _daily_peak[key] = peak
    dd = pnl_pct - float(peak)
    return float(peak), float(dd)


def daily_loss_triggered(telegram_id: int, pnl_pct: float, pnl_usdt: float = 0.0, cost_usdt: float = 0.0) -> Tuple[bool, str]:
    """حد الخسارة اليومي مبني على ربح/خسارة حقيقية من سعر الدخول — مش قيمة الباقي.

    - pnl_pct: ((القيمة الحالية - تكلفة الدخول) / تكلفة الدخول) * 100 للمراكز المفتوحة
    - يتفعل لو:
        1) الخسارة المطلقة <= DAILY_LOSS_LIMIT_PCT  (مثلاً -8%)
        أو
        2) الهبوط من أفضل نسبة ربح اليوم <= DAILY_LOSS_LIMIT_PCT نقاط
    """
    peak, dd_from_peak = update_daily_peak_pnl(telegram_id, pnl_pct)
    hit_abs = pnl_pct <= DAILY_LOSS_LIMIT_PCT
    hit_dd = dd_from_peak <= DAILY_LOSS_LIMIT_PCT and peak > 0
    detail = (
        f"PnL مفتوح: {pnl_pct:+.1f}% ({pnl_usdt:+.1f}$ على تكلفة {cost_usdt:.1f}$) | "
        f"أفضل اليوم: {peak:+.1f}% | من القمة: {dd_from_peak:+.1f} نقطة"
    )
    if hit_abs or hit_dd:
        return True, f"حد خسارة يومي: {detail}"
    return False, detail


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
