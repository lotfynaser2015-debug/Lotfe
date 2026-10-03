"""
محرك دفتر الأوامر — مصادر: Binance (مرجع عام) + MEXC (منصة التنفيذ).
مجاني عبر ccxt public + عميل MEXC الحالي.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_binance = None


def _get_binance():
    global _binance
    if _binance is None:
        try:
            import ccxt
            _binance = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "spot"}})
        except Exception as e:
            logger.warning("binance init fail: %s", e)
            _binance = False
    return _binance if _binance is not False else None


def _pct_change(ex, pair: str, tf: str, limit: int = 6) -> float:
    try:
        ohlcv = ex.fetch_ohlcv(pair, timeframe=tf, limit=limit)
        if ohlcv and len(ohlcv) >= 2:
            a, b = float(ohlcv[-2][4]), float(ohlcv[-1][4])
            if a > 0:
                return (b / a - 1.0) * 100.0
    except Exception:
        pass
    return 0.0


def _book_metrics(ex, pair: str, depth: int = 20) -> Dict[str, float]:
    """imbalance in [-1,1], bid_usdt, ask_usdt."""
    out = {"imbalance": 0.0, "bid_usdt": 0.0, "ask_usdt": 0.0}
    try:
        ob = ex.fetch_order_book(pair, limit=depth)
        bids, asks = ob.get("bids") or [], ob.get("asks") or []
        bid_usdt = sum(float(p) * float(q) for p, q in bids[:depth])
        ask_usdt = sum(float(p) * float(q) for p, q in asks[:depth])
        total = bid_usdt + ask_usdt
        imb = ((bid_usdt - ask_usdt) / total) if total > 0 else 0.0
        out = {"imbalance": imb, "bid_usdt": bid_usdt, "ask_usdt": ask_usdt}
    except Exception as e:
        logger.debug("book %s %s: %s", getattr(ex, "id", "?"), pair, e)
    return out


@dataclass
class PressurePlan:
    score: float  # 0..100 ضغط بيع/هبوط (أعلى = بيع أقوى)
    buy_score: float  # 0..100 ضغط شراء
    levels: List[float]
    budget_mult: float
    regime: str  # strong_sell | sell | neutral | buy | strong_buy
    reason: str


def analyze_btc_eth_pressure(mexc_client) -> PressurePlan:
    """
    يجمع Binance + MEXC لـ BTC/ETH.
    score عالي = ضغط بيع → سلم أعمق / أوامر شراء أبعد.
    buy_score عالي = ضغط شراء → سلم أقرب.
    """
    try:
        mexc = mexc_client.exchange
        if not getattr(mexc, "markets", None):
            mexc.load_markets()
    except Exception as e:
        return PressurePlan(
            40, 40, [-10.0, -20.0], 1.0, "neutral",
            f"تعذر MEXC: {e}",
        )

    bn = _get_binance()
    pairs = ["BTC/USDT", "ETH/USDT"]
    imbs_m, imbs_b = [], []
    drops = []

    for pair in pairs:
        mm = _book_metrics(mexc, pair)
        imbs_m.append(mm["imbalance"])
        drops.append(_pct_change(mexc, pair, "1h"))
        drops.append(_pct_change(mexc, pair, "4h"))
        if bn:
            try:
                if not getattr(bn, "markets", None):
                    bn.load_markets()
            except Exception:
                pass
            bb = _book_metrics(bn, pair)
            imbs_b.append(bb["imbalance"])

    imb_m = sum(imbs_m) / max(1, len(imbs_m))
    imb_b = sum(imbs_b) / max(1, len(imbs_b)) if imbs_b else imb_m
    # وزن: Binance 0.55 + MEXC 0.45 إن وُجد
    if imbs_b:
        book = 0.55 * imb_b + 0.45 * imb_m
    else:
        book = imb_m
    price_drop = min(drops) if drops else 0.0

    sell_score = 50.0
    if price_drop <= -6:
        sell_score += 25
    elif price_drop <= -3:
        sell_score += 15
    elif price_drop <= -1:
        sell_score += 8
    elif price_drop >= 3:
        sell_score -= 15
    elif price_drop >= 1:
        sell_score -= 8

    # book سالب = بيع
    if book <= -0.30:
        sell_score += 30
    elif book <= -0.15:
        sell_score += 18
    elif book <= -0.05:
        sell_score += 8
    elif book >= 0.30:
        sell_score -= 25
    elif book >= 0.15:
        sell_score -= 15
    elif book >= 0.05:
        sell_score -= 8

    sell_score = max(0.0, min(100.0, sell_score))
    buy_score = max(0.0, min(100.0, 100.0 - sell_score))

    # سلم حسب النظام:
    # بيع قوي → أوامر أعمق | شراء قوي → أوامر أقرب
    if sell_score >= 70:
        levels, mult, regime, tag = [-15.0, -28.0], 1.25, "strong_sell", "ضغط بيع قوي → أوامر شراء أعمق + ميزانية أعلى"
    elif sell_score >= 55:
        levels, mult, regime, tag = [-12.0, -24.0], 1.10, "sell", "ضغط بيع → سلم أعمق"
    elif buy_score >= 70:
        levels, mult, regime, tag = [-4.0, -9.0], 0.85, "strong_buy", "ضغط شراء قوي → أوامر أقرب (أقل خصم)"
    elif buy_score >= 55:
        levels, mult, regime, tag = [-6.0, -12.0], 0.95, "buy", "ضغط شراء → سلم أقرب"
    else:
        levels, mult, regime, tag = [-10.0, -20.0], 1.0, "neutral", "متوازن → سلم وقائي"

    src = "Binance+MEXC" if imbs_b else "MEXC فقط"
    reason = (
        f"{tag}\n"
        f"مصدر: {src} | بيع `{sell_score:.0f}` شراء `{buy_score:.0f}`\n"
        f"دفتر موحّد `{book:+.2f}` | هبوط `{price_drop:+.1f}%`\n"
        f"MEXC imb `{imb_m:+.2f}`"
        + (f" | Binance imb `{imb_b:+.2f}`" if imbs_b else "")
    )
    return PressurePlan(sell_score, buy_score, levels, mult, regime, reason)


def symbol_book_signal(mexc_client, symbol: str) -> Tuple[str, float, str]:
    """
    إشارة لكل عملة على MEXC: buy / sell / wait + قوة 0..100
    """
    pair = f"{symbol}/USDT" if "/" not in symbol else symbol
    try:
        ex = mexc_client.exchange
        if not getattr(ex, "markets", None):
            ex.load_markets()
        m = _book_metrics(ex, pair, depth=15)
        chg = _pct_change(ex, pair, "15m") if hasattr(ex, "fetch_ohlcv") else 0.0
        # 15m may fail on some - try 5m
        if chg == 0.0:
            chg = _pct_change(ex, pair, "5m")
    except Exception as e:
        return "wait", 0.0, str(e)[:80]

    imb = m["imbalance"]
    # قوة شراء
    strength = max(0.0, min(100.0, 50 + imb * 80 + (5 if chg > 0.3 else -5 if chg < -0.3 else 0)))
    if imb >= 0.18 and chg > -0.5:
        return "buy", strength, f"imb `{imb:+.2f}` chg `{chg:+.2f}%`"
    if imb <= -0.18 and chg < 0.5:
        return "sell", 100 - strength, f"imb `{imb:+.2f}` chg `{chg:+.2f}%`"
    return "wait", 50.0, f"imb `{imb:+.2f}` chg `{chg:+.2f}%`"
