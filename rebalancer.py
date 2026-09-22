from typing import Dict, List, Optional, Any
from mexc_client import MexcClient
import logging
from smart_levels import (
    compute_smart_levels,
    trailing_stop_price,
    reentry_trigger_price,
)

logger = logging.getLogger(__name__)

# Auto re-entry: price must recover this % above the stop that was hit
REENTRY_RECOVER_PCT = 1.2
# Minimum minutes after stop before auto re-entry is allowed (soft; enforced in bot if needed)
REENTRY_COOLDOWN_HINT_MIN = 20
# Trailing distance when ATR is unknown (percent)
DEFAULT_TRAIL_PCT = 1.5


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
        tp1_pct: float,
        tp2_pct: float,
        tp3_pct: float,
        stop_loss_pct: float,
        tp1_sell_pct: float = 40.0,
        tp2_sell_pct: float = 30.0,
        skip_stages: Optional[List[str]] = None,
    ) -> List[Dict]:
        """Place the remaining TP limit orders on MEXC + calculate the SL.

        ``skip_stages`` is used when a target was already filled and the
        portfolio's targets are being refreshed. It prevents recreating a
        sell order for a target that has already been taken.
        """
        results = []
        skipped = set(skip_stages or [])
        for item in coins_data:
            symbol = item["symbol"]
            amount = float(item.get("amount") or 0)
            entry = float(item.get("entry_price") or 0)
            if amount <= 0:
                # Prefer free; fall back to total (locked in old orders may still exist)
                amount = self.client.get_free_amount(symbol)
                if amount <= 0:
                    amount = self.client.get_total_amount(symbol)
                amount = amount * 0.998
            if entry <= 0:
                entry = self.client.get_ticker_price(f"{symbol}/{self.quote}")
            if amount <= 0 or entry <= 0:
                results.append({"symbol": symbol, "error": "no amount or price"})
                continue

            tp1 = entry * (1 + tp1_pct / 100.0)
            tp2 = entry * (1 + tp2_pct / 100.0)
            tp3 = entry * (1 + tp3_pct / 100.0)
            sl = entry * (1 - stop_loss_pct / 100.0)

            orders = {"tp1_order_id": None, "tp2_order_id": None, "tp3_order_id": None}
            errors = []
            planned = [
                ("tp1", "tp1_order_id", tp1, max(0.0, tp1_sell_pct)),
                ("tp2", "tp2_order_id", tp2, max(0.0, tp2_sell_pct)),
                ("tp3", "tp3_order_id", tp3, max(0.0, 100.0 - tp1_sell_pct - tp2_sell_pct)),
            ]
            active = [stage for stage in planned if stage[0] not in skipped and stage[3] > 0]
            # فلترة الشرائح اللي قيمتها أقل من 1 USDT ودمج وزنها في آخر شريحة صالحة
            MIN_NOTIONAL = 1.05  # هامش فوق حد MEXC (1 USDT)
            viable = []
            leftover_weight = 0.0
            total_weight = sum(s[3] for s in active) or 1.0
            for stage, key, price, weight in active:
                qty = amount * (weight / total_weight)
                notional = qty * price
                if notional < MIN_NOTIONAL:
                    leftover_weight += weight
                else:
                    viable.append([stage, key, price, weight])
            if viable and leftover_weight > 0:
                # ادمج الوزن الصغير في آخر هدف صالح
                viable[-1][3] += leftover_weight
            elif not viable and active:
                # كل الشرائح صغيرة → حط أمر واحد على أقرب هدف (TP1 أو أول متاح)
                stage, key, price, weight = active[0]
                viable = [[stage, key, price, total_weight]]

            active_weight = sum(s[3] for s in viable) or 1.0
            for stage, key, price, weight in viable:
                qty = amount * (weight / active_weight) if active_weight > 0 else 0.0
                if qty <= 0:
                    continue
                try:
                    order = self.client.create_limit_sell(symbol, qty, price)
                    if order is None:
                        # أقل من الحد الأدنى — تجاوز بهدوء
                        continue
                    orders[key] = order.get("id") if order else None
                except Exception as e:
                    logger.warning(f"{key} failed for {symbol}: {e}")
                    errors.append(f"{key}: {e}")

            results.append({
                "symbol": symbol,
                "entry_price": entry,
                "tp1_price": tp1,
                "tp2_price": tp2,
                "tp3_price": tp3,
                "sl_price": sl,
                "original_sl_price": sl,
                "amount": amount,
                "remaining_amount": amount,
                **orders,
                "error": "; ".join(errors) if errors else None,
            })
        return results

    def cancel_tp_orders(self, coins_with_orders: List[Dict]) -> Dict[str, List]:
        results = {"cancelled": [], "errors": []}
        for item in coins_with_orders:
            sym = item.get("symbol")
            if not sym:
                continue
            for key in ("tp_order_id", "tp1_order_id", "tp2_order_id", "tp3_order_id"):
                oid = item.get(key)
                if oid:
                    try:
                        self.client.cancel_order(oid, sym, strict=True)
                        results["cancelled"].append({"symbol": sym, "order_id": oid, "field": key})
                    except Exception as exc:
                        error_text = str(exc)
                        normalized_error = error_text.lower()
                        if any(marker in normalized_error for marker in (
                            "not found", "does not exist", "already canceled",
                            "already cancelled", "order closed", "filled",
                        )):
                            # The exchange confirms that this ID is no longer
                            # open, so it is safe to continue the cleanup.
                            results["cancelled"].append({
                                "symbol": sym,
                                "order_id": oid,
                                "field": key,
                                "already_closed": True,
                            })
                        else:
                            results["errors"].append({
                                "symbol": sym,
                                "order_id": oid,
                                "field": key,
                                "error": error_text,
                            })
        return results

    def _order_filled(self, order_id: Optional[str], symbol: str) -> bool:
        return self._filled_order_info(order_id, symbol) is not None

    def _filled_order_info(self, order_id: Optional[str], symbol: str) -> Optional[Dict[str, float]]:
        if not order_id:
            return None
        try:
            order = self.client.fetch_order(order_id, symbol)
            if not order:
                return None
            st = (order.get("status") or "").lower()
            # A cancelled TP order is not a filled target. Treating it as
            # filled raises the stop to an untouched TP price and can trigger
            # an incorrect sell/re-entry cycle.
            if st in ("canceled", "cancelled", "rejected", "expired"):
                return None
            if st not in ("closed", "filled"):
                return None
            filled = order.get("filled")
            try:
                filled_amount = float(filled or 0)
            except (TypeError, ValueError):
                filled_amount = 0.0
            if filled is not None and filled_amount <= 0:
                return None
            average = order.get("average") or order.get("price") or 0
            try:
                average_price = float(average or 0)
            except (TypeError, ValueError):
                average_price = 0.0
            return {"filled": filled_amount, "average": average_price}
        except Exception:
            return None

    def check_and_manage_positions(self, positions: List[Any]) -> List[Dict]:
        """
        Multi-TP + smart re-entry (optimized: one price batch per cycle).
        """
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
            prices = self.client.get_all_prices(symbols)
        except Exception:
            prices = {}
        for coin in active:
            symbol = coin.symbol
            status = coin.position_status or "idle"
            price = float(prices.get(symbol) or 0)
            if price <= 0:
                try:
                    price = self.client.get_ticker_price(f"{symbol}/{self.quote}")
                except Exception:
                    continue
            if price <= 0:
                continue

            # ----- waiting for auto re-entry (simple) -----
            # Trigger when price recovers REENTRY_RECOVER_PCT above the stored level.
            if status == "waiting_reentry":
                if getattr(coin, "reentry_used", False):
                    continue
                reentry = float(getattr(coin, "reentry_price", 0) or 0)
                if reentry <= 0:
                    continue
                if price >= reentry:
                    actions.append({
                        "coin_id": getattr(coin, "id", None),
                        "symbol": symbol,
                        "action": "reentry_buy",
                        "price": price,
                        "reentry_price": reentry,
                    })
                continue

            # ----- normal TP detection -----
            tp1_fill = self._filled_order_info(getattr(coin, "tp1_order_id", None), symbol)
            if status == "open" and tp1_fill:
                actions.append({
                    "coin_id": getattr(coin, "id", None),
                    "symbol": symbol,
                    "action": "tp1_hit",
                    # TP1 → break-even (entry)
                    "new_sl": float(coin.entry_price or 0),
                    "price": price,
                    "filled_amount": tp1_fill.get("filled", 0.0),
                    "fill_price": tp1_fill.get("average") or coin.tp1_price,
                })
                continue
            tp2_fill = self._filled_order_info(getattr(coin, "tp2_order_id", None), symbol)
            if status in ("open", "tp1_hit") and tp2_fill:
                # After TP2: protect profit by moving SL to TP1 (not to TP2 itself).
                # This avoids the "raise SL to TP2 → instant stop-out" bug.
                protect = float(coin.tp1_price or 0) or float(coin.entry_price or 0)
                entry = float(coin.entry_price or 0)
                if protect < entry:
                    protect = entry
                actions.append({
                    "coin_id": getattr(coin, "id", None),
                    "symbol": symbol,
                    "action": "tp2_hit",
                    "new_sl": protect,
                    "price": price,
                    "filled_amount": tp2_fill.get("filled", 0.0),
                    "fill_price": tp2_fill.get("average") or coin.tp2_price,
                })
                continue
            tp3_fill = self._filled_order_info(getattr(coin, "tp3_order_id", None), symbol)
            if status in ("open", "tp1_hit", "tp2_hit") and tp3_fill:
                actions.append({
                    "coin_id": getattr(coin, "id", None),
                    "symbol": symbol,
                    "action": "tp3_hit",
                    "price": price,
                    "filled_amount": tp3_fill.get("filled", 0.0),
                    "fill_price": tp3_fill.get("average") or coin.tp3_price,
                })
                continue

            # ----- trailing stop after TP2 (remaining runner) -----
            if status == "tp2_hit":
                sl_now = float(coin.current_sl_price or 0)
                # Trail only while price is above protected level
                new_trail = trailing_stop_price(
                    price,
                    sl_now,
                    atr=0.0,
                    trail_atr_mult=1.1,
                    min_trail_pct=DEFAULT_TRAIL_PCT,
                )
                # Only emit when meaningfully higher (avoid spam from noise)
                if new_trail > sl_now * 1.0015:
                    actions.append({
                        "coin_id": getattr(coin, "id", None),
                        "symbol": symbol,
                        "action": "trail_update",
                        "new_sl": new_trail,
                        "price": price,
                    })
                    # do not continue — still allow SL check below with old sl this cycle

            # ----- stop loss -----
            sl = float(coin.current_sl_price or 0)
            if sl > 0 and price <= sl:
                for oid in (
                    getattr(coin, "tp1_order_id", None),
                    getattr(coin, "tp2_order_id", None),
                    getattr(coin, "tp3_order_id", None),
                    getattr(coin, "tp_order_id", None),
                ):
                    if oid:
                        try:
                            self.client.cancel_order(oid, symbol)
                        except Exception:
                            pass
                amount = self.client.get_free_amount(symbol) * 0.998
                sold = False
                dust = False
                if amount > 0:
                    try:
                        order = self.client.create_market_order(
                            f"{symbol}/{self.quote}", "sell", amount
                        )
                        if order is None:
                            # كمية أقل من الحد الأدنى (dust) — اعتبرها بيعت بدون خطأ
                            dust = True
                            sold = True
                        else:
                            sold = True
                    except Exception as e:
                        actions.append({
                            "coin_id": getattr(coin, "id", None),
                            "symbol": symbol,
                            "action": "sl_sell_failed",
                            "error": str(e),
                            "price": price,
                        })
                        continue
                else:
                    # مفيش رصيد حر — اعتبر المركز اتقفل (dust)
                    sold = True
                    dust = True

                was_raised = status in ("tp1_hit", "tp2_hit", "tp3_hit", "tp_hit")
                already_used = bool(getattr(coin, "reentry_used", False))
                # Auto re-entry trigger = stop level recovered by REENTRY_RECOVER_PCT
                trigger = reentry_trigger_price(sl if sl > 0 else price, REENTRY_RECOVER_PCT)
                actions.append({
                    "coin_id": getattr(coin, "id", None),
                    "symbol": symbol,
                    "action": "sl_hit_sold",
                    "sl": sl,
                    "price": price,
                    "amount": amount,
                    "sold": sold,
                    "dust": dust,
                    "was_raised": was_raised,
                    "reentry_price": trigger,
                    "reentry_available": bool(trigger > 0 and not already_used),
                    "auto_reentry": True,
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
