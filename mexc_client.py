import ccxt
import config
from typing import Dict, List, Optional


class MexcClient:
    def __init__(self):
        self.exchange = ccxt.mexc({
            'apiKey': config.MEXC_API_KEY,
            'secret': config.MEXC_API_SECRET,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'spot',
                'recvWindow': 10000,
            }
        })
        self.quote = config.QUOTE_ASSET

    @staticmethod
    def normalize_asset_symbol(symbol: str) -> str:
        """Normalize a wallet asset or a configured trading symbol to its base asset."""
        value = str(symbol or "").strip().upper().replace(" ", "")
        value = value.lstrip("$")
        for separator in ("/", ":", "-"):
            if separator in value:
                value = value.split(separator, 1)[0]
                break
        return value

    @staticmethod
    def _as_float(value) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    def get_balance(self) -> Dict:
        """Return free balances only (non-zero)"""
        balance = self.exchange.fetch_balance()
        free = {}
        for asset, amount in balance.get('free', {}).items():
            value = self._as_float(amount)
            if value > 0:
                key = self.normalize_asset_symbol(asset)
                free[key] = max(free.get(key, 0.0), value)
        return free

    def get_total_balance(self) -> Dict[str, float]:
        """Return total balances, including amounts locked in open orders."""
        balance = self.exchange.fetch_balance()
        totals = balance.get("total") or {}
        free = balance.get("free") or {}
        result = {}
        for asset in set(totals) | set(free):
            # Some exchange responses expose a zero/empty total while free
            # already contains the actual available amount. Use the larger
            # value so a present wallet asset is not reported as missing.
            value = max(
                self._as_float(totals.get(asset)),
                self._as_float(free.get(asset)),
            )
            if value > 0:
                key = self.normalize_asset_symbol(asset)
                result[key] = max(result.get(key, 0.0), value)
        return result

    def get_portfolio_presence(self, symbols: List[str]) -> Dict[str, Dict[str, float]]:
        """Return whether each configured coin exists in the wallet.

        Total balance is intentional here: a coin locked in an open sell order
        is still owned and must not be offered for re-entry.

        A small residual amount can remain after a market sell because the
        rebalancer keeps a safety buffer for fees and exchange precision.
        That dust should not make a sold portfolio coin look present.
        """
        requested = [str(symbol).upper().strip() for symbol in symbols if symbol]
        normalized = list(dict.fromkeys(
            self.normalize_asset_symbol(symbol) for symbol in requested
        ))
        balances = self.get_total_balance()
        prices = self.get_all_prices(normalized)
        result = {}
        minimum_value = max(
            0.0,
            float(getattr(config, "BALANCE_PRESENCE_MIN_USDT", 1.0)),
        )
        for requested_symbol in requested:
            asset = self.normalize_asset_symbol(requested_symbol)
            amount = float(balances.get(asset, 0.0))
            price = float(prices.get(asset, 0.0))
            market_value = amount * price if price > 0 else 0.0
            # If the ticker is unavailable, do not classify an asset as
            # missing just because its value cannot be calculated.
            present = amount > 0 and (
                price <= 0 or market_value >= minimum_value
            )
            info = {
                "amount": amount,
                "price": price,
                "market_value": market_value,
                "present": present,
            }
            # Keep the requested key for the bot UI, and the normalized alias
            # for callers that already use base symbols.
            result[requested_symbol] = info
            result[asset] = info
        return result

    def get_free_usdt(self) -> float:
        bal = self.get_balance()
        return float(bal.get(self.quote, 0.0))

    def get_ticker_price(self, symbol: str) -> float:
        """symbol like BTC/USDT"""
        ticker = self.exchange.fetch_ticker(symbol)
        return float(ticker['last'])

    def get_all_prices(self, symbols: List[str]) -> Dict[str, float]:
        """symbols = ['BTC', 'ETH'] -> prices in USDT.
        Uses batch fetch_tickers when possible for much higher speed.
        """
        prices: Dict[str, float] = {self.quote: 1.0}
        if not symbols:
            return prices

        unique = list(dict.fromkeys(
            self.normalize_asset_symbol(s) for s in symbols if s
        ))
        pairs = [f"{s}/{self.quote}" for s in unique]

        # Batch request — dramatically faster than sequential fetch_ticker
        try:
            tickers = self.exchange.fetch_tickers(pairs)
            for s, pair in zip(unique, pairs):
                t = tickers.get(pair) or {}
                last = t.get("last") or t.get("close")
                prices[s] = float(last) if last is not None else 0.0
        except Exception:
            # Fallback to sequential if batch fails
            for s in unique:
                pair = f"{s}/{self.quote}"
                try:
                    prices[s] = self.get_ticker_price(pair)
                except Exception:
                    prices[s] = 0.0
        return prices

    def get_expert_market_context(self, symbols: List[str], timeframe: str = "1h") -> Dict:
        """Build a conservative, read-only market context for the expert engine."""
        allowed = {"15m", "1h", "4h", "1d", "1w"}
        tf = timeframe if timeframe in allowed else "1h"
        clean = [self.normalize_asset_symbol(s) for s in (symbols or [])]
        contexts = []
        for symbol in list(dict.fromkeys(clean))[:12]:
            pair = f"{symbol}/{self.quote}"
            try:
                candles = self.exchange.fetch_ohlcv(pair, timeframe=tf, limit=80)
                if len(candles) < 30:
                    continue
                closes = [float(row[4]) for row in candles]
                volumes = [float(row[5] or 0) for row in candles]
                last = closes[-1]
                sma20 = sum(closes[-20:]) / 20.0
                sma50 = sum(closes[-50:]) / 50.0 if len(closes) >= 50 else sma20
                momentum_return = (last / closes[-6] - 1.0) if closes[-6] else 0.0
                avg_volume = sum(volumes[-21:-1]) / 20.0 if sum(volumes[-21:-1]) else 0.0
                volume_ratio = (volumes[-1] / avg_volume) if avg_volume else 0.0
                high20 = max(closes[-21:-1])
                low20 = min(closes[-21:-1])
                candle_range = max(float(candles[-1][2]) - float(candles[-1][3]), 0.0)
                candle_body = abs(float(candles[-1][4]) - float(candles[-1][1]))
                try:
                    book = self.exchange.fetch_order_book(pair, limit=20)
                    bids = sum(float(x[1]) for x in book.get("bids", [])[:10])
                    asks = sum(float(x[1]) for x in book.get("asks", [])[:10])
                    imbalance = (bids - asks) / (bids + asks) if bids + asks else 0.0
                except Exception:
                    imbalance = 0.0
                trend = "up" if last > sma20 * 1.002 and sma20 >= sma50 else (
                    "down" if last < sma20 * 0.998 and sma20 <= sma50 else "sideways"
                )
                momentum = "strong" if abs(momentum_return) >= 0.015 else (
                    "cool" if abs(momentum_return) >= 0.004 else "weak"
                )
                candle_strength = "strong" if candle_range and candle_body / candle_range >= 0.55 else "weak"
                consolidation = ((high20 - low20) / last) < 0.04 if last else True
                breakout = (last > high20 * 1.001 or last < low20 * 0.999) and volume_ratio >= 1.15
                contexts.append({
                    "trend": trend,
                    "momentum": momentum,
                    "volume_weak": volume_ratio < 0.80,
                    "candle_strength": candle_strength,
                    "orderflow_bias": "buy" if imbalance > 0.12 else ("sell" if imbalance < -0.12 else "neutral"),
                    "consolidation": consolidation,
                    "breakout_confirmed": breakout,
                    "rr_ok": bool(not consolidation and abs(momentum_return) >= 0.008),
                    "regime": "uptrend" if trend == "up" else ("downtrend" if trend == "down" else "consolidation"),
                    "symbols_used": symbol,
                })
            except Exception:
                continue
        if not contexts:
            return {
                "volume_weak": True, "candle_strength": "weak", "orderflow_bias": "neutral",
                "trend": "sideways", "momentum": "cool", "regime": "consolidation",
                "consolidation": True, "breakout_confirmed": False, "rr_ok": False,
                "data_source": "unavailable",
            }
        def majority(key, default):
            values = [c.get(key, default) for c in contexts]
            return max(set(values), key=values.count)
        return {
            "trend": majority("trend", "sideways"),
            "momentum": majority("momentum", "cool"),
            "volume_weak": sum(bool(c.get("volume_weak")) for c in contexts) > len(contexts) / 2,
            "candle_strength": majority("candle_strength", "weak"),
            "orderflow_bias": majority("orderflow_bias", "neutral"),
            "consolidation": sum(bool(c.get("consolidation")) for c in contexts) > len(contexts) / 2,
            "breakout_confirmed": sum(bool(c.get("breakout_confirmed")) for c in contexts) >= max(1, len(contexts) // 2),
            "rr_ok": sum(bool(c.get("rr_ok")) for c in contexts) > len(contexts) / 2,
            "regime": majority("regime", "consolidation"),
            "data_source": "MEXC public OHLCV + order book",
            "symbols_used": ", ".join(c["symbols_used"] for c in contexts),
        }

    def get_portfolio_value(self) -> Dict:
        """
        Returns full account value.
        {
            'total_usdt': float,
            'assets': {
                'BTC': {'amount': x, 'usdt_value': y, 'percent': z, 'price': p},
                ...
            }
        }
        """
        balances = self.get_balance()
        if not balances:
            return {'total_usdt': 0.0, 'assets': {}}

        assets = [a for a in balances.keys() if a != self.quote]
        prices = self.get_all_prices(assets)

        total_usdt = 0.0
        result_assets = {}

        for asset, amount in balances.items():
            price = prices.get(asset, 0.0) if asset != self.quote else 1.0
            usdt_value = amount * price
            total_usdt += usdt_value
            result_assets[asset] = {
                'amount': amount,
                'price': price,
                'usdt_value': usdt_value,
                'percent': 0.0
            }

        if total_usdt > 0:
            for asset in result_assets:
                result_assets[asset]['percent'] = (result_assets[asset]['usdt_value'] / total_usdt) * 100

        return {
            'total_usdt': total_usdt,
            'assets': result_assets
        }

    def get_coins_value(self, symbols: List[str]) -> Dict:
        """Value of specific coins only (for a virtual portfolio).

        Uses total balance (free + locked in open orders) so that coins
        sitting in TP limit-sell orders are still counted correctly.
        """
        balances = self.get_total_balance()
        prices = self.get_all_prices(symbols)
        total = 0.0
        details = {}
        for s in symbols:
            key = self.normalize_asset_symbol(s)
            amount = float(balances.get(key, 0.0) or balances.get(s, 0.0))
            price = float(prices.get(key, 0.0) or prices.get(s, 0.0))
            usdt_value = amount * price
            total += usdt_value
            details[s] = {
                'amount': amount,
                'price': price,
                'usdt_value': usdt_value
            }
        return {'total_usdt': total, 'assets': details}

    def create_market_order(self, symbol: str, side: str, amount: float) -> Optional[dict]:
        """
        symbol: BTC/USDT
        side: buy or sell
        amount: base currency amount

        Returns None (without raising) when the amount is dust / below
        exchange minimum precision or min notional. This prevents spam
        when stop-loss tries to sell tiny leftover balances.
        """
        try:
            if not getattr(self.exchange, "markets", None):
                self.exchange.load_markets()
            market = self.exchange.market(symbol)
            limits = market.get("limits") or {}
            min_amount = float((limits.get("amount") or {}).get("min") or 0)
            min_cost = float((limits.get("cost") or {}).get("min") or 1.0)
            if min_cost <= 0:
                min_cost = 1.0

            # Round down to exchange precision first
            try:
                amount = float(self.exchange.amount_to_precision(symbol, amount))
            except Exception:
                pass

            if amount <= 0:
                return None
            if min_amount > 0 and amount < min_amount:
                return None  # dust below min amount

            # Check notional value for sells (and buys when possible)
            try:
                ticker = self.exchange.fetch_ticker(symbol)
                price = float(ticker.get("last") or ticker.get("close") or 0)
            except Exception:
                price = 0.0
            if price > 0 and (amount * price) < min_cost:
                return None  # dust below min notional

            order = self.exchange.create_order(
                symbol=symbol,
                type='market',
                side=side,
                amount=amount
            )
            return order
        except Exception as e:
            err = str(e).lower()
            # Treat precision / min-amount errors as dust instead of hard failure
            if any(x in err for x in (
                "minimum amount precision",
                "min amount",
                "amount of",
                "filter failure",
                "notional",
                "too small",
            )):
                return None
            raise Exception(f"Order failed: {str(e)}")

    def create_market_buy_usdt(self, symbol: str, usdt_amount: float) -> Optional[dict]:
        """Buy using quote amount (USDT). Tries create_order with cost, falls back to amount calculation."""
        pair = f"{symbol}/{self.quote}"
        try:
            # Prefer cost-based if supported
            order = self.exchange.create_order(
                symbol=pair,
                type='market',
                side='buy',
                amount=None,
                params={'cost': usdt_amount}
            )
            return order
        except Exception:
            # Fallback: calculate amount from price
            price = self.get_ticker_price(pair)
            if price <= 0:
                raise Exception(f"Cannot get price for {pair}")
            amount = (usdt_amount * 0.998) / price   # small buffer for fees
            return self.create_market_order(pair, 'buy', amount)

    def get_markets(self) -> List[str]:
        """Return list of available base assets that have /USDT pair"""
        markets = self.exchange.load_markets()
        bases = []
        for symbol, market in markets.items():
            if market.get('quote') == self.quote and market.get('active', True) and market.get('spot', True):
                bases.append(market['base'])
        return sorted(set(bases))

    def create_limit_sell(self, symbol: str, amount: float, price: float) -> Optional[dict]:
        """Place a limit sell order (used for Take Profit visible on MEXC).

        Returns None (without raising) when amount/notional is below exchange
        minimums so tiny dust positions do not spam errors.
        MEXC typically requires min notional ≈ 1 USDT.
        """
        pair = f"{symbol}/{self.quote}"
        try:
            # Ensure markets are loaded for precision checks
            if not getattr(self.exchange, "markets", None):
                self.exchange.load_markets()
            market = self.exchange.market(pair)
            limits = market.get("limits") or {}
            min_amount = float((limits.get("amount") or {}).get("min") or 0)
            min_cost = float((limits.get("cost") or {}).get("min") or 1.0)  # MEXC ≈ 1 USDT
            if min_cost <= 0:
                min_cost = 1.0
            precision_amount = market.get("precision", {}).get("amount")
            # Round down to exchange precision
            amount = float(self.exchange.amount_to_precision(pair, amount))
            price = float(self.exchange.price_to_precision(pair, price))
            if amount <= 0 or price <= 0:
                return None
            if min_amount > 0 and amount < min_amount:
                return None
            notional = amount * price
            if notional < min_cost:
                return None
            # Some MEXC pairs treat precision as minimum step
            if precision_amount is not None:
                try:
                    step = float(precision_amount)
                    if 0 < step < 1 and amount < step:
                        return None
                except (TypeError, ValueError):
                    pass
            order = self.exchange.create_order(
                symbol=pair,
                type='limit',
                side='sell',
                amount=amount,
                price=price,
            )
            return order
        except Exception as e:
            msg = str(e).lower()
            # Treat precision / minimum amount / min volume errors as skippable
            if any(x in msg for x in (
                "minimum amount", "min amount", "precision", "too small",
                "minimum transaction", "cannot be less", "min notional",
                "30002",
            )):
                return None
            raise Exception(f"Limit sell failed for {pair}: {str(e)}")

    def cancel_order(self, order_id: str, symbol: str, strict: bool = False) -> Optional[dict]:
        """Cancel an open order by id."""
        pair = f"{symbol}/{self.quote}" if "/" not in symbol else symbol
        try:
            return self.exchange.cancel_order(order_id, pair)
        except Exception as e:
            # Order may already be filled/cancelled
            if strict:
                raise
            return None

    def fetch_open_orders(self, symbol: str = None) -> List[dict]:
        """Fetch open orders, optionally filtered by symbol."""
        try:
            if symbol:
                pair = f"{symbol}/{self.quote}" if "/" not in symbol else symbol
                return self.exchange.fetch_open_orders(pair)
            return self.exchange.fetch_open_orders()
        except Exception:
            return []

    def fetch_open_sell_orders(self, symbol: str) -> List[dict]:
        """Fetch only open sell orders for a base asset."""
        return [
            order for order in self.fetch_open_orders(symbol)
            if str(order.get("side") or "").lower() == "sell"
        ]

    def fetch_order(self, order_id: str, symbol: str) -> Optional[dict]:
        """Fetch a single order status."""
        pair = f"{symbol}/{self.quote}" if "/" not in symbol else symbol
        try:
            return self.exchange.fetch_order(order_id, pair)
        except Exception:
            return None

    def get_free_amount(self, symbol: str) -> float:
        """Free balance of a base asset."""
        bal = self.get_balance()
        key = self.normalize_asset_symbol(symbol)
        return float(bal.get(key, 0.0) or bal.get(symbol, 0.0))

    def get_total_amount(self, symbol: str) -> float:
        """Total balance (free + locked in open orders) of a base asset."""
        bal = self.get_total_balance()
        key = self.normalize_asset_symbol(symbol)
        return float(bal.get(key, 0.0) or bal.get(symbol, 0.0))

    def cancel_all_open_sells(self, symbol: str) -> Dict:
        """Cancel every open sell order for a symbol on the exchange."""
        result = {"cancelled": [], "errors": []}
        try:
            orders = self.fetch_open_sell_orders(symbol)
            for order in orders:
                oid = order.get("id")
                if not oid:
                    continue
                try:
                    self.cancel_order(str(oid), symbol, strict=False)
                    result["cancelled"].append(str(oid))
                except Exception as exc:
                    result["errors"].append({"order_id": str(oid), "error": str(exc)})
        except Exception as exc:
            result["errors"].append({"error": str(exc)})
        return result


    def get_expert_market_context(self, symbols: List[str], timeframe: str = "1h") -> Dict:
        """
        يبني سياق حي لنظام الخبراء من بيانات MEXC Spot فقط.
        - OHLCV للحجم والاتجاه والزخم والنطاق
        - Order Book للانحياز اللحظي
        يرجع dict جاهز لـ ExpertsSystem.decide()
        """
        tf_map = {
            "15m": "15m", "1h": "1h", "4h": "4h",
            "1d": "1d", "1w": "1w",
        }
        tf = tf_map.get(timeframe, "1h")

        # نختار مرجع قوي: BTC إن وُجد، وإلا أول عملة صالحة
        clean = []
        for s in (symbols or []):
            base = self.normalize_asset_symbol(s)
            if base and base != self.quote and base not in clean:
                clean.append(base)

        if not clean:
            clean = ["BTC"]

        # نقرأ حتى 5 عملات كحد أقصى (سرعة + تمثيل المحفظة)
        to_scan = clean[:5]
        if "BTC" in clean and "BTC" not in to_scan:
            to_scan = ["BTC"] + to_scan[:4]

        per_symbol = {}
        errors = []

        for base in to_scan:
            pair = f"{base}/{self.quote}"
            try:
                ohlcv = self.exchange.fetch_ohlcv(pair, tf, limit=50)
                if not ohlcv or len(ohlcv) < 15:
                    errors.append(f"{base}: قليل الشموع")
                    continue

                closes = [float(c[4]) for c in ohlcv]
                highs = [float(c[2]) for c in ohlcv]
                lows = [float(c[3]) for c in ohlcv]
                volumes = [float(c[5]) for c in ohlcv]
                last = closes[-1]
                if last <= 0:
                    continue

                # حجم
                avg_vol = sum(volumes[:-1]) / max(len(volumes) - 1, 1)
                last_vol = volumes[-1]
                vol_ratio = (last_vol / avg_vol) if avg_vol > 0 else 0.0
                volume_weak = vol_ratio < 0.75
                candle_strength = "strong" if vol_ratio >= 1.25 else ("weak" if vol_ratio < 0.85 else "normal")

                # اتجاه (SMA 8 vs SMA 21)
                sma_fast = sum(closes[-8:]) / 8
                sma_slow = sum(closes[-21:]) / min(21, len(closes))
                ratio = sma_fast / sma_slow if sma_slow else 1.0
                if ratio > 1.008:
                    trend = "up"
                elif ratio < 0.992:
                    trend = "down"
                else:
                    trend = "sideways"

                # زخم
                mom = abs(ratio - 1.0)
                if mom > 0.02:
                    momentum = "strong"
                elif mom > 0.008:
                    momentum = "cool"
                else:
                    momentum = "weak"

                # نطاق / consolidation
                window = 20
                hi = max(highs[-window:])
                lo = min(lows[-window:])
                range_pct = (hi - lo) / last if last else 0.0
                consolidation = range_pct < 0.045

                # كسر مؤكد
                prev_hi = max(highs[-window:-1]) if len(highs) > 1 else hi
                prev_lo = min(lows[-window:-1]) if len(lows) > 1 else lo
                breakout_up = last > prev_hi * 1.004
                breakout_dn = last < prev_lo * 0.996
                breakout_confirmed = breakout_up or breakout_dn

                # Order book bias
                orderflow_bias = "neutral"
                try:
                    ob = self.exchange.fetch_order_book(pair, limit=15)
                    bid_vol = sum(float(b[1]) for b in (ob.get("bids") or [])[:10])
                    ask_vol = sum(float(a[1]) for a in (ob.get("asks") or [])[:10])
                    if bid_vol > ask_vol * 1.18:
                        orderflow_bias = "buy"
                    elif ask_vol > bid_vol * 1.18:
                        orderflow_bias = "sell"
                except Exception:
                    pass

                # ATR تقريبي لنسبة المخاطرة
                trs = []
                for i in range(1, min(15, len(ohlcv))):
                    h, l, pc = highs[-i], lows[-i], closes[-i - 1]
                    trs.append(max(h - l, abs(h - pc), abs(l - pc)))
                atr = sum(trs) / len(trs) if trs else last * 0.02
                atr_pct = atr / last if last else 0.02
                # نعتبر RR مقبول لو ATR مش متفجر (سوق قابل لإدارة الاستوب)
                rr_ok = 0.005 < atr_pct < 0.08

                per_symbol[base] = {
                    "volume_weak": volume_weak,
                    "candle_strength": candle_strength,
                    "orderflow_bias": orderflow_bias,
                    "trend": trend,
                    "momentum": momentum,
                    "consolidation": consolidation,
                    "breakout_confirmed": breakout_confirmed,
                    "rr_ok": rr_ok,
                    "range_pct": round(range_pct * 100, 2),
                    "vol_ratio": round(vol_ratio, 2),
                    "last": last,
                }
            except Exception as e:
                errors.append(f"{base}: {e}")

        if not per_symbol:
            # fallback محافظ — يمنع أي دخول عشوائي
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
                "data_source": "fallback (فشل قراءة MEXC)",
                "symbols_used": ", ".join(to_scan),
                "errors": errors[:5],
            }

        # تجميع الأغلبية عبر العملات المقروءة
        def majority(key, default):
            vals = [d[key] for d in per_symbol.values() if key in d]
            if not vals:
                return default
            from collections import Counter
            return Counter(vals).most_common(1)[0][0]

        volume_weak = majority("volume_weak", True)
        # لو أي عملة حجمها قوي نخفف الفيتو قليلاً
        if any(not d.get("volume_weak", True) for d in per_symbol.values()):
            # أغلبية ضعيفة فقط لو أكثر من نصفها ضعيف
            weak_count = sum(1 for d in per_symbol.values() if d.get("volume_weak", True))
            volume_weak = weak_count > len(per_symbol) / 2

        trend = majority("trend", "sideways")
        momentum = majority("momentum", "cool")
        consolidation = majority("consolidation", True)
        breakout = any(d.get("breakout_confirmed") for d in per_symbol.values())
        rr_ok = majority("rr_ok", False)
        orderflow = majority("orderflow_bias", "neutral")
        candle = majority("candle_strength", "weak")

        if trend == "up":
            regime = "uptrend"
        elif trend == "down":
            regime = "downtrend"
        else:
            regime = "consolidation"

        # على السلسلة / مزاج / أخبار: محايد حالياً (مصادر خارجية لاحقاً)
        # لكن نستخدم orderflow كإشارة تقريبية للسيولة الفورية
        whales = "neutral"
        if orderflow == "buy" and not volume_weak:
            whales = "accumulate"
        elif orderflow == "sell" and not volume_weak:
            whales = "distribute"

        used = ", ".join(per_symbol.keys())
        detail = " | ".join(
            f"{k}:T={v['trend']}/V={v['vol_ratio']}/R={v['range_pct']}%"
            for k, v in list(per_symbol.items())[:3]
        )

        return {
            "netflow": "neutral",
            "whales": whales,
            "volume_weak": volume_weak,
            "candle_strength": candle,
            "orderflow_bias": orderflow,
            "trend": trend,
            "momentum": momentum,
            "regime": regime,
            "mood": "neutral",
            "macro": "neutral",
            "consolidation": consolidation,
            "breakout_confirmed": breakout,
            "rr_ok": rr_ok,
            "data_source": f"MEXC Spot OHLCV+OB ({tf})",
            "symbols_used": used,
            "detail": detail,
            "errors": errors[:3] if errors else [],
            "per_symbol": {k: {kk: vv for kk, vv in v.items() if kk != "last"} for k, v in per_symbol.items()},
        }
