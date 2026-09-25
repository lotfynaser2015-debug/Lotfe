"""
    MEXC Portfolio Manager
    بوت تليجرام لإدارة محافظ متعددة على MEXC Spot مع أهداف ذكية وTrailing.
"""
import logging
import re
from typing import Optional, Dict, Any
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

import config
from database import (
    init_db, SessionLocal, get_or_create_user, get_portfolios, get_portfolio,
    create_portfolio, add_coin_to_portfolio, remove_coin_from_portfolio,
    close_portfolio, delete_portfolio_completely, clear_coin_position,
    delete_orphaned_portfolio_records,
    set_portfolio_running, log_action,
    Portfolio, PortfolioCoin, PortfolioTrade, RebalanceLog, UserSettings,
    update_coin_position, reset_coin_positions, get_open_positions,
    record_trade_event, get_trade_event, get_reentry_candidates,
    get_portfolio_trade_events, mark_reentry_events_used,
)
from mexc_client import MexcClient
from rebalancer import Rebalancer
from auto_system import (
    portfolio_specs,
    format_coins_message,
    set_system_enabled,
    is_system_enabled,
    detect_market_regime,
    should_allow_entry,
    daily_loss_triggered,
    should_alert_defense,
    AUTO_PORTFOLIO_NAMES,
    PORTFOLIO_CORE_NAME,
    PORTFOLIO_GROWTH_NAME,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

(
    CREATE_NAME, CREATE_AMOUNT, CREATE_COINS, ADD_COIN, INCREASE_AMOUNT,
) = range(5)

_mexc: Optional[MexcClient] = None
_reb: Optional[Rebalancer] = None


def get_mexc() -> MexcClient:
    global _mexc
    if _mexc is None:
        _mexc = MexcClient()
    return _mexc


def get_reb() -> Rebalancer:
    global _reb
    if _reb is None:
        _reb = Rebalancer(get_mexc())
    return _reb


def is_admin(user_id: int) -> bool:
    if not config.ADMIN_TELEGRAM_ID:
        return True
    return user_id == config.ADMIN_TELEGRAM_ID


async def ensure_admin(update: Update) -> bool:
    user = update.effective_user
    if not user or not is_admin(user.id):
        msg = update.effective_message
        if msg:
            await msg.reply_text("⛔ *غير مصرح*\nهذا البوت مخصص للأدمن فقط.", parse_mode="Markdown")
        return False
    return True


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 محافظي", callback_data="list_pf"),
            InlineKeyboardButton("➕ محفظة جديدة", callback_data="create_pf"),
        ],
        [InlineKeyboardButton("📡 حالة السوق", callback_data="auto_sys_status")],
        [InlineKeyboardButton("💰 الرصيد", callback_data="balance")],
        [
            InlineKeyboardButton("🧹 تنظيف القاعدة", callback_data="cleanup_db"),
            InlineKeyboardButton("⚙️ الإعدادات", callback_data="settings"),
        ],
    ])


def pf_keyboard(pf_id: int, is_running: bool):
    rows = [
        [
            InlineKeyboardButton("▶️ تشغيل" if not is_running else "⏹ إيقاف", callback_data=f"toggle_{pf_id}"),
            InlineKeyboardButton("📊 تفاصيل", callback_data=f"view_{pf_id}"),
        ],
        [
            InlineKeyboardButton("➕ عملة", callback_data=f"addcoin_{pf_id}"),
            InlineKeyboardButton("➖ عملة", callback_data=f"removecoin_{pf_id}"),
        ],
        [
            InlineKeyboardButton("💰 زيادة المبلغ", callback_data=f"increase_{pf_id}"),
            InlineKeyboardButton("🔄 تحديث الأهداف", callback_data=f"refresh_tp_{pf_id}"),
        ],
        [InlineKeyboardButton("🧠/✍️ وضع التحكم", callback_data=f"mode_{pf_id}")],
        [
            InlineKeyboardButton("🔎 فحص الناقص", callback_data=f"check_missing_{pf_id}"),
            InlineKeyboardButton("📊 إحصائيات", callback_data=f"stats_{pf_id}"),
        ],
        [
            InlineKeyboardButton("♻️ إعادة بناء", callback_data=f"rebuild_{pf_id}"),
            InlineKeyboardButton("🗑 حذف", callback_data=f"delete_pf_{pf_id}"),
        ],
        [InlineKeyboardButton("⬅️ القائمة", callback_data="menu")],
    ]
    return InlineKeyboardMarkup(rows)


def _missing_selection_key(pf_id: int) -> str:
    return f"missing_reentry_selection_{pf_id}"


def _missing_reentry_keyboard(pf_id: int, missing_symbols, selected, allow_selection=True):
    rows = []
    if allow_selection:
        for symbol in missing_symbols:
            marker = "✅" if symbol in selected else "⬜"
            rows.append([
                InlineKeyboardButton(
                    f"{marker} {symbol}",
                    callback_data=f"missing_toggle_{pf_id}_{symbol}",
                )
            ])
    if selected and allow_selection:
        rows.append([
            InlineKeyboardButton(
                f"✅ تأكيد إعادة الدخول ({len(selected)})",
                callback_data=f"missing_confirm_{pf_id}",
            )
        ])
    rows.append([InlineKeyboardButton("🔄 إعادة الفحص", callback_data=f"missing_{pf_id}")])
    rows.append([InlineKeyboardButton("⬅️ المحفظة", callback_data=f"view_{pf_id}")])
    return InlineKeyboardMarkup(rows)


def format_pf(p, current_value: float = None, prices: dict = None, events=None) -> str:
    """عرض محفظة بربح/خسارة حقيقية (من سعر الدخول + المحقق).

    مش بيقارن قيمة الباقي بالمخصص الأصلي — ده كان بيظهر خسارة وهمية بعد البيع.
    """
    status = "🟢 شغالة" if p.is_running else "⚪ متوقفة"
    mode = "يدوي: أهداف + وقف ثابت" if str(getattr(p, "control_mode", "smart") or "smart").lower() == "manual" else "ذكي: مستويات + وقف متحرك"
    allocated = float(p.investment_usdt or 0)
    prices = prices or {}
    events = events or []

    # --- مراكز مفتوحة: تكلفة الدخول vs القيمة الحالية ---
    open_cost = 0.0
    open_value = 0.0
    open_count = 0
    for coin in p.coins:
        status_c = (coin.position_status or "idle")
        if status_c not in ("open", "tp1_hit", "tp2_hit", "tp3_hit", "tp_hit"):
            # لو لسه فيه كمية متبقية نعتبرها مفتوحة
            rem = float(coin.remaining_amount or coin.amount or 0)
            entry = float(coin.entry_price or 0)
            if rem <= 0 or entry <= 0:
                continue
        else:
            rem = float(coin.remaining_amount or coin.amount or 0)
            entry = float(coin.entry_price or 0)
            if rem <= 0 or entry <= 0:
                continue
        px = float(prices.get(coin.symbol) or 0)
        if px <= 0 and current_value is not None:
            # prices dict may be incomplete
            pass
        if px <= 0:
            px = entry
        open_cost += entry * rem
        open_value += px * rem
        open_count += 1

    # لو current_value اتبعت من المنصة (رصيد فعلي) استخدمه للقيمة
    if current_value is not None and current_value >= 0:
        # استخدم الرصيد الفعلي كقيمة، والتكلفة من الدخول للكمية المتبقية
        if open_cost > 0:
            open_value = float(current_value)
        elif float(current_value or 0) > 0 and open_cost <= 0:
            open_value = float(current_value)

    unrealized = open_value - open_cost if open_cost > 0 else 0.0
    unrealized_pct = ((open_value / open_cost) - 1.0) * 100.0 if open_cost > 0 else 0.0

    realized = sum(float(getattr(e, "realized_pnl", 0) or 0) for e in events)
    total_pnl = realized + unrealized
    total_basis = open_cost + abs(min(realized, 0))  # rough
    # نسبة الإجمالي على المخصص فقط لو لسه في مراكز أو في محقق
    if open_cost > 0:
        total_pct = (total_pnl / open_cost) * 100.0
    elif allocated > 0 and (realized != 0 or open_value > 0):
        total_pct = (total_pnl / allocated) * 100.0
    else:
        total_pct = 0.0

    symbols = [c.symbol for c in p.coins]
    if symbols:
        rows = []
        for i in range(0, len(symbols), 3):
            chunk = symbols[i:i + 3]
            cells = [f"▣ *{s}*" for s in chunk]
            rows.append("   ".join(cells))
        coins_block = "\n".join(rows)
    else:
        coins_block = "—"

    out = [
        f"📁 *{p.name}*  `#{p.id}`",
        "━━━━━━━━━━━━━━━━━━━━",
        f"الحالة: *{status}*",
        f"التحكم: *{mode}*",
        f"المخصص الأصلي: *{allocated:.2f}* USDT",
    ]
    out.append(f"قيمة المراكز المفتوحة: *{open_value:.2f}* USDT")
    if open_cost > 0:
        out.append(f"تكلفة الدخول (المتبقي): *{open_cost:.2f}* USDT")

    u_emoji = "🟢" if unrealized >= 0 else "🔴"
    r_emoji = "🟢" if realized >= 0 else "🔴"
    t_emoji = "🟢" if total_pnl >= 0 else "🔴"

    if open_cost > 0 or open_count > 0:
        out.append(
            f"{u_emoji} غير المحقق: *{unrealized:+.2f}* USDT (*{unrealized_pct:+.2f}%*)"
        )
    if events:
        out.append(f"{r_emoji} المحقق: *{realized:+.2f}* USDT")
        out.append(f"{t_emoji} *الإجمالي: {total_pnl:+.2f} USDT*")
    elif open_cost > 0:
        out.append(
            f"{t_emoji} الربح/الخسارة: *{unrealized:+.2f}* USDT (*{unrealized_pct:+.2f}%*)"
        )
    elif current_value is not None and allocated > 0 and open_value < 1.0:
        # مفيش مراكز مفتوحة تقريبًا — المخصص اتحول لسيولة بعد البيع
        out.append("_المراكز مغلقة/مباعة — راجع الإحصائيات للمحقق_")

    out.append("")
    out.append(f"*العملات* ({len(symbols)})")
    out.append(coins_block)
    return "\n".join(out)


def format_portfolio_stats(p, events, prices=None) -> str:
    """Format realized and current unrealized P&L for one portfolio."""
    prices = prices or {}
    realized = sum(float(e.realized_pnl or 0) for e in events)
    open_cost = 0.0
    open_value = 0.0
    open_lines = []
    for coin in p.coins:
        if coin.position_status not in ("open", "tp1_hit", "tp2_hit", "tp3_hit", "tp_hit"):
            continue
        remaining = float(coin.remaining_amount or coin.amount or 0)
        entry = float(coin.entry_price or 0)
        price = float(prices.get(coin.symbol) or entry or 0)
        if remaining <= 0 or entry <= 0:
            continue
        cost = remaining * entry
        value = remaining * price
        open_cost += cost
        open_value += value
        pnl = value - cost
        emoji = "🟢" if pnl >= 0 else "🔴"
        open_lines.append(f"{emoji} `{coin.symbol}`: `{pnl:+.2f}` USDT")
    unrealized = open_value - open_cost
    total = realized + unrealized
    r_emoji = "🟢" if realized >= 0 else "🔴"
    u_emoji = "🟢" if unrealized >= 0 else "🔴"
    t_emoji = "🟢" if total >= 0 else "🔴"
    result = (
        f"📊 *إحصائيات محفظة {p.name}*\n"
        "━━━━━━━━━━━━━━━━\n"
        f"{r_emoji} المحقق: `{realized:+.2f}` USDT\n"
        f"{u_emoji} غير المحقق: `{unrealized:+.2f}` USDT\n"
        f"{t_emoji} *الإجمالي: `{total:+.2f}` USDT*\n"
        f"عدد العمليات: `{len(events)}`"
    )
    if open_lines:
        result += "\n\n*المراكز المفتوحة:*\n" + "\n".join(open_lines)
    return result


def format_source(s) -> str:
    status = "🟢 *مفعل*" if s.enabled else "🔴 *معطل*"
    return (
        f"📡 *{s.name}*  `#{s.id}`\n"
        "━━━━━━━━━━━━━━━━\n"
        f"الحالة: {status}\n"
        f"الحد الأدنى: `{s.min_usd:,.0f}` $\n"
        f"أقصى تحويلات: `{s.max_tx_count}`\n"
        f"شراء: {'✅ مسموح' if s.allow_buy else '❌ ممنوع'}\n"
        f"بيع: {'✅ مسموح' if s.allow_sell else '❌ ممنوع'}\n"
        f"محافظ الشراء: `{s.buy_portfolio_ids or '—'}`\n"
        f"محافظ البيع: `{s.sell_portfolio_ids or '—'}`\n"
        f"التبريد: `{s.cooldown_minutes}` دقيقة"
    )


def _build_cleanup_plan(db, telegram_id: int, presence: Dict[str, Dict[str, float]]) -> Dict:
    """Find only data that is provably inactive without touching working portfolios."""
    portfolios = get_portfolios(db, telegram_id, status=None)
    closed_portfolios = []
    stale_positions = []

    for portfolio in portfolios:
        portfolio_has_live_asset = any(
            presence.get(coin.symbol, {}).get("present", False)
            for coin in portfolio.coins
        )
        if portfolio.status != "active" and not portfolio_has_live_asset:
            closed_portfolios.append({
                "id": portfolio.id,
                "name": portfolio.name,
                "coins": list(portfolio.coins),
            })
            continue

        # A running portfolio is always protected. A stopped portfolio keeps
        # its configuration, but stale position/TP/SL tracking is removable
        # when MEXC confirms that the asset is no longer held.
        if portfolio.status != "active" or portfolio.is_running:
            continue
        for coin in portfolio.coins:
            if coin.position_status in (None, "", "idle", "waiting_reentry"):
                continue
            if presence.get(coin.symbol, {}).get("present", False):
                continue
            stale_positions.append({
                "id": coin.id,
                "portfolio_id": portfolio.id,
                "portfolio_name": portfolio.name,
                "symbol": coin.symbol,
                "position_status": coin.position_status,
                "coin": coin,
            })

    portfolio_ids = {portfolio.id for portfolio in portfolios}
    trade_query = db.query(PortfolioTrade).filter(
        PortfolioTrade.telegram_id == telegram_id
    )
    log_query = db.query(RebalanceLog).filter(
        RebalanceLog.telegram_id == telegram_id
    )
    if portfolio_ids:
        trade_query = trade_query.filter(~PortfolioTrade.portfolio_id.in_(portfolio_ids))
        log_query = log_query.filter(~RebalanceLog.portfolio_id.in_(portfolio_ids))

    return {
        "closed_portfolios": closed_portfolios,
        "stale_positions": stale_positions,
        "orphan_trades": trade_query.count(),
        "orphan_logs": log_query.count(),
    }


def _cleanup_report(plan: Dict) -> str:
    closed = plan["closed_portfolios"]
    stale = plan["stale_positions"]
    orphan_trades = plan["orphan_trades"]
    orphan_logs = plan["orphan_logs"]
    lines = [
        "🔎 *فحص قاعدة البيانات*",
        "",
        "تم الإبقاء على كل محفظة تعمل وكل عملة لها رصيد أو أمر بيع قائم على MEXC.",
        "",
        f"🗑 محافظ مغلقة بلا أصول: `{len(closed)}`",
        f"🧹 مراكز قديمة بلا رصيد: `{len(stale)}`",
        f"🧾 سجلات عمليات يتيمة: `{orphan_trades}` | سجلات إعادة توازن يتيمة: `{orphan_logs}`",
    ]
    if closed:
        lines.append("\n*المحافظ المرشحة للحذف:*")
        lines.extend(f"• #{item['id']} {item['name']}" for item in closed[:10])
        if len(closed) > 10:
            lines.append(f"• ... و`{len(closed) - 10}` أخرى")
    if stale:
        lines.append("\n*بيانات المراكز المرشحة للمسح:*")
        lines.extend(
            f"• {item['portfolio_name']} — `{item['symbol']}` ({item['position_status']})"
            for item in stale[:15]
        )
        if len(stale) > 15:
            lines.append(f"• ... و`{len(stale) - 15}` أخرى")
    if not closed and not stale and not orphan_trades and not orphan_logs:
        lines.append("\n✅ لا توجد بيانات قديمة آمنة للتنظيف.")
    else:
        lines.extend([
            "",
            "لن يتم بيع أي أصل في عملية التنظيف.",
            "سيتم إلغاء أوامر الأهداف المرتبطة بالبيانات القديمة ثم حذفها فقط بعد إعادة الفحص.",
        ])
    return "\n".join(lines)


