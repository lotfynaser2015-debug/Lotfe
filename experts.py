# -*- coding: utf-8 -*-
"""
نظام الخبراء متعدد الوكلاء + خبير المخاطر
قرار دخول / خروج / انتظار مع أسباب واضحة وحماية رأس المال
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


@dataclass
class ExpertVote:
    name: str
    decision: str  # BUY | SELL | FLAT
    confidence: float  # 0.0 - 1.0
    reason: str
    weight: float = 1.0


@dataclass
class DecisionResult:
    final_action: str  # BUY | SELL | FLAT
    confidence: float
    reason_summary: str
    votes: List[ExpertVote] = field(default_factory=list)
    most_agree: Optional[str] = None
    most_disagree: Optional[str] = None
    risk_veto: bool = False
    risk_message: str = ""
    timeframe: str = "1h"
    size_hint: str = "small"  # small | medium | full
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_signal_text(
        self,
        source_name: str = "ExpertsSystem",
        portfolios: str = "1",
    ) -> str:
        """صيغة متوافقة مع محرك الإشارات في البوت"""
        action = self.final_action if self.final_action in ("BUY", "SELL") else "FLAT"
        if action == "FLAT":
            return (
                f"إشارة\n"
                f"المصدر: {source_name}\n"
                f"القرار: انتظار\n"
                f"السبب: {self.reason_summary}\n"
                f"المحافظ: {portfolios}\n"
                f"الحجم: {self.size_hint}\n"
                f"التايم فريم: {self.timeframe}\n"
                f"الثقة: {self.confidence:.0%}\n"
                f"أكثر موافق: {self.most_agree or '-'}\n"
                f"أكثر معارض: {self.most_disagree or '-'}"
            )
        return (
            f"#SIGNAL\n"
            f"Source: {source_name}\n"
            f"Action: {action}\n"
            f"Reason: {self.reason_summary} | TF:{self.timeframe} | Conf:{self.confidence:.0%} | "
            f"موافق:{self.most_agree or '-'} | معارض:{self.most_disagree or '-'}\n"
            f"Portfolios: {portfolios}\n"
            f"Size: {self.size_hint}"
        )


class RiskManager:
    """خبير المخاطر — فيتو مطلق + حجم صغير + شروط سلامة"""

    MAX_POSITION_PCT = 0.01          # 1% كحد أقصى من رأس المال للفكرة الواحدة
    MIN_RR = 1.6                     # أقل نسبة مخاطرة/عائد مقبولة
    MAX_CONSECUTIVE_LOSSES = 3
    ALLOWED_ACTIONS_WITHOUT_VETO = {"FLAT"}

    def __init__(self):
        self.consecutive_losses = 0
        self.enabled = True

    def evaluate(
        self,
        proposed: str,
        votes: List[ExpertVote],
        market_context: Optional[Dict[str, Any]] = None,
    ) -> tuple[str, bool, str, str]:
        """
        يرجع: (القرار النهائي, هل حصل فيتو, رسالة المخاطر, حجم مقترح)
        """
        market_context = market_context or {}
        if not self.enabled:
            return proposed, False, "المخاطر معطلة", "small"

        if self.consecutive_losses >= self.MAX_CONSECUTIVE_LOSSES:
            return "FLAT", True, f"توقف مؤقت بعد {self.consecutive_losses} خسائر متتالية", "small"

        # رفض أي دخول/خروج لو الإجماع ضعيف
        buy_score = sum(v.weight * v.confidence for v in votes if v.decision == "BUY")
        sell_score = sum(v.weight * v.confidence for v in votes if v.decision == "SELL")
        flat_score = sum(v.weight * v.confidence for v in votes if v.decision == "FLAT")
        total = buy_score + sell_score + flat_score or 1.0

        # سيولة / حجم ضعيف → فيتو
        volume_weak = market_context.get("volume_weak", True)
        consolidation = market_context.get("consolidation", True)
        rr_ok = market_context.get("rr_ok", False)
        dominant = max(buy_score, sell_score)

        if proposed in ("BUY", "SELL"):
            if volume_weak:
                return "FLAT", True, "السيولة والأحجام ضعيفة — رفض الدخول", "small"
            if consolidation and not market_context.get("breakout_confirmed", False):
                return "FLAT", True, "السوق في نطاق ضيق بدون كسر مؤكد", "small"
            if not rr_ok:
                return "FLAT", True, "نسبة المخاطرة للعائد غير مقبولة", "small"
            # لازم أغلبية واضحة
            if dominant / total < 0.55:
                return "FLAT", True, "الإجماع بين الخبراء ضعيف", "small"

        size = "small"
        if proposed in ("BUY", "SELL") and dominant / total >= 0.75 and not volume_weak:
            size = "medium"

        return proposed, False, "تم قبول الشروط مع حجم محافظ", size

    def record_loss(self):
        self.consecutive_losses += 1

    def record_win(self):
        self.consecutive_losses = 0


class ExpertsSystem:
    """
    الخبراء:
    1 خبير السلسلة والحيتان
    2 خبير السيولة والأحجام
    3 خبير الشارت
    4 خبير حالة السوق
    5 خبير مزاج الناس
    6 خبير الأخبار
    7 خبير المخاطر (فيتو)
    """

    def __init__(self):
        self.risk = RiskManager()
        self.default_timeframe = "1h"

    def _expert_onchain(self, ctx: Dict) -> ExpertVote:
        netflow = ctx.get("netflow", "neutral")  # inflow / outflow / neutral
        whales = ctx.get("whales", "neutral")    # accumulate / distribute / neutral
        if whales == "accumulate" or netflow == "outflow":
            return ExpertVote("خبير السلسلة والحيتان", "BUY", 0.65, "تجميع حيتان أو خروج من البورصات", 1.3)
        if whales == "distribute" or netflow == "inflow":
            return ExpertVote("خبير السلسلة والحيتان", "SELL", 0.60, "توزيع أو دخول للبورصات", 1.3)
        return ExpertVote("خبير السلسلة والحيتان", "FLAT", 0.55, "لا تجميع واضح من الحيتان", 1.3)

    def _expert_orderflow(self, ctx: Dict) -> ExpertVote:
        volume_weak = ctx.get("volume_weak", True)
        candle_strength = ctx.get("candle_strength", "weak")  # strong / weak
        if volume_weak or candle_strength == "weak":
            return ExpertVote("خبير السيولة والأحجام", "FLAT", 0.70, "حجم ضعيف وخروج/دخول غير قوي", 1.4)
        bias = ctx.get("orderflow_bias", "neutral")
        if bias == "buy":
            return ExpertVote("خبير السيولة والأحجام", "BUY", 0.60, "شراء أقوى من البيع مع حجم", 1.4)
        if bias == "sell":
            return ExpertVote("خبير السيولة والأحجام", "SELL", 0.60, "بيع أقوى من الشراء مع حجم", 1.4)
        return ExpertVote("خبير السيولة والأحجام", "FLAT", 0.55, "السيولة محايدة", 1.4)

    def _expert_technical(self, ctx: Dict) -> ExpertVote:
        trend = ctx.get("trend", "sideways")  # up / down / sideways
        momentum = ctx.get("momentum", "cool")  # strong / cool / weak
        if trend == "up" and momentum == "strong":
            return ExpertVote("خبير الشارت", "BUY", 0.70, "اتجاه صاعد وزخم قوي", 1.2)
        if trend == "down" and momentum == "strong":
            return ExpertVote("خبير الشارت", "SELL", 0.70, "اتجاه هابط وزخم قوي", 1.2)
        if trend == "up" and momentum == "cool":
            return ExpertVote("خبير الشارت", "FLAT", 0.60, "اتجاه صاعد لكن الزخم مبرد", 1.2)
        return ExpertVote("خبير الشارت", "FLAT", 0.55, "الشارت في تردد أو نطاق", 1.2)

    def _expert_regime(self, ctx: Dict) -> ExpertVote:
        regime = ctx.get("regime", "consolidation")
        if regime == "uptrend":
            return ExpertVote("خبير حالة السوق", "BUY", 0.55, "السوق في ترند صاعد", 1.0)
        if regime == "downtrend":
            return ExpertVote("خبير حالة السوق", "SELL", 0.55, "السوق في ترند هابط", 1.0)
        return ExpertVote("خبير حالة السوق", "FLAT", 0.65, "مرحلة توقف أو نطاق", 1.0)

    def _expert_mood(self, ctx: Dict) -> ExpertVote:
        mood = ctx.get("mood", "greed")  # fear / greed / extreme_fear / extreme_greed / neutral
        if mood == "extreme_fear":
            return ExpertVote("خبير مزاج الناس", "BUY", 0.60, "خوف شديد — غالباً فرصة", 0.9)
        if mood == "extreme_greed":
            return ExpertVote("خبير مزاج الناس", "SELL", 0.55, "طمع مفرط — خطر", 0.9)
        if mood == "greed":
            return ExpertVote("خبير مزاج الناس", "FLAT", 0.50, "طمع بدون تطرف", 0.9)
        return ExpertVote("خبير مزاج الناس", "FLAT", 0.50, "مزاج محايد", 0.9)

    def _expert_macro(self, ctx: Dict) -> ExpertVote:
        news = ctx.get("macro", "neutral")  # positive / negative / neutral
        if news == "positive":
            return ExpertVote("خبير الأخبار", "BUY", 0.50, "أخبار داعمة", 0.8)
        if news == "negative":
            return ExpertVote("خبير الأخبار", "SELL", 0.50, "أخبار سلبية", 0.8)
        return ExpertVote("خبير الأخبار", "FLAT", 0.55, "لا خبر كبير مؤثر", 0.8)

    def decide(
        self,
        timeframe: str = "1h",
        market_context: Optional[Dict[str, Any]] = None,
        portfolios: str = "1",
    ) -> DecisionResult:
        ctx = market_context or self._default_conservative_context()
        ctx["timeframe"] = timeframe

        votes = [
            self._expert_onchain(ctx),
            self._expert_orderflow(ctx),
            self._expert_technical(ctx),
            self._expert_regime(ctx),
            self._expert_mood(ctx),
            self._expert_macro(ctx),
        ]

        # ترجيح مبدئي
        scores = {"BUY": 0.0, "SELL": 0.0, "FLAT": 0.0}
        for v in votes:
            scores[v.decision] += v.weight * v.confidence

        proposed = max(scores, key=scores.get)
        total = sum(scores.values()) or 1.0
        conf = scores[proposed] / total

        # خبير المخاطر
        final, veto, risk_msg, size_hint = self.risk.evaluate(proposed, votes, ctx)

        # أكثر موافق / معارض
        most_agree = None
        most_disagree = None
        if final in ("BUY", "SELL"):
            supporters = [v for v in votes if v.decision == final]
            opponents = [v for v in votes if v.decision != final and v.decision != "FLAT"]
            if supporters:
                most_agree = max(supporters, key=lambda x: x.confidence * x.weight).name
            if opponents:
                most_disagree = max(opponents, key=lambda x: x.confidence * x.weight).name
        else:
            flats = [v for v in votes if v.decision == "FLAT"]
            if flats:
                most_agree = max(flats, key=lambda x: x.confidence * x.weight).name
            actives = [v for v in votes if v.decision in ("BUY", "SELL")]
            if actives:
                most_disagree = max(actives, key=lambda x: x.confidence * x.weight).name

        reasons = [f"{v.name}: {v.decision} — {v.reason}" for v in votes]
        summary = (
            f"القرار={final} | {risk_msg} | "
            + " | ".join(reasons[:3])
        )
        if len(summary) > 400:
            summary = summary[:397] + "..."

        return DecisionResult(
            final_action=final,
            confidence=conf if not veto else min(conf, 0.4),
            reason_summary=summary,
            votes=votes,
            most_agree=most_agree,
            most_disagree=most_disagree,
            risk_veto=veto,
            risk_message=risk_msg,
            timeframe=timeframe,
            size_hint=size_hint,
        )

    def _default_conservative_context(self) -> Dict[str, Any]:
        """سياق محافظ عندما مفيش بيانات حية — يمنع الدخول العشوائي"""
        return {
            "netflow": "neutral",
            "whales": "neutral",
            "volume_weak": True,
            "candle_strength": "weak",
            "orderflow_bias": "neutral",
            "trend": "sideways",
            "momentum": "cool",
            "regime": "consolidation",
            "mood": "neutral",
            "macro": "neutral",
            "consolidation": True,
            "breakout_confirmed": False,
            "rr_ok": False,
        }


def run_decision(timeframe: str = "1h", portfolios: str = "1", **ctx) -> DecisionResult:
    """واجهة سريعة لاستدعاء القرار"""
    system = ExpertsSystem()
    return system.decide(timeframe=timeframe, market_context=ctx or None, portfolios=portfolios)
