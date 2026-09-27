"""
Sniper Session — نظام اقتناص منفصل عن المحافظ.
جلسة بهدف دولار، حجم صفقة حسب قوة الإشارة، جلسات متتالية من الإعدادات.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---- افتراضيات ----
DEFAULT_TARGET_USDT = 5.0
DEFAULT_STOP_USDT = 5.0
DEFAULT_MIN_SIZE = 30.0
DEFAULT_MAX_SIZE = 80.0
DEFAULT_MAX_POSITIONS = 1
DEFAULT_SESSIONS = 3
DEFAULT_TRAIL_PCT = 2.0
DEFAULT_INITIAL_SL_PCT = 2.0
DEFAULT_WATCHLIST = [
    "BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT", "NEAR",
    "SUI", "SEI", "APT", "ARB", "OP", "INJ", "TIA", "FET", "RENDER", "WLD",
    "LDO", "GRT", "AAVE", "UNI", "ATOM", "FIL", "ICP", "AR", "STX", "PEPE",
    "WIF", "BONK", "TAO", "KAS", "ROSE", "LPT", "IMX", "RUNE", "ENA", "JUP",
]


def log_sniper(event: str, **kwargs):
    parts = [f"[SNIPER] {event}"]
    for k, v in kwargs.items():
        if v is None:
            continue
        if isinstance(v, float):
            parts.append(f"{k}={v:.6g}")
        else:
            parts.append(f"{k}={v}")
    logger.info(" | ".join(parts))


@dataclass
class SniperSettings:
    target_usdt: float = DEFAULT_TARGET_USDT
    stop_usdt: float = DEFAULT_STOP_USDT
    min_size_usdt: float = DEFAULT_MIN_SIZE
    max_size_usdt: float = DEFAULT_MAX_SIZE
    max_positions: int = DEFAULT_MAX_POSITIONS
    sessions_planned: int = DEFAULT_SESSIONS
    trail_pct: float = DEFAULT_TRAIL_PCT
    initial_sl_pct: float = DEFAULT_INITIAL_SL_PCT
    continue_after_loss: bool = False
    watchlist: List[str] = field(default_factory=lambda: list(DEFAULT_WATCHLIST))


@dataclass
class OpenSniperPos:
    symbol: str
    entry: float
    amount: float
    cost_usdt: float
    sl: float
    opened_at: float
    signal_score: float = 0.0


@dataclass
class SniperRuntime:
    active: bool = False
    settings: SniperSettings = field(default_factory=SniperSettings)
    session_index: int = 0  # 1-based when active
    session_realized: float = 0.0
    session_started_at: Optional[float] = None
    positions: Dict[str, OpenSniperPos] = field(default_factory=dict)
    last_scan_at: float = 0.0
    last_entry_at: float = 0.0
    status_message: str = ""
    telegram_id: Optional[int] = None


# حالة في الذاكرة (process واحد على Railway)
_RUNTIME = SniperRuntime()


def get_runtime() -> SniperRuntime:
    return _RUNTIME


def settings_from_db_row(row) -> SniperSettings:
    """يبني الإعدادات من صف SniperConfig إن وُجد."""
    if row is None:
        return SniperSettings()
    wl = getattr(row, "watchlist", None) or ""
    coins = [c.strip().upper() for c in str(wl).split(",") if c.strip()]
    if not coins:
        coins = list(DEFAULT_WATCHLIST)
    return SniperSettings(
        target_usdt=float(getattr(row, "target_usdt", None) or DEFAULT_TARGET_USDT),
        stop_usdt=float(getattr(row, "stop_usdt", None) or DEFAULT_STOP_USDT),
        min_size_usdt=float(getattr(row, "min_size_usdt", None) or DEFAULT_MIN_SIZE),
        max_size_usdt=float(getattr(row, "max_size_usdt", None) or DEFAULT_MAX_SIZE),
        max_positions=int(getattr(row, "max_positions", None) or DEFAULT_MAX_POSITIONS),
        sessions_planned=int(getattr(row, "sessions_planned", None) or DEFAULT_SESSIONS),
        trail_pct=float(getattr(row, "trail_pct", None) or DEFAULT_TRAIL_PCT),
        initial_sl_pct=float(getattr(row, "initial_sl_pct", None) or DEFAULT_INITIAL_SL_PCT),
        continue_after_loss=bool(getattr(row, "continue_after_loss", False)),
        watchlist=coins,
    )


def score_symbol(client, symbol: str, btc_chg: float = 0.0) -> Tuple[float, Dict[str, float]]:
    """
    درجة 0–100 من زخم قصير + حجم تقريبي.
    يستخدم شموع 5m / 15m إن توفرت.
    """
    meta: Dict[str, float] = {}
    pair = f"{symbol}/USDT"
    try:
        # ccxt ohlcv عبر العميل
        ex = client.exchange
        if not getattr(ex, "markets", None):
            ex.load_markets()
        ohlcv = ex.fetch_ohlcv(pair, timeframe="5m", limit=30)
        if not ohlcv or len(ohlcv) < 6:
            return 0.0, meta
        closes = [float(c[4]) for c in ohlcv]
        volumes = [float(c[5]) for c in ohlcv]
        last = closes[-1]
        if last <= 0:
            return 0.0, meta
        # تغيّر آخر 3 شموع (~15د) وآخر 6 (~30د)
        chg_15 = ((last / closes[-4]) - 1.0) * 100.0 if closes[-4] > 0 else 0.0
        chg_30 = ((last / closes[-7]) - 1.0) * 100.0 if len(closes) >= 7 and closes[-7] > 0 else chg_15
        vol_recent = sum(volumes[-3:]) / 3.0
        vol_avg = sum(volumes[:-3]) / max(1, len(volumes) - 3)
        vol_ratio = (vol_recent / vol_avg) if vol_avg > 0 else 1.0
        rel = chg_15 - btc_chg  # قوة نسبية تقريبية

        meta = {
            "chg_15": chg_15,
            "chg_30": chg_30,
            "vol_ratio": vol_ratio,
            "rel": rel,
            "price": last,
        }

        score = 0.0
        # زخم 15د
        if chg_15 >= 3.0:
            score += 35
        elif chg_15 >= 2.0:
            score += 28
        elif chg_15 >= 1.2:
            score += 18
        elif chg_15 >= 0.6:
            score += 8
        else:
            score += 0

        # حجم
        if vol_ratio >= 2.0:
            score += 30
        elif vol_ratio >= 1.5:
            score += 22
        elif vol_ratio >= 1.2:
            score += 12
        else:
            score += 4

        # قوة نسبية
        if rel >= 1.5:
            score += 25
        elif rel >= 0.8:
            score += 15
        elif rel >= 0.3:
            score += 8

        # خصم مطاردة: صعود 30د عنيف جدًا
        if chg_30 >= 8.0:
            score -= 25
        elif chg_30 >= 5.0:
            score -= 12

        score = max(0.0, min(100.0, score))
        return score, meta
    except Exception as e:
        log_sniper("score_fail", symbol=symbol, error=str(e)[:120])
        return 0.0, meta


def size_from_score(score: float, settings: SniperSettings) -> float:
    """حجم الصفقة بين min و max حسب الدرجة."""
    if score < 40:
        return 0.0
    if score >= 80:
        return float(settings.max_size_usdt)
    if score >= 60:
        # 50–80% من المدى
        t = (score - 60) / 20.0
        return settings.min_size_usdt + (settings.max_size_usdt - settings.min_size_usdt) * (0.5 + 0.5 * t)
    # 40–60
    t = (score - 40) / 20.0
    return settings.min_size_usdt + (settings.max_size_usdt - settings.min_size_usdt) * (0.15 + 0.35 * t)


def scan_candidates(client, settings: SniperSettings, limit: int = 5) -> List[Dict[str, Any]]:
    """يرجّع أفضل المرشحين مرتبين بالدرجة."""
    btc_chg = 0.0
    try:
        _, btc_meta = score_symbol(client, "BTC", 0.0)
        btc_chg = float(btc_meta.get("chg_15") or 0)
    except Exception:
        pass

    results = []
    for sym in settings.watchlist:
        if sym in _RUNTIME.positions:
            continue
        score, meta = score_symbol(client, sym, btc_chg)
        if score < 40:
            continue
        size = size_from_score(score, settings)
        if size < settings.min_size_usdt * 0.9:
            continue
        results.append({
            "symbol": sym,
            "score": score,
            "size_usdt": size,
            **meta,
        })
    results.sort(key=lambda x: x["score"], reverse=True)
    log_sniper("scan", candidates=len(results), top=(results[0]["symbol"] if results else "-"))
    return results[:limit]


def start_sniper(telegram_id: int, settings: SniperSettings) -> str:
    rt = _RUNTIME
    if rt.active:
        return "السنايبر شغّال بالفعل."
    rt.active = True
    rt.settings = settings
    rt.session_index = 1
    rt.session_realized = 0.0
    rt.session_started_at = time.time()
    rt.positions = {}
    rt.telegram_id = telegram_id
    rt.status_message = "جلسة 1 بدأت"
    log_sniper(
        "start",
        tid=telegram_id,
        target=settings.target_usdt,
        stop=settings.stop_usdt,
        sessions=settings.sessions_planned,
        max_pos=settings.max_positions,
    )
    return (
        f"▶️ *بدأ السنايبر*\n"
        f"جلسة `1/{settings.sessions_planned}`\n"
        f"تارجت: `+{settings.target_usdt:g}$` | وقف: `−{settings.stop_usdt:g}$`\n"
        f"حجم: `{settings.min_size_usdt:g}`–`{settings.max_size_usdt:g}$` حسب الإشارة\n"
        f"أقصى مراكز: `{settings.max_positions}` (حد أقصى — مش إجباري)"
    )


def stop_sniper(client=None, reason: str = "manual") -> str:
    rt = _RUNTIME
    notes = []
    if client and rt.positions:
        for sym, pos in list(rt.positions.items()):
            try:
                client.create_market_order(f"{sym}/USDT", "sell", pos.amount * 0.998)
                notes.append(f"{sym} بيع")
                log_sniper("force_sell", symbol=sym, reason=reason)
            except Exception as e:
                notes.append(f"{sym} فشل: {e}")
                log_sniper("force_sell_fail", symbol=sym, error=str(e)[:100])
        rt.positions.clear()
    rt.active = False
    rt.status_message = f"متوقف ({reason})"
    log_sniper("stop", reason=reason, realized=rt.session_realized)
    extra = ("\n" + "، ".join(notes)) if notes else ""
    return f"⏹ *توقف السنايبر* — {reason}\nربح/خسارة آخر جلسة: `{rt.session_realized:+.2f}$`{extra}"


def _unrealized(client, rt: SniperRuntime) -> float:
    total = 0.0
    for sym, pos in rt.positions.items():
        try:
            px = float(client.get_ticker_price(f"{sym}/USDT") or 0)
            if px > 0:
                total += (px - pos.entry) * pos.amount
        except Exception:
            pass
    return total


def tick(client) -> List[Dict[str, Any]]:
    """
    دورة واحدة: تريل / استوب / تارجت جلسة / دخول جديد.
    يرجع قائمة إشعارات للتليجرام.
    """
    rt = _RUNTIME
    msgs: List[Dict[str, Any]] = []
    if not rt.active:
        return msgs

    s = rt.settings
    now = time.time()

    # ---- إدارة المراكز المفتوحة ----
    for sym in list(rt.positions.keys()):
        pos = rt.positions[sym]
        try:
            px = float(client.get_ticker_price(f"{sym}/USDT") or 0)
        except Exception:
            continue
        if px <= 0:
            continue

        # تريل
        candidate = px * (1.0 - s.trail_pct / 100.0)
        if candidate > pos.sl:
            old = pos.sl
            pos.sl = candidate
            log_sniper("trail", symbol=sym, old=old, new=pos.sl, price=px)

        # ضرب استوب
        if px <= pos.sl:
            pnl = (px - pos.entry) * pos.amount
            try:
                client.create_market_order(f"{sym}/USDT", "sell", pos.amount * 0.998)
                sold = True
            except Exception as e:
                sold = False
                log_sniper("sl_sell_fail", symbol=sym, error=str(e)[:120])
                msgs.append({"text": f"❌ سنايبر: فشل بيع `{sym}` عند الاستوب\n`{e}`"})
                continue
            rt.session_realized += pnl
            del rt.positions[sym]
            log_sniper("sl_hit", symbol=sym, pnl=pnl, price=px, session_pnl=rt.session_realized)
            msgs.append({
                "text": (
                    f"{'✅' if pnl >= 0 else '🔻'} *سنايبر — خروج*\n"
                    f"`{sym}` @ `{px:.6g}`\n"
                    f"PnL صفقة: `{pnl:+.2f}$`\n"
                    f"جلسة الآن: `{rt.session_realized:+.2f}$`"
                )
            })

    # ---- تارجت / وقف الجلسة ----
    unreal = _unrealized(client, rt)
    equity = rt.session_realized + unreal

    if equity >= s.target_usdt:
        # أقفل المراكز وحقق التارجت
        for sym, pos in list(rt.positions.items()):
            try:
                px = float(client.get_ticker_price(f"{sym}/USDT") or pos.entry)
                client.create_market_order(f"{sym}/USDT", "sell", pos.amount * 0.998)
                pnl = (px - pos.entry) * pos.amount
                rt.session_realized += pnl
            except Exception as e:
                log_sniper("target_close_fail", symbol=sym, error=str(e)[:100])
            del rt.positions[sym]
        log_sniper("session_target", pnl=rt.session_realized, session=rt.session_index)
        msgs.append({
            "text": (
                f"🎯 *تم تارجت الجلسة {rt.session_index}/{s.sessions_planned}*\n"
                f"الربح: `{rt.session_realized:+.2f}$`"
            )
        })
        _advance_or_stop(msgs, won=True)
        return msgs

    if equity <= -abs(s.stop_usdt):
        for sym, pos in list(rt.positions.items()):
            try:
                px = float(client.get_ticker_price(f"{sym}/USDT") or pos.entry)
                client.create_market_order(f"{sym}/USDT", "sell", pos.amount * 0.998)
                pnl = (px - pos.entry) * pos.amount
                rt.session_realized += pnl
            except Exception as e:
                log_sniper("stop_close_fail", symbol=sym, error=str(e)[:100])
            if sym in rt.positions:
                del rt.positions[sym]
        log_sniper("session_stop_loss", pnl=rt.session_realized, session=rt.session_index)
        msgs.append({
            "text": (
                f"🛑 *وقف جلسة {rt.session_index}/{s.sessions_planned}*\n"
                f"النتيجة: `{rt.session_realized:+.2f}$`"
            )
        })
        _advance_or_stop(msgs, won=False)
        return msgs

    # ---- دخول جديد ----
    if len(rt.positions) >= s.max_positions:
        return msgs
    if now - rt.last_entry_at < 90:  # تبريد 90 ثانية
        return msgs
    if now - rt.last_scan_at < 45:
        return msgs
    rt.last_scan_at = now

    free = 0.0
    try:
        free = float(client.get_free_usdt() or 0)
    except Exception:
        pass

    cands = scan_candidates(client, s, limit=3)
    for c in cands:
        if len(rt.positions) >= s.max_positions:
            break
        size = float(c["size_usdt"])
        if size > free * 0.95:
            size = free * 0.95
        if size < s.min_size_usdt * 0.85:
            continue
        sym = c["symbol"]
        try:
            order = client.create_market_buy_usdt(sym, size)
            if not order:
                log_sniper("entry_skip", symbol=sym, reason="order_none")
                continue
            px = float(c.get("price") or client.get_ticker_price(f"{sym}/USDT") or 0)
            if px <= 0:
                continue
            amount = (size * 0.997) / px
            sl = px * (1.0 - s.initial_sl_pct / 100.0)
            rt.positions[sym] = OpenSniperPos(
                symbol=sym,
                entry=px,
                amount=amount,
                cost_usdt=size,
                sl=sl,
                opened_at=now,
                signal_score=float(c["score"]),
            )
            rt.last_entry_at = now
            free -= size
            log_sniper(
                "entry",
                symbol=sym,
                score=c["score"],
                size=size,
                price=px,
                sl=sl,
            )
            msgs.append({
                "text": (
                    f"⚡ *سنايبر — دخول*\n"
                    f"`{sym}` @ `{px:.6g}`\n"
                    f"الحجم: `{size:.2f}$` | إشارة: `{c['score']:.0f}/100`\n"
                    f"استوب: `{sl:.6g}` | تريل `{s.trail_pct:g}%`"
                )
            })
        except Exception as e:
            log_sniper("entry_fail", symbol=sym, error=str(e)[:120])
            msgs.append({"text": f"❌ سنايبر دخول `{sym}` فشل:\n`{e}`"})

    return msgs


def _advance_or_stop(msgs: List[Dict], won: bool):
    rt = _RUNTIME
    s = rt.settings
    if not won and not s.continue_after_loss:
        rt.active = False
        rt.status_message = "توقف بعد وقف جلسة"
        msgs.append({"text": "⏹ السنايبر توقف (وقف جلسة — لا إعادة تلقائية)."})
        log_sniper("halt_after_loss")
        return

    if rt.session_index >= s.sessions_planned:
        rt.active = False
        rt.status_message = "اكتملت كل الجلسات"
        msgs.append({
            "text": (
                f"✅ *انتهت كل الجلسات* ({s.sessions_planned})\n"
                f"آخر جلسة: `{rt.session_realized:+.2f}$`"
            )
        })
        log_sniper("all_sessions_done")
        return

    # جلسة تالية
    rt.session_index += 1
    rt.session_realized = 0.0
    rt.session_started_at = time.time()
    rt.positions = {}
    rt.status_message = f"جلسة {rt.session_index} بدأت"
    msgs.append({
        "text": (
            f"🔄 *بدء جلسة {rt.session_index}/{s.sessions_planned}*\n"
            f"تارجت `+{s.target_usdt:g}$` | وقف `−{s.stop_usdt:g}$`"
        )
    })
    log_sniper("next_session", index=rt.session_index)


def status_text() -> str:
    rt = _RUNTIME
    s = rt.settings
    if not rt.active:
        return (
            "🎯 *السنايبر*\n"
            "الحالة: ⚪ متوقف\n\n"
            f"الإعدادات المحفوظة:\n"
            f"تارجت `{s.target_usdt:g}$` | وقف `{s.stop_usdt:g}$`\n"
            f"حجم `{s.min_size_usdt:g}`–`{s.max_size_usdt:g}$` | أقصى أقصى مراكز `{s.max_positions}` (اختياري)\n"
            f"جلسات متتالية: `{s.sessions_planned}`\n"
            f"تريل `{s.trail_pct:g}%` | استوب دخول `{s.initial_sl_pct:g}%`"
        )
    pos_lines = []
    for sym, p in rt.positions.items():
        pos_lines.append(f"• `{sym}` دخول `{p.entry:.6g}` استوب `{p.sl:.6g}` إشارة `{p.signal_score:.0f}`")
    pos_block = "\n".join(pos_lines) if pos_lines else "_لا مراكز مفتوحة_"
    return (
        f"🎯 *السنايبر*\n"
        f"الحالة: 🟢 شغّال\n"
        f"الجلسة: `{rt.session_index}/{s.sessions_planned}`\n"
        f"محقّق الجلسة: `{rt.session_realized:+.2f}$`\n"
        f"تارجت `+{s.target_usdt:g}$` | وقف `−{s.stop_usdt:g}$`\n\n"
        f"*المراكز:*\n{pos_block}"
    )