async def _show_cleanup_scan(query, context, tid):
    import asyncio

    db = SessionLocal()
    try:
        await query.edit_message_text("⏳ جاري فحص المحافظ والرصيد قبل اقتراح التنظيف...")
        portfolios = get_portfolios(db, tid, status=None)
        symbols = sorted({
            coin.symbol for portfolio in portfolios for coin in portfolio.coins
        })
        try:
            loop = asyncio.get_event_loop()
            presence = await loop.run_in_executor(
                None,
                lambda: get_mexc().get_portfolio_presence(symbols) if symbols else {},
            )
        except Exception as exc:
            logger.exception("Database cleanup scan failed")
            await query.edit_message_text(
                "⚠️ تعذر فحص الرصيد من MEXC.\n"
                "لم يتم حذف أو تعديل أي بيانات.\n\n"
                f"الخطأ: `{exc}`",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(),
            )
            return

        plan = _build_cleanup_plan(db, tid, presence)
        context.user_data["cleanup_ready"] = True
        buttons = []
        if plan["closed_portfolios"] or plan["stale_positions"]:
            buttons.append([
                InlineKeyboardButton("✅ تأكيد التنظيف", callback_data="cleanup_confirm"),
            ])
        buttons.extend([
            [InlineKeyboardButton("🔄 إعادة الفحص", callback_data="cleanup_db")],
            [InlineKeyboardButton("⬅️ القائمة", callback_data="menu")],
        ])
        await query.edit_message_text(
            _cleanup_report(plan),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    finally:
        db.close()


async def _do_cleanup(query, context, tid):
    import asyncio

    db = SessionLocal()
    try:
        await query.edit_message_text("⏳ إعادة الفحص ثم تنظيف البيانات غير النشطة...")
        portfolios = get_portfolios(db, tid, status=None)
        symbols = sorted({
            coin.symbol for portfolio in portfolios for coin in portfolio.coins
        })
        try:
            loop = asyncio.get_event_loop()
            presence = await loop.run_in_executor(
                None,
                lambda: get_mexc().get_portfolio_presence(symbols) if symbols else {},
            )
        except Exception as exc:
            logger.exception("Database cleanup preflight failed")
            await query.edit_message_text(
                "⚠️ تعذر إعادة فحص الرصيد.\nلم يتم حذف أو تعديل أي بيانات.\n\n"
                f"الخطأ: `{exc}`",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(),
            )
            return

        plan = _build_cleanup_plan(db, tid, presence)
        if (
            not plan["closed_portfolios"]
            and not plan["stale_positions"]
            and not plan["orphan_trades"]
            and not plan["orphan_logs"]
        ):
            context.user_data.pop("cleanup_ready", None)
            await query.edit_message_text(
                "✅ لا توجد بيانات قديمة آمنة للتنظيف بعد إعادة الفحص.",
                reply_markup=main_menu_keyboard(),
            )
            return

        cancelled = 0
        cleared = 0
        deleted_portfolios = 0

        for item in plan["stale_positions"]:
            coin = item["coin"]
            result = get_reb().cancel_tp_orders([{
                "symbol": coin.symbol,
                "tp_order_id": coin.tp_order_id,
                "tp1_order_id": coin.tp1_order_id,
                "tp2_order_id": coin.tp2_order_id,
                "tp3_order_id": coin.tp3_order_id,
            }])
            cancelled += len(result.get("cancelled", []))
            if result.get("errors"):
                continue
            if clear_coin_position(db, coin.id):
                cleared += 1

        for item in plan["closed_portfolios"]:
            portfolio_cancel_failed = False
            for coin in item["coins"]:
                result = get_reb().cancel_tp_orders([{
                    "symbol": coin.symbol,
                    "tp_order_id": coin.tp_order_id,
                    "tp1_order_id": coin.tp1_order_id,
                    "tp2_order_id": coin.tp2_order_id,
                    "tp3_order_id": coin.tp3_order_id,
                }])
                cancelled += len(result.get("cancelled", []))
                portfolio_cancel_failed = portfolio_cancel_failed or bool(result.get("errors"))
            if not portfolio_cancel_failed and delete_portfolio_completely(db, item["id"], tid):
                deleted_portfolios += 1

        deleted_trades, deleted_logs = delete_orphaned_portfolio_records(db, tid)
        context.user_data.pop("cleanup_ready", None)
        await query.edit_message_text(
            "✅ *تم تنظيف قاعدة البيانات بعد إعادة الفحص.*\n\n"
            f"المحافظ المحذوفة: `{deleted_portfolios}`\n"
            f"بيانات المراكز القديمة الممسوحة: `{cleared}`\n"
            f"أوامر الأهداف الملغاة: `{cancelled}`\n\n"
            f"سجلات العمليات اليتيمة المحذوفة: `{deleted_trades}`\n"
            f"سجلات إعادة التوازن اليتيمة المحذوفة: `{deleted_logs}`\n\n"
            "تم ترك المحافظ العاملة والعملات ذات الرصيد كما هي.",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
    finally:
        db.close()


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_admin(update):
        return
    db = SessionLocal()
    try:
        get_or_create_user(db, update.effective_user.id)
    finally:
        db.close()
    await update.message.reply_text(
        "🚀 *MEXC Portfolio Manager*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "إدارة محافظ متعددة + نظام إشارات ذكي\n"
        "تنفيذ تلقائي من المجموعة • أهداف ربح ووقف خسارة\n\n"
        "اختر من القائمة أدناه 👇",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "❎ تم الإلغاء.\nرجعت للقائمة الرئيسية 👇",
        reply_markup=ReplyKeyboardRemove(),
    )
    await update.message.reply_text(
        "🏠 *القائمة الرئيسية*",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )
    return ConversationHandler.END


async def on_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    if not await ensure_admin(update):
        return

    text = update.message.text

    if context.user_data.get("waiting"):
        # تعديل إعدادات عامة
        user_field = context.user_data.get("edit_user_tpsl")
        if user_field:
            try:
                val = float(text.strip().replace("%", "").replace(",", "."))
                if val < 0 or val > 100:
                    await update.message.reply_text("أدخل رقم بين 0 و 100.")
                    return
                db = SessionLocal()
                try:
                    user = get_or_create_user(db, update.effective_user.id)
                    if hasattr(user, user_field):
                        setattr(user, user_field, val)
                        db.commit()
                        await update.message.reply_text(
                            f"✅ تم تحديث `{user_field}` = `{val}`",
                            parse_mode="Markdown",
                            reply_markup=main_menu_keyboard(),
                        )
                    else:
                        await update.message.reply_text("حقل غير معروف.", reply_markup=main_menu_keyboard())
                finally:
                    db.close()
                context.user_data.clear()
            except ValueError:
                await update.message.reply_text("أدخل رقم صحيح (مثال: 5)")
            return

        # تعديل أهداف محفظة معيّنة
        pf_edit = context.user_data.get("edit_pf_tpsl")
        if pf_edit:
            try:
                val = float(text.strip().replace("%", "").replace(",", "."))
                if val < 0 or val > 100:
                    await update.message.reply_text("أدخل رقم بين 0 و 100 (0 = استخدم العام).")
                    return
                field = pf_edit.get("field")
                pf_id = pf_edit.get("pf_id")
                db = SessionLocal()
                try:
                    p = get_portfolio(db, pf_id, update.effective_user.id)
                    if not p:
                        await update.message.reply_text("المحفظة غير موجودة.")
                        return
                    if field and hasattr(p, field):
                        setattr(p, field, val if val > 0 else None)
                        db.commit()
                        await update.message.reply_text(
                            f"✅ تم تحديث `{field}` = `{val}`",
                            parse_mode="Markdown",
                            reply_markup=main_menu_keyboard(),
                        )
                    else:
                        await update.message.reply_text("حقل غير معروف.", reply_markup=main_menu_keyboard())
                finally:
                    db.close()
                context.user_data.clear()
            except ValueError:
                await update.message.reply_text("أدخل رقم صحيح (مثال: 5)")
            return
        return


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # Answer immediately so Telegram doesn't timeout (query expires ~seconds)
    try:
        await query.answer()
    except Exception:
        pass
    if not await ensure_admin(update):
        return
    data = query.data or ""
    tid = update.effective_user.id

    if data == "menu":
        await query.edit_message_text(
            "🏠 *القائمة الرئيسية*\nاختر ما تريد:",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "auto_sys_status":
        try:
            reg = detect_market_regime(get_mexc())
            db = SessionLocal()
            try:
                pfs = get_portfolios(db, tid, status="active")
                if pfs:
                    pf_lines = "\n".join(
                        f"{'🟢 إدارة شغالة' if p.is_running else '⚪ متوقفة'} — `{p.name}`"
                        for p in pfs
                    )
                else:
                    pf_lines = "_لا محافظ — أنشئ ثم شغّل من داخل المحفظة_"
            finally:
                db.close()
            text = (
                f"📡 *حالة السوق*\n{reg.message}\n\n"
                f"*محافظك:*\n{pf_lines}\n\n"
                f"شغّل المحفظة من داخلها ← الإدارة الذكية تشتغل على المحفظة دي فقط."
            )
        except Exception as e:
            text = f"⚠️ `{e}`"
        await query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=main_menu_keyboard()
        )
        return

    if data == "auto_sys_start":
        await query.edit_message_text(
            "الإدارة *لكل محفظة لوحدها*.\nافتح المحفظة → *▶️ تشغيل المحفظة*.",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "auto_sys_stop":
        await query.edit_message_text(
            "لإيقاف الإدارة: داخل المحفظة → *⏹ إيقاف المحفظة*.",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "list_pf":
        db = SessionLocal()
        try:
            pfs = get_portfolios(db, tid, status="active")
            if not pfs:
                await query.edit_message_text(
                    "📭 لا توجد محافظ نشطة حالياً.\nاضغط ➕ *محفظة جديدة* للبدء.",
                    parse_mode="Markdown",
                    reply_markup=main_menu_keyboard(),
                )
                return
            buttons = [
                [InlineKeyboardButton(
                    f"{'🟢' if p.is_running else '⚪'}"
                    f" #{p.id} {p.name} · {p.investment_usdt:.0f}$",
                    callback_data=f"view_{p.id}",
                )]
                for p in pfs
            ]
            buttons.append([InlineKeyboardButton("⬅️ القائمة الرئيسية", callback_data="menu")])
            await query.edit_message_text(
                f"📋 *محافظك النشطة* ({len(pfs)})\n━━━━━━━━━━━━━━━━",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        finally:
            db.close()
        return

    if data == "balance":
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(None, lambda: get_mexc().get_portfolio_value())
            total = float(info.get("total_usdt") or 0)
            assets = info.get("assets") or {}
            lines = [f"💰 *رصيد الحساب*", f"الإجمالي ≈ `{total:.2f}` USDT", ""]
            # sort by value desc
            items = sorted(
                ((k, v) for k, v in assets.items() if float(v.get("usdt_value") or 0) > 0.5),
                key=lambda x: float(x[1].get("usdt_value") or 0),
                reverse=True,
            )
            for sym, row in items[:25]:
                lines.append(
                    f"`{sym}`: `{float(row.get('amount') or 0):.6g}` ≈ `{float(row.get('usdt_value') or 0):.2f}$`"
                )
            if not items:
                lines.append("_لا توجد أرصدة تُذكر_")
            await query.edit_message_text(
                "\n".join(lines),
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(),
            )
        except Exception as e:
            logger.exception("balance failed")
            await query.edit_message_text(
                f"⚠️ فشل جلب الرصيد: `{e}`",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(),
            )
        return

    if data == "settings":
        db = SessionLocal()
        try:
            user = get_or_create_user(db, tid)
            msg = (
                "⚙️ *الإعدادات العامة*\n\n"
                f"TP1: `{user.tp1_pct or 3}%` | بيع: `{user.tp1_sell_pct or 40}%`\n"
                f"TP2: `{user.tp2_pct or 5}%` | بيع: `{user.tp2_sell_pct or 30}%`\n"
                f"TP3: `{user.tp3_pct or 8}%`\n"
                f"وقف الخسارة: `{user.stop_loss_pct or 3}%`\n"
                f"أقصى عملات/محفظة: `{user.max_coins_per_portfolio or 30}`\n\n"
                "_لتعديل أهداف محفظة معيّنة: افتح المحفظة → الأهداف_"
            )
            await query.edit_message_text(
                msg,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("TP1 %", callback_data="set_tp1_pct"),
                     InlineKeyboardButton("بيع 1 %", callback_data="set_tp1_sell_pct")],
                    [InlineKeyboardButton("TP2 %", callback_data="set_tp2_pct"),
                     InlineKeyboardButton("بيع 2 %", callback_data="set_tp2_sell_pct")],
                    [InlineKeyboardButton("TP3 %", callback_data="set_tp3_pct"),
                     InlineKeyboardButton("استوب %", callback_data="set_stop_loss_pct")],
                    [InlineKeyboardButton("⬅️ القائمة", callback_data="menu")],
                ]),
            )
        finally:
            db.close()
        return

    if data.startswith("set_") and data in (
        "set_tp1_pct", "set_tp2_pct", "set_tp3_pct",
        "set_tp1_sell_pct", "set_tp2_sell_pct", "set_stop_loss_pct",
    ):
        field = data[len("set_"):]
        context.user_data["waiting"] = True
        context.user_data["edit_user_tpsl"] = field
        labels = {
            "tp1_pct": "هدف 1 %",
            "tp2_pct": "هدف 2 %",
            "tp3_pct": "هدف 3 %",
            "tp1_sell_pct": "نسبة البيع عند الهدف 1",
            "tp2_sell_pct": "نسبة البيع عند الهدف 2",
            "stop_loss_pct": "وقف الخسارة %",
        }
        await query.edit_message_text(
            f"أرسل قيمة *{labels.get(field, field)}*:\n(رقم بين 0 و 100)",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ رجوع", callback_data="settings")],
            ]),
        )
        return

    if data == "cleanup_db":
        await _show_cleanup_scan(query, context, tid)
        return

    if data == "cleanup_confirm":
        if not context.user_data.get("cleanup_ready"):
            await query.edit_message_text(
                "⚠️ اعمل فحص أولاً من زر التنظيف.",
                reply_markup=main_menu_keyboard(),
            )
            return
        await _do_cleanup(query, context, tid)
        return

    if data.startswith("view_") and not data.startswith("view_src_"):
        pf_id = int(data.split("_")[1])
        db = SessionLocal()
        try:
            p = get_portfolio(db, pf_id, tid)
            if not p:
                await query.edit_message_text("المحفظة غير موجودة.", reply_markup=main_menu_keyboard())
                return
            coins = [c.symbol for c in p.coins]
            current_value = None
            prices = {}
            if coins:
                try:
                    val = get_mexc().get_coins_value(coins)
                    current_value = float(val.get("total_usdt") or 0)
                    assets = val.get("assets") or {}
                    for sym, row in assets.items():
                        try:
                            prices[sym] = float(row.get("price") or 0)
                        except Exception:
                            pass
                except Exception:
                    current_value = None
                if not prices:
                    try:
                        prices = get_mexc().get_all_prices(coins) or {}
                    except Exception:
                        prices = {}
            events = []
            try:
                events = get_portfolio_trade_events(db, p.id, tid) or []
            except Exception:
                events = []
            await query.edit_message_text(
                format_pf(p, current_value=current_value, prices=prices, events=events),
                parse_mode="Markdown",
                reply_markup=pf_keyboard(
                    p.id, p.is_running,
                ),
            )
        finally:
            db.close()
        return

    # ——— خبراء داخل المحفظة ———
    if data.startswith("pf_tpsl_"):
        pf_id = int(data.split("_")[2])
        db = SessionLocal()
        try:
            pf = get_portfolio(db, pf_id, tid)
            if not pf:
                await query.edit_message_text("غير موجودة.", reply_markup=main_menu_keyboard())
                return
            user = get_or_create_user(db, tid)
            def show(v, default):
                return f"`{v}`" if v is not None and float(v) > 0 else f"`{default}` (عام)"
            t1 = getattr(pf, "tp1_pct", None)
            t2 = getattr(pf, "tp2_pct", None)
            t3 = getattr(pf, "tp3_pct", None)
            s1 = getattr(pf, "tp1_sell_pct", None)
            s2 = getattr(pf, "tp2_sell_pct", None)
            sl = getattr(pf, "stop_loss_pct", None)
            ut1 = getattr(user, "tp1_pct", 3.0) or 3.0
            ut2 = getattr(user, "tp2_pct", 5.0) or 5.0
            ut3 = getattr(user, "tp3_pct", 8.0) or 8.0
            us1 = getattr(user, "tp1_sell_pct", 40.0) or 40.0
            us2 = getattr(user, "tp2_sell_pct", 30.0) or 30.0
            usl = getattr(user, "stop_loss_pct", 3.0) or 3.0
            msg = (
                f"🎯 *أهداف المحفظة:* {pf.name}\n\n"
                f"TP1: {show(t1, ut1)}% | بيع: {show(s1, us1)}%\n"
                f"TP2: {show(t2, ut2)}% | بيع: {show(s2, us2)}%\n"
                f"TP3: {show(t3, ut3)}%\n"
                f"استوب: {show(sl, usl)}%\n\n"
                f"_لو فاضية = تستخدم الإعدادات العامة_"
            )
            await query.edit_message_text(
                msg, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("TP1 %", callback_data=f"pftpsl_{pf_id}_tp1_pct"),
                     InlineKeyboardButton("بيع 1 %", callback_data=f"pftpsl_{pf_id}_tp1_sell_pct")],
                    [InlineKeyboardButton("TP2 %", callback_data=f"pftpsl_{pf_id}_tp2_pct"),
                     InlineKeyboardButton("بيع 2 %", callback_data=f"pftpsl_{pf_id}_tp2_sell_pct")],
                    [InlineKeyboardButton("TP3 %", callback_data=f"pftpsl_{pf_id}_tp3_pct"),
                     InlineKeyboardButton("استوب %", callback_data=f"pftpsl_{pf_id}_stop_loss_pct")],
                    [InlineKeyboardButton("🗑 امسح تخصيص المحفظة", callback_data=f"pftpsl_clear_{pf_id}")],
                    [InlineKeyboardButton("⬅️ رجوع", callback_data=f"view_{pf_id}")],
                ]),
            )
        finally:
            db.close()
        return

    if data.startswith("pftpsl_clear_"):
        pf_id = int(data.split("_")[2])
        db = SessionLocal()
        try:
            pf = get_portfolio(db, pf_id, tid)
            if pf:
                for f in ("tp1_pct", "tp2_pct", "tp3_pct", "tp1_sell_pct", "tp2_sell_pct", "stop_loss_pct"):
                    setattr(pf, f, None)
                db.commit()
            await query.edit_message_text(
                "✅ تم مسح تخصيص المحفظة — هتستخدم الإعدادات العامة.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ رجوع", callback_data=f"pf_tpsl_{pf_id}")]]),
            )
        finally:
            db.close()
        return

    if data.startswith("pftpsl_") and not data.startswith("pftpsl_clear_"):
        # pftpsl_{id}_{field}
        parts = data.split("_", 2)
        # data = pftpsl_12_tp1_pct  -> need careful parse
        rest = data[len("pftpsl_"):]  # 12_tp1_pct
        pf_id_str, field = rest.split("_", 1)
        pf_id = int(pf_id_str)
        context.user_data["waiting"] = True
        context.user_data["edit_pf_tpsl"] = {"pf_id": pf_id, "field": field}
        labels = {
            "tp1_pct": "هدف 1 %",
            "tp2_pct": "هدف 2 %",
            "tp3_pct": "هدف 3 %",
            "tp1_sell_pct": "نسبة البيع عند الهدف 1",
            "tp2_sell_pct": "نسبة البيع عند الهدف 2",
            "stop_loss_pct": "وقف الخسارة %",
        }
        await query.edit_message_text(
            f"أرسل قيمة *{labels.get(field, field)}* لهذه المحفظة:\n(أو `0` لمسح التخصيص واستخدام العام)",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ رجوع", callback_data=f"pf_tpsl_{pf_id}")]]),
        )
        return
    if data.startswith("increase_"):
        pf_id = int(data.split("_")[1])
        await query.edit_message_text(
            "ℹ️ زيادة رأس المال صارت عبر *♻️ إعادة بناء المراكز*:\n"
            "يكمّل الناقص من رصيد USDT المتاح ويعيد الأهداف الذكية.\n"
            "حوّل USDT للحساب ثم اضغط الزر.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("♻️ إعادة بناء المراكز", callback_data=f"rebuild_{pf_id}")],
                [InlineKeyboardButton("⬅️ المحفظة", callback_data=f"view_{pf_id}")],
            ]),
        )
        return
    if data.startswith("addcoin_"):
        context.user_data["addcoin_pf"] = int(data.split("_")[1])
        context.user_data["waiting"] = True
        await query.edit_message_text("أرسل رمز العملة (مثال: BTC أو ETH):")
        return ADD_COIN
    if data.startswith("removecoin_"):
        pf_id = int(data.split("_")[1])
        db = SessionLocal()
        try:
            p = get_portfolio(db, pf_id, tid)
            if not p or not p.coins:
                await query.edit_message_text("لا توجد عملات.", reply_markup=main_menu_keyboard())
                return
            buttons = [[InlineKeyboardButton(f"حذف {c.symbol}", callback_data=f"delcoin_{pf_id}_{c.symbol}")] for c in p.coins]
            buttons.append([InlineKeyboardButton("⬅️ رجوع", callback_data=f"view_{pf_id}")])
            await query.edit_message_text("اختر العملة للحذف:", reply_markup=InlineKeyboardMarkup(buttons))
        finally:
            db.close()
        return
    if data.startswith("delcoin_"):
        parts = data.split("_")
        await _do_remove_coin(query, tid, int(parts[1]), parts[2])
        return
    if data.startswith("close_"):
        pf_id = int(data.split("_")[1])
        await query.edit_message_text(
            "⚠️ هل أنت متأكد من حذف المحفظة؟",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ نعم، احذفها", callback_data=f"confirm_close_{pf_id}")],
                [InlineKeyboardButton("❌ إلغاء", callback_data=f"view_{pf_id}")],
            ])
        )
        return
    if data.startswith("confirm_close_"):
        await _do_close(query, tid, int(data.split("_")[2]))
        return

    # ——— اختيار وضع التحكم داخل المحفظة ———
    if data.startswith("mode_"):
        pf_id = int(data.split("_")[1])
        db = SessionLocal()
        try:
            p = get_portfolio(db, pf_id, tid)
            if not p:
                await query.edit_message_text("المحفظة غير موجودة.", reply_markup=main_menu_keyboard())
                return
            if p.is_running:
                await query.edit_message_text(
                    "⚠️ أوقف المحفظة أولاً قبل تغيير وضع التحكم، ثم أعد بناء المراكز.",
                    reply_markup=pf_keyboard(pf_id, True),
                )
                return
            current = str(getattr(p, "control_mode", "smart") or "smart").lower()
            p.control_mode = "manual" if current != "manual" else "smart"
            db.commit()
            mode_name = "يدوي (TP/SL ثابت)" if p.control_mode == "manual" else "ذكي (ATR + Trailing)"
            await query.edit_message_text(
                f"✅ تم اختيار وضع: *{mode_name}*\n\n"
                "أعد تشغيل المحفظة أو استخدم إعادة بناء المراكز لتطبيق الوضع على المراكز.",
                parse_mode="Markdown",
                reply_markup=pf_keyboard(pf_id, False),
            )
        finally:
            db.close()
        return

    # ——— أزرار التحكم داخل المحفظة ———
    if data.startswith("toggle_"):
        pf_id = int(data.split("_")[1])
        db = SessionLocal()
        try:
            p = get_portfolio(db, pf_id, tid)
            if not p:
                await query.edit_message_text("المحفظة غير موجودة.", reply_markup=main_menu_keyboard())
                return
            if p.is_running:
                await _do_stop(query, tid, pf_id)
            else:
                await _do_start(query, tid, pf_id)
        finally:
            db.close()
        return

    if data.startswith("rebuild_"):
        pf_id = int(data.split("_")[1])
        await _do_rebuild_positions(query, tid, pf_id)
        return

    if data.startswith("refresh_tp_"):
        pf_id = int(data.split("_")[1])
        await _do_refresh_tpsl(query, tid, pf_id)
        return

    if data.startswith("stats_"):
        pf_id = int(data.split("_")[1])
        db = SessionLocal()
        try:
            p = get_portfolio(db, pf_id, tid)
            if not p:
                await query.edit_message_text("المحفظة غير موجودة.", reply_markup=main_menu_keyboard())
                return
            events = []
            try:
                events = get_portfolio_trade_events(db, p.id, tid) or []
            except Exception:
                events = []
            prices = {}
            symbols = [c.symbol for c in p.coins]
            if symbols:
                try:
                    prices = get_mexc().get_all_prices(symbols) or {}
                except Exception:
                    prices = {}
            await query.edit_message_text(
                format_portfolio_stats(p, events, prices),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ المحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
        finally:
            db.close()
        return

    # فحص الناقص — الزر يبعت check_missing_ أو missing_
    if data.startswith("check_missing_") or (data.startswith("missing_") and not data.startswith("missing_toggle_") and not data.startswith("missing_confirm_")):
        # check_missing_12  أو  missing_12
        parts = data.split("_")
        pf_id = int(parts[-1])
        await _show_missing_reentry(query, context, tid, pf_id)
        return

    if data.startswith("missing_toggle_"):
        # missing_toggle_{pf_id}_{SYMBOL}
        rest = data[len("missing_toggle_"):]
        pf_id_str, symbol = rest.split("_", 1)
        await _show_missing_reentry(query, context, tid, int(pf_id_str), toggle_symbol=symbol)
        return

    if data.startswith("missing_confirm_"):
        pf_id = int(data.split("_")[-1])
        await _do_missing_reentry(query, context, tid, pf_id)
        return

    # حذف المحفظة — الزر يبعت delete_pf_
    if data.startswith("delete_pf_"):
        pf_id = int(data.split("_")[-1])
        await query.edit_message_text(
            "⚠️ هل أنت متأكد من حذف المحفظة؟",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ نعم، احذفها", callback_data=f"confirm_close_{pf_id}")],
                [InlineKeyboardButton("❌ إلغاء", callback_data=f"view_{pf_id}")],
            ]),
        )
        return

    # أهداف المحفظة (من زر التفاصيل الداخلي إن وُجد)
    if data.startswith("pf_tpsl_") and data.count("_") == 2:
        # already handled above as pf_tpsl_
        pass


