# -*- coding: utf-8 -*-
"""
Smart TP / SL levels based on coin behaviour (ATR + structure).
Lightweight: heavy OHLCV work runs only at creation or manual refresh.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Any
import logging

logger = logging.getLogger(__name__)


@dataclass
class SmartLevels:
    symbol: str
    entry: float
    atr: float
    atr_pct: float
    volatility: str          # low | medium | high
    trend: str               # up | down | sideways
    tp1_price: float
    tp2_price: float
    tp3_price: float
    sl_price: float
    tp1_pct: float
    tp2_pct: float
    tp3_pct: float
    stop_loss_pct: float
    trail_atr_mult: float
    reason: str

    def as_pct_dict(self) -> Dict[str, float]:
        return {
            "tp1_pct": round(self.tp1_pct, 3),
            "tp2_pct": round(self.tp2_pct, 3),
            "tp3_pct": round(self.tp3_pct, 3),
            "stop_loss_pct": round(self.stop_loss_pct, 3),
        }


def _sma(values: List[float], period: int) -> float:
    if not values or period <= 0:
        return 0.0
    window = values[-period:]
    if not window:
        return 0.0
    return sum(window) / len(window)


def _atr_from_ohlcv(candles: List[List[float]], period: int = 14) -> float:
    """Classic ATR from OHLCV rows [ts, o, h, l, c, v]."""
    if not candles or len(candles) < period + 1:
        return 0.0
    trs: List[float] = []
    for i in range(1, len(candles)):
        high = float(candles[i][2])
        low = float(candles[i][3])
        prev_close = float(candles[i - 1][4])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    if len(trs) < period:
        return sum(trs) / len(trs) if trs else 0.0
    return sum(trs[-period:]) / period


def classify_volatility(atr_pct: float) -> str:
    if atr_pct < 1.8:
        return "low"
    if atr_pct < 4.5:
        return "medium"
    return "high"


def classify_trend(closes: List[float]) -> str:
    if len(closes) < 30:
        return "sideways"
    last = closes[-1]
    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50) if len(closes) >= 50 else sma20
    if last > sma20 * 1.004 and sma20 >= sma50 * 0.998:
        return "up"
    if last < sma20 * 0.996 and sma20 <= sma50 * 1.002:
        return "down"
    return "sideways"


def compute_smart_levels(
    symbol: str,
    entry: float,
    candles: List[List[float]],
    *,
    fallback_tp1: float = 3.0,
    fallback_tp2: float = 5.0,
    fallback_tp3: float = 8.0,
    fallback_sl: float = 3.0,
) -> SmartLevels:
    """
    Build adaptive targets from ATR + trend.

    Multipliers (relative to ATR %):
      low vol   → tighter targets, tighter SL
      medium    → balanced
      high vol  → wider targets, wider SL (room for pumps)
      strong up → stretch TP3 further
    """
    symbol = (symbol or "").upper()
    entry = float(entry or 0)
    if entry <= 0:
        return SmartLevels(
            symbol=symbol, entry=0, atr=0, atr_pct=0, volatility="medium",
            trend="sideways",
            tp1_price=0, tp2_price=0, tp3_price=0, sl_price=0,
            tp1_pct=fallback_tp1, tp2_pct=fallback_tp2, tp3_pct=fallback_tp3,
            stop_loss_pct=fallback_sl, trail_atr_mult=1.0,
            reason="no entry price — used fallback %",
        )

    atr = _atr_from_ohlcv(candles, 14)
    closes = [float(c[4]) for c in candles] if candles else []
    atr_pct = (atr / entry) * 100.0 if atr > 0 else 0.0
    vol = classify_volatility(atr_pct) if atr_pct > 0 else "medium"
    trend = classify_trend(closes)

    # Base multiples of ATR for targets / SL
    if vol == "low":
        m1, m2, m3, msl, trail = 1.0, 1.8, 2.8, 1.1, 0.9
    elif vol == "high":
        m1, m2, m3, msl, trail = 1.3, 2.4, 4.0, 1.5, 1.3
    else:
        m1, m2, m3, msl, trail = 1.1, 2.0, 3.3, 1.25, 1.1

    if trend == "up":
        m2 *= 1.1
        m3 *= 1.25
    elif trend == "down":
        m1 *= 0.9
        m2 *= 0.85
        m3 *= 0.8
        msl *= 0.95

    if atr_pct <= 0:
        # Fallback to user defaults when not enough candle data
        tp1_pct, tp2_pct, tp3_pct, sl_pct = fallback_tp1, fallback_tp2, fallback_tp3, fallback_sl
        reason = "insufficient candles — used configured %"
    else:
        tp1_pct = max(1.2, min(12.0, atr_pct * m1))
        tp2_pct = max(tp1_pct + 0.8, min(18.0, atr_pct * m2))
        tp3_pct = max(tp2_pct + 1.0, min(28.0, atr_pct * m3))
        sl_pct = max(1.0, min(10.0, atr_pct * msl))
        # Never let SL be wider than TP1 in a silly way for low-vol coins
        if sl_pct > tp1_pct * 1.15:
            sl_pct = tp1_pct * 1.05
        reason = f"ATR={atr_pct:.2f}% vol={vol} trend={trend}"

    tp1 = entry * (1 + tp1_pct / 100.0)
    tp2 = entry * (1 + tp2_pct / 100.0)
    tp3 = entry * (1 + tp3_pct / 100.0)
    sl = entry * (1 - sl_pct / 100.0)

    return SmartLevels(
        symbol=symbol,
        entry=entry,
        atr=atr,
        atr_pct=round(atr_pct, 4),
        volatility=vol,
        trend=trend,
        tp1_price=tp1,
        tp2_price=tp2,
        tp3_price=tp3,
        sl_price=sl,
        tp1_pct=tp1_pct,
        tp2_pct=tp2_pct,
        tp3_pct=tp3_pct,
        stop_loss_pct=sl_pct,
        trail_atr_mult=trail,
        reason=reason,
    )


def trailing_stop_price(
    current_price: float,
    current_sl: float,
    atr: float = 0.0,
    trail_atr_mult: float = 1.1,
    min_trail_pct: float = 1.2,
) -> float:
    """
    Only moves SL upward. Prefer ATR distance; fall back to min_trail_pct.
    """
    price = float(current_price or 0)
    sl = float(current_sl or 0)
    if price <= 0:
        return sl
    if atr and atr > 0:
        candidate = price - (atr * trail_atr_mult)
    else:
        candidate = price * (1 - min_trail_pct / 100.0)
    if candidate > sl:
        return candidate
    return sl


def reentry_trigger_price(stop_hit_price: float, recover_pct: float = 1.2) -> float:
    """Price that must be reached after a stop for automatic re-entry."""
    p = float(stop_hit_price or 0)
    if p <= 0:
        return 0.0
    return p * (1 + recover_pct / 100.0)
