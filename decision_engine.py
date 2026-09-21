# -*- coding: utf-8 -*-
"""
محرك القرار — يربط نظام الخبراء بمحرك الإشارات في البوت
- أنت تحدد التايم فريم
- القرار يخرج مع السبب + أكثر موافق + أكثر معارض
- خبير المخاطر يمنع التنفيذ لو الشروط مش مستوفاة
- التنفيذ التلقائي يتم فقط عبر صيغة الإشارة المعتمدة في البوت (بعد موافقة المخاطر)
"""
from __future__ import annotations

import logging
from typing import Optional, Dict, Any, List

from experts import ExpertsSystem, DecisionResult, run_decision

logger = logging.getLogger(__name__)


class DecisionEngine:
    def __init__(self):
        self.system = ExpertsSystem()
        # محافظ افتراضية — عدّلها من الإعدادات أو عند الاستدعاء
        self.default_portfolios = "1"
        self.default_timeframe = "1h"
        # أمان افتراضي: التحليل والتقرير فقط. التفعيل يحتاج بيانات حية ومراجعة صريحة.
        self.auto_execute_enabled = False

    def set_timeframe(self, tf: str):
        allowed = {"15m", "1h", "4h", "1d", "1w"}
        if tf not in allowed:
            tf = "1h"
        self.default_timeframe = tf
        return tf

    def analyze(
        self,
        timeframe: Optional[str] = None,
        portfolios: Optional[str] = None,
        market_context: Optional[Dict[str, Any]] = None,
    ) -> DecisionResult:
        tf = timeframe or self.default_timeframe
        pfs = portfolios or self.default_portfolios
        result = self.system.decide(
            timeframe=tf,
            market_context=market_context,
            portfolios=pfs,
        )
        logger.info(
            "قرار الخبراء: %s | فيتو=%s | %s",
            result.final_action,
            result.risk_veto,
            result.risk_message,
        )
        return result

    def build_executable_signal(
        self,
        result: DecisionResult,
        portfolios: Optional[str] = None,
        source_name: str = "ExpertsSystem",
    ) -> Optional[str]:
        """
        يرجع نص إشارة جاهز لمحرك البوت فقط لو القرار BUY أو SELL
        ومر من خبير المخاطر بدون فيتو.
        """
        if not self.auto_execute_enabled:
            return None
        if result.risk_veto:
            return None
        if result.final_action not in ("BUY", "SELL"):
            return None
        pfs = portfolios or self.default_portfolios
        return result.to_signal_text(source_name=source_name, portfolios=pfs)

    def full_report(self, result: DecisionResult) -> str:
        action_ar = {"BUY": "شراء", "SELL": "بيع", "FLAT": "انتظار"}.get(
            result.final_action, result.final_action
        )
        size_ar = {"small": "صغير", "medium": "متوسط", "full": "كامل"}.get(
            result.size_hint, result.size_hint
        )
        lines = [
            "════════════════════════",
            "📋 تقرير الخبراء",
            "════════════════════════",
            f"القرار: {action_ar} ({result.final_action})",
            f"التايم فريم: {result.timeframe}",
            f"الثقة: {result.confidence:.0%}",
            f"فيتو المخاطر: {'نعم — ' + result.risk_message if result.risk_veto else 'لا'}",
            f"الحجم المقترح: {size_ar}",
            f"أكثر موافق: {result.most_agree or '—'}",
            f"أكثر معارض: {result.most_disagree or '—'}",
            "",
            "أصوات الخبراء:",
        ]
        for v in result.votes:
            dec = {"BUY": "شراء", "SELL": "بيع", "FLAT": "انتظار"}.get(v.decision, v.decision)
            lines.append(f"• {v.name}: {dec} ({v.confidence:.0%}) — {v.reason}")
        lines.append("")
        lines.append(f"ملخص: {result.reason_summary}")
        lines.append("════════════════════════")
        return "\n".join(lines)


# واجهة مختصرة للاستخدام من البوت أو سكربت خارجي
_engine: Optional[DecisionEngine] = None


def get_engine() -> DecisionEngine:
    global _engine
    if _engine is None:
        _engine = DecisionEngine()
    return _engine


def decide_now(
    timeframe: str = "1h",
    portfolios: str = "1",
    **market_context,
) -> DecisionResult:
    return get_engine().analyze(
        timeframe=timeframe,
        portfolios=portfolios,
        market_context=market_context or None,
    )