async def _do_rebuild_positions(query, tid, pf_id):
    """دورة كاملة داخل المحفظة:
    1) إلغاء كل أوامر البيع القديمة من المنصة + مسحها من القاعدة
    2) التعرف على الرصيد الفعلي
    3) شراء العملات الناقصة
    4) إعادة وضع أهداف TP/SL الجديدة
    """
    import asyncio
    import time

    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, tid)
        if not p:
            await query.edit_message_text("المحفظة غير موجودة.", reply_markup=main_menu_keyboard())
            return
        if not p.is_running:
            await query.edit_message_text(
                "⚠️ المحفظة متوقفة.\nشغّلها أولاً ثم استخدم *إعادة بناء المراكز*.",
                parse_mode="Markdown",
                reply_markup=pf_keyboard(pf_id, False),
            )
            return
        if not p.coins:
            await query.edit_message_text(
                "لا توجد عملات في هذه المحفظة.",
                reply_markup=pf_keyboard(pf_id, True),
            )
            return

        user = get_or_create_user(db, tid)

        def pct(pf_val, user_val, default):
            if pf_val is not None and float(pf_val) > 0:
                return float(pf_val)
            if user_val is not None and float(user_val) > 0:
                return float(user_val)
            return default

        tp1 = pct(getattr(p, "tp1_pct", None), getattr(user, "tp1_pct", None), 3.0)
        tp2 = pct(getattr(p, "tp2_pct", None), getattr(user, "tp2_pct", None), 5.0)
        tp3 = pct(getattr(p, "tp3_pct", None), getattr(user, "tp3_pct", None), 8.0)
        s1 = pct(getattr(p, "tp1_sell_pct", None), getattr(user, "tp1_sell_pct", None), 40.0)
        s2 = pct(getattr(p, "tp2_sell_pct", None), getattr(user, "tp2_sell_pct", None), 30.0)
        sl_pct = pct(getattr(p, "stop_loss_pct", None), getattr(user, "stop_loss_pct", None), 3.0)

        await query.edit_message_text(
            f"♻️ *إعادة بناء مراكز محفظة {p.name}*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "① إلغاء الأهداف القديمة من المنصة...",
            parse_mode="Markdown",
        )

        client = get_mexc()
        reb = get_reb()
        symbols = [c.symbol for c in p.coins]
        cancelled_count = 0
        cancel_errors = []

        # 1) Cancel DB-known TP orders + any open sells on exchange
        for coin in p.coins:
            try:
                res = reb.cancel_tp_orders([{
                    "symbol": coin.symbol,
                    "tp_order_id": getattr(coin, "tp_order_id", None),
                    "tp1_order_id": getattr(coin, "tp1_order_id", None),
                    "tp2_order_id": getattr(coin, "tp2_order_id", None),
                    "tp3_order_id": getattr(coin, "tp3_order_id", None),
                }])
                cancelled_count += len(res.get("cancelled") or [])
                for e in res.get("errors") or []:
                    cancel_errors.append(f"{coin.symbol}: {e.get('error', e)}")
            except Exception as exc:
                cancel_errors.append(f"{coin.symbol}: {exc}")
            try:
                extra = client.cancel_all_open_sells(coin.symbol)
                cancelled_count += len(extra.get("cancelled") or [])
            except Exception:
                pass
            # Clear order IDs from DB immediately
            update_coin_position(
                db, coin.id,
                tp_order_id=None,
                tp1_order_id=None,
                tp2_order_id=None,
                tp3_order_id=None,
            )

        await asyncio.sleep(1.0)
        await query.edit_message_text(
            f"♻️ *إعادة بناء مراكز محفظة {p.name}*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"① أُلغي `{cancelled_count}` أمر قديم\n"
            "② فحص الرصيد الفعلي...",
            parse_mode="Markdown",
        )

        # 2) انتظار قصير بعد الإلغاء ثم قراءة الرصيد من جديد
        await asyncio.sleep(1.5)
        presence = client.get_portfolio_presence(symbols)
        min_usdt = float(getattr(config, "BALANCE_PRESENCE_MIN_USDT", 1.0))
        allocated = float(p.investment_usdt or 0)
        n_coins = len(p.coins)
        target_per_coin = allocated / n_coins if n_coins > 0 else 0.0
        free_usdt = client.get_free_usdt()

        # هدف كل عملة = التخصيص ÷ العدد
        # أي فرق ≥ 1$ يُشترى من رصيد USDT المتاح في الحساب (حتى من خارج المحفظة)
        coin_status = []  # (coin, amount, price, market_value, need_buy_usdt)
        total_value = 0.0
        total_need = 0.0
        for coin in p.coins:
            info = presence.get(coin.symbol) or presence.get(coin.symbol.upper()) or {}
            val = float(info.get("market_value") or 0)
            amt = float(info.get("amount") or 0)
            price = float(info.get("price") or 0)
            if amt <= 0 or val < min_usdt:
                val = 0.0
                amt = 0.0
            need = max(0.0, target_per_coin - val)
            # تجاهل فرق أقل من 1$ (غبار)
            if need < 1.0:
                need = 0.0
            total_value += val
            total_need += need
            coin_status.append((coin, amt, price, val, need))

        under = [x for x in coin_status if x[4] >= 1.0]
        ok_coins = [x for x in coin_status if x[4] < 1.0]

        await query.edit_message_text(
            f"♻️ *إعادة بناء مراكز محفظة {p.name}*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"① أُلغي `{cancelled_count}` أمر قديم\n"
            f"② هدف كل عملة: *`{target_per_coin:.2f}$`*  (إجمالي `{allocated:.0f}$`)\n"
            f"القيمة الحالية: `{total_value:.2f}$` | الناقص: `{total_need:.2f}$`\n"
            f"USDT متاح في الحساب: `{free_usdt:.2f}$`\n"
            f"يحتاج تكميل: `{len(under)}` | مكتمل: `{len(ok_coins)}`\n"
            "③ شراء/تكميل من رصيد الحساب...",
            parse_mode="Markdown",
        )

        # 3) تكميل كل عملة تحت الهدف من USDT المتاح (حتى من خارج المحفظة)
        bought = []
        topped = []
        buy_errors = []
        total_spent = 0.0

        for coin, amt, price, val, need in under:
            # حدّث الرصيد الحر قبل كل شراء
            free_usdt = client.get_free_usdt()
            buy_amt = min(need, free_usdt * 0.995)
            if buy_amt < 1.0:
                buy_errors.append(
                    f"{coin.symbol}: يحتاج `{need:.2f}$` لكن USDT المتاح `{free_usdt:.2f}$`"
                )
                continue
            try:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda c=coin, a=buy_amt: reb.reentry_buy_and_place_tp(
                        c.symbol, a, tp1, tp2, tp3, sl_pct, s1, s2,
                        control_mode=getattr(p, "control_mode", "smart"),
                    ),
                )
                if result.get("error"):
                    buy_errors.append(f"{coin.symbol}: {result['error']}")
                    continue
                entry = float(result.get("entry_price") or 0)
                amount = float(result.get("amount") or result.get("remaining_amount") or 0)
                # بعد الشراء: الكمية الكلية ≈ القديمة + الجديدة
                new_total_amt = max(amount, client.get_total_amount(coin.symbol) * 0.998)
                update_coin_position(
                    db, coin.id,
                    entry_price=entry if entry > 0 else (price or coin.entry_price or 0),
                    amount=new_total_amt,
                    remaining_amount=new_total_amt,
                    tp1_price=result.get("tp1_price", 0),
                    tp2_price=result.get("tp2_price", 0),
                    tp3_price=result.get("tp3_price", 0),
                    tp_price=result.get("tp1_price", 0),
                    current_sl_price=result.get("sl_price", 0),
                    original_sl_price=result.get("original_sl_price", 0),
                    tp1_order_id=result.get("tp1_order_id"),
                    tp2_order_id=result.get("tp2_order_id"),
                    tp3_order_id=result.get("tp3_order_id"),
                    position_status="open",
                    reentry_used=False,
                    reentry_touched=False,
                )
                mark_reentry_events_used(db, p.id, coin.symbol)
                total_spent += buy_amt
                if val < min_usdt:
                    bought.append(coin.symbol)
                else:
                    topped.append(f"{coin.symbol}(+{buy_amt:.1f}$)")
            except Exception as exc:
                buy_errors.append(f"{coin.symbol}: {exc}")
            time.sleep(0.35)

        # 4) إعادة وضع الأهداف للعملات المكتملة مسبقاً
        await query.edit_message_text(
            f"♻️ *إعادة بناء مراكز محفظة {p.name}*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"① أُلغي `{cancelled_count}` أمر\n"
            f"② هدف كل عملة: `{target_per_coin:.2f}$`\n"
            f"③ اشتريت: `{len(bought)}` | كمّلت: `{len(topped)}` | صُرف: `{total_spent:.2f}$`\n"
            "④ إعادة وضع الأهداف للباقي...",
            parse_mode="Markdown",
        )

        refreshed = list(bought)  # already have TP from reentry_buy
        refresh_errors = []
        already_done = set(bought) | {t.split("(")[0] for t in topped}

        for coin, amt, price, val, need in coin_status:
            if coin.symbol in already_done:
                if coin.symbol not in refreshed:
                    refreshed.append(coin.symbol)
                continue
            try:
                free_amt = client.get_free_amount(coin.symbol)
                use_amt = free_amt if free_amt > 0 else amt
                use_amt = use_amt * 0.998
                entry = float(coin.entry_price or 0)
                if entry <= 0:
                    entry = price or client.get_ticker_price(f"{coin.symbol}/{client.quote}")
                if use_amt <= 0 or entry <= 0:
                    refresh_errors.append(f"{coin.symbol}: لا يوجد رصيد كافٍ")
                    continue
                # الوضع اليدوي يحافظ على نسب المستخدم؛ الذكي فقط يحسب ATR.
                if str(getattr(p, "control_mode", "smart") or "smart").lower() == "manual":
                    use_tp1, use_tp2, use_tp3, use_sl = tp1, tp2, tp3, sl_pct
                else:
                    try:
                        levels = reb.build_smart_levels_for_entry(
                            coin.symbol, entry, tp1, tp2, tp3, sl_pct,
                        )
                        use_tp1, use_tp2, use_tp3, use_sl = (
                            levels.tp1_pct, levels.tp2_pct, levels.tp3_pct, levels.stop_loss_pct,
                        )
                    except Exception:
                        use_tp1, use_tp2, use_tp3, use_sl = tp1, tp2, tp3, sl_pct
                result = reb.place_tp_orders(
                    [{"symbol": coin.symbol, "amount": use_amt, "entry_price": entry}],
                    use_tp1, use_tp2, use_tp3, use_sl, s1, s2,
                    control_mode=getattr(p, "control_mode", "smart"),
                )[0]
                if result.get("error"):
                    refresh_errors.append(f"{coin.symbol}: {result['error']}")
                update_coin_position(
                    db, coin.id,
                    entry_price=entry,
                    amount=use_amt,
                    remaining_amount=use_amt,
                    tp1_price=result.get("tp1_price", 0),
                    tp2_price=result.get("tp2_price", 0),
                    tp3_price=result.get("tp3_price", 0),
                    tp_price=result.get("tp1_price", 0),
                    current_sl_price=result.get("sl_price", 0),
                    original_sl_price=result.get("original_sl_price", 0),
                    tp1_order_id=result.get("tp1_order_id"),
                    tp2_order_id=result.get("tp2_order_id"),
                    tp3_order_id=result.get("tp3_order_id"),
                    position_status="open",
                    reentry_used=False,
                    reentry_touched=False,
                    reentry_price=0.0,
                )
                refreshed.append(coin.symbol)
            except Exception as exc:
                refresh_errors.append(f"{coin.symbol}: {exc}")

        log_action(
            db, tid, "rebuild_positions",
            f"Rebuild {p.name}: target={target_per_coin:.2f} cancel={cancelled_count} "
            f"buy={bought} top={topped} spent={total_spent:.2f}",
            not (buy_errors or refresh_errors),
            pf_id,
        )

        free_after = client.get_free_usdt()
        # Final report
        lines = [
            f"✅ *تمت إعادة بناء مراكز* `{p.name}`",
            "━━━━━━━━━━━━━━━━━━━━",
            f"💰 التخصيص: `{allocated:.0f}$` ÷ `{n_coins}` = *`{target_per_coin:.2f}$`* لكل عملة",
            f"📊 كانت القيمة: `{total_value:.2f}$` | الناقص: `{total_need:.2f}$`",
            f"🗑 أوامر قديمة ملغاة: `{cancelled_count}`",
            f"🛒 عملات جديدة: `{len(bought)}`" + (f" — {', '.join(f'`{x}`' for x in bought)}" if bought else ""),
            f"📈 تم تكميل: `{len(topped)}`" + (f" — {', '.join(f'`{x}`' for x in topped[:8])}" if topped else ""),
            f"💵 صُرف من الحساب: `{total_spent:.2f}$` | USDT متبقي: `{free_after:.2f}$`",
            f"🎯 أهداف ذكية وُضعت لـ: `{len(refreshed)}` عملة",
            "(كل عملة حسب ATR والاتجاه — مع حد أدنى لحماية مساحة البامب)",
        ]
        all_errs = cancel_errors[:3] + buy_errors + refresh_errors
        if all_errs:
            lines.append("\n⚠️ ملاحظات:")
            for e in all_errs[:10]:
                lines.append(f"• {e}")
            if any("USDT المتاح" in str(e) for e in buy_errors):
                lines.append("\n💡 حوّل USDT إضافي للحساب ثم أعد *إعادة بناء المراكز*.")
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=pf_keyboard(pf_id, True),
        )
    except Exception as exc:
        logger.exception("rebuild_positions failed")
        await query.edit_message_text(
            f"❌ فشل إعادة البناء:\n`{exc}`",
            parse_mode="Markdown",
            reply_markup=pf_keyboard(pf_id, True),
        )
    finally:
        db.close()


