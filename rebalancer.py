from typing import Dict, List, Optional, Any
from mexc_client import MexcClient
import logging
from smart_levels import (
    compute_smart_levels,
    trailing_stop_price,
    reentry_trigger_price,
)

logger = logging.getLogger(__name__)

# ===== نظام الوقف المتحرك (Trailing Stop) — بدون أهداف ثابتة =====
# الاستوب الابتدائي تحت الدخول
INITIAL_SL_PCT = 3.0
# بعد ربح بهذا القدر → الاستوب على سعر الدخول (break-even)
BE_LOCK_PCT = 1.2
# مسافة الـ trailing الافتراضية تحت السعر الحالي
DEFAULT_TRAIL_PCT = 2.0
# عند ربح قوي: مسافة أوسع عشان مايتقفلش بدري
PUMP_GAIN_PCT = 6.0
PUMP_STRONG_GAIN_PCT = 12.0
PUMP_TRAIL_PCT = 3.2
PUMP_STRONG_TRAIL_PCT = 4.0
# إعادة الدخول: السعر لازم يرجع فوق الاستوب المضروب بهذا القدر
REENTRY_RECOVER_PCT = 1.0
# تبريد خفيف بالثواني بين محاولات إعادة الدخول لنفس العملة (منع سبام أوامر)
REENTRY_COOLDOWN_SEC = 90
# حد أدنى لقيمة المركز عشان نعتبره مفتوح (dust)
MIN_POSITION_USDT = 0.8


