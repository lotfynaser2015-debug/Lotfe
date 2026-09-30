"""
متابع CPR — وضع تجريبي (ورقي) ووضع حقيقي + متابعة نتائج.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from cpr import cpr_from_ohlcv, CPRLevels

logger = logging.getLogger(__name__)

DEFAULT_COINS = [
    "BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "AVAX", "LINK", "DOT", "NEAR",
    "SUI", "SEI", "APT", "ARB", "OP", "INJ", "TIA", "FET", "WLD", "TAO",
]


def log_cpr(event: str, **kwargs):
    parts = [f"[CPR] {event}"]
    for k, v in kwargs.items():
        if v is None:
            continue
        if isinstance(v, float):
            parts.append(f"{k}={v:.6g}")
        else:
            parts.append(f"{k}={v}")
    logger.info(" | ".join(parts))


@dataclass
class CPRSettings:
    mode: str = "paper"  # paper | live
    investment_usdt: float = 100.0
    size_usdt: float = 15.0  # حجم الصفقة الواحدة
    max_positions: int = 3
    coins: List[str] = field(default_factory=lambda: list(DEFAULT_COINS))
    take_r1: bool = True  # بيع عند R1
    exit_on_bc: bool = True  # خروج لو كسر BC


@dataclass
class CPRPosition:
    symbol: str
    entry: float
    amount: float
    cost_usdt: float
    opened_at: float
    tc: float
    bc: float
    r1: float
    mode: str


@dataclass
class ClosedTrade:
    symbol: str
    entry: float
    exit: float
    amount: float
    pnl: float
    reason: str
    opened_at: float
    closed_at: float
    mode: str


@dataclass
class CPRRuntime:
    active: bool = False
    settings: CPRSettings = field(default_factory=CPRSettings)
    # ورقية
    paper_cash: float = 100.0
    # مراكز مفتوحة (ورقي أو حقيقي متتبَّع)
    positions: Dict[str, CPRPosition] = field(default_factory=dict)
    closed: List[ClosedTrade] = field(default_factory=list)
    # آخر حالة إشارة لكل رمز: below_bc | inside | above_tc
    zone: Dict[str, str] = field(default_factory=dict)
    levels_cache: Dict[str, CPRLevels] = field(default_factory=dict)
    levels_day: Dict[str, str] = field(default_factory=dict)  # YYYY-MM-DD
    telegram_id: Optional[int] = None
    last_scan_at: float = 0.0
    started_at: float = 0.0


_RT = CPRRuntime()


def get_runtime() -> CPRRuntime:
    return _RT


def settings_from_row(row) -> CPRSettings:
    if row is None:
        return CPRSettings()
    coins_raw = getattr(row, "coins", None) or getattr(row, "watchlist", None) or ""
    coins = [c.strip().upper() for c in str(coins_raw).split(",") if c.strip()]
    if not coins:
        coins = list(DEFAULT_COINS)
    return CPRSettings(
        mode=str(getattr(row, "mode", None) or "paper").lower(),
        investment_usdt=float(getattr(row, "investment_usdt", None) or 100),
        size_usdt=float(getattr(row, "size_usdt", None) or 15),
        max_positions=int(getattr(row, "max_positions", None) or 3),
        coins=coins[:40],
        take_r1=bool(getattr(row, "take_r1", True)),
        exit_on_bc=bool(getattr(row, "exit_on_bc", True)),
    )


def _fetch_levels(client, symbol: str) -> Optional[CPRLevels]:
    pair = f"{symbol}/USDT"
    try:
        ex = client.exchange
        if not getattr(ex, "markets", None):
            ex.load_markets()
        ohlcv = ex.fetch_ohlcv(pair, timeframe="1d", limit=5)
        return cpr_from_ohlcv(ohlcv)
    except Exception as e:
        log_cpr("levels_fail", symbol=symbol, error=str(e)[:100])
        return None


def _zone_for_price(px: float, lv: CPRLevels) -> str:
    if px > lv.tc:
        return "above_tc"
    if px < lv.bc:
        return "below_bc"
    return "inside"


def start_cpr(telegram_id: int, settings: CPRSettings) -> str:
    rt = _RT
    rt.active = True
    rt.settings = settings
    rt.telegram_id = telegram_id
    rt.positions.clear()
    rt.closed.clear()
    rt.zone.clear()
    rt.levels_cache.clear()
    rt.started_at = time.time()
    if settings.mode == "paper":
        rt.paper_cash = float(settings.investment_usdt)
    else:
        rt.paper_cash = 0.0
    log_cpr(
        "start",
        mode=settings.mode,
        invest=settings.investment_usdt,
        size=settings.size_usdt,
        coins=len(settings.coins),
        max_pos=settings.max_positions,
    )
    mode_ar = "تجريبي (ورقي)" if settings.mode == "paper" else "حقيقي"
    return (
        f"▶️ *CPR شغّال* — وضع: *{mode_ar}*\n"
        f"رأس المال: `{settings.investment_usdt:g}$`\n"
        f"حجم الصفقة: `{settings.size_usdt:g}$` | أقصى مراكز: `{settings.max_positions}`\n"
        f"العملات: `{len(settings.coins)}`\n"
        f"دخول: كسر فوق TC | خروج: R1 و/أو كسر BC\n"
        + ("⚠️ *تجريبي:* مفيش أوامر على MEXC — نتائج وهمية للمتابعة." if settings.mode == "paper"
           else "⚠️ *حقيقي:* أوامر سوق على MEXC.")
    )


def stop_cpr(client=None, reason: str = "manual") -> str:
    rt = _RT
    notes = []
    if rt.settings.mode == "live" and client and rt.positions:
        for sym, pos in list(rt.positions.items()):
            try:
                client.create_market_order(f"{sym}/USDT", "sell", pos.amount * 0.998)
                notes.append(sym)
            except Exception as e:
                log_cpr("force_sell_fail", symbol=sym, error=str(e)[:80])
        rt.positions.clear()
    elif rt.positions:
        # ورق: أقفل بسعر السوق للملخص
        if client:
            for sym, pos in list(rt.positions.items()):
                try:
                    px = float(client.get_ticker_price(f"{sym}/USDT") or pos.entry)
                    pnl = (px - pos.entry) * pos.amount
                    rt.paper_cash += pos.amount * px
                    rt.closed.append(ClosedTrade(
                        symbol=sym, entry=pos.entry, exit=px, amount=pos.amount,
                        pnl=pnl, reason="stop", opened_at=pos.opened_at,
                        closed_at=time.time(), mode="paper",
                    ))
                except Exception:
                    rt.paper_cash += pos.cost_usdt
            rt.positions.clear()
    rt.active = False
    log_cpr("stop", reason=reason)
    stats = stats_text()
    extra = f"\nبيع: {', '.join(notes)}" if notes else ""
    return f"⏹ *توقف CPR* ({reason}){extra}\n\n{stats}"


def stats_text() -> str:
    rt = _RT
    s = rt.settings
    wins = [t for t in rt.closed if t.pnl > 0]
    losses = [t for t in rt.closed if t.pnl <= 0]
    realized = sum(t.pnl for t in rt.closed)
    open_pnl = 0.0
    open_cost = sum(p.cost_usdt for p in rt.positions.values())
    mode_ar = "تجريبي" if s.mode == "paper" else "حقيقي"
    if s.mode == "paper":
        equity = rt.paper_cash + open_cost  # تقريبي قبل تحديث أسعار
        # open value will be refined in status with prices
        start_cap = s.investment_usdt
    else:
        equity = None
        start_cap = s.investment_usdt
    lines = [
        f"📊 *نتائج CPR* ({mode_ar})",
        f"صفقات مغلقة: `{len(rt.closed)}` | ✅ `{len(wins)}` | ❌ `{len(losses)}`",
        f"محقّق: `{realized:+.2f}$`",
        f"مراكز مفتوحة: `{len(rt.positions)}`",
    ]
    if s.mode == "paper":
        lines.append(f"نقد ورقي متاح: `{rt.paper_cash:.2f}$`")
        lines.append(f"رأس المال الابتدائي: `{start_cap:.2f}$`")
    if rt.closed:
        avg = realized / len(rt.closed)
        lines.append(f"متوسط الصفقة: `{avg:+.2f}$`")
        last = rt.closed[-5:]
        lines.append("*آخر صفقات:*")
        for t in reversed(last):
            em = "🟢" if t.pnl >= 0 else "🔴"
            lines.append(f"{em} `{t.symbol}` `{t.pnl:+.2f}$` ({t.reason})")
    return "\n".join(lines)


def status_text(client=None) -> str:
    rt = _RT
    s = rt.settings
    mode_ar = "تجريبي 🧪" if s.mode == "paper" else "حقيقي 💰"
    if not rt.active:
        return (
            f"📈 *CPR*\n"
            f"الحالة: ⚪ متوقف\n"
            f"الوضع: *{mode_ar}*\n"
            f"رأس المال: `{s.investment_usdt:g}$` | صفقة: `{s.size_usdt:g}$`\n"
            f"أقصى مراكز: `{s.max_positions}` | عملات: `{len(s.coins)}`\n\n"
            f"{stats_text()}"
        )
    open_val = 0.0
    pos_lines = []
    for sym, p in rt.positions.items():
        px = p.entry
        if client:
            try:
                px = float(client.get_ticker_price(f"{sym}/USDT") or p.entry)
            except Exception:
                pass
        u = (px - p.entry) * p.amount
        open_val += p.amount * px
        pos_lines.append(f"• `{sym}` @{p.entry:.5g} الآن {px:.5g} `{u:+.2f}$`")
    if s.mode == "paper":
        equity = rt.paper_cash + open_val
        pnl_all = equity - s.investment_usdt
        cap_line = f"حقوق الملكية: `{equity:.2f}$` | الكل: `{pnl_all:+.2f}$`"
    else:
        cap_line = f"تكلفة مفتوحة: `{sum(p.cost_usdt for p in rt.positions.values()):.2f}$`"
    pos_block = "\n".join(pos_lines) if pos_lines else "_لا مراكز_"
    return (
        f"📈 *CPR* — {mode_ar}\n"
        f"الحالة: 🟢 شغّال\n"
        f"صفقة `{s.size_usdt:g}$` | أقصى `{s.max_positions}` | عملات `{len(s.coins)}`\n"
        f"{cap_line}\n\n"
        f"*المراكز:*\n{pos_block}\n\n"
        f"{stats_text()}"
    )


def _open(client, symbol: str, lv: CPRLevels, px: float) -> Optional[str]:
    rt = _RT
    s = rt.settings
    if symbol in rt.positions:
        return None
    if len(rt.positions) >= s.max_positions:
        return None
    size = float(s.size_usdt)
    if s.mode == "paper":
        if size > rt.paper_cash * 0.995:
            size = rt.paper_cash * 0.995
        if size < 5:
            return f"⚠️ نقد تجريبي غير كافٍ لـ `{symbol}`"
        amount = size / px
        rt.paper_cash -= size
        rt.positions[symbol] = CPRPosition(
            symbol=symbol, entry=px, amount=amount, cost_usdt=size,
            opened_at=time.time(), tc=lv.tc, bc=lv.bc, r1=lv.r1, mode="paper",
        )
        log_cpr("paper_entry", symbol=symbol, price=px, size=size)
        return (
            f"🟢 *CPR شراء (تجريبي)* `{symbol}`\n"
            f"@{px:.6g} | `{size:.2f}$`\n"
            f"TC `{lv.tc:.6g}` → هدف R1 `{lv.r1:.6g}`"
        )
    # live
    try:
        free = float(client.get_free_usdt() or 0)
        size = min(size, free * 0.95)
        if size < 10:
            return f"❌ USDT غير كافٍ لـ `{symbol}`"
        order = client.create_market_buy_usdt(symbol, size)
        if not order:
            return f"❌ فشل شراء `{symbol}`"
        amount = (size * 0.997) / px
        rt.positions[symbol] = CPRPosition(
            symbol=symbol, entry=px, amount=amount, cost_usdt=size,
            opened_at=time.time(), tc=lv.tc, bc=lv.bc, r1=lv.r1, mode="live",
        )
        log_cpr("live_entry", symbol=symbol, price=px, size=size)
        return (
            f"🟢 *CPR شراء (حقيقي)* `{symbol}`\n"
            f"@{px:.6g} | `{size:.2f}$`\n"
            f"هدف R1 `{lv.r1:.6g}` | حماية BC `{lv.bc:.6g}`"
        )
    except Exception as e:
        log_cpr("entry_fail", symbol=symbol, error=str(e)[:100])
        return f"❌ دخول `{symbol}`: `{e}`"


def _close(client, symbol: str, px: float, reason: str) -> Optional[str]:
    rt = _RT
    pos = rt.positions.get(symbol)
    if not pos:
        return None
    pnl = (px - pos.entry) * pos.amount
    if pos.mode == "paper":
        rt.paper_cash += pos.amount * px
        rt.closed.append(ClosedTrade(
            symbol=symbol, entry=pos.entry, exit=px, amount=pos.amount,
            pnl=pnl, reason=reason, opened_at=pos.opened_at,
            closed_at=time.time(), mode="paper",
        ))
        del rt.positions[symbol]
        log_cpr("paper_exit", symbol=symbol, pnl=pnl, reason=reason)
        em = "🟢" if pnl >= 0 else "🔴"
        return f"{em} *CPR بيع (تجريبي)* `{symbol}` @{px:.6g}\nPnL `{pnl:+.2f}$` — {reason}"
    try:
        client.create_market_order(f"{symbol}/USDT", "sell", pos.amount * 0.998)
        rt.closed.append(ClosedTrade(
            symbol=symbol, entry=pos.entry, exit=px, amount=pos.amount,
            pnl=pnl, reason=reason, opened_at=pos.opened_at,
            closed_at=time.time(), mode="live",
        ))
        del rt.positions[symbol]
        log_cpr("live_exit", symbol=symbol, pnl=pnl, reason=reason)
        em = "🟢" if pnl >= 0 else "🔴"
        return f"{em} *CPR بيع (حقيقي)* `{symbol}` @{px:.6g}\nPnL `{pnl:+.2f}$` — {reason}"
    except Exception as e:
        log_cpr("exit_fail", symbol=symbol, error=str(e)[:100])
        return f"❌ بيع `{symbol}`: `{e}`"


def tick(client) -> List[Dict[str, Any]]:
    rt = _RT
    msgs: List[Dict[str, Any]] = []
    if not rt.active:
        return msgs
    now = time.time()
    if now - rt.last_scan_at < 30:
        return msgs
    rt.last_scan_at = now
    s = rt.settings
    day_key = time.strftime("%Y-%m-%d", time.gmtime())

    for sym in list(s.coins):
        # مستويات يومية — كاش
        if rt.levels_day.get(sym) != day_key or sym not in rt.levels_cache:
            lv = _fetch_levels(client, sym)
            if not lv:
                continue
            rt.levels_cache[sym] = lv
            rt.levels_day[sym] = day_key
        lv = rt.levels_cache[sym]
        try:
            px = float(client.get_ticker_price(f"{sym}/USDT") or 0)
        except Exception:
            continue
        if px <= 0:
            continue

        z = _zone_for_price(px, lv)
        prev = rt.zone.get(sym)
        rt.zone[sym] = z

        # إدارة مركز مفتوح
        if sym in rt.positions:
            pos = rt.positions[sym]
            if s.take_r1 and px >= pos.r1:
                msg = _close(client, sym, px, "هدف R1")
                if msg:
                    msgs.append({"text": msg})
                continue
            if s.exit_on_bc and px < pos.bc:
                msg = _close(client, sym, px, "كسر BC")
                if msg:
                    msgs.append({"text": msg})
                continue
            continue

        # دخول: انتقال من داخل/تحت إلى فوق TC
        if prev is None:
            continue
        if z == "above_tc" and prev != "above_tc":
            msg = _open(client, sym, lv, px)
            if msg:
                msgs.append({"text": msg})
            log_cpr("signal_break_tc", symbol=sym, price=px, tc=lv.tc)

    return msgs