async def _do_refresh_tpsl(query, tid, pf_id):
    """تحويل المراكز المفتوحة لنظام الوقف المتحرك بدون أي بيع."""
    import asyncio
    import time as _time

    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, tid)
        if not p:
            await query.edit_message_text("المحفظة غير موجودة.", reply_markup=main_menu_keyboard())
            return
        if not p.is_running:
            await query.edit_message_text(
                "المحفظة متوقفة؛ شغّلها أولاً ثم اضغط *تحديث الأهداف*.",
                parse_mode="Markdown",
                reply_markup=pf_keyboard(pf_id, False),
            )
            return
        if str(getattr(p, "control_mode", "smart") or "smart").lower() == "manual":
            await query.edit_message_text(
                "⚠️ هذه المحفظة على الوضع اليدوي.\n"
                "لن يتم تحويلها للوقف المتحرك حتى لا تتغير أهدافك اليدوية.",
                reply_markup=pf_keyboard(pf_id, p.is_running),
            )
            return

        user = get_or_create_user(db, tid)

        def pct(pf_val, user_val, default):
            if pf_val is not None and float(pf_val) > 0:
                return float(pf_val)
            if user_val is not None and float(user_val) > 0:
                return float(user_val)
            return default

        fb_sl = pct(getattr(p, "stop_loss_pct", None), getattr(user, "stop_loss_pct", None), 3.0)

        await query.edit_message_text(
            "⏳ جاري تحويل المحفظة لنظام *الوقف المتحرك*...\n"
            "إلغاء أوامر الأهداف القديمة + ضبط الاستوب — بدون بيع.",
            parse_mode="Markdown",
        )

        updated = []
        details = []
        errors = []
        reb = get_reb()
        client = get_mexc()

        symbols = [c.symbol for c in p.coins]
        try:
            prices = await asyncio.get_event_loop().run_in_executor(
                None, lambda: client.get_all_prices(symbols) or {}
            )
        except Exception:
            prices = {}

        for coin in p.coins:
            status = (coin.position_status or "idle")
            if status not in ("open", "tp1_hit", "tp2_hit", "tp3_hit", "tp_hit"):
                continue
            amount = float(coin.remaining_amount or coin.amount or 0)
            entry = float(coin.entry_price or 0)
            if amount <= 0 or entry <= 0:
                continue
            try:
                reb.cancel_tp_orders([{
                    "symbol": coin.symbol,
                    "tp_order_id": getattr(coin, "tp_order_id", None),
                    "tp1_order_id": getattr(coin, "tp1_order_id", None),
                    "tp2_order_id": getattr(coin, "tp2_order_id", None),
                    "tp3_order_id": getattr(coin, "tp3_order_id", None),
                }])

                result = reb.place_tp_orders(
                    [{
                        "symbol": coin.symbol,
                        "amount": amount,
                        "entry_price": entry,
                        "tp1_order_id": getattr(coin, "tp1_order_id", None),
                        "tp2_order_id": getattr(coin, "tp2_order_id", None),
                        "tp3_order_id": getattr(coin, "tp3_order_id", None),
                    }],
                    0, 0, 0, fb_sl, 0, 0,
                    control_mode="smart",
                )[0]
                if result.get("error"):
                    errors.append(f"{coin.symbol}: {result['error']}")
                    continue

                new_sl = float(result.get("sl_price") or result.get("stop_loss_price") or 0)
                price = float(prices.get(coin.symbol) or prices.get(f"{coin.symbol}/USDT") or 0)
                if price <= 0:
                    try:
                        price = float(client.get_ticker_price(f"{coin.symbol}/{client.quote}") or 0)
                    except Exception:
                        price = 0

                if price > 0 and entry > 0:
                    gain_pct = ((price / entry) - 1.0) * 100.0
                    from rebalancer import (
                        DEFAULT_TRAIL_PCT, BE_LOCK_PCT, PUMP_GAIN_PCT, PUMP_TRAIL_PCT,
                        PUMP_STRONG_GAIN_PCT, PUMP_STRONG_TRAIL_PCT,
                    )
                    if gain_pct >= PUMP_STRONG_GAIN_PCT:
                        trail_pct = PUMP_STRONG_TRAIL_PCT
                    elif gain_pct >= PUMP_GAIN_PCT:
                        trail_pct = PUMP_TRAIL_PCT
                    else:
                        trail_pct = DEFAULT_TRAIL_PCT
                    candidate = price * (1.0 - trail_pct / 100.0)
                    if gain_pct >= BE_LOCK_PCT:
                        candidate = max(candidate, entry)
                    if candidate > new_sl:
                        new_sl = candidate

                update_coin_position(
                    db,
                    coin.id,
                    position_status="open",
                    tp1_price=0.0,
                    tp2_price=0.0,
                    tp3_price=0.0,
                    tp_price=0.0,
                    current_sl_price=new_sl,
                    original_sl_price=result.get("original_sl_price") or new_sl,
                    tp1_order_id=None,
                    tp2_order_id=None,
                    tp3_order_id=None,
                    tp_order_id=None,
                )
                updated.append(coin.symbol)
                gain_txt = ""
                if price > 0 and entry > 0:
                    g = ((price / entry) - 1.0) * 100.0
                    gain_txt = f" | ربح `{g:+.1f}%`"
                details.append(f"`{coin.symbol}` SL `{new_sl:.6g}`{gain_txt}")
                _time.sleep(0.25)
            except Exception as exc:
                errors.append(f"{coin.symbol}: {exc}")

        log_action(
            db, tid, "refresh_tpsl",
            f"Trailing migrate {p.name}: {', '.join(updated) or 'none'}",
            not errors, pf_id,
        )
        lines = [
            f"✅ *تم التحويل للوقف المتحرك* — محفظة *{p.name}*",
            "لم يتم بيع أي عملة.",
            "اتلغت أوامر الأهداف القديمة واتظبط الاستوب المتحرك.",
            f"تم تحديث: *{len(updated)}* عملة",
        ]
        if details:
            lines.append("")
            lines.extend(details[:20])
            if len(details) > 20:
                lines.append(f"... و {len(details) - 20} أخرى")
        if errors:
            lines.append("")
            lines.append("⚠️ ملاحظات:")
            lines.extend(f"• {e}" for e in errors[:10])
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=pf_keyboard(pf_id, True),
        )
    except Exception as e:
        logger.exception("refresh trailing migrate failed")
        await query.edit_message_text(
            f"⚠️ فشل التحديث: `{e}`",
            parse_mode="Markdown",
            reply_markup=pf_keyboard(pf_id, True),
        )
    finally:
        db.close()


