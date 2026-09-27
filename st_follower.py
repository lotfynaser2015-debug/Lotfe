"""
متابع SuperTrend — وضع يدوي + وضع تلقائي على سلة.
السنايبر غير مستخدم هنا.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from supertrend import compute_supertrend, signal_label

logger = logging.getLogger(__name__)

DEFAULT_TF = "15m"
DEFAULT_ATR = 10
DEFAULT_MULT = 3.0
DEFAULT_SIZE = 50.0
DEFAULT_MAX_POS = 2
DEFAULT_WATCHLIST = [
    "BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT", "NEAR",
    "SUI", "SEI", "APT", "ARB", "OP", "INJ", "TIA", "FET", "RENDER", "WLD",
    "LDO", "ATOM", "FIL", "ICP", "AR", "STX", "TAO", "KAS", "ROSE", "IMX",
]


def log_st(event: str, **kwargs):
    parts = [f"[ST] {event}"]
    for k, v in kwargs.items():
        if v is None:
            continue
        if isinstance(v, float):
            parts.append(f"{k}={v:.6g}")
        else:
            parts.append(f"{k}={v}")
    logger.info(" | ".join(parts))


@dataclass
class STSettings:
    timeframe: str = DEFAULT_TF
    atr_period: int = DEFAULT_ATR
    multiplier: float = DEFAULT_MULT
    size_usdt: float = DEFAULT_SIZE
    max_positions: int = DEFAULT_MAX_POS
    auto_enabled: bool = False  # مسح السلة تلقائي
    confirm_before_entry: bool = False  # لو True: إشعار فقط للدخول
    watchlist: List[str] = field(default_factory=lambda: list(DEFAULT_WATCHLIST))


@dataclass
class STPosition:
    symbol: str
    entry: float
    amount: float
    cost_usdt: float
    opened_at: float
    mode: str  # manual | auto


@dataclass
class STRuntime:
    active: bool = False
    settings: STSettings = field(default_factory=STSettings)
    positions: Dict[str, STPosition] = field(default_factory=dict)
    # عملات المراقبة اليدوية (أنت ضفتها)
    manual_watch: Set[str] = field(default_factory=set)
    # آخر اتجاه معروف لكل رمز (لتفادي تكرار الإشعار)
    last_dir: Dict[str, int] = field(default_factory=dict)
    telegram_id: Optional[int] = None
    last_scan_at: float = 0.0


_RT = STRuntime()


def get_runtime() -> STRuntime:
    return _RT


def settings_from_row(row) -> STSettings:
    if row is None:
        return STSettings()
    wl = getattr(row, "watchlist", None) or ""
    coins = [c.strip().upper() for c in str(wl).split(",") if c.strip()]
    if not coins:
        coins = list(DEFAULT_WATCHLIST)
    return STSettings(
        timeframe=str(getattr(row, "timeframe", None) or DEFAULT_TF),
        atr_period=int(getattr(row, "atr_period", None) or DEFAULT_ATR),
        multiplier=float(getattr(row, "multiplier", None) or DEFAULT_MULT),
        size_usdt=float(getattr(row, "size_usdt", None) or DEFAULT_SIZE),
        max_positions=int(getattr(row, "max_positions", None) or DEFAULT_MAX_POS),
        auto_enabled=bool(getattr(row, "auto_enabled", False)),
        confirm_before_entry=bool(getattr(row, "confirm_before_entry", False)),
        watchlist=coins,
    )


def fetch_st(client, symbol: str, settings: STSettings):
    pair = f"{symbol}/USDT"
    try:
        ex = client.exchange
        if not getattr(ex, "markets", None):
            ex.load_markets()
        ohlcv = ex.fetch_ohlcv(pair, timeframe=settings.timeframe, limit=max(80, settings.atr_period + 40))
        if not ohlcv or len(ohlcv) < settings.atr_period + 5:
            return None
        highs = [float(c[2]) for c in ohlcv]
        lows = [float(c[3]) for c in ohlcv]
        closes = [float(c[4]) for c in ohlcv]
        return compute_supertrend(highs, lows, closes, settings.atr_period, settings.multiplier)
    except Exception as e:
        log_st("ohlcv_fail", symbol=symbol, error=str(e)[:120])
        return None


def start_follower(telegram_id: int, settings: STSettings) -> str:
    rt = _RT
    rt.active = True
    rt.settings = settings
    rt.telegram_id = telegram_id
    log_st(
        "start",
        tid=telegram_id,
        tf=settings.timeframe,
        atr=settings.atr_period,
        mult=settings.multiplier,
        size=settings.size_usdt,
        auto=settings.auto_enabled,
    )
    return (
        f"▶️ *متابع SuperTrend شغّال*\n"
        f"فريم: `{settings.timeframe}` | ATR `{settings.atr_period}` × `{settings.multiplier:g}`\n"
        f"حجم الصفقة: `{settings.size_usdt:g}$`\n"
        f"أقصى مراكز: `{settings.max_positions}`\n"
        f"تلقائي على السلة: `{'نعم' if settings.auto_enabled else 'لا'}`\n\n"
        f"يدوي: أرسل رمز عملة (مثال `SEI`) لإضافتها للمراقبة."
    )


def stop_follower(client=None, reason: str = "manual") -> str:
    rt = _RT
    notes = []
    if client and rt.positions:
        for sym, pos in list(rt.positions.items()):
            try:
                client.create_market_order(f"{sym}/USDT", "sell", pos.amount * 0.998)
                notes.append(sym)
                log_st("force_sell", symbol=sym, reason=reason)
            except Exception as e:
                log_st("force_sell_fail", symbol=sym, error=str(e)[:100])
        rt.positions.clear()
    rt.active = False
    rt.manual_watch.clear()
    log_st("stop", reason=reason)
    extra = f"\nتم بيع: {', '.join(notes)}" if notes else ""
    return f"⏹ *توقف متابع SuperTrend* ({reason}){extra}"


def add_manual_symbol(symbol: str) -> str:
    sym = symbol.strip().upper().replace("/USDT", "")
    if not sym or not sym.isalnum():
        return "رمز غير صالح."
    rt = _RT
    if not rt.active:
        return "شغّل المتابع أولاً من القائمة."
    rt.manual_watch.add(sym)
    log_st("manual_add", symbol=sym)
    return f"✅ تمت إضافة `{sym}` للمراقبة اليدوية (SuperTrend `{rt.settings.timeframe}`)."


def remove_manual_symbol(symbol: str) -> str:
    sym = symbol.strip().upper().replace("/USDT", "")
    rt = _RT
    rt.manual_watch.discard(sym)
    return f"تمت إزالة `{sym}` من المراقبة اليدوية."


def _buy(client, symbol: str, settings: STSettings, mode: str) -> Optional[str]:
    rt = _RT
    if symbol in rt.positions:
        return None
    if len(rt.positions) >= settings.max_positions:
        return f"⚠️ وصلنا لأقصى مراكز (`{settings.max_positions}`) — تخطي `{symbol}`"
    try:
        free = float(client.get_free_usdt() or 0)
    except Exception:
        free = 0.0
    size = min(settings.size_usdt, free * 0.95)
    if size < 10:
        return f"❌ رصيد USDT غير كافٍ لدخول `{symbol}`"
    try:
        px = float(client.get_ticker_price(f"{symbol}/USDT") or 0)
        order = client.create_market_buy_usdt(symbol, size)
        if not order or px <= 0:
            return f"❌ فشل شراء `{symbol}`"
        amount = (size * 0.997) / px
        rt.positions[symbol] = STPosition(
            symbol=symbol, entry=px, amount=amount, cost_usdt=size,
            opened_at=time.time(), mode=mode,
        )
        log_st("entry", symbol=symbol, price=px, size=size, mode=mode)
        return (
            f"🟢 *SuperTrend شراء* — `{symbol}`\n"
            f"السعر: `{px:.6g}` | الحجم: `{size:.2f}$`\n"
            f"وضع: `{mode}` | فريم `{settings.timeframe}`"
        )
    except Exception as e:
        log_st("entry_fail", symbol=symbol, error=str(e)[:120])
        return f"❌ فشل دخول `{symbol}`: `{e}`"


def _sell(client, symbol: str, reason: str) -> Optional[str]:
    rt = _RT
    pos = rt.positions.get(symbol)
    if not pos:
        return None
    try:
        px = float(client.get_ticker_price(f"{symbol}/USDT") or pos.entry)
        client.create_market_order(f"{symbol}/USDT", "sell", pos.amount * 0.998)
        pnl = (px - pos.entry) * pos.amount
        del rt.positions[symbol]
        log_st("exit", symbol=symbol, price=px, pnl=pnl, reason=reason)
        return (
            f"🔴 *SuperTrend بيع* — `{symbol}`\n"
            f"السعر: `{px:.6g}` | PnL: `{pnl:+.2f}$`\n"
            f"سبب: {reason}"
        )
    except Exception as e:
        log_st("exit_fail", symbol=symbol, error=str(e)[:120])
        return f"❌ فشل بيع `{symbol}`: `{e}`"


def tick(client) -> List[Dict[str, Any]]:
    """دورة مراقبة: يدوي + تلقائي."""
    rt = _RT
    msgs: List[Dict[str, Any]] = []
    if not rt.active:
        return msgs

    s = rt.settings
    now = time.time()
    if now - rt.last_scan_at < 20:
        return msgs
    rt.last_scan_at = now

    # رموز للمراقبة
    symbols: Set[str] = set(rt.manual_watch)
    if s.auto_enabled:
        symbols.update(s.watchlist)
    # المراكز المفتوحة لازم تتراقب للبيع
    symbols.update(rt.positions.keys())

    for sym in sorted(symbols):
        st = fetch_st(client, sym, s)
        if st is None:
            continue

        prev = rt.last_dir.get(sym)
        rt.last_dir[sym] = st.direction

        # أول قراءة: خزّن الاتجاه فقط
        if prev is None:
            log_st("init_dir", symbol=sym, direction=st.direction, value=st.value)
            continue

        # تحوّل لشراء
        if prev == -1 and st.direction == 1:
            log_st("signal_buy", symbol=sym, st=st.value, tf=s.timeframe)
            if sym in rt.positions:
                continue
            if s.confirm_before_entry:
                msgs.append({
                    "text": (
                        f"📢 *إشارة شراء SuperTrend*\n"
                        f"`{sym}` | فريم `{s.timeframe}`\n"
                        f"ST=`{st.value:.6g}`\n"
                        f"للتنفيذ اليدوي: أعد إرسال الرمز بعد تعطيل التأكيد، "
                        f"أو عطّل «تأكيد قبل الدخول»."
                    ),
                    "symbol": sym,
                    "signal": "buy",
                })
                continue
            # دخول فقط إذا يدوي مُراقب أو تلقائي مفعّل
            mode = "manual" if sym in rt.manual_watch else "auto"
            if mode == "auto" and not s.auto_enabled:
                continue
            if mode == "manual" or s.auto_enabled:
                msg = _buy(client, sym, s, mode)
                if msg:
                    msgs.append({"text": msg})

        # تحوّل لبيع
        elif prev == 1 and st.direction == -1:
            log_st("signal_sell", symbol=sym, st=st.value, tf=s.timeframe)
            if sym in rt.positions:
                msg = _sell(client, sym, "إشارة بيع SuperTrend")
                if msg:
                    msgs.append({"text": msg})
            else:
                msgs.append({
                    "text": (
                        f"📢 *إشارة بيع SuperTrend*\n"
                        f"`{sym}` | فريم `{s.timeframe}` | ST=`{st.value:.6g}`\n"
                        f"(لا يوجد مركز مفتوح)"
                    )
                })

    return msgs


def status_text() -> str:
    rt = _RT
    s = rt.settings
    if not rt.active:
        return (
            "📈 *متابع SuperTrend*\n"
            "الحالة: ⚪ متوقف\n\n"
            f"فريم: `{s.timeframe}` | ATR `{s.atr_period}` × `{s.multiplier:g}`\n"
            f"حجم: `{s.size_usdt:g}$` | أقصى مراكز: `{s.max_positions}`\n"
            f"تلقائي: `{'نعم' if s.auto_enabled else 'لا'}`\n"
            f"سلة: `{len(s.watchlist)}` عملة"
        )
    pos = "\n".join(
        f"• `{p.symbol}` @ `{p.entry:.6g}` ({p.mode})"
        for p in rt.positions.values()
    ) or "_لا مراكز_"
    manual = ", ".join(sorted(rt.manual_watch)) or "—"
    return (
        f"📈 *متابع SuperTrend*\n"
        f"الحالة: 🟢 شغّال\n"
        f"فريم: `{s.timeframe}` | ATR `{s.atr_period}` × `{s.multiplier:g}`\n"
        f"حجم: `{s.size_usdt:g}$` | أقصى مراكز: `{s.max_positions}`\n"
        f"تلقائي: `{'نعم' if s.auto_enabled else 'لا'}`\n"
        f"مراقبة يدوية: `{manual}`\n\n"
        f"*المراكز:*\n{pos}"
    )