class Rebalancer:
    """Buy / sell / multi-TP placement & cloud SL monitor. No rebalancing."""

    def __init__(self, client: MexcClient):
        self.client = client
        self.quote = client.quote

    def fetch_candles(self, symbol: str, timeframe: str = "1h", limit: int = 80) -> List:
        pair = f"{symbol}/{self.quote}"
        try:
            return self.client.exchange.fetch_ohlcv(pair, timeframe=timeframe, limit=limit) or []
        except Exception as e:
            logger.warning("OHLCV failed for %s: %s", symbol, e)
            return []

    def build_smart_levels_for_entry(
        self,
        symbol: str,
        entry: float,
        fallback_tp1: float = 3.0,
        fallback_tp2: float = 5.0,
        fallback_tp3: float = 8.0,
        fallback_sl: float = 3.0,
        timeframe: str = "1h",
    ):
        candles = self.fetch_candles(symbol, timeframe=timeframe, limit=80)
        return compute_smart_levels(
            symbol,
            entry,
            candles,
            fallback_tp1=fallback_tp1,
            fallback_tp2=fallback_tp2,
            fallback_tp3=fallback_tp3,
            fallback_sl=fallback_sl,
        )

    def calculate_targets(self, coins: List[str], method: str = "equal") -> Dict[str, float]:
        if not coins:
            return {}
        pct = 100.0 / len(coins)
        return {c: pct for c in coins}

    def start_portfolio(
        self,
        coins: List[str],
        total_usdt: float,
        method: str = "equal",
        min_trade_usdt: float = 5.0,
        dry_run: bool = False,
    ) -> Dict:
        """Buy coins using up to total_usdt only."""
        results = {
            "action": "start",
            "total_usdt": total_usdt,
            "executed": [],
            "errors": [],
            "dry_run": dry_run,
        }
        free_usdt = self.client.get_free_usdt()
        if free_usdt < total_usdt:
            results["errors"].append(
                f"رصيد USDT الحر غير كافٍ. المتاح: `{free_usdt:.2f}$` | المطلوب: `{total_usdt:.2f}$`"
            )
            return results

        targets = self.calculate_targets(coins, method)
        for coin, pct in targets.items():
            usdt_for_coin = total_usdt * (pct / 100.0)
            if usdt_for_coin < min_trade_usdt:
                results["errors"].append(f"`{coin}`: المبلغ صغير جداً ({usdt_for_coin:.2f}$)")
                continue
            try:
                if dry_run:
                    results["executed"].append({
                        "symbol": f"{coin}/{self.quote}",
                        "side": "buy",
                        "usdt": usdt_for_coin,
                        "status": "dry_run",
                    })
                else:
                    order = self.client.create_market_buy_usdt(coin, usdt_for_coin)
                    results["executed"].append({
                        "symbol": f"{coin}/{self.quote}",
                        "side": "buy",
                        "usdt": usdt_for_coin,
                        "status": "filled",
                        "order_id": order.get("id") if order else None,
                    })
            except Exception as e:
                results["errors"].append({coin: str(e)})
        return results

    def stop_portfolio(
        self,
        coins: List[str],
        dry_run: bool = False,
        amount_overrides: Optional[Dict[str, float]] = None,
    ) -> Dict:
        """Sell all holdings of the given coins."""
        results = {
            "action": "stop",
            "executed": [],
            "errors": [],
            "dry_run": dry_run,
            "total_sold_usdt": 0.0,
        }
        balances = self.client.get_balance()
        prices = self.client.get_all_prices(coins)
        for coin in coins:
            override = (amount_overrides or {}).get(coin)
            if override is None:
                amount = float(balances.get(coin, 0.0))
            else:
                amount = min(float(override or 0.0), float(balances.get(coin, 0.0)))
            if amount <= 0:
                continue
            amount = amount * 0.999
            usdt_value = amount * prices.get(coin, 0.0)
            try:
                if dry_run:
                    results["executed"].append({
                        "symbol": f"{coin}/{self.quote}",
                        "side": "sell",
                        "amount": amount,
                        "usdt": usdt_value,
                        "status": "dry_run",
                    })
                    results["total_sold_usdt"] += usdt_value
                else:
                    order = self.client.create_market_order(
                        symbol=f"{coin}/{self.quote}", side="sell", amount=amount
                    )
                    results["executed"].append({
                        "symbol": f"{coin}/{self.quote}",
                        "side": "sell",
                        "amount": amount,
                        "usdt": usdt_value,
                        "status": "filled",
                        "order_id": order.get("id") if order else None,
                    })
                    results["total_sold_usdt"] += usdt_value
            except Exception as e:
                results["errors"].append({coin: str(e)})
        return results

    def place_tp_orders(
        self,
        coins_data: List[Dict[str, Any]],
        tp1_pct: float = 0.0,
        tp2_pct: float = 0.0,
        tp3_pct: float = 0.0,
        stop_loss_pct: float = None,
        tp1_sell_pct: float = 0.0,
        tp2_sell_pct: float = 0.0,
        skip_stages: Optional[List[str]] = None,
    ) -> List[Dict]:
        """تهيئة وقف متحرك فقط — بدون أوامر أهداف ثابتة على المنصة.

        - يلغي أي أوامر TP قديمة إن وُجدت
        - يحسب استوب ابتدائي تحت الدخول (INITIAL_SL_PCT أو stop_loss_pct)
        - الإدارة الفعلية تتم في check_and_manage_positions (رفع الاستوب مع السعر)
        """
        results = []
        sl_pct = float(stop_loss_pct) if stop_loss_pct and float(stop_loss_pct) > 0 else INITIAL_SL_PCT
        for item in coins_data:
            symbol = item["symbol"]
            amount = float(item.get("amount") or 0)
            entry = float(item.get("entry_price") or 0)
            if amount <= 0:
                amount = self.client.get_free_amount(symbol)
                if amount <= 0:
                    amount = self.client.get_total_amount(symbol)
                amount = amount * 0.998
            if entry <= 0:
                entry = self.client.get_ticker_price(f"{symbol}/{self.quote}")
            if amount <= 0 or entry <= 0:
                results.append({"symbol": symbol, "error": "no amount or price"})
                continue

            # إلغاء أوامر بيع قديمة (أهداف ثابتة من النظام السابق)
            for oid_key in ("tp_order_id", "tp1_order_id", "tp2_order_id", "tp3_order_id"):
                oid = item.get(oid_key)
                if oid:
                    try:
                        self.client.cancel_order(oid, f"{symbol}/{self.quote}")
                    except Exception:
                        pass

            # استوب ابتدائي — يُرفع لاحقاً بالـ trailing
            try:
                levels = self.build_smart_levels_for_entry(
                    symbol, entry,
                    fallback_tp1=3.0, fallback_tp2=5.0, fallback_tp3=8.0,
                    fallback_sl=sl_pct,
                )
                smart_sl = float(getattr(levels, "stop_loss_pct", 0) or 0)
                if smart_sl > 0:
                    sl_pct_use = min(max(smart_sl, 1.5), 6.0)
                else:
                    sl_pct_use = sl_pct
            except Exception:
                sl_pct_use = sl_pct

            sl = entry * (1.0 - sl_pct_use / 100.0)
            results.append({
                "symbol": symbol,
                "amount": amount,
                "entry_price": entry,
                "tp1_price": 0.0,
                "tp2_price": 0.0,
                "tp3_price": 0.0,
                "stop_loss_price": sl,
                "sl_price": sl,
                "original_sl_price": sl,
                "tp1_order_id": None,
                "tp2_order_id": None,
                "tp3_order_id": None,
                "tp_order_id": None,
                "mode": "trailing_only",
                "trail_pct": DEFAULT_TRAIL_PCT,
                "sl_pct": sl_pct_use,
                "error": None,
            })
        return results

    def check_and_manage_positions(self, positions: List[Any]) -> List[Dict]:
        """وقف متحرك + إعادة دخول متكررة (بدون أهداف ثابتة).

        المنطق:
        1) لو الرصيد ≈ 0 → إغلاق
        2) لو waiting_reentry والسعر ≥ سعر إعادة الدخول → إشارة شراء (متكرر، مش مرة واحدة)
        3) لو السعر ≤ الاستوب → بيع سوق + waiting_reentry
        4) وإلا ارفع الاستوب مع السعر (trailing) بعد قفل break-even
        """
        import time
        actions = []
        active = [
            c for c in positions
            if (c.position_status or "idle") in (
                "open", "tp1_hit", "tp2_hit", "tp3_hit", "tp_hit", "waiting_reentry"
            )
        ]
        if not active:
            return actions

        symbols = list({c.symbol for c in active})
        try:
            prices = self.client.get_all_prices(symbols) or {}
        except Exception:
            prices = {}

        now = time.time()
        # تبريد إعادة الدخول في الذاكرة (per process)
        if not hasattr(self, "_reentry_cooldown"):
            self._reentry_cooldown = {}

        for coin in active:
            symbol = coin.symbol
            status = (coin.position_status or "open").strip()
            price = float(prices.get(symbol) or prices.get(f"{symbol}/{self.quote}") or 0)
            if price <= 0:
                try:
                    price = float(self.client.get_ticker_price(f"{symbol}/{self.quote}") or 0)
                except Exception:
                    price = 0
            if price <= 0:
                continue

            entry = float(coin.entry_price or 0)
            remaining = float(coin.remaining_amount or coin.amount or 0)
            sl = float(coin.current_sl_price or 0)

            # ----- رصيد فعلي -----
            try:
                free_amt = float(self.client.get_free_amount(symbol) or 0)
            except Exception:
                free_amt = remaining

            market_val = free_amt * price
            if status != "waiting_reentry":
                if free_amt <= 0 or market_val < MIN_POSITION_USDT:
                    actions.append({
                        "coin_id": getattr(coin, "id", None),
                        "symbol": symbol,
                        "action": "tp_full_close",
                        "stage": "balance_zero",
                        "price": price,
                        "filled_amount": remaining or free_amt,
                        "fill_price": price,
                    })
                    continue

            # ----- إعادة دخول متكررة (سلسة) -----
            if status == "waiting_reentry":
                reentry_px = float(getattr(coin, "reentry_price", 0) or 0)
                if reentry_px <= 0:
                    # لو مفيش سعر محفوظ، ابنِه من آخر استوب
                    last_sl = float(coin.current_sl_price or 0)
                    reentry_px = reentry_trigger_price(last_sl if last_sl > 0 else price, REENTRY_RECOVER_PCT)

                # تبريد: متسمحش بمحاولة كل ثانية
                last_try = float(self._reentry_cooldown.get(symbol, 0) or 0)
                if now - last_try < REENTRY_COOLDOWN_SEC:
                    continue

                if price >= reentry_px > 0:
                    self._reentry_cooldown[symbol] = now
                    actions.append({
                        "coin_id": getattr(coin, "id", None),
                        "symbol": symbol,
                        "action": "reentry_buy",
                        "price": price,
                        "reentry_price": reentry_px,
                        # مهم: متكررة — البوت لازم يصفّر reentry_used بعد النجاح
                        "repeatable": True,
                    })
                elif price >= reentry_px * 0.995 and not getattr(coin, "reentry_touched", False):
                    actions.append({
                        "coin_id": getattr(coin, "id", None),
                        "symbol": symbol,
                        "action": "reentry_touch",
                        "price": price,
                        "reentry_price": reentry_px,
                    })
                continue

            # ----- ضرب الاستوب → بيع -----
            if sl > 0 and price <= sl:
                sold = False
                dust = False
                amount = free_amt * 0.998 if free_amt > 0 else remaining * 0.998
                if amount > 0 and amount * price >= MIN_POSITION_USDT:
                    try:
                        self.client.create_market_order(
                            f"{symbol}/{self.quote}", "sell", amount
                        )
                        sold = True
                    except Exception as e:
                        actions.append({
                            "coin_id": getattr(coin, "id", None),
                            "symbol": symbol,
                            "action": "sl_sell_failed",
                            "error": str(e),
                            "price": price,
                            "sl": sl,
                        })
                        continue
                else:
                    sold = True
                    dust = True

                trigger = reentry_trigger_price(sl if sl > 0 else price, REENTRY_RECOVER_PCT)
                # امسح التبريد عشان تدخل تاني بسرعة بعد الضربة
                self._reentry_cooldown.pop(symbol, None)
                actions.append({
                    "coin_id": getattr(coin, "id", None),
                    "symbol": symbol,
                    "action": "sl_hit_sold",
                    "sl": sl,
                    "price": price,
                    "amount": amount,
                    "sold": sold,
                    "dust": dust,
                    "was_raised": sl >= entry * 0.999 if entry > 0 else False,
                    "reentry_price": trigger,
                    "reentry_available": True,   # دايماً متاح
                    "auto_reentry": True,
                    "repeatable": True,
                })
                continue

            # ----- Trailing: ارفع الاستوب مع السعر -----
            if entry <= 0:
                continue

            gain_pct = ((price / entry) - 1.0) * 100.0

            # اختيار مسافة الـ trail حسب قوة الحركة
            if gain_pct >= PUMP_STRONG_GAIN_PCT:
                trail_pct = PUMP_STRONG_TRAIL_PCT
                mode = "pump_strong"
            elif gain_pct >= PUMP_GAIN_PCT:
                trail_pct = PUMP_TRAIL_PCT
                mode = "pump"
            else:
                trail_pct = DEFAULT_TRAIL_PCT
                mode = "trail"

            # استوب مقترح من السعر الحالي
            candidate = trailing_stop_price(
                current_price=price,
                current_sl=sl if sl > 0 else 0.0,
                atr=0.0,
                trail_atr_mult=1.15,
                min_trail_pct=trail_pct,
            )

            # قفل break-even بعد ربح BE_LOCK_PCT
            if gain_pct >= BE_LOCK_PCT:
                candidate = max(candidate, entry)

            # الاستوب الابتدائي لو لسه مش متعيّن
            if sl <= 0:
                candidate = max(candidate, entry * (1.0 - INITIAL_SL_PCT / 100.0))

            # ارفع فقط — عمره ما ينزل
            if candidate > sl + (price * 0.0003):  # هامش ضئيل ضد الضوضاء
                actions.append({
                    "coin_id": getattr(coin, "id", None),
                    "symbol": symbol,
                    "action": "trail_update",
                    "new_sl": candidate,
                    "old_sl": sl,
                    "price": price,
                    "gain_pct": gain_pct,
                    "mode": mode,
                    "trail_pct": trail_pct,
                })

        return actions

    def reentry_buy_and_place_tp(
        self,
        symbol: str,
        usdt_amount: float,
        tp1_pct: float,
        tp2_pct: float,
        tp3_pct: float,
        stop_loss_pct: float,
        tp1_sell_pct: float = 40.0,
        tp2_sell_pct: float = 30.0,
        use_smart: bool = True,
    ) -> Dict:
        """Market buy then place multi-TP limits for a re-entry (smart levels by default)."""
        result = {"symbol": symbol, "error": None}
        try:
            order = self.client.create_market_buy_usdt(symbol, usdt_amount)
            result["buy_order_id"] = order.get("id") if order else None
        except Exception as e:
            result["error"] = str(e)
            return result
        import time
        time.sleep(1.0)
        amount = self.client.get_free_amount(symbol) * 0.998
        entry = self.client.get_ticker_price(f"{symbol}/{self.quote}")
        if use_smart and entry > 0:
            try:
                levels = self.build_smart_levels_for_entry(
                    symbol, entry, tp1_pct, tp2_pct, tp3_pct, stop_loss_pct,
                )
                tp1_pct = levels.tp1_pct
                tp2_pct = levels.tp2_pct
                tp3_pct = levels.tp3_pct
                stop_loss_pct = levels.stop_loss_pct
                result["smart_reason"] = levels.reason
            except Exception as e:
                logger.warning("smart levels failed on reentry %s: %s", symbol, e)
        placed = self.place_tp_orders(
            [{"symbol": symbol, "amount": amount, "entry_price": entry}],
            tp1_pct, tp2_pct, tp3_pct, stop_loss_pct, tp1_sell_pct, tp2_sell_pct,
        )
        if placed:
            result.update(placed[0])
            if result.get("error"):
                result["tp_warning"] = result["error"]
                result["error"] = None
        return result