async def _show_missing_reentry(query, context, tid, pf_id, toggle_symbol=None):
    """Show missing portfolio coins and let the user build a buy selection."""
    import asyncio

    db = SessionLocal()
    try:
        pf = get_portfolio(db, pf_id, tid)
        if not pf:
            await query.edit_message_text("المحفظة غير موجودة.", reply_markup=main_menu_keyboard())
            return

        symbols = [c.symbol for c in pf.coins]
        if not symbols:
            await query.edit_message_text(
                "المحفظة لا تحتوي على عملات.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ المحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
            return

        loop = asyncio.get_event_loop()
        try:
            presence = await loop.run_in_executor(
                None, lambda: get_mexc().get_portfolio_presence(symbols)
            )
        except Exception as exc:
            logger.exception("Portfolio missing-coin scan failed")
            await query.edit_message_text(
                "⚠️ تعذر فحص الرصيد من MEXC.\n"
                "لم يتم اعتبار أي عملة ناقصة ولم يتم تنفيذ أي شراء.\n\n"
                f"الخطأ: `{exc}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 إعادة المحاولة", callback_data=f"missing_{pf_id}")],
                    [InlineKeyboardButton("⬅️ المحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
            return

        missing_symbols = [
            symbol for symbol in symbols
            if not presence.get(symbol, {}).get("present", False)
        ]
        key = _missing_selection_key(pf_id)
        selected = set(context.user_data.get(key, []))
        selected.intersection_update(missing_symbols)
        if not pf.is_running:
            selected.clear()
            context.user_data.pop(key, None)

        if toggle_symbol and pf.is_running:
            toggle_symbol = toggle_symbol.upper()
            if toggle_symbol in missing_symbols:
                if toggle_symbol in selected:
                    selected.remove(toggle_symbol)
                else:
                    selected.add(toggle_symbol)
            context.user_data[key] = sorted(selected)

        if not missing_symbols:
            context.user_data.pop(key, None)
            await query.edit_message_text(
                f"✅ فحص *{pf.name}* مكتمل.\n"
                f"كل العملات المسجلة ({len(symbols)}) موجودة بقيمة سوقية فعلية "
                f"(أكبر من `{config.BALANCE_PRESENCE_MIN_USDT:g}` USDT).\n"
                "تم احتساب العملات الموجودة داخل أوامر البيع المفتوحة، "
                "وتجاهل بقايا البيع الصغيرة.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ المحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
            return

        present_count = len(symbols) - len(missing_symbols)
        lines = [
            f"🔎 *فحص العملات — {pf.name}*",
            "",
            f"المسجلة: `{len(symbols)}` | الموجودة: `{present_count}` | الناقصة: `{len(missing_symbols)}`",
            "",
            f"يتم تجاهل بقايا البيع الأقل من `{config.BALANCE_PRESENCE_MIN_USDT:g}` USDT.",
            (
                "اختر العملات التي تريد إعادة دخولها."
                if pf.is_running
                else "⚠️ المحفظة متوقفة؛ الفحص متاح للعرض فقط، ولن يظهر خيار شراء."
            ),
            "لا يوجد شراء عند الاختيار؛ الشراء لا يبدأ إلا بعد زر التأكيد.",
            "",
            "العملة المحددة: " + (
                ", ".join(f"`{symbol}`" for symbol in sorted(selected))
                if selected else "—"
            ),
        ]
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=_missing_reentry_keyboard(
                pf_id, missing_symbols, selected, allow_selection=pf.is_running
            ),
        )
    finally:
        db.close()


async def _do_missing_reentry(query, context, tid, pf_id):
    """Re-check and buy only the coins explicitly confirmed by the user."""
    import asyncio

    key = _missing_selection_key(pf_id)
    if context.user_data.get(f"{key}_in_progress"):
        await query.edit_message_text("⏳ إعادة الدخول قيد التنفيذ بالفعل.")
        return

    selected = set(context.user_data.get(key, []))
    if not selected:
        await query.edit_message_text(
            "اختر عملة واحدة على الأقل أولاً.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔎 فحص العملات الناقصة", callback_data=f"missing_{pf_id}")],
                [InlineKeyboardButton("⬅️ المحفظة", callback_data=f"view_{pf_id}")],
            ]),
        )
        return

    db = SessionLocal()
    try:
        pf = get_portfolio(db, pf_id, tid)
        if not pf:
            await query.edit_message_text("المحفظة غير موجودة.", reply_markup=main_menu_keyboard())
            return
        if not pf.is_running:
            await query.edit_message_text(
                "⚠️ المحفظة أصبحت متوقفة. لم يتم تنفيذ أي شراء.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ المحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
            return

        coin_map = {coin.symbol.upper(): coin for coin in pf.coins}
        selected = {symbol.upper() for symbol in selected if symbol.upper() in coin_map}
        if not selected:
            context.user_data.pop(key, None)
            await query.edit_message_text(
                "لم تعد هناك عملات صالحة لإعادة الدخول.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ المحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
            return

        # Re-check immediately before placing any order. A balance appearing
        # after the first scan must remove that coin from the buy list.
        loop = asyncio.get_event_loop()
        try:
            presence = await loop.run_in_executor(
                None, lambda: get_mexc().get_portfolio_presence(list(coin_map))
            )
        except Exception as exc:
            logger.exception("Final portfolio missing-coin scan failed")
            await query.edit_message_text(
                "⚠️ تعذر إعادة فحص الرصيد قبل التنفيذ.\n"
                "تم إلغاء العملية بالكامل ولم يتم شراء أي عملة.\n\n"
                f"الخطأ: `{exc}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 إعادة الفحص", callback_data=f"missing_{pf_id}")],
                    [InlineKeyboardButton("⬅️ المحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
            return

        no_longer_missing = {
            symbol for symbol in selected
            if presence.get(symbol, {}).get("present", False)
        }
        selected -= no_longer_missing
        if not selected:
            context.user_data.pop(key, None)
            await query.edit_message_text(
                "ℹ️ العملات المحددة أصبحت موجودة بالفعل في الرصيد.\n"
                "تم إلغاء الشراء.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ المحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
            return

        per_coin_usdt = max(
            5.0,
            float(pf.investment_usdt or 0) / max(1, len(pf.coins)),
        )
        required_usdt = per_coin_usdt * len(selected)
        try:
            free_usdt = await loop.run_in_executor(None, get_mexc().get_free_usdt)
        except Exception as exc:
            logger.exception("Free USDT preflight failed")
            await query.edit_message_text(
                "⚠️ تعذر التحقق من رصيد USDT الحر.\n"
                "تم إلغاء العملية ولم يتم شراء أي عملة.\n\n"
                f"الخطأ: `{exc}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 إعادة الفحص", callback_data=f"missing_{pf_id}")],
                    [InlineKeyboardButton("⬅️ المحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
            return
        if free_usdt < required_usdt:
            await query.edit_message_text(
                f"⚠️ رصيد USDT الحر غير كافٍ.\n"
                f"المتاح: `{free_usdt:.2f}` USDT\n"
                f"المطلوب تقريباً: `{required_usdt:.2f}` USDT\n\n"
                "تم إلغاء العملية ولم يتم شراء أي عملة.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ المحفظة", callback_data=f"view_{pf_id}")],
                ]),
            )
            return

        context.user_data[f"{key}_in_progress"] = True
        selected_symbols = sorted(selected)
        await query.edit_message_text(
            "⏳ تم التأكيد.\n"
            f"سيتم تنفيذ إعادة دخول لـ `{len(selected_symbols)}` عملة فقط:\n"
            + ", ".join(f"`{symbol}`" for symbol in selected_symbols),
            parse_mode="Markdown",
        )

        user = get_or_create_user(db, tid)

        def pct(pf_val, user_val, default):
            if pf_val is not None and float(pf_val) > 0:
                return float(pf_val)
            if user_val is not None and float(user_val) > 0:
                return float(user_val)
            return default

        tp1 = pct(getattr(pf, "tp1_pct", None), getattr(user, "tp1_pct", None), 3.0)
        tp2 = pct(getattr(pf, "tp2_pct", None), getattr(user, "tp2_pct", None), 5.0)
        tp3 = pct(getattr(pf, "tp3_pct", None), getattr(user, "tp3_pct", None), 8.0)
        s1 = pct(getattr(pf, "tp1_sell_pct", None), getattr(user, "tp1_sell_pct", None), 40.0)
        s2 = pct(getattr(pf, "tp2_sell_pct", None), getattr(user, "tp2_sell_pct", None), 30.0)
        sl_pct = pct(getattr(pf, "stop_loss_pct", None), getattr(user, "stop_loss_pct", None), 3.0)

        succeeded = []
        errors = []
        for symbol in selected_symbols:
            coin = coin_map[symbol]
            try:
                result = await loop.run_in_executor(
                    None,
            lambda symbol=symbol: get_reb().reentry_buy_and_place_tp(
                        symbol, per_coin_usdt, tp1, tp2, tp3, sl_pct, s1, s2,
                        control_mode=getattr(pf, "control_mode", "smart"),
                    ),
                )
            except Exception as exc:
                logger.exception("Missing-coin re-entry failed for %s", symbol)
                errors.append(f"`{symbol}`: {exc}")
                continue

            if result.get("error"):
                errors.append(f"`{symbol}`: {result['error']}")
                continue

            update_coin_position(
                db, coin.id,
                entry_price=result.get("entry_price", 0),
                tp1_price=result.get("tp1_price", 0),
                tp2_price=result.get("tp2_price", 0),
                tp3_price=result.get("tp3_price", 0),
                tp_price=result.get("tp1_price", 0),
                current_sl_price=result.get("sl_price", 0),
                original_sl_price=result.get("original_sl_price") or result.get("sl_price", 0),
                amount=result.get("amount", 0),
                remaining_amount=result.get("amount", 0),
                tp1_order_id=result.get("tp1_order_id"),
                tp2_order_id=result.get("tp2_order_id"),
                tp3_order_id=result.get("tp3_order_id"),
                position_status="open",
                reentry_used=False,
                reentry_touched=False,
                reentry_price=0.0,
            )
            mark_reentry_events_used(db, pf.id, symbol)
            log_action(db, tid, "missing_coin_reentry", f"Re-entry for missing {symbol}", True, pf.id)
            success_line = f"`{symbol}` عند `{float(result.get('entry_price') or 0):.6g}`"
            if result.get("tp_warning"):
                success_line += f" (تحذير TP: {result['tp_warning']})"
            succeeded.append(success_line)

        context.user_data.pop(key, None)
        lines = ["🔄 *نتيجة إعادة الدخول*"]
        if succeeded:
            lines.append("\n✅ تم الشراء:")
            lines.extend(f"• {item}" for item in succeeded)
        if errors:
            lines.append("\n⚠️ لم يتم الشراء:")
            lines.extend(f"• {item}" for item in errors)
        lines.append("\nلم يتم تنفيذ أي عملة لم تكن محددة في شاشة التأكيد.")
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=pf_keyboard(pf.id, pf.is_running),
        )
    finally:
        context.user_data.pop(f"{key}_in_progress", None)
        db.close()


async def _do_manual_reentry(query, tid, event_id):
    """Buy a stopped coin again only after the user presses its button."""
    import asyncio

    db = SessionLocal()
    try:
        event = get_trade_event(db, event_id, tid)
        if not event or event.event_type != "stop_loss":
            await query.edit_message_text("عملية إعادة الدخول غير متاحة أو تم تنفيذها مسبقاً.", reply_markup=main_menu_keyboard())
            return
        pf = get_portfolio(db, event.portfolio_id, tid)
        coin = next((c for c in (pf.coins if pf else []) if c.id == event.portfolio_coin_id), None)
        if not pf or not coin:
            await query.edit_message_text("المحفظة أو العملة غير موجودة.", reply_markup=main_menu_keyboard())
            return
        user = get_or_create_user(db, tid)

        def pct(pf_val, user_val, default):
            if pf_val is not None and float(pf_val) > 0:
                return float(pf_val)
            if user_val is not None and float(user_val) > 0:
                return float(user_val)
            return default

        tp1 = pct(getattr(pf, "tp1_pct", None), getattr(user, "tp1_pct", None), 3.0)
        tp2 = pct(getattr(pf, "tp2_pct", None), getattr(user, "tp2_pct", None), 5.0)
        tp3 = pct(getattr(pf, "tp3_pct", None), getattr(user, "tp3_pct", None), 8.0)
        s1 = pct(getattr(pf, "tp1_sell_pct", None), getattr(user, "tp1_sell_pct", None), 40.0)
        s2 = pct(getattr(pf, "tp2_sell_pct", None), getattr(user, "tp2_sell_pct", None), 30.0)
        sl_pct = pct(getattr(pf, "stop_loss_pct", None), getattr(user, "stop_loss_pct", None), 3.0)
        usdt = max(5.0, float(pf.investment_usdt or 0) / max(1, len(pf.coins)))

        await query.edit_message_text(f"⏳ جاري إعادة دخول `{coin.symbol}`...", parse_mode="Markdown")
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: get_reb().reentry_buy_and_place_tp(
                coin.symbol, usdt, tp1, tp2, tp3, sl_pct, s1, s2,
                control_mode=getattr(pf, "control_mode", "smart"),
            ),
        )
        if result.get("error"):
            await query.edit_message_text(
                f"⚠️ فشل إعادة دخول `{coin.symbol}`:\n`{result['error']}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🛑 الاستوبات / إعادة الدخول", callback_data=f"stopped_{pf.id}")],
                    [InlineKeyboardButton("⬅️ المحفظة", callback_data=f"view_{pf.id}")],
                ]),
            )
            return

        update_coin_position(
            db, coin.id,
            entry_price=result.get("entry_price", 0),
            tp1_price=result.get("tp1_price", 0),
            tp2_price=result.get("tp2_price", 0),
            tp3_price=result.get("tp3_price", 0),
            tp_price=result.get("tp1_price", 0),
            current_sl_price=result.get("sl_price", 0),
            original_sl_price=result.get("original_sl_price") or result.get("sl_price", 0),
            amount=result.get("amount", 0),
            remaining_amount=result.get("amount", 0),
            tp1_order_id=result.get("tp1_order_id"),
            tp2_order_id=result.get("tp2_order_id"),
            tp3_order_id=result.get("tp3_order_id"),
            position_status="open",
            reentry_used=False,
            reentry_touched=False,
            reentry_price=0.0,
        )
        event.reentry_available = False
        event.reentry_used = True
        db.commit()
        log_action(db, tid, "manual_reentry", f"Manual re-entry for {coin.symbol}", True, pf.id)
        await query.edit_message_text(
            f"✅ تمت إعادة دخول `{coin.symbol}` في محفظة *{pf.name}*\n"
            f"الدخول `{result.get('entry_price', 0):.6g}` | "
            f"TP1 `{result.get('tp1_price', 0):.6g}` | "
            f"TP2 `{result.get('tp2_price', 0):.6g}` | "
            f"TP3 `{result.get('tp3_price', 0):.6g}`\n"
            f"SL `{result.get('sl_price', 0):.6g}`",
            parse_mode="Markdown",
            reply_markup=pf_keyboard(pf.id, pf.is_running),
        )
    finally:
        db.close()


async def _do_start(query, tid, pf_id):
    import asyncio

    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, tid)
        if not p:
            await query.edit_message_text("غير موجودة.", reply_markup=main_menu_keyboard())
            return
        coins = [c.symbol for c in p.coins]
        if not coins:
            await query.edit_message_text("المحفظة بدون عملات.", reply_markup=pf_keyboard(pf_id, False))
            return
        if p.is_running:
            await query.edit_message_text("المحفظة شغالة مسبقاً.", reply_markup=pf_keyboard(pf_id, True))
            return

        # Check the wallet before buying so restarting a stopped portfolio
        # cannot purchase coins that are already held.
        try:
            loop = asyncio.get_event_loop()
            presence = await loop.run_in_executor(
                None, lambda: get_mexc().get_portfolio_presence(coins)
            )
        except Exception as exc:
            logger.exception("Portfolio start balance preflight failed")
            await query.edit_message_text(
                "⚠️ تعذر فحص أرصدة المحفظة من MEXC.\n"
                "لم يتم تشغيل المحفظة ولم يتم تنفيذ أي شراء.\n\n"
                f"الخطأ: `{exc}`",
                parse_mode="Markdown",
                reply_markup=pf_keyboard(pf_id, False),
            )
            return

        present_coins = [
            coin for coin in coins
            if presence.get(str(coin).upper().strip(), {}).get("present", False)
        ]
        missing_coins = [coin for coin in coins if coin not in present_coins]
        balance_lines = []
        for coin in present_coins:
            info = presence.get(str(coin).upper().strip(), {})
            amount = float(info.get("amount") or 0)
            value = float(info.get("market_value") or 0)
            balance_lines.append(f"`{coin}`: `{amount:.6g}` ≈ `{value:.2f}` USDT")

        user = get_or_create_user(db, tid)
        # Portfolio-specific overrides, else user defaults
        def _pct(pf_val, user_val, default):
            if pf_val is not None and float(pf_val) > 0:
                return float(pf_val)
            if user_val is not None and float(user_val) > 0:
                return float(user_val)
            return default
        tp1 = _pct(getattr(p, "tp1_pct", None), getattr(user, "tp1_pct", None), 3.0)
        tp2 = _pct(getattr(p, "tp2_pct", None), getattr(user, "tp2_pct", None), 5.0)
        tp3 = _pct(getattr(p, "tp3_pct", None), getattr(user, "tp3_pct", None), 8.0)
        s1 = _pct(getattr(p, "tp1_sell_pct", None), getattr(user, "tp1_sell_pct", None), 40.0)
        s2 = _pct(getattr(p, "tp2_sell_pct", None), getattr(user, "tp2_sell_pct", None), 30.0)
        sl_pct = _pct(getattr(p, "stop_loss_pct", None), getattr(user, "stop_loss_pct", None), 3.0)
        if str(getattr(p, "control_mode", "smart") or "smart").lower() == "manual":
            # Keep the resolved user defaults on the portfolio so the monitor
            # uses exactly the same percentages after this session ends.
            p.tp1_pct, p.tp2_pct, p.tp3_pct = tp1, tp2, tp3
            p.tp1_sell_pct, p.tp2_sell_pct, p.stop_loss_pct = s1, s2, sl_pct
            db.flush()

        purchase_result = {"executed": [], "errors": []}
        if missing_coins:
            # Keep the original per-coin allocation when only part of the
            # stopped portfolio is missing; never buy already-held coins.
            per_coin_usdt = float(p.investment_usdt or 0) / max(1, len(coins))
            purchase_total = per_coin_usdt * len(missing_coins)
            await query.edit_message_text(
                "⏳ جاري فحص المحفظة ثم شراء العملات الناقصة فقط...\n"
                f"الموجود: `{len(present_coins)}` | الناقص: `{len(missing_coins)}`"
            )
            purchase_result = await loop.run_in_executor(
                None,
                lambda: get_reb().start_portfolio(
                    coins=missing_coins,
                    total_usdt=purchase_total,
                    method=p.allocation_method or "equal",
                    min_trade_usdt=5.0,
                    dry_run=False,
                ),
            )
            if purchase_result.get("errors") and not purchase_result.get("executed"):
                err = "\n".join(str(e) for e in purchase_result["errors"])
                await query.edit_message_text(
                    f"❌ فشل شراء العملات الناقصة:\n`{err}`",
                    parse_mode="Markdown",
                    reply_markup=pf_keyboard(pf_id, False),
                )
                return
        else:
            await query.edit_message_text("✅ الرصيد موجود. جاري تشغيل المحفظة بدون شراء جديد...")

        import time
        time.sleep(1.5)
        coins_data = []
        for c in p.coins:
            amount = get_mexc().get_free_amount(c.symbol)
            price = get_mexc().get_ticker_price(f"{c.symbol}/USDT")
            coins_data.append({"symbol": c.symbol, "amount": amount, "entry_price": price})

        tp_results = get_reb().place_tp_orders(
            coins_data, tp1, tp2, tp3, sl_pct, s1, s2,
            control_mode=getattr(p, "control_mode", "smart"),
        )

        lines = [
            (
                f"✅ الرصيد موجود وتم تشغيل *{p.name}* بدون شراء جديد."
                if not missing_coins
                else f"✅ تم تشغيل *{p.name}*"
            ),
            f"🎯 TP1 `{tp1}%`({s1}%) | TP2 `{tp2}%`({s2}%) | TP3 `{tp3}%`",
            f"🛡 استوب `{sl_pct}%`",
            "",
        ]
        if balance_lines:
            lines.extend(["💰 *الرصيد الموجود:*", *balance_lines, ""])
        if missing_coins:
            lines.append("🛒 تم شراء العملات الناقصة فقط: " + ", ".join(f"`{x}`" for x in missing_coins))
            lines.append("")
        if purchase_result.get("errors"):
            lines.append("⚠️ ملاحظات الشراء:\n" + "\n".join(f"• {x}" for x in purchase_result["errors"]))
        for r in tp_results:
            coin_obj = next((c for c in p.coins if c.symbol == r["symbol"]), None)
            if not coin_obj:
                continue
            update_coin_position(
                db, coin_obj.id,
                entry_price=r.get("entry_price", 0),
                tp1_price=r.get("tp1_price", 0),
                tp2_price=r.get("tp2_price", 0),
                tp3_price=r.get("tp3_price", 0),
                tp_price=r.get("tp1_price", 0),
                current_sl_price=r.get("sl_price", 0),
                original_sl_price=r.get("original_sl_price") or r.get("sl_price", 0),
                amount=r.get("amount", 0),
                remaining_amount=r.get("amount", 0),
                tp1_order_id=r.get("tp1_order_id"),
                tp2_order_id=r.get("tp2_order_id"),
                tp3_order_id=r.get("tp3_order_id"),
                position_status="open",
                reentry_used=False,
                reentry_touched=False,
                reentry_price=0.0,
            )
            if r.get("error"):
                lines.append(f"⚠️ `{r['symbol']}`: {r['error']}")
            lines.append(
                f"`{r['symbol']}` دخول `{r.get('entry_price',0):.6g}`\n"
                f"  TP1 `{r.get('tp1_price',0):.6g}` | TP2 `{r.get('tp2_price',0):.6g}` | "
                f"TP3 `{r.get('tp3_price',0):.6g}` | SL `{r.get('sl_price',0):.6g}`"
            )

        set_portfolio_running(db, pf_id, True)
        log_action(db, tid, "start", f"Started {p.name} multi-TP", True, pf_id)
        await query.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=pf_keyboard(pf_id, True))
    finally:
        db.close()


