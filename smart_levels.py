# -*- coding: utf-8 -*-
"""
Smart TP / SL levels based on coin behaviour (ATR + structure + floors).
Lightweight: heavy OHLCV work runs only at creation or manual refresh.

Design goals:
- Adapt to each coin's volatility and trend
- Never produce absurdly tight targets (floors) so pumps can develop
- Keep SL reasonable vs TP1 for a usable R:R
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List
import logging

logger = logging.getLogger(__name__)


# ---- Absolute floors / ceilings (percent) so low-ATR hours don't kill trades ----
FLOOR_TP1 = 3.0
FLOOR_TP2 = 6.0
FLOOR_TP3 = 12.0
FLOOR_SL = 2.0

CAP_TP1 = 12.0
CAP_TP2 = 20.0
CAP_TP3 = 35.0
CAP_SL = 8.0


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


def _recent_range_pct(candles: List[List[float]], entry: float, lookback: int = 40) -> float:
    """Highest high - lowest low over recent bars as % of entry (pump room signal)."""
    if not candles or entry <= 0:
        return 0.0
    window = candles[-lookback:] if len(candles) >= lookback else candles
    highs = [float(c[2]) for c in window]
    lows = [float(c[3]) for c in window]
    if not highs or not lows:
        return 0.0
    span = max(highs) - min(lows)
    return (span / entry) * 100.0


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
    Smart adaptive targets:

    1) Start from ATR-based multiples (behaviour of the coin)
    2) Apply trend stretch (up → wider runners, down → tighter)
    3) Blend in recent range (if the coin already swings hard, give room)
    4) Enforce floors so quiet hours don't produce 1% targets
    5) Cap extremes so a crazy ATR doesn't set 40% targets
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
    range_pct = _recent_range_pct(candles, entry, 40)
    vol = classify_volatility(atr_pct) if atr_pct > 0 else "medium"
    trend = classify_trend(closes)

    if atr_pct <= 0:
        tp1_pct, tp2_pct, tp3_pct, sl_pct = fallback_tp1, fallback_tp2, fallback_tp3, fallback_sl
        trail = 1.1
        reason = "insufficient candles — used configured %"
    else:
        # ATR multiples — wider than v1 so quiet coins still have room
        if vol == "low":
            m1, m2, m3, msl, trail = 1.6, 3.0, 5.0, 1.35, 1.0
        elif vol == "high":
            m1, m2, m3, msl, trail = 1.8, 3.5, 6.0, 1.6, 1.35
        else:
            m1, m2, m3, msl, trail = 1.7, 3.2, 5.5, 1.45, 1.15

        if trend == "up":
            m1 *= 1.05
            m2 *= 1.15
            m3 *= 1.30
        elif trend == "down":
            m1 *= 0.95
            m2 *= 0.90
            m3 *= 0.85
            msl *= 0.95

        # Raw ATR-based
        raw_tp1 = atr_pct * m1
        raw_tp2 = atr_pct * m2
        raw_tp3 = atr_pct * m3
        raw_sl = atr_pct * msl

        # Blend a fraction of recent range into the runner (TP3) and TP2
        # so coins that already moved in a wide band get more room for pumps
        if range_pct > 0:
            raw_tp2 = max(raw_tp2, range_pct * 0.35)
            raw_tp3 = max(raw_tp3, range_pct * 0.55)

        # Floors: never tighter than these (your main complaint)
        tp1_pct = max(FLOOR_TP1, min(CAP_TP1, raw_tp1))
        tp2_pct = max(FLOOR_TP2, min(CAP_TP2, raw_tp2))
        tp3_pct = max(FLOOR_TP3, min(CAP_TP3, raw_tp3))
        sl_pct = max(FLOOR_SL, min(CAP_SL, raw_sl))

        # Keep ordering: TP1 < TP2 < TP3
        if tp2_pct < tp1_pct + 1.2:
            tp2_pct = tp1_pct + 1.5
        if tp3_pct < tp2_pct + 1.5:
            tp3_pct = tp2_pct + 2.0

        # SL should stay meaningful but not wider than ~80% of TP1 (R:R bias)
        if sl_pct > tp1_pct * 0.85:
            sl_pct = max(FLOOR_SL, tp1_pct * 0.75)

        reason = (
            f"ATR={atr_pct:.2f}% range40={range_pct:.1f}% "
            f"vol={vol} trend={trend}"
        )

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
        tp1_pct=round(tp1_pct, 3),
        tp2_pct=round(tp2_pct, 3),
        tp3_pct=round(tp3_pct, 3),
        stop_loss_pct=round(sl_pct, 3),
        trail_atr_mult=trail if atr_pct > 0 else 1.1,
        reason=reason,
    )


def trailing_stop_price(
    current_price: float,
    current_sl: float,
    atr: float = 0.0,
    trail_atr_mult: float = 1.15,
    min_trail_pct: float = 1.8,
) -> float:
    """Only moves SL upward. Prefer ATR distance; fall back to min_trail_pct."""
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
