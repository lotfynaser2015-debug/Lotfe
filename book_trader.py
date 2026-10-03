"""
نظام دخول/خروج بقرار دفتر الأوامر — تجريبي (ورقي) وحقيقي.
عملات + مبلغ يحددهما المستخدم.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from orderbook_engine import symbol_book_signal

logger = logging.getLogger(__name__)


@dataclass
class BookSettings:
    mode: str = "paper"  # paper | live
    size_usdt: float = 15.0
    max_positions: int = 5
    coins: List[str] = field(default_factory=list)
    min_strength: float = 60.0  # أقل قوة لدخول/خروج


@dataclass
class BookPos:
    symbol: str
    entry: float
    amount: float
    cost: float
    opened_at: float
    mode: str


@dataclass
class BookRuntime:
    active: bool = False
    settings: BookSettings = field(default_factory=BookSettings)
    paper_cash: float = 100.0
    positions: Dict[str, BookPos] = field(default_factory=dict)
    closed_pnl: float = 0.0
    trades: int = 0
    wins: int = 0
    last_scan: float = 0.0
    telegram_id: Optional[int] = None


_RT = BookRuntime()


def get_runtime() -> BookRuntime:
    return _RT


def start(telegram_id: int, settings: BookSettings) -> str:
    rt = _RT
    rt.active = True
    rt.settings = settings
    rt.telegram_id = telegram_id
    rt.positions.clear()
    rt.closed_pnl = 0.0
    rt.trades = 0
    rt.wins = 0
    if settings.mode == "paper":
        rt.paper_cash = float(settings.size_usdt) * max(1, settings.max_positions)
    else:
        rt.paper_cash = 0.0
    mode_ar = "تجريبي 🧪" if settings.mode == "paper" else "حقيقي 💰"
    return (
        f"▶️ *دفتر الأوامر شغّال* — {mode_ar}\n"
        f"عملات: `{len(settings.coins)}` | صفقة `{settings.size_usdt:g}$` | أقصى `{settings.max_positions}`\n"
        f"دخول: ضغط شراء ≥ `{settings.min_strength:g}`\n"
        f"خروج: ضغط بيع ≥ `{settings.min_strength:g}`\n"
        + ("ورقي: مفيش أوامر MEXC." if settings.mode == "paper" else "حقيقي: أوامر سوق على MEXC.")
    )


def stop(client=None) -> str:
    rt = _RT
    notes = []
    if rt.settings.mode == "live" and client:
        for sym, pos in list(rt.positions.items()):
            try:
                client.create_market_order(f"{sym}/USDT", "sell", pos.amount * 0.998)
                notes.append(sym)
            except Exception as e:
                logger.warning("book stop sell %s: %s", sym, e)
    rt.positions.clear()
    rt.active = False
    extra = f"\nبيع: {', '.join(notes)}" if notes else ""
    return f"⏹ توقف دفتر الأوامر.{extra}\n{status_text(None)}"


def status_text(client=None) -> str:
    rt = _RT
    s = rt.settings
    mode_ar = "تجريبي 🧪" if s.mode == "paper" else "حقيقي 💰"
    st = "🟢 شغّال" if rt.active else "⚪ متوقف"
    lines = [
        f"📊 *نظام دفتر الأوامر*",
        f"الحالة: {st} | {mode_ar}",
        f"عملات: `{len(s.coins)}` | صفقة `{s.size_usdt:g}$` | أقصى `{s.max_positions}`",
        f"صفقات: `{rt.trades}` | ✅ `{rt.wins}` | محقّق `{rt.closed_pnl:+.2f}$`",
    ]
    if s.mode == "paper":
        lines.append(f"نقد ورقي: `{rt.paper_cash:.2f}$`")
    if rt.positions:
        lines.append("*مراكز:*")
        for sym, p in rt.positions.items():
            px = p.entry
            if client:
                try:
                    px = float(client.get_ticker_price(f"{sym}/USDT") or p.entry)
                except Exception:
                    pass
            u = (px - p.entry) * p.amount
            lines.append(f"• `{sym}` `{u:+.2f}$`")
    return "\n".join(lines)


def tick(client) -> List[str]:
    rt = _RT
    if not rt.active or not rt.settings.coins:
        return []
    now = time.time()
    if now - rt.last_scan < 45:
        return []
    rt.last_scan = now
    s = rt.settings
    msgs: List[str] = []

    for sym in list(s.coins):
        sig, strength, detail = symbol_book_signal(client, sym)
        # خروج
        if sym in rt.positions and sig == "sell" and strength >= s.min_strength:
            pos = rt.positions[sym]
            try:
                px = float(client.get_ticker_price(f"{sym}/USDT") or pos.entry)
            except Exception:
                px = pos.entry
            pnl = (px - pos.entry) * pos.amount
            if s.mode == "paper":
                rt.paper_cash += pos.amount * px
            else:
                try:
                    client.create_market_order(f"{sym}/USDT", "sell", pos.amount * 0.998)
                except Exception as e:
                    msgs.append(f"❌ بيع `{sym}`: {e}")
                    continue
            rt.closed_pnl += pnl
            rt.trades += 1
            if pnl > 0:
                rt.wins += 1
            del rt.positions[sym]
            em = "🟢" if pnl >= 0 else "🔴"
            msgs.append(f"{em} *خروج دفتر* `{sym}` @{px:.6g}\nPnL `{pnl:+.2f}$` | {detail}")
            logger.info("[BOOK] exit %s pnl=%.2f", sym, pnl)
            continue

        # دخول
        if sym in rt.positions:
            continue
        if len(rt.positions) >= s.max_positions:
            continue
        if sig != "buy" or strength < s.min_strength:
            continue

        size = float(s.size_usdt)
        try:
            px = float(client.get_ticker_price(f"{sym}/USDT") or 0)
        except Exception:
            continue
        if px <= 0:
            continue

        if s.mode == "paper":
            if size > rt.paper_cash * 0.99:
                continue
            amt = size / px
            rt.paper_cash -= size
            rt.positions[sym] = BookPos(sym, px, amt, size, time.time(), "paper")
            msgs.append(f"🟢 *دخول دفتر (تجريبي)* `{sym}` @{px:.6g} `{size:.1f}$`\n{detail}")
        else:
            try:
                free = float(client.get_free_usdt() or 0)
                size = min(size, free * 0.95)
                if size < 10:
                    continue
                client.create_market_buy_usdt(sym, size)
                amt = (size * 0.997) / px
                rt.positions[sym] = BookPos(sym, px, amt, size, time.time(), "live")
                msgs.append(f"🟢 *دخول دفتر (حقيقي)* `{sym}` @{px:.6g} `{size:.1f}$`\n{detail}")
            except Exception as e:
                msgs.append(f"❌ دخول `{sym}`: {e}")
                continue
        logger.info("[BOOK] entry %s strength=%.0f", sym, strength)

    return msgs
