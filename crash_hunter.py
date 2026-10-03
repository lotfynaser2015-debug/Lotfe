"""
اصطياد الانهيار — احترافي (مرحلة 1):
- ميزانية مستقلة + سقف لكل عملة
- سلم مستويين (افتراضي)
- تتبّع الأوامر + بعد الامتلاء: هدف حد + وقف منطقي
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

from orderbook_engine import analyze_btc_eth_pressure


@dataclass
class CrashSettings:
    budget_usdt: float = 50.0
    max_per_coin_usdt: float = 25.0
    levels_pct: List[float] = field(default_factory=lambda: [-10.0, -20.0])
    tp_pct: float = 8.0
    sl_pct: float = 5.0


@dataclass
class TrackedOrder:
    symbol: str
    order_id: str
    limit_price: float
    usdt: float
    pct: float
    placed_at: float
    status: str = "open"
    fill_price: float = 0.0
    amount: float = 0.0
    tp_order_id: Optional[str] = None


_TRACK: Dict[str, TrackedOrder] = {}
_SETTINGS = CrashSettings()


def get_settings() -> CrashSettings:
    return _SETTINGS


def update_settings(**kwargs) -> CrashSettings:
    for k, v in kwargs.items():
        if hasattr(_SETTINGS, k) and v is not None:
            setattr(_SETTINGS, k, v)
    return _SETTINGS


def parse_levels(text: str) -> List[float]:
    raw = text.replace("%", " ").replace("،", ",").replace("\n", ",")
    parts = []
    for p in raw.replace(" ", ",").split(","):
        p = p.strip()
        if not p:
            continue
        try:
            v = float(p)
        except ValueError:
            continue
        if v > 0:
            v = -v
        if v >= 0:
            continue
        if v < -80:
            v = -80.0
        parts.append(v)
    return sorted(set(round(x, 2) for x in parts), reverse=True)


def default_manual_levels() -> List[float]:
    return [-10.0, -20.0]



def analyze_market_for_auto(client):
    """تحليل Binance+MEXC → سلم مستويين + سبب. يُرجِع أيضاً مضاعف ميزانية عبر attribute على القائمة."""
    plan = analyze_btc_eth_pressure(client)
    levels = list(plan.levels)[:2]
    # نحفظ المضاعف ليستخدمه المستدعي
    analyze_market_for_auto.last_budget_mult = float(plan.budget_mult)
    analyze_market_for_auto.last_regime = plan.regime
    return levels, plan.reason



def place_crash_ladder(
    client,
    symbols: List[str],
    levels_pct: List[float],
    budget_usdt: float,
    mode: str = "manual",
    max_per_coin_usdt: Optional[float] = None,
) -> Dict:
    s = _SETTINGS
    max_coin = float(max_per_coin_usdt if max_per_coin_usdt is not None else s.max_per_coin_usdt)
    result = {
        "placed": [],
        "skipped": [],
        "errors": [],
        "cancelled": 0,
        "levels": levels_pct,
        "budget": budget_usdt,
        "max_per_coin": max_coin,
        "mode": mode,
        "tp_pct": s.tp_pct,
        "sl_pct": s.sl_pct,
    }
    if not symbols or not levels_pct or budget_usdt < 5:
        result["errors"].append("لا عملات أو ميزانية/مستويات غير كافية")
        return result

    # حد أقصى مستويان — حتى لو الإعدادات القديمة فيها 3
    levels_pct = list(levels_pct)[:2]
    result["levels"] = levels_pct

    try:
        free = float(client.get_free_usdt() or 0)
    except Exception as e:
        result["errors"].append(f"رصيد USDT: {e}")
        return result
    if free + 0.01 < min(budget_usdt, 10):
        result["errors"].append(f"USDT المتاح `{free:.2f}` أقل من المطلوب")
        return result

    budget_usdt = min(budget_usdt, free * 0.98)
    n_coins = len(symbols)
    n_lvl = len(levels_pct)
    per_coin_cap = min(max_coin, budget_usdt / n_coins)
    per_order = per_coin_cap / n_lvl
    if per_order < 1.0:
        result["errors"].append(
            f"حجم الأمر صغير ({per_order:.2f}$). زِد الميزانية أو قلّل العملات."
        )
        return result

    for sym in symbols:
        try:
            clr = client.cancel_open_buy_orders(sym)
            result["cancelled"] += len(clr.get("cancelled") or [])
        except Exception as e:
            result["errors"].append(f"{sym} إلغاء: {e}")

        try:
            px = float(client.get_ticker_price(f"{sym}/USDT") or 0)
        except Exception as e:
            result["errors"].append(f"{sym} سعر: {e}")
            continue
        if px <= 0:
            result["skipped"].append(f"{sym}: لا سعر")
            continue

        for pct in levels_pct:
            limit_px = px * (1.0 + pct / 100.0)
            if limit_px <= 0:
                continue
            amount = per_order / limit_px
            try:
                order = client.create_limit_buy(sym, amount, limit_px)
                if order is None:
                    result["skipped"].append(f"{sym} @{limit_px:.6g} ({pct:g}%): تحت الحد")
                    continue
                oid = str(order.get("id") or "")
                result["placed"].append({
                    "symbol": sym,
                    "pct": pct,
                    "price": limit_px,
                    "usdt": per_order,
                    "order_id": oid,
                })
                if oid:
                    _TRACK[oid] = TrackedOrder(
                        symbol=sym,
                        order_id=oid,
                        limit_price=limit_px,
                        usdt=per_order,
                        pct=pct,
                        placed_at=time.time(),
                        amount=amount,
                    )
                logger.info(
                    "[CRASH] place %s pct=%s price=%s usdt=%.2f id=%s",
                    sym, pct, limit_px, per_order, oid,
                )
            except Exception as e:
                result["errors"].append(f"{sym} {pct:g}%: {e}")

    return result


def cancel_crash_orders(client, symbols: List[str]) -> Dict:
    cancelled = 0
    errors = []
    for sym in symbols:
        try:
            r = client.cancel_open_buy_orders(sym)
            cancelled += len(r.get("cancelled") or [])
            errors.extend(r.get("errors") or [])
        except Exception as e:
            errors.append(f"{sym}: {e}")
    for t in _TRACK.values():
        if t.symbol in symbols and t.status == "open":
            t.status = "cancelled"
    return {"cancelled": cancelled, "errors": errors}


def monitor_crash_fills(client) -> List[str]:
    msgs: List[str] = []
    s = _SETTINGS
    if not _TRACK:
        return msgs

    open_ids = set()
    symbols = sorted({t.symbol for t in _TRACK.values() if t.status == "open"})
    for sym in symbols:
        try:
            orders = client.fetch_open_orders(sym) or []
            for o in orders:
                if o.get("id"):
                    open_ids.add(str(o["id"]))
        except Exception:
            continue

    for oid, t in list(_TRACK.items()):
        if t.status != "open":
            continue
        if oid in open_ids:
            continue
        try:
            px = float(client.get_ticker_price(f"{t.symbol}/USDT") or t.limit_price)
            free_amt = float(client.get_free_amount(t.symbol) or 0)
        except Exception:
            px = t.limit_price
            free_amt = 0.0

        if free_amt * px < 0.8:
            t.status = "cancelled"
            continue

        t.status = "filled"
        t.fill_price = t.limit_price
        t.amount = free_amt if free_amt > 0 else t.amount
        fill_px = t.fill_price or t.limit_price
        tp_px = fill_px * (1.0 + s.tp_pct / 100.0)
        try:
            sell_amt = t.amount * 0.997
            order = client.create_limit_sell(t.symbol, sell_amt, tp_px)
            if order and order.get("id"):
                t.tp_order_id = str(order["id"])
            msgs.append(
                f"🎯 *امتلاء اصطياد* `{t.symbol}`\n"
                f"دخول ~`{fill_px:.6g}` | هدف `{s.tp_pct:g}%` @ `{tp_px:.6g}`\n"
                f"وقف منطقي `{s.sl_pct:g}%` تحت الدخول"
            )
            logger.info("[CRASH] filled %s fill~%s tp=%s", t.symbol, fill_px, tp_px)
        except Exception as e:
            msgs.append(f"⚠️ امتلاء `{t.symbol}` لكن فشل وضع الهدف: {e}")
            logger.exception("crash tp place %s", t.symbol)

    for oid, t in list(_TRACK.items()):
        if t.status != "filled" or t.fill_price <= 0:
            continue
        try:
            px = float(client.get_ticker_price(f"{t.symbol}/USDT") or 0)
        except Exception:
            continue
        if px <= 0:
            continue
        sl_px = t.fill_price * (1.0 - s.sl_pct / 100.0)
        if px <= sl_px:
            try:
                if t.tp_order_id:
                    try:
                        client.cancel_order(t.tp_order_id, t.symbol, strict=False)
                    except Exception:
                        pass
                amt = float(client.get_free_amount(t.symbol) or 0) * 0.997
                if amt > 0:
                    client.create_market_order(f"{t.symbol}/USDT", "sell", amt)
                pnl_pct = (px / t.fill_price - 1.0) * 100.0
                msgs.append(
                    f"🛡 *وقف اصطياد* `{t.symbol}` @ `{px:.6g}` ({pnl_pct:+.1f}%)"
                )
                t.status = "cancelled"
                logger.info("[CRASH] SL %s px=%s", t.symbol, px)
            except Exception as e:
                logger.exception("crash SL %s: %s", t.symbol, e)

    return msgs


def format_result(res: Dict, title: str) -> str:
    lines = [
        f"🎯 *{title}*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"الوضع: `{res.get('mode')}`",
        f"المستويات: `{', '.join(str(x) for x in res.get('levels') or [])}` %",
        f"الميزانية: `{res.get('budget', 0):.2f}$` | سقف/عملة: `{res.get('max_per_coin', 0):.2f}$`",
        f"بعد الامتلاء: هدف `{res.get('tp_pct', 8):g}%` | وقف `{res.get('sl_pct', 5):g}%`",
        f"✅ أوامر وُضعت: `{len(res.get('placed') or [])}`",
        f"🗑 مُلغاة قديمة: `{res.get('cancelled', 0)}`",
    ]
    skipped = res.get("skipped") or []
    if skipped:
        lines.append(f"⏭ تخطي: `{len(skipped)}`")
    errs = res.get("errors") or []
    if errs:
        lines.append("⚠️ أخطاء:")
        for e in errs[:8]:
            lines.append(f"• {e}")
    placed = res.get("placed") or []
    if placed:
        lines.append("\n*عينات:*")
        for p in placed[:6]:
            lines.append(
                f"• `{p['symbol']}` {p['pct']:g}% @ `{p['price']:.6g}` (~{p['usdt']:.1f}$)"
            )
        if len(placed) > 6:
            lines.append(f"… و `{len(placed) - 6}` أخرى")
    lines.append("\n_بعد الامتلاء يُوضع هدف حد تلقائيًا ويُراقب الوقف._")
    return "\n".join(lines)


def status_summary() -> str:
    open_n = sum(1 for t in _TRACK.values() if t.status == "open")
    filled_n = sum(1 for t in _TRACK.values() if t.status == "filled")
    s = _SETTINGS
    return (
        f"متتبَّع: مفتوح `{open_n}` | ممتلئ `{filled_n}`\n"
        f"هدف `{s.tp_pct:g}%` | وقف `{s.sl_pct:g}%` | سقف/عملة `{s.max_per_coin_usdt:g}$`"
    )
