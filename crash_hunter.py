"""
اصطياد الانهيار — أوامر حد شراء على سلم هبوط داخل المحفظة.
يدوي: نسب ثابتة يختارها المستخدم.
تلقائي: نسب حسب شدة السوق (BTC / اتساع الهبوط).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class CrashPlan:
    levels_pct: List[float]  # سالبة مثل [-10, -20, -30]
    budget_usdt: float
    mode: str  # manual | auto
    reason: str = ""


def parse_levels(text: str) -> List[float]:
    """
    يقبل: -10,-20,-30 أو 10,20,30 أو -10% -20%
    يرجّع قائمة نسب سالبة مرتبة تصاعديًا (الأقرب للصفر أولاً).
    """
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
    # unique + sort: -10 then -20 then -30
    parts = sorted(set(round(x, 2) for x in parts), reverse=True)
    return parts


def default_manual_levels() -> List[float]:
    return [-10.0, -20.0, -30.0]


def analyze_market_for_auto(client) -> Tuple[List[float], str]:
    """
    يحلّل حركة BTC القصيرة ويختار سلم انهيار.
    لا يبالغ: أقصى عمق حسب الشدة.
    """
    chg_1h = 0.0
    chg_4h = 0.0
    try:
        ex = client.exchange
        if not getattr(ex, "markets", None):
            ex.load_markets()
        ohlcv = ex.fetch_ohlcv("BTC/USDT", timeframe="1h", limit=6)
        if ohlcv and len(ohlcv) >= 2:
            c0 = float(ohlcv[-2][4])
            c1 = float(ohlcv[-1][4])
            if c0 > 0:
                chg_1h = (c1 / c0 - 1.0) * 100.0
        ohlcv4 = ex.fetch_ohlcv("BTC/USDT", timeframe="4h", limit=4)
        if ohlcv4 and len(ohlcv4) >= 2:
            a = float(ohlcv4[-2][4])
            b = float(ohlcv4[-1][4])
            if a > 0:
                chg_4h = (b / a - 1.0) * 100.0
    except Exception as e:
        logger.warning("auto crash analyze fail: %s", e)
        return [-12.0, -20.0, -28.0], "تحليل افتراضي (تعذر قراءة BTC)"

    # شدة الهبوط (نستخدم الأسوأ)
    drop = min(chg_1h, chg_4h)
    if drop <= -6:
        levels = [-15.0, -25.0, -35.0]
        reason = f"هبوط حاد BTC 1h={chg_1h:+.1f}% 4h={chg_4h:+.1f}% → سلم عميق"
    elif drop <= -3:
        levels = [-12.0, -20.0, -28.0]
        reason = f"هبوط متوسط BTC 1h={chg_1h:+.1f}% 4h={chg_4h:+.1f}%"
    elif drop <= -1:
        levels = [-8.0, -15.0, -22.0]
        reason = f"ضعف خفيف BTC 1h={chg_1h:+.1f}% 4h={chg_4h:+.1f}%"
    else:
        levels = [-10.0, -18.0, -25.0]
        reason = f"سوق مستقر نسبيًا BTC 1h={chg_1h:+.1f}% 4h={chg_4h:+.1f}% → سلم وقائي"

    return levels, reason


def place_crash_ladder(
    client,
    symbols: List[str],
    levels_pct: List[float],
    budget_usdt: float,
    mode: str = "manual",
) -> Dict:
    """
    يلغي أوامر شراء مفتوحة قديمة لكل رمز ثم يضع سلم حد شراء.
    الميزانية تُقسَّم: على العملات ثم على المستويات بالتساوي.
    """
    result = {
        "placed": [],
        "skipped": [],
        "errors": [],
        "cancelled": 0,
        "levels": levels_pct,
        "budget": budget_usdt,
        "mode": mode,
    }
    if not symbols or not levels_pct or budget_usdt < 5:
        result["errors"].append("لا عملات أو ميزانية/مستويات غير كافية")
        return result

    n_coins = len(symbols)
    n_lvl = len(levels_pct)
    per_order = budget_usdt / (n_coins * n_lvl)
    if per_order < 1.0:
        result["errors"].append(
            f"حجم الأمر صغير جدًا ({per_order:.2f}$). زِد الميزانية أو قلّل العملات/المستويات."
        )
        return result

    for sym in symbols:
        # تنظيف أوامر شراء قديمة لهذا الزوج (اصطياد سابق)
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
                    result["skipped"].append(
                        f"{sym} @{limit_px:.6g} ({pct:g}%): تحت الحد الأدنى"
                    )
                    continue
                oid = order.get("id")
                result["placed"].append({
                    "symbol": sym,
                    "pct": pct,
                    "price": limit_px,
                    "usdt": per_order,
                    "order_id": oid,
                })
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
    return {"cancelled": cancelled, "errors": errors}


def format_result(res: Dict, title: str) -> str:
    lines = [
        f"🎯 *{title}*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"الوضع: `{res.get('mode')}`",
        f"المستويات: `{', '.join(str(x) for x in res.get('levels') or [])}` %",
        f"الميزانية: `{res.get('budget', 0):.2f}$`",
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
    lines.append(
        "\n_الأوامر حد شراء معلّقة على المنصة — تتنفّذ إذا وصل السعر للمستوى._"
    )
    return "\n".join(lines)