async def _do_stop(query, tid, pf_id):
    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, tid)
        if not p:
            await query.edit_message_text("غير موجودة.", reply_markup=main_menu_keyboard())
            return
        coins = [c.symbol for c in p.coins]
        await query.edit_message_text("⏳ جاري الإيقاف (إلغاء أوامر الهدف + بيع)...")

        # Cancel any open TP limit orders on MEXC
        tp_orders = []
        for c in p.coins:
            tp_orders.append({
                "symbol": c.symbol,
                "tp_order_id": getattr(c, "tp_order_id", None),
                "tp1_order_id": getattr(c, "tp1_order_id", None),
                "tp2_order_id": getattr(c, "tp2_order_id", None),
                "tp3_order_id": getattr(c, "tp3_order_id", None),
            })
        get_reb().cancel_tp_orders(tp_orders)

        stop_result = get_reb().stop_portfolio(coins, dry_run=False) if coins else {"executed": []}
        for sold in stop_result.get("executed", []):
            symbol = str(sold.get("symbol", "")).split("/")[0]
            coin = next((c for c in p.coins if c.symbol == symbol), None)
            amount = float(sold.get("amount") or 0)
            usdt = float(sold.get("usdt") or 0)
            exit_price = usdt / amount if amount > 0 else 0.0
            if coin and amount > 0 and float(coin.entry_price or 0) > 0:
                record_trade_event(
                    db, tid, p.id, coin.id, symbol, "manual_stop",
                    coin.entry_price, exit_price, amount,
                    (exit_price - coin.entry_price) * amount,
                    details="Manual portfolio stop",
                )

        from database import reset_coin_positions
        reset_coin_positions(db, pf_id)
        set_portfolio_running(db, pf_id, False)
        log_action(db, tid, "stop", f"Stopped {p.name}", True, pf_id)
        await query.edit_message_text(
            f"⏹ تم إيقاف *{p.name}*\nتم إلغاء أوامر الهدف وبيع العملات.\nالمحفظة محفوظة.",
            parse_mode="Markdown",
            reply_markup=pf_keyboard(pf_id, False),
        )
    finally:
        db.close()



async def _do_remove_coin(query, tid, pf_id, symbol):
    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, tid)
        if not p:
            await query.edit_message_text("غير موجودة.", reply_markup=main_menu_keyboard())
            return
        coin = next((c for c in p.coins if c.symbol.upper() == symbol.upper()), None)
        if not coin:
            await query.edit_message_text(
                f"العملة `{symbol}` غير موجودة في المحفظة.",
                parse_mode="Markdown",
                reply_markup=pf_keyboard(pf_id, p.is_running),
            )
            return

        await query.edit_message_text(
            f"⏳ جاري إلغاء أهداف `{coin.symbol}` وبيع الرصيد المتاح بسعر السوق..."
        )
        cancel_result = get_reb().cancel_tp_orders([{
            "symbol": coin.symbol,
            "tp_order_id": getattr(coin, "tp_order_id", None),
            "tp1_order_id": getattr(coin, "tp1_order_id", None),
            "tp2_order_id": getattr(coin, "tp2_order_id", None),
            "tp3_order_id": getattr(coin, "tp3_order_id", None),
        }])
        if cancel_result.get("errors"):
            error_text = "\n".join(
                str(error.get("error") or error)
                for error in cancel_result["errors"]
            )
            await query.edit_message_text(
                f"❌ تعذر إلغاء كل أهداف `{coin.symbol}`.\n"
                "لم يتم البيع أو حذف العملة من قاعدة البيانات حفاظًا على المركز.\n\n"
                f"`{error_text}`",
                parse_mode="Markdown",
                reply_markup=pf_keyboard(pf_id, p.is_running),
            )
            return
        other_running = db.query(PortfolioCoin).join(Portfolio).filter(
            PortfolioCoin.symbol == coin.symbol,
            PortfolioCoin.portfolio_id != p.id,
            Portfolio.telegram_id == tid,
            Portfolio.status == "active",
            Portfolio.is_running == True,
        ).first()
        tracked_amount = float(
            getattr(coin, "remaining_amount", 0)
            or getattr(coin, "amount", 0)
            or 0
        )
        if other_running and tracked_amount <= 0:
            # This portfolio has no tracked position to sell. Do not sell the
            # shared wallet balance that belongs to another running portfolio.
            stop_result = {"executed": [], "errors": []}
        else:
            stop_result = get_reb().stop_portfolio(
                [coin.symbol],
                dry_run=False,
                amount_overrides={coin.symbol: tracked_amount} if other_running else None,
            )
        errors = stop_result.get("errors") or []
        if errors:
            error_text = "\n".join(str(error) for error in errors)
            await query.edit_message_text(
                f"❌ لم يتم حذف `{coin.symbol}` من قاعدة البيانات لأن البيع لم يكتمل.\n"
                "تمت محاولة إلغاء أهدافها، لكن يجب معالجة خطأ البيع أولًا.\n\n"
                f"`{error_text}`",
                parse_mode="Markdown",
                reply_markup=pf_keyboard(pf_id, p.is_running),
            )
            return

        if not remove_coin_from_portfolio(db, pf_id, coin.symbol):
            await query.edit_message_text(
                "تعذر حذف سجل العملة من قاعدة البيانات بعد نجاح البيع.",
                reply_markup=pf_keyboard(pf_id, p.is_running),
            )
            return
        cancelled = len(cancel_result.get("cancelled", []))
        sold = sum(float(item.get("usdt") or 0) for item in stop_result.get("executed", []))
        await query.edit_message_text(
            f"✅ تم حذف `{coin.symbol}` من المحفظة.\n"
            f"أوامر الأهداف الملغاة: `{cancelled}`\n"
            f"البيع بسعر السوق: `{sold:.2f}` USDT\n"
            "تم حذف وقف الخسارة السحابي مع بيانات العملة.",
            parse_mode="Markdown",
            reply_markup=pf_keyboard(pf_id, p.is_running),
        )
    finally:
        db.close()


async def _do_close(query, tid, pf_id):
    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, tid)
        if not p:
            await query.edit_message_text("غير موجودة.", reply_markup=main_menu_keyboard())
            return
        coins = [c.symbol for c in p.coins]
        if p.is_running and coins:
            tp_orders = [{
                "symbol": c.symbol,
                "tp_order_id": getattr(c, "tp_order_id", None),
                "tp1_order_id": getattr(c, "tp1_order_id", None),
                "tp2_order_id": getattr(c, "tp2_order_id", None),
                "tp3_order_id": getattr(c, "tp3_order_id", None),
            } for c in p.coins]
            get_reb().cancel_tp_orders(tp_orders)
            get_reb().stop_portfolio(coins, dry_run=False)
        close_portfolio(db, pf_id)
        await query.edit_message_text("✅ تم حذف المحفظة.", reply_markup=main_menu_keyboard())
    finally:
        db.close()


async def create_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["create"]["name"] = update.message.text.strip()
    await update.message.reply_text("أرسل *مبلغ الاستثمار* بالـ USDT:", parse_mode="Markdown")
    return CREATE_AMOUNT


async def create_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip().replace(",", ""))
        if amount < 5:
            await update.message.reply_text("المبلغ لازم يكون ≥ 5 USDT")
            return CREATE_AMOUNT
        context.user_data["create"]["amount"] = amount
    except ValueError:
        await update.message.reply_text("أدخل رقم صحيح.")
        return CREATE_AMOUNT
    await update.message.reply_text("أرسل رموز العملات مفصولة بمسافة\nمثال: `BTC ETH SOL`", parse_mode="Markdown")
    return CREATE_COINS


async def create_coins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().upper().replace(",", " ")
    coins = [c.strip() for c in raw.split() if c.strip()]
    if not coins:
        await update.message.reply_text("أدخل عملة واحدة على الأقل.")
        return CREATE_COINS
    if len(coins) > 30:
        await update.message.reply_text("الحد الأقصى 30 عملة.")
        return CREATE_COINS
    data = context.user_data["create"]
    db = SessionLocal()
    try:
        p = create_portfolio(db, update.effective_user.id, data["name"], data["amount"], coins)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ تم إنشاء *{p.name}* (#{p.id})\nالمبلغ: `{p.investment_usdt}`\nالعملات: `{', '.join(coins)}`",
            parse_mode="Markdown", reply_markup=main_menu_keyboard())
    finally:
        db.close()
    return ConversationHandler.END


async def add_coin_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = update.message.text.strip().upper()
    pf_id = context.user_data.get("addcoin_pf")
    if not pf_id:
        context.user_data.clear()
        await update.message.reply_text("حدث خطأ.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END
    db = SessionLocal()
    try:
        user = get_or_create_user(db, update.effective_user.id)
        ok, msg = add_coin_to_portfolio(db, pf_id, symbol, max_coins=user.max_coins_per_portfolio or 30)
        context.user_data.clear()
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_menu_keyboard())
    finally:
        db.close()
    return ConversationHandler.END


async def increase_amount_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip().replace(",", ""))
        if amount <= 0:
            await update.message.reply_text("المبلغ لازم يكون موجب.")
            return INCREASE_AMOUNT
    except ValueError:
        await update.message.reply_text("أدخل رقم صحيح.")
        return INCREASE_AMOUNT
    pf_id = context.user_data.get("increase_pf")
    db = SessionLocal()
    try:
        p = get_portfolio(db, pf_id, update.effective_user.id)
        if not p:
            await update.message.reply_text("غير موجودة.", reply_markup=main_menu_keyboard())
            context.user_data.clear()
            return ConversationHandler.END
        p.investment_usdt += amount
        db.commit()
        if p.is_running:
            coins = [c.symbol for c in p.coins]
            if coins:
                get_reb().start_portfolio(coins=coins, total_usdt=amount,
                                          method=p.allocation_method or "equal", min_trade_usdt=5.0, dry_run=False)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ تم زيادة `{amount}` USDT\nالمخصص الجديد: `{p.investment_usdt:.2f}`",
            parse_mode="Markdown", reply_markup=main_menu_keyboard())
    finally:
        db.close()
    return ConversationHandler.END


