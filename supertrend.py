"""
حساب SuperTrend الحقيقي من شموع OHLCV (نفس منطق المؤشر الشائع).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class SuperTrendResult:
    value: float
    direction: int  # 1 = صاعد (شراء) ، -1 = هابط (بيع)
    atr: float
    prev_direction: int
    flipped: bool  # تحوّل في آخر شمعة مغلقة


def _rma(values: List[float], period: int) -> List[float]:
    """Wilder RMA (يستخدم في ATR الكلاسيكي)."""
    out = [0.0] * len(values)
    if len(values) < period:
        return out
    s = sum(values[:period]) / period
    out[period - 1] = s
    alpha = 1.0 / period
    for i in range(period, len(values)):
        s = alpha * values[i] + (1 - alpha) * s
        out[i] = s
    return out


def compute_supertrend(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    period: int = 10,
    multiplier: float = 3.0,
) -> Optional[SuperTrendResult]:
    """
    يحسب SuperTrend على سلسلة الشموع.
    يعتمد على آخر شمعة مكتملة (قبل الحالية غالباً إن كانت تتشكل).
    """
    n = len(closes)
    if n < period + 3 or len(highs) != n or len(lows) != n:
        return None

    tr = [0.0] * n
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )

    atr = _rma(tr, period)
    hl2 = [(highs[i] + lows[i]) / 2.0 for i in range(n)]

    basic_upper = [0.0] * n
    basic_lower = [0.0] * n
    final_upper = [0.0] * n
    final_lower = [0.0] * n
    direction = [1] * n
    st = [0.0] * n

    for i in range(period - 1, n):
        basic_upper[i] = hl2[i] + multiplier * atr[i]
        basic_lower[i] = hl2[i] - multiplier * atr[i]

        if i == period - 1:
            final_upper[i] = basic_upper[i]
            final_lower[i] = basic_lower[i]
            direction[i] = 1 if closes[i] >= final_lower[i] else -1
            st[i] = final_lower[i] if direction[i] == 1 else final_upper[i]
            continue

        # final bands
        if basic_lower[i] > final_lower[i - 1] or closes[i - 1] < final_lower[i - 1]:
            final_lower[i] = basic_lower[i]
        else:
            final_lower[i] = final_lower[i - 1]

        if basic_upper[i] < final_upper[i - 1] or closes[i - 1] > final_upper[i - 1]:
            final_upper[i] = basic_upper[i]
        else:
            final_upper[i] = final_upper[i - 1]

        # direction
        if direction[i - 1] == 1:
            if closes[i] < final_lower[i]:
                direction[i] = -1
            else:
                direction[i] = 1
        else:
            if closes[i] > final_upper[i]:
                direction[i] = 1
            else:
                direction[i] = -1

        st[i] = final_lower[i] if direction[i] == 1 else final_upper[i]

    # آخر شمعة مكتملة: نستخدم i = n-2 لو الأخيرة لسه تتكون، وإلا n-1
    # عمليًا نستخدم آخر شمعة في السلسلة (ccxt عادةً تتضمن الحالية)
    i = n - 1
    prev_i = n - 2
    flipped = direction[i] != direction[prev_i] if prev_i >= period else False
    return SuperTrendResult(
        value=st[i],
        direction=direction[i],
        atr=atr[i],
        prev_direction=direction[prev_i] if prev_i >= 0 else direction[i],
        flipped=flipped,
    )


def signal_label(direction: int) -> str:
    return "شراء" if direction == 1 else "بيع"
