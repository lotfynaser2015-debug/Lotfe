"""
حساب Central Pivot Range (CPR) من شمعة يومية سابقة.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class CPRLevels:
    pivot: float
    bc: float  # bottom central
    tc: float  # top central
    r1: float
    r2: float
    s1: float
    s2: float
    width_pct: float  # عرض CPR كنسبة من الـ pivot


def compute_cpr(high: float, low: float, close: float) -> Optional[CPRLevels]:
    if high <= 0 or low <= 0 or close <= 0 or high < low:
        return None
    pivot = (high + low + close) / 3.0
    bc = (high + low) / 2.0
    tc = (pivot - bc) + pivot
    # ترتيب TC فوق BC دائمًا
    if tc < bc:
        tc, bc = bc, tc
    r1 = 2 * pivot - low
    s1 = 2 * pivot - high
    r2 = pivot + (high - low)
    s2 = pivot - (high - low)
    width = (tc - bc) / pivot * 100.0 if pivot else 0.0
    return CPRLevels(
        pivot=pivot, bc=bc, tc=tc,
        r1=r1, r2=r2, s1=s1, s2=s2,
        width_pct=width,
    )


def cpr_from_ohlcv(ohlcv: List[list]) -> Optional[CPRLevels]:
    """
    ohlcv من ccxt: [ts, o, h, l, c, vol]
    نستخدم آخر شمعة مكتملة (قبل الحالية إن وُجدت).
    """
    if not ohlcv or len(ohlcv) < 2:
        if ohlcv and len(ohlcv) == 1:
            row = ohlcv[0]
            return compute_cpr(float(row[2]), float(row[3]), float(row[4]))
        return None
    # الشمعة قبل الأخيرة = يوم مكتمل غالبًا
    row = ohlcv[-2]
    return compute_cpr(float(row[2]), float(row[3]), float(row[4]))