async def _auto_system_start(query, tid: int):
    """تفعيل الإدارة الذكية على محافظ المستخدم بعد إنشائها يدوياً."""
    db = SessionLocal()
    try:
        set_system_enabled(tid, True)
        pfs = get_portfolios(db, tid, status="active")
        if not pfs:
            await query.edit_message_text(
                "📭 *لا توجد محافظ بعد*\n\n"
                "1) أنشئ محفظة من *➕ محفظة جديدة*\n"
                "2) اختر العملات والمبلغ\n"
                "3) اضغط *▶️ تشغيل النظام*\n\n"
                "بعدها البوت يتولى الأهداف / البامب / الدفاع.",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(),
            )
            return

        started = []
        for p in pfs:
            if not p.is_running:
                set_portfolio_running(db, p.id, True)
                started.append(p.name)
            else:
                started.append(f"{p.name} (كانت شغالة)")

        reg = detect_market_regime(get_mexc())
        lines_msg = [
            "✅ *تم تشغيل الإدارة الذكية*",
            "━━━━━━━━━━━━━━━━━━━━",
            f"المحافظ تحت الإدارة: *{len(pfs)}*",
        ]
        lines_msg += [f"• `{n}`" for n in started]
        lines_msg += [
            "",
            reg.message,
            "",
            "البوت الآن على محافظك:",
            "• أهداف ذكية + وضع البامب",
            "• Trailing ومتابعة الصعود",
            "• حسّ السوق من BTC",
            "• حماية أرباح عند الضعف",
            "• خروج دفاعي عند الانهيار",
            "",
            "لو لسه ما اشتريتش: *♻️ إعادة بناء المراكز* داخل كل محفظة.",
        ]
        await query.edit_message_text(
            "\n".join(lines_msg),
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
    except Exception as e:
        logger.exception("auto_sys_start")
        await query.edit_message_text(
            f"❌ فشل تشغيل النظام:\n`{e}`",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
    finally:
        db.close()


async def _auto_system_stop(query, tid: int):
    db = SessionLocal()
    try:
        set_system_enabled(tid, False)
        msg = (
            "⏹ *تم إيقاف الإدارة الذكية*\n"
            "• حسّ السوق / الخروج الدفاعي: متوقف\n"
            "• المراكز المفتوحة تفضل تحت مراقبة الأهداف والاستوب\n"
            "• تقدر توقف أي محفظة يدوياً من داخلها"
        )
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=main_menu_keyboard())
    except Exception as e:
        await query.edit_message_text(f"خطأ: `{e}`", parse_mode="Markdown", reply_markup=main_menu_keyboard())
    finally:
        db.close()


# ---- Smart levels soft refresh (low API pressure) ----
# coin_id -> last refresh unix time
_smart_refresh_ts: dict = {}
SMART_REFRESH_EVERY_SEC = 3 * 3600   # كل عملة مرة كل 3 ساعات كحد أقصى
SMART_REFRESH_MAX_PER_RUN = 4        # أقصى 4 عملات في الدورة
SMART_REFRESH_MIN_CHANGE_PCT = 0.8   # حدّث الأوامر فقط لو التغير أكبر من 0.8%


async def market_sense_job(context: ContextTypes.DEFAULT_TYPE):
    """
    يحس بالسوق كل دقيقتين:
    - weak: يمنع دخول + يرفع استوب الصفقات الرابحة لـ break-even
    - black/crash: خروج دفاعي (بيع المراكز في المحافظ التلقائية)
    - حد خسارة يومي من قمة اليوم
    """
    import asyncio
    db = SessionLocal()
    try:
        client = get_mexc()
        reg = detect_market_regime(client)
        positions = get_open_positions(db)
        if not positions and reg.defense_level == 0:
            return

        # تجميع حسب المستخدم
        by_user: Dict[int, list] = {}
        for coin in positions:
            pf = coin.portfolio
            if not pf or not pf.is_running:
                continue
            # المحافظ اليدوية لا تدخل في قرارات الدفاع أو الخروج الذكي.
            if str(getattr(pf, "control_mode", "smart") or "smart").lower() == "manual":
                continue
            by_user.setdefault(int(pf.telegram_id), []).append(coin)

        # مستخدمون مفعّل عندهم النظام حتى بدون مراكز (لتنبيه فقط)
        # نمر على by_user + أي tid شغال من الذاكرة عبر المراكز فقط لتبسيط

        for tid, coins in by_user.items():
            # الإدارة لكل محفظة شغالة (is_running) بدون نظام عام

            # ربح/خسارة حقيقية من سعر الدخول — مش قيمة الباقي بعد البيع الجزئي
            symbols = list({c.symbol for c in coins})
            prices = {}
            try:
                prices = client.get_all_prices(symbols) or {}
            except Exception:
                prices = {}

            cost_usdt = 0.0   # تكلفة الدخول للكمية المتبقية
            value_usdt = 0.0  # القيمة السوقية الحالية
            for c in coins:
                px = float(prices.get(c.symbol) or prices.get(f"{c.symbol}/USDT") or 0)
                if px <= 0:
                    try:
                        px = float(client.get_ticker_price(f"{c.symbol}/{client.quote}") or 0)
                    except Exception:
                        px = 0
                amt = float(c.remaining_amount or c.amount or 0)
                entry = float(c.entry_price or 0)
                if amt <= 0:
                    continue
                if entry > 0:
                    cost_usdt += entry * amt
                if px > 0:
                    value_usdt += px * amt
                elif entry > 0:
                    value_usdt += entry * amt  # fallback

            pnl_usdt = value_usdt - cost_usdt
            pnl_pct = ((value_usdt / cost_usdt) - 1.0) * 100.0 if cost_usdt > 0 else 0.0

            loss_hit, loss_msg = daily_loss_triggered(tid, pnl_pct, pnl_usdt, cost_usdt)
            defense = reg.defense_level
            # الخسارة اليومية وحدها ما تحولش لخروج طارئ لو السوق متوازن
            # (منع البيع الجماعي بسبب بيع أهداف ناجحة أو تذبذب عادي)
            if loss_hit and reg.defense_level >= 1:
                defense = max(defense, 2)
            elif loss_hit and reg.defense_level == 0:
                # تنبيه فقط — منغير تصفية
                defense = 0

            if defense <= 0:
                continue
            if not should_alert_defense(tid, 600):
                # حتى لو مننبعتش رسالة، في الانهيار ننفذ الخروج
                if defense < 2:
                    continue

            reb = get_reb()
            loop = asyncio.get_event_loop()
            protected = 0
            exited = 0

            if defense == 1:
                # حماية: الاستوب → الدخول للصفقات الرابحة
                for c in coins:
                    entry = float(c.entry_price or 0)
                    px = float(prices.get(c.symbol) or 0)
                    if entry <= 0 or px <= 0:
                        continue
                    if px > entry * 1.005:
                        cur_sl = float(c.current_sl_price or 0)
                        if cur_sl < entry:
                            update_coin_position(db, c.id, current_sl_price=entry)
                            protected += 1
                if should_alert_defense(tid, 1):  # already gated above mostly
                    msg = (
                        f"🛡️ *دفاع السوق*\n{reg.message}\n"
                        f"{loss_msg}\n"
                        f"تم رفع استوب *{protected}* صفقة رابحة لسعر الدخول.\n"
                        f"دخول جديد: ❌ متوقف"
                    )
                    try:
                        await context.bot.send_message(tid, msg, parse_mode="Markdown")
                    except Exception:
                        pass

            if defense >= 2:
                # خروج طارئ من المحافظ التلقائية
                for c in coins:
                    symbol = c.symbol
                    try:
                        await loop.run_in_executor(
                            None,
                            lambda s=symbol, coin=c: reb.cancel_tp_orders([{
                                "symbol": s,
                                "tp_order_id": getattr(coin, "tp_order_id", None),
                                "tp1_order_id": getattr(coin, "tp1_order_id", None),
                                "tp2_order_id": getattr(coin, "tp2_order_id", None),
                                "tp3_order_id": getattr(coin, "tp3_order_id", None),
                            }]),
                        )
                        amount = await loop.run_in_executor(
                            None, lambda s=symbol: client.get_free_amount(s) * 0.998
                        )
                        if amount > 0:
                            await loop.run_in_executor(
                                None,
                                lambda s=symbol, a=amount: client.create_market_order(
                                    f"{s}/{client.quote}", "sell", a
                                ),
                            )
                        entry = float(c.entry_price or 0)
                        px = float(prices.get(symbol) or entry)
                        amt = float(c.remaining_amount or c.amount or 0)
                        pnl = (px - entry) * amt if entry else 0
                        record_trade_event(
                            db, tid, c.portfolio_id, c.id, symbol, "defense_exit",
                            entry, px, amt, pnl, details=reg.message,
                        )
                        update_coin_position(
                            db, c.id,
                            position_status="closed",
                            current_sl_price=0.0,
                            remaining_amount=0.0,
                            amount=0.0,
                            tp1_order_id=None, tp2_order_id=None, tp3_order_id=None,
                            tp_order_id=None,
                        )
                        exited += 1
                    except Exception:
                        logger.exception("defense exit %s", symbol)
                msg = (
                    f"🔴 *خروج دفاعي — انهيار/سواد*\n{reg.message}\n"
                    f"{loss_msg}\n"
                    f"تم إغلاق *{exited}* مركز في المحافظ التلقائية.\n"
                    f"الدخول الجديد متوقف حتى يتحسن السوق."
                )
                try:
                    await context.bot.send_message(tid, msg, parse_mode="Markdown")
                except Exception:
                    pass
    except Exception:
        logger.exception("market_sense_job error")
    finally:
        db.close()


async def smart_levels_refresh_job(context: ContextTypes.DEFAULT_TYPE):
    """
    تحديث خفيف للأهداف الذكية بدون ضغط API:
    - كل دورة تعالج عدد صغير من العملات فقط
    - كل عملة تتحدث مرة كل عدة ساعات
    - OHLCV يُجلب فقط للعملات المختارة
    - أوامر المنصة تتغير فقط لو الفرق معنوي
    """
    import asyncio
    import time as _time

    db = SessionLocal()
    try:
        positions = get_open_positions(db)
        openish = [
            c for c in positions
            if (c.position_status or "") in ("open", "tp1_hit", "tp2_hit")
            and str(getattr(getattr(c, "portfolio", None), "control_mode", "smart") or "smart").lower() != "manual"
        ]
        if not openish:
            return

        now = _time.time()
        due = []
        for c in openish:
            last = float(_smart_refresh_ts.get(c.id) or 0)
            if now - last >= SMART_REFRESH_EVERY_SEC:
                due.append(c)
        # الأقدم أولاً
        due.sort(key=lambda c: float(_smart_refresh_ts.get(c.id) or 0))
        batch = due[:SMART_REFRESH_MAX_PER_RUN]
        if not batch:
            return

        reb = get_reb()
        loop = asyncio.get_event_loop()

        for coin in batch:
            try:
                pf = coin.portfolio
                if not pf or not pf.is_running:
                    _smart_refresh_ts[coin.id] = now
                    continue
                tid = pf.telegram_id
                user = get_or_create_user(db, tid)

                def pct(pf_val, user_val, default):
                    if pf_val is not None and float(pf_val) > 0:
                        return float(pf_val)
                    if user_val is not None and float(user_val) > 0:
                        return float(user_val)
                    return default

                fb1 = pct(getattr(pf, "tp1_pct", None), getattr(user, "tp1_pct", None), 3.0)
                fb2 = pct(getattr(pf, "tp2_pct", None), getattr(user, "tp2_pct", None), 5.0)
                fb3 = pct(getattr(pf, "tp3_pct", None), getattr(user, "tp3_pct", None), 8.0)
                fb_sl = pct(getattr(pf, "stop_loss_pct", None), getattr(user, "stop_loss_pct", None), 3.0)
                s1 = pct(getattr(pf, "tp1_sell_pct", None), getattr(user, "tp1_sell_pct", None), 40.0)
                s2 = pct(getattr(pf, "tp2_sell_pct", None), getattr(user, "tp2_sell_pct", None), 30.0)

                entry = float(coin.entry_price or 0)
                amount = float(coin.remaining_amount or coin.amount or 0)
                if entry <= 0 or amount <= 0:
                    _smart_refresh_ts[coin.id] = now
                    continue

                levels = await loop.run_in_executor(
                    None,
                    lambda: reb.build_smart_levels_for_entry(
                        coin.symbol, entry, fb1, fb2, fb3, fb_sl, "1h",
                    ),
                )

                old_tp1 = float(coin.tp1_price or 0)
                new_tp1 = float(levels.tp1_price or 0)
                # لو التغير صغير → متلمسش أوامر المنصة
                changed = True
                if old_tp1 > 0 and new_tp1 > 0:
                    rel = abs(new_tp1 - old_tp1) / old_tp1 * 100.0
                    if rel < SMART_REFRESH_MIN_CHANGE_PCT:
                        changed = False

                status = coin.position_status or "open"
                if not changed:
                    _smart_refresh_ts[coin.id] = now
                    continue

                # إلغاء وإعادة وضع للأهداف المتبقية فقط
                await loop.run_in_executor(
                    None,
                    lambda: reb.cancel_tp_orders([{
                        "symbol": coin.symbol,
                        "tp_order_id": getattr(coin, "tp_order_id", None),
                        "tp1_order_id": getattr(coin, "tp1_order_id", None),
                        "tp2_order_id": getattr(coin, "tp2_order_id", None),
                        "tp3_order_id": getattr(coin, "tp3_order_id", None),
                    }]),
                )
                skipped = []
                if status in ("tp1_hit", "tp2_hit", "tp3_hit"):
                    skipped.append("tp1")
                if status in ("tp2_hit", "tp3_hit"):
                    skipped.append("tp2")
                if status == "tp3_hit":
                    skipped.append("tp3")

                result = await loop.run_in_executor(
                    None,
                    lambda: reb.place_tp_orders(
                        [{"symbol": coin.symbol, "amount": amount, "entry_price": entry}],
                        levels.tp1_pct, levels.tp2_pct, levels.tp3_pct, levels.stop_loss_pct,
                        s1, s2, skip_stages=skipped,
                    )[0],
                )
                if result.get("error"):
                    logger.warning("smart refresh %s: %s", coin.symbol, result["error"])
                    _smart_refresh_ts[coin.id] = now
                    continue

                # سياسة الاستوب حسب المرحلة (متغيرش حماية الربح)
                if status == "open":
                    new_sl = result.get("sl_price", coin.current_sl_price)
                elif status == "tp1_hit":
                    new_sl = entry
                elif status == "tp2_hit":
                    new_sl = float(result.get("tp1_price") or coin.tp1_price or entry)
                    if new_sl < entry:
                        new_sl = entry
                else:
                    new_sl = coin.current_sl_price

                update_coin_position(
                    db, coin.id,
                    tp1_price=result.get("tp1_price", coin.tp1_price),
                    tp2_price=result.get("tp2_price", coin.tp2_price),
                    tp3_price=result.get("tp3_price", coin.tp3_price),
                    current_sl_price=new_sl,
                    tp1_order_id=result.get("tp1_order_id") if "tp1" not in skipped else None,
                    tp2_order_id=result.get("tp2_order_id") if "tp2" not in skipped else None,
                    tp3_order_id=result.get("tp3_order_id") if "tp3" not in skipped else None,
                )
                _smart_refresh_ts[coin.id] = now
                logger.info(
                    "smart refresh %s: TP1=%.2f%% TP2=%.2f%% TP3=%.2f%% (%s)",
                    coin.symbol, levels.tp1_pct, levels.tp2_pct, levels.tp3_pct, levels.reason,
                )
                await asyncio.sleep(0.4)
            except Exception:
                logger.exception("smart_levels_refresh_job coin=%s", getattr(coin, "symbol", "?"))
                _smart_refresh_ts[coin.id] = now
    except Exception:
        logger.exception("smart_levels_refresh_job error")
    finally:
        db.close()


async def monitor_positions_job(context: ContextTypes.DEFAULT_TYPE):
    """Background job: check open positions for TP fill / SL hit / re-entry."""
    import asyncio
    db = SessionLocal()
    try:
        positions = get_open_positions(db)
        if not positions:
            return
        # Run sync CCXT work off the event loop so Telegram stays responsive
        loop = asyncio.get_event_loop()
        actions = await loop.run_in_executor(
            None, lambda: get_reb().check_and_manage_positions(positions)
        )
        trail_batches = {}  # tid -> list of meaningful trail updates
        for act in actions:
            symbol = act["symbol"]
            coin_id = act.get("coin_id")
            if coin_id is None:
                # Compatibility with action payloads produced by older code.
                coin = next((c for c in positions if c.symbol == symbol), None)
            else:
                # A symbol can exist in multiple portfolios. Always reload the
                # exact row that produced the action, and skip it if a user
                # removed it while this monitor cycle was running.
                coin = db.query(PortfolioCoin).filter(
                    PortfolioCoin.id == coin_id
                ).first()
            if not coin:
                continue
            pf = coin.portfolio
            tid = pf.telegram_id if pf else config.ADMIN_TELEGRAM_ID

            if act["action"] == "tp_full_close":
                remaining_before = float(coin.remaining_amount or coin.amount or 0)
                filled_amount = float(act.get("filled_amount") or remaining_before)
                filled_amount = min(filled_amount, remaining_before) if remaining_before > 0 else filled_amount
                fill_price = float(act.get("fill_price") or act.get("price") or 0)
                stage = act.get("stage") or "tp"
                pnl = (fill_price - float(coin.entry_price or 0)) * filled_amount if fill_price and coin.entry_price else 0
                record_trade_event(
                    db, tid, pf.id, coin.id, symbol, stage if stage != "balance_zero" else "tp_full",
                    coin.entry_price, fill_price, filled_amount, pnl,
                    details=f"Full close ({stage})",
                )
                update_coin_position(
                    db, coin.id,
                    position_status="closed",
                    current_sl_price=0.0,
                    remaining_amount=0.0,
                    amount=0.0,
                    tp1_order_id=None,
                    tp2_order_id=None,
                    tp3_order_id=None,
                    tp_order_id=None,
                )
                msg = (
                    f"✅ *إغلاق كامل* — `{symbol}`\n"
                    f"السعر: `{act['price']:.6g}`\n"
                    f"تم بيع الكمية كلها (هدف واحد / رصيد صفر)\n"
                    f"المحفظة: *{pf.name if pf else '—'}*"
                )
                try:
                    await context.bot.send_message(tid, msg, parse_mode="Markdown")
                except Exception:
                    pass

            elif act["action"] == "tp1_hit":
                remaining_before = float(coin.remaining_amount or coin.amount or 0)
                filled_amount = float(act.get("filled_amount") or 0)
                if filled_amount <= 0:
                    filled_amount = remaining_before * 0.40
                filled_amount = min(filled_amount, remaining_before)
                # لو بعد الخصم مفيش باقي → إغلاق كامل
                new_rem = max(0.0, remaining_before - filled_amount)
                fill_price = float(act.get("fill_price") or act.get("price") or coin.tp1_price or 0)
                pnl = (fill_price - float(coin.entry_price or 0)) * filled_amount
                if new_rem <= 0 or new_rem < remaining_before * 0.05:
                    record_trade_event(
                        db, tid, pf.id, coin.id, symbol, "tp1",
                        coin.entry_price, fill_price, filled_amount, pnl,
                        details="TP1 full close",
                    )
                    update_coin_position(
                        db, coin.id,
                        position_status="closed",
                        current_sl_price=0.0,
                        remaining_amount=0.0,
                        amount=0.0,
                        tp1_order_id=None,
                        tp2_order_id=None,
                        tp3_order_id=None,
                        tp_order_id=None,
                    )
                    msg = (
                        f"✅ *إغلاق كامل (هدف 1)* — `{symbol}`\n"
                        f"السعر: `{act['price']:.6g}`\n"
                        f"المحفظة: *{pf.name if pf else '—'}*"
                    )
                else:
                    record_trade_event(
                        db, tid, pf.id, coin.id, symbol, "tp1",
                        coin.entry_price, fill_price, filled_amount, pnl,
                        details="TP1 filled",
                    )
                    update_coin_position(
                        db, coin.id,
                        position_status="tp1_hit",
                        current_sl_price=(
                            coin.current_sl_price
                            if str(getattr(getattr(coin, "portfolio", None), "control_mode", "smart") or "smart").lower() == "manual"
                            else coin.entry_price
                        ),
                        remaining_amount=new_rem,
                        tp1_order_id=None,
                    )
                    manual_mode = str(getattr(getattr(coin, "portfolio", None), "control_mode", "smart") or "smart").lower() == "manual"
                    tp1_protection = "الاستوب اليدوي ظل ثابتاً" if manual_mode else f"تم نقل الاستوب إلى سعر الدخول `{coin.entry_price:.6g}`"
                    msg = (
                        f"🎯 *تحقق الهدف 1* — `{symbol}`\n"
                        f"السعر: `{act['price']:.6g}`\n"
                        f"{tp1_protection}\n"
                        f"المحفظة: *{pf.name if pf else '—'}*"
                    )
                try:
                    await context.bot.send_message(tid, msg, parse_mode="Markdown")
                except Exception:
                    pass

            elif act["action"] == "tp2_hit":
                remaining_before = float(coin.remaining_amount or coin.amount or 0)
                filled_amount = float(act.get("filled_amount") or 0)
                if filled_amount <= 0:
                    filled_amount = remaining_before * 0.50
                filled_amount = min(filled_amount, remaining_before)
                new_rem = max(0.0, remaining_before - filled_amount)
                fill_price = float(act.get("fill_price") or act.get("price") or coin.tp2_price or 0)
                pnl = (fill_price - float(coin.entry_price or 0)) * filled_amount
                if new_rem <= 0 or new_rem < remaining_before * 0.05:
                    record_trade_event(
                        db, tid, pf.id, coin.id, symbol, "tp2",
                        coin.entry_price, fill_price, filled_amount, pnl,
                        details="TP2 full close",
                    )
                    update_coin_position(
                        db, coin.id,
                        position_status="closed",
                        current_sl_price=0.0,
                        remaining_amount=0.0,
                        amount=0.0,
                        tp1_order_id=None,
                        tp2_order_id=None,
                        tp3_order_id=None,
                        tp_order_id=None,
                    )
                    msg = (
                        f"✅ *إغلاق كامل (هدف 2)* — `{symbol}`\n"
                        f"السعر: `{act['price']:.6g}`\n"
                        f"المحفظة: *{pf.name if pf else '—'}*"
                    )
                else:
                    record_trade_event(
                        db, tid, pf.id, coin.id, symbol, "tp2",
                        coin.entry_price, fill_price, filled_amount, pnl,
                        details="TP2 filled",
                    )
                    update_coin_position(
                        db, coin.id,
                        position_status="tp2_hit",
                        current_sl_price=(
                            coin.current_sl_price
                            if str(getattr(getattr(coin, "portfolio", None), "control_mode", "smart") or "smart").lower() == "manual"
                            else act["new_sl"]
                        ),
                        remaining_amount=new_rem,
                        tp2_order_id=None,
                    )
                    manual_mode = str(getattr(getattr(coin, "portfolio", None), "control_mode", "smart") or "smart").lower() == "manual"
                    tp2_protection = "الاستوب اليدوي ظل ثابتاً" if manual_mode else f"تم رفع الاستوب لحماية الربح إلى `{act['new_sl']:.6g}`"
                    tp2_mode = "(الجزء المتبقي يعمل بالأهداف اليدوية)" if manual_mode else "(الجزء المتبقي يعمل بـ Trailing Stop)"
                    msg = (
                        f"🎯 *تحقق الهدف 2* — `{symbol}`\n"
                        f"السعر: `{act['price']:.6g}`\n"
                        f"{tp2_protection}\n"
                        f"{tp2_mode}\n"
                        f"المحفظة: *{pf.name if pf else '—'}*"
                    )
                try:
                    await context.bot.send_message(tid, msg, parse_mode="Markdown")
                except Exception:
                    pass

            elif act["action"] == "pump_mode":
                # لا تتابع لو المركز اتقفل أو مفيش كمية
                if (coin.position_status or "") == "closed" or float(coin.remaining_amount or coin.amount or 0) <= 0:
                    continue
                new_sl = float(act.get("new_sl") or 0)
                gain = float(act.get("gain_pct") or 0)
                trail_pct = float(act.get("trail_pct") or 3.5)
                kwargs = {
                    "current_sl_price": new_sl if new_sl > 0 else coin.current_sl_price,
                    "tp2_order_id": None,
                    "tp3_order_id": None,
                    "tp_order_id": None,
                }
                # من open: نلغي TP1 الثابت كمان ونحوّل لوضع runner
                if (coin.position_status or "") == "open":
                    kwargs["tp1_order_id"] = None
                    kwargs["position_status"] = "tp2_hit"  # runner + trailing
                elif (coin.position_status or "") == "tp1_hit":
                    kwargs["position_status"] = "tp2_hit"
                update_coin_position(db, coin.id, **kwargs)
                msg = (
                    f"🚀 *وضع بامب* — `{symbol}`\n"
                    f"الربح الحالي: `+{gain:.1f}%`\n"
                    f"تم إلغاء الأهداف الثابتة المتبقية\n"
                    f"الاعتماد على Trailing `{trail_pct:.1f}%` عشان نركب الموجة\n"
                    f"الاستوب: `{new_sl:.6g}`\n"
                    f"المحفظة: *{pf.name if pf else '—'}*"
                )
                try:
                    await context.bot.send_message(tid, msg, parse_mode="Markdown")
                except Exception:
                    pass

            elif act["action"] == "trail_update":
                new_sl = float(act.get("new_sl") or 0)
                if new_sl <= 0:
                    continue
                old_sl = float(coin.current_sl_price or 0)
                if new_sl <= old_sl:
                    continue
                update_coin_position(db, coin.id, current_sl_price=new_sl)
                # إشعار فقط عند رفع معتبر (≥0.4%) أو قفل رأس المال
                move_pct = ((new_sl / old_sl) - 1.0) * 100.0 if old_sl > 0 else 100.0
                gain = float(act.get("gain_pct") or 0)
                entry = float(coin.entry_price or 0)
                be_lock = gain >= 4.0 and entry > 0 and old_sl < entry * 0.999
                if move_pct < 0.4 and not be_lock:
                    continue
                trail_batches.setdefault(tid, []).append({
                    "symbol": symbol,
                    "old_sl": old_sl,
                    "new_sl": new_sl,
                    "price": act.get("price"),
                    "gain": gain,
                    "pf": pf.name if pf else "—",
                    "be_lock": be_lock,
                })
            elif act["action"] == "tp3_hit":
                remaining_before = float(coin.remaining_amount or coin.amount or 0)
                filled_amount = float(act.get("filled_amount") or remaining_before)
                filled_amount = min(filled_amount, remaining_before)
                fill_price = float(act.get("fill_price") or act.get("price") or coin.tp3_price or 0)
                pnl = (fill_price - float(coin.entry_price or 0)) * filled_amount
                record_trade_event(
                    db, tid, pf.id, coin.id, symbol, "tp3",
                    coin.entry_price, fill_price, filled_amount, pnl,
                    details="TP3 filled",
                )
                update_coin_position(
                    db, coin.id,
                    position_status="closed",
                    current_sl_price=0.0,
                    remaining_amount=max(0.0, remaining_before - filled_amount),
                    amount=max(0.0, remaining_before - filled_amount),
                    tp3_order_id=None,
                )
                msg = (
                    f"🎯 *تحقق الهدف 3 (الأخير)* — `{symbol}`\n"
                    f"السعر: `{act['price']:.6g}`\n"
                    f"المحفظة: *{pf.name if pf else '—'}*"
                )
                try:
                    await context.bot.send_message(tid, msg, parse_mode="Markdown")
                except Exception:
                    pass

            elif act["action"] == "sl_hit_wait_reentry":
                update_coin_position(
                    db, coin.id,
                    position_status="waiting_reentry",
                    current_sl_price=0.0,
                    tp1_order_id=None,
                    tp2_order_id=None,
                    tp3_order_id=None,
                    amount=0.0,
                    reentry_price=act["reentry_price"],
                    reentry_touched=False,
                    reentry_used=False,
                )
                msg = (
                    f"🛡 *ضرب الاستوب{' المرفوع' if act.get('was_raised') else ''}* — `{symbol}`\n"
                    f"تم البيع ≈ `{act['price']:.6g}`\n"
                    f"⏳ انتظار إعادة دخول عند الاستوب الأصلي `{act['reentry_price']:.6g}`\n"
                    f"(لمس + ارتداد 1%)\n"
                    f"المحفظة: *{pf.name if pf else '—'}*"
                )
                try:
                    await context.bot.send_message(tid, msg, parse_mode="Markdown")
                except Exception:
                    pass

            elif act["action"] == "reentry_touched":
                update_coin_position(db, coin.id, reentry_touched=True)
                msg = (
                    f"📍 *لمس منطقة إعادة الدخول* — `{symbol}`\n"
                    f"السعر `{act['price']:.6g}` ≤ `{act['reentry_price']:.6g}`\n"
                    f"في انتظار ارتداد +1% للشراء..."
                )
                try:
                    await context.bot.send_message(tid, msg, parse_mode="Markdown")
                except Exception:
                    pass

            elif act["action"] == "reentry_buy":
                # buy again with equal share of portfolio investment
                user = get_or_create_user(db, tid)
                def _pct(pf_val, user_val, default):
                    if pf_val is not None and float(pf_val) > 0:
                        return float(pf_val)
                    if user_val is not None and float(user_val) > 0:
                        return float(user_val)
                    return default
                tp1 = _pct(getattr(pf, "tp1_pct", None), getattr(user, "tp1_pct", None), 3.0)
                tp2 = _pct(getattr(pf, "tp2_pct", None), getattr(user, "tp2_pct", None), 5.0)
                tp3 = _pct(getattr(pf, "tp3_pct", None), getattr(user, "tp3_pct", None), 8.0)
                s1 = _pct(getattr(pf, "tp1_sell_pct", None), getattr(user, "tp1_sell_pct", None), 40.0)
                s2 = _pct(getattr(pf, "tp2_sell_pct", None), getattr(user, "tp2_sell_pct", None), 30.0)
                sl_pct = _pct(getattr(pf, "stop_loss_pct", None), getattr(user, "stop_loss_pct", None), 3.0)
                n_coins = max(1, len(pf.coins) if pf else 1)
                usdt = (pf.investment_usdt if pf else 20) / n_coins
                loop = asyncio.get_event_loop()
                buy_res = await loop.run_in_executor(
                    None,
                    lambda: get_reb().reentry_buy_and_place_tp(
                        symbol, usdt, tp1, tp2, tp3, sl_pct, s1, s2
                    ),
                )
                if buy_res.get("error"):
                    try:
                        await context.bot.send_message(
                            tid, f"⚠️ فشل إعادة دخول `{symbol}`: `{buy_res['error']}`", parse_mode="Markdown"
                        )
                    except Exception:
                        pass
                else:
                    update_coin_position(
                        db, coin.id,
                        entry_price=buy_res.get("entry_price", act["price"]),
                        tp1_price=buy_res.get("tp1_price", 0),
                        tp2_price=buy_res.get("tp2_price", 0),
                        tp3_price=buy_res.get("tp3_price", 0),
                        current_sl_price=buy_res.get("sl_price", 0),
                        original_sl_price=buy_res.get("original_sl_price") or buy_res.get("sl_price", 0),
                        amount=buy_res.get("amount", 0),
                        remaining_amount=buy_res.get("amount", 0),
                        tp1_order_id=buy_res.get("tp1_order_id"),
                        tp2_order_id=buy_res.get("tp2_order_id"),
                        tp3_order_id=buy_res.get("tp3_order_id"),
                        position_status="open",
                        reentry_used=False,
                        reentry_touched=False,
                        reentry_price=0.0,
                    )
                    msg = (
                        f"🔄 *إعادة دخول* — `{symbol}`\n"
                        f"شراء عند ≈ `{buy_res.get('entry_price', act['price']):.6g}`\n"
                        f"وقف متحرك | SL `{buy_res.get('sl_price', 0):.6g}`\n"
                        f"وقف متحرك مفعّل — إعادة الدخول متاحة مرة أخرى بعد أي استوب\n"
                        f"المحفظة: *{pf.name if pf else '—'}*"
                    )
                    try:
                        await context.bot.send_message(tid, msg, parse_mode="Markdown")
                    except Exception:
                        pass

            elif act["action"] == "sl_hit_sold":
                exit_price = float(act.get("price") or 0)
                stopped_amount = float(act.get("amount") or 0)
                entry_price = float(coin.entry_price or 0)
                pnl = (exit_price - entry_price) * stopped_amount
                reentry_available = bool(act.get("reentry_available"))
                record_trade_event(
                    db, tid, pf.id, coin.id, symbol, "stop_loss",
                    entry_price, exit_price, stopped_amount, pnl,
                    reentry_available=reentry_available,
                    details="Stop loss filled",
                )
                # Auto re-entry: park in waiting_reentry with trigger price.
                # Manual button still available as backup.
                # دائماً متاح لإعادة الدخول — مش مرة واحدة
                reentry_available = True
                trigger = float(act.get("reentry_price") or 0)
                update_coin_position(
                    db, coin.id,
                    position_status="waiting_reentry",
                    current_sl_price=float(act.get("sl") or 0) or 0.0,
                    tp_order_id=None,
                    tp1_order_id=None,
                    tp2_order_id=None,
                    tp3_order_id=None,
                    amount=0.0,
                    remaining_amount=0.0,
                    reentry_price=trigger,
                    reentry_touched=False,
                    reentry_used=False,
                )
                raised = " (بعد التريلينج)" if act.get("was_raised") else ""
                if trigger > 0:
                    extra = (
                        f"⏳ إعادة دخول تلقائي عند الرجوع فوق `{trigger:.6g}` "
                        f"(+1%) — متكرر كلما اتضرب الاستوب"
                    )
                else:
                    extra = "إعادة الدخول التلقائي مفعّلة."
                msg = (
                    f"🛡 *ضرب الاستوب{raised}* — `{symbol}`\n"
                    f"تم البيع فوراً بسعر السوق ≈ `{act['price']:.6g}`\n"
                    f"النتيجة: `{pnl:+.2f}` USDT\n"
                    f"{extra}\n"
                    f"المحفظة: *{pf.name if pf else '—'}*"
                )
                try:
                    await context.bot.send_message(
                        tid,
                        msg,
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton(
                                "🔄 عرض العملات وإعادة الدخول",
                                callback_data=f"stopped_{pf.id}",
                            )]
                        ]) if reentry_available else None,
                    )
                except Exception:
                    pass

            elif act["action"] == "sl_sell_failed":
                try:
                    await context.bot.send_message(
                        tid,
                        f"⚠️ فشل بيع `{symbol}` بعد ضرب الاستوب:\n`{act.get('error')}`",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
        # إشعارات الـ trailing المجمّعة (رسالة واحدة لكل مستخدم)
        for _tid, items in trail_batches.items():
            if not items:
                continue
            lines = [f"📈 *تحديث وقف متحرك* ({len(items)})", ""]
            for it in items[:25]:
                g = it.get("gain")
                gtxt = f" | `+{g:.1f}%`" if g is not None else ""
                be = " 🔒" if it.get("be_lock") else ""
                lines.append(
                    f"`{it['symbol']}`{be}: `{it['old_sl']:.6g}` → `{it['new_sl']:.6g}`{gtxt}"
                )
            if len(items) > 25:
                lines.append(f"... و {len(items) - 25} أخرى")
            try:
                await context.bot.send_message(_tid, "\n".join(lines), parse_mode="Markdown")
            except Exception:
                pass

    except Exception:
        logger.exception("monitor_positions_job error")
    finally:
        db.close()


def main():
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN مطلوب")
    if not config.MEXC_API_KEY or not config.MEXC_API_SECRET:
        raise SystemExit("MEXC_API_KEY و MEXC_API_SECRET مطلوبان")
    if not config.DATABASE_URL:
        raise SystemExit("DATABASE_URL مطلوب")

    init_db()
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    # Cloud monitor for TP/SL every 25 seconds
    if app.job_queue:
        app.job_queue.run_repeating(
            monitor_positions_job,
            interval=60,
            first=15,
            job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 30},
        )
        logger.info("Position monitor job scheduled (every 45s)")
        # تحديث أهداف ذكي خفيف: كل 10 دقائق، أقصى 4 عملات، وكل عملة كل 3 ساعات
        app.job_queue.run_repeating(
            smart_levels_refresh_job,
            interval=600,
            first=90,
            job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 120},
        )
        logger.info("Smart levels soft-refresh scheduled (every 10min, max 4 coins/run)")
        app.job_queue.run_repeating(
            market_sense_job,
            interval=120,
            first=40,
            job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 60},
        )
        logger.info("Market sense / defense job scheduled (every 2min)")
    else:
        logger.warning("JobQueue not available — install python-telegram-bot[job-queue]")

    async def entry_create(update, context):
        query = update.callback_query
        try:
            await query.answer()
        except Exception:
            pass
        if not await ensure_admin(update):
            return ConversationHandler.END
        context.user_data["create"] = {}
        await query.edit_message_text(
            "📝 أرسل *اسم المحفظة*:",
            parse_mode="Markdown",
        )
        return CREATE_NAME

    async def entry_addcoin(update, context):
        query = update.callback_query
        try:
            await query.answer()
        except Exception:
            pass
        if not await ensure_admin(update):
            return ConversationHandler.END
        data = query.data or ""
        pf_id = int(data.split("_")[1])
        context.user_data["addcoin_pf"] = pf_id
        await query.edit_message_text("أرسل رمز العملة (مثال: BTC أو ETH):")
        return ADD_COIN

    async def entry_increase(update, context):
        # زيادة رأس المال صارت عبر إعادة البناء — نوجّه المستخدم
        await on_callback(update, context)
        return ConversationHandler.END

    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(entry_create, pattern="^create_pf$"),
            CallbackQueryHandler(entry_addcoin, pattern="^addcoin_"),
            CallbackQueryHandler(entry_increase, pattern="^increase_"),
        ],
        states={
            CREATE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_name)],
            CREATE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_amount)],
            CREATE_COINS: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_coins)],
            ADD_COIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_coin_msg)],
            INCREASE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, increase_amount_msg)],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        allow_reentry=True,
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_message))

    logger.info("Bot starting (MEXC Portfolio Manager)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
