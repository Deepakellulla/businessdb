import asyncio
import csv
import io
import json
import logging
from datetime import date, timedelta, time as dtime
from html import escape

from telegram import (
    Update, ReplyKeyboardRemove, InputFile, BotCommand,
    InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import db
import sheets
from config import BOT_TOKEN, REMINDER_HOUR, REMINDER_MINUTE, REMINDER_DAYS_BEFORE

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------- conversation states ----------
SALE_ITEM, SALE_CUSTOMER, SALE_GROSS, SALE_DISCOUNT, SALE_COST, SALE_PAYMENT, SALE_CONFIRM = range(7)
(SUB_CUSTOMER, SUB_ITEM, SUB_GROSS, SUB_DISCOUNT, SUB_COST, SUB_PAYMENT,
 SUB_DURATION, SUB_CONFIRM) = range(7, 15)
EDIT_SALE_PRICE, EDIT_SALE_COST = range(15, 17)
EDIT_SUB_PRICE, EDIT_SUB_COST = range(17, 19)


# ================= small helpers =================

def esc(x) -> str:
    return escape(str(x)) if x is not None else ""


def display_name(update: Update) -> str:
    user = update.effective_user
    return (user.username and f"@{user.username}") or user.first_name or str(user.id)


async def reply_to(update: Update, text: str, **kwargs):
    kwargs.setdefault("parse_mode", ParseMode.HTML)
    if update.callback_query:
        return await update.callback_query.message.reply_text(text, **kwargs)
    return await update.message.reply_text(text, **kwargs)


async def reply_document_to(update: Update, **kwargs):
    if update.callback_query:
        return await update.callback_query.message.reply_document(**kwargs)
    return await update.message.reply_document(**kwargs)


async def ack(update: Update):
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass


async def require_auth(update: Update) -> bool:
    if db.is_authorized(update.effective_user.id):
        return True
    await reply_to(update, "🚫 You're not authorized to use this bot. Ask the owner to add you with /addstaff.")
    return False


def payment_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💵 Cash", callback_data="pay:Cash"),
            InlineKeyboardButton("📲 UPI", callback_data="pay:UPI"),
            InlineKeyboardButton("💳 Card", callback_data="pay:Card"),
        ],
        [InlineKeyboardButton("✏️ Other (type it)", callback_data="pay:other")],
    ])


# ================= /start & housekeeping =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if db.get_owner_chat_id() is None:
        db.set_setting("owner_chat_id", str(update.effective_chat.id))
        await reply_to(update, "👋 <b>You're registered as the owner of this bot.</b>")
    elif not db.is_authorized(update.effective_user.id):
        await reply_to(
            update,
            "👋 Hi! This bot is private. Ask the owner to add you with:\n"
            f"<code>/addstaff {update.effective_user.id}</code>",
        )
        return

    await reply_to(
        update,
        "✨ <b>Business Tracker</b>\n\n"
        "Tap the menu button (☰) next to the text box any time, or send /menu "
        "for a button-based menu.\n\n"
        "<b>Quick reference</b>\n"
        "🧾 /newsale — log a sale\n"
        "📋 /newsub — log a subscription\n"
        "📊 /stats · 📅 /monthly · 🏆 /top · ♻️ /renewal\n"
        "👤 /customer &lt;name&gt; · 🔍 /search &lt;keyword&gt;\n"
        "📤 /export · 💾 /backup · 📊 /syncsheets\n"
        "🛠 /editsale, /deletesale, /editsub, /deletesub\n"
        "👥 /myid, /addstaff, /removestaff, /staff\n"
        "❌ /cancel — cancel current input",
    )


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("🧾 New Sale", callback_data="menu:newsale"),
            InlineKeyboardButton("📋 New Sub", callback_data="menu:newsub"),
        ],
        [
            InlineKeyboardButton("📊 Stats", callback_data="menu:stats"),
            InlineKeyboardButton("📅 Monthly", callback_data="menu:monthly"),
        ],
        [
            InlineKeyboardButton("📋 Active Subs", callback_data="menu:subs"),
            InlineKeyboardButton("🧾 Recent Sales", callback_data="menu:sales"),
        ],
        [
            InlineKeyboardButton("🏆 Top", callback_data="menu:top"),
            InlineKeyboardButton("♻️ Renewal Rate", callback_data="menu:renewal"),
        ],
        [
            InlineKeyboardButton("📤 Export CSV", callback_data="menu:export"),
            InlineKeyboardButton("💾 Backup", callback_data="menu:backup"),
        ],
    ]
    await reply_to(update, "✨ <b>Menu</b> — tap an action:", reply_markup=InlineKeyboardMarkup(keyboard))


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ack(update)
    action = update.callback_query.data.split(":", 1)[1]
    dispatch = {
        "stats": stats_cmd, "monthly": monthly_cmd, "subs": subs_cmd, "sales": sales_cmd,
        "top": top_cmd, "renewal": renewal_cmd, "export": export_cmd, "backup": backup_cmd,
    }
    fn = dispatch.get(action)
    if fn:
        context.args = []
        await fn(update, context)


async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_to(update, f"Your Telegram user ID is: <code>{update.effective_user.id}</code>")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_to(update, "❌ Cancelled.", reply_markup=ReplyKeyboardRemove())
    context.user_data.clear()
    return ConversationHandler.END


# ================= staff management (owner only) =================

async def addstaff_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner_id = db.get_owner_chat_id()
    if owner_id is None or update.effective_user.id != int(owner_id):
        await reply_to(update, "Only the owner can add staff.")
        return
    if not context.args:
        await reply_to(update, "Usage: <code>/addstaff &lt;telegram_user_id&gt; [name]</code>")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await reply_to(update, "User ID must be a number. Ask them to send /myid to get it.")
        return
    name = " ".join(context.args[1:]) or f"user {user_id}"
    db.add_staff(user_id, name)
    await reply_to(update, f"✅ <b>{esc(name)}</b> ({user_id}) can now use this bot.")


async def removestaff_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner_id = db.get_owner_chat_id()
    if owner_id is None or update.effective_user.id != int(owner_id):
        await reply_to(update, "Only the owner can remove staff.")
        return
    if not context.args:
        await reply_to(update, "Usage: <code>/removestaff &lt;telegram_user_id&gt;</code>")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await reply_to(update, "User ID must be a number.")
        return
    removed = db.remove_staff(user_id)
    await reply_to(update, "✅ Removed." if removed else "That user wasn't on the staff list.")


async def staff_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.list_staff()
    if not rows:
        await reply_to(update, "No staff added yet. Owner can use /addstaff &lt;id&gt;.")
        return
    lines = ["👥 <b>Authorized staff</b>"]
    for r in rows:
        lines.append(f"• {esc(r['name'])} (<code>{r['user_id']}</code>) — added {r['added_date']}")
    await reply_to(update, "\n".join(lines))


# ================= /newsale flow =================

async def newsale_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update):
        return ConversationHandler.END
    context.user_data.clear()
    await reply_to(update, "🧾 <b>New Sale</b> — Step 1/6\n\n🏷 What item/product was sold?")
    return SALE_ITEM


async def newsale_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["item"] = update.message.text.strip()
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⏭ Skip", callback_data="cust_skip")]])
    await reply_to(
        update,
        "🧾 <b>New Sale</b> — Step 2/6\n\n👤 Customer name? (or tap Skip)",
        reply_markup=keyboard,
    )
    return SALE_CUSTOMER


async def _sale_ask_gross(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_to(update, "🧾 <b>New Sale</b> — Step 3/6\n\n💰 Price charged (before any discount)?")
    return SALE_GROSS


async def newsale_customer_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ack(update)
    context.user_data["customer"] = None
    return await _sale_ask_gross(update, context)


async def newsale_customer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["customer"] = None if text == "-" else text
    return await _sale_ask_gross(update, context)


async def _sale_ask_discount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("0️⃣ No discount", callback_data="disc_zero")]])
    await reply_to(
        update,
        "🧾 <b>New Sale</b> — Step 4/6\n\n🏷 Any discount given? Send the amount, or tap for none.",
        reply_markup=keyboard,
    )
    return SALE_DISCOUNT


async def newsale_gross(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["gross_price"] = float(update.message.text.strip())
    except ValueError:
        await reply_to(update, "Please send a valid number.")
        return SALE_GROSS
    return await _sale_ask_discount(update, context)


async def _sale_ask_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_to(update, "🧾 <b>New Sale</b> — Step 5/6\n\n💵 Cost price? (number)")
    return SALE_COST


async def newsale_discount_zero(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ack(update)
    context.user_data["discount"] = 0.0
    return await _sale_ask_cost(update, context)


async def newsale_discount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        discount = float(update.message.text.strip())
    except ValueError:
        await reply_to(update, "Please send a valid number (0 if no discount).")
        return SALE_DISCOUNT
    if discount < 0:
        await reply_to(update, "Discount can't be negative. Send 0 or a positive amount.")
        return SALE_DISCOUNT
    context.user_data["discount"] = discount
    return await _sale_ask_cost(update, context)


async def newsale_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["cost_price"] = float(update.message.text.strip())
    except ValueError:
        await reply_to(update, "Please send a valid number for cost price.")
        return SALE_COST
    await reply_to(update, "🧾 <b>New Sale</b> — Step 6/6\n\n💳 Payment method?", reply_markup=payment_keyboard())
    return SALE_PAYMENT


def _sale_summary_text(d: dict) -> str:
    sold = d["gross_price"] - d["discount"]
    profit = sold - d["cost_price"]
    lines = ["🔎 <b>Review Sale</b>", ""]
    lines.append(f"🏷 Item: <b>{esc(d['item'])}</b>")
    lines.append(f"👤 Customer: {esc(d['customer']) if d['customer'] else '—'}")
    price_line = f"💰 Price: {d['gross_price']:.2f}"
    if d["discount"]:
        price_line += f" (−{d['discount']:.2f} discount)"
    lines.append(price_line)
    lines.append(f"💵 Cost: {d['cost_price']:.2f}")
    lines.append(f"💳 Payment: {esc(d['payment_method'])}")
    lines.append("")
    lines.append(f"➡️ Net received: <b>{sold:.2f}</b>")
    lines.append(f"➡️ Profit: <b>{profit:.2f}</b>")
    return "\n".join(lines)


def _confirm_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm & Save", callback_data=f"{prefix}:save")],
        [
            InlineKeyboardButton("🔁 Start Over", callback_data=f"{prefix}:restart"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"{prefix}:cancel"),
        ],
    ])


async def _sale_show_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_to(update, _sale_summary_text(context.user_data), reply_markup=_confirm_keyboard("saleconfirm"))
    return SALE_CONFIRM


async def newsale_payment_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ack(update)
    method = update.callback_query.data.split(":", 1)[1]
    if method == "other":
        await reply_to(update, "✏️ Type the payment method:")
        return SALE_PAYMENT
    context.user_data["payment_method"] = method
    return await _sale_show_confirmation(update, context)


async def newsale_payment_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["payment_method"] = update.message.text.strip()
    return await _sale_show_confirmation(update, context)


async def sale_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ack(update)
    action = update.callback_query.data.split(":", 1)[1]
    if action == "save":
        d = context.user_data
        sold_price = d["gross_price"] - d["discount"]
        sale_id, profit = db.add_sale(
            d["item"], d["customer"], sold_price, d["cost_price"],
            logged_by=display_name(update), discount=d["discount"],
            gross_price=d["gross_price"], payment_method=d["payment_method"],
        )
        if sheets.is_configured():
            sale_row = dict(db.get_sale(sale_id))
            asyncio.create_task(asyncio.to_thread(sheets.push_sale, sale_row))
        await reply_to(update, f"✅ <b>Sale #{sale_id} saved!</b>\nProfit: <b>{profit:.2f}</b>")
        context.user_data.clear()
        return ConversationHandler.END
    elif action == "restart":
        context.user_data.clear()
        await reply_to(update, "🧾 <b>New Sale</b> — Step 1/6\n\n🏷 What item/product was sold?")
        return SALE_ITEM
    else:
        context.user_data.clear()
        await reply_to(update, "❌ Cancelled.")
        return ConversationHandler.END


# ================= /newsub flow =================

async def newsub_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update):
        return ConversationHandler.END
    context.user_data.clear()
    await reply_to(update, "📋 <b>New Subscription</b> — Step 1/7\n\n👤 Customer name?")
    return SUB_CUSTOMER


async def newsub_customer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["customer"] = update.message.text.strip()
    await reply_to(update, "📋 <b>New Subscription</b> — Step 2/7\n\n🏷 What subscription/item is this?")
    return SUB_ITEM


async def newsub_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["item"] = update.message.text.strip()
    await reply_to(update, "📋 <b>New Subscription</b> — Step 3/7\n\n💰 Price charged (before any discount)?")
    return SUB_GROSS


async def _sub_ask_discount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("0️⃣ No discount", callback_data="disc_zero")]])
    await reply_to(
        update,
        "📋 <b>New Subscription</b> — Step 4/7\n\n🏷 Any discount given? Send the amount, or tap for none.",
        reply_markup=keyboard,
    )
    return SUB_DISCOUNT


async def newsub_gross(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["gross_price"] = float(update.message.text.strip())
    except ValueError:
        await reply_to(update, "Please send a valid number.")
        return SUB_GROSS
    return await _sub_ask_discount(update, context)


async def _sub_ask_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_to(update, "📋 <b>New Subscription</b> — Step 5/7\n\n💵 Your cost price for this subscription?")
    return SUB_COST


async def newsub_discount_zero(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ack(update)
    context.user_data["discount"] = 0.0
    return await _sub_ask_cost(update, context)


async def newsub_discount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        discount = float(update.message.text.strip())
    except ValueError:
        await reply_to(update, "Please send a valid number (0 if no discount).")
        return SUB_DISCOUNT
    if discount < 0:
        await reply_to(update, "Discount can't be negative. Send 0 or a positive amount.")
        return SUB_DISCOUNT
    context.user_data["discount"] = discount
    return await _sub_ask_cost(update, context)


async def newsub_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["cost_price"] = float(update.message.text.strip())
    except ValueError:
        await reply_to(update, "Please send a valid number.")
        return SUB_COST
    await reply_to(
        update, "📋 <b>New Subscription</b> — Step 6/7\n\n💳 Payment method?", reply_markup=payment_keyboard()
    )
    return SUB_PAYMENT


async def _sub_ask_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("30 days", callback_data="dur:30"),
        InlineKeyboardButton("90 days", callback_data="dur:90"),
        InlineKeyboardButton("365 days", callback_data="dur:365"),
    ]])
    await reply_to(
        update,
        "📋 <b>New Subscription</b> — Step 7/7\n\n📅 Duration? Tap a preset, or type the number of days.",
        reply_markup=keyboard,
    )
    return SUB_DURATION


async def newsub_payment_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ack(update)
    method = update.callback_query.data.split(":", 1)[1]
    if method == "other":
        await reply_to(update, "✏️ Type the payment method:")
        return SUB_PAYMENT
    context.user_data["payment_method"] = method
    return await _sub_ask_duration(update, context)


async def newsub_payment_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["payment_method"] = update.message.text.strip()
    return await _sub_ask_duration(update, context)


def _sub_summary_text(d: dict) -> str:
    sold = d["gross_price"] - d["discount"]
    profit = sold - d["cost_price"]
    today = date.today()
    end_date = today.fromordinal(today.toordinal() + d["duration"])
    lines = ["🔎 <b>Review Subscription</b>", ""]
    lines.append(f"👤 Customer: <b>{esc(d['customer'])}</b>")
    lines.append(f"🏷 Item: <b>{esc(d['item'])}</b>")
    price_line = f"💰 Price: {d['gross_price']:.2f}"
    if d["discount"]:
        price_line += f" (−{d['discount']:.2f} discount)"
    lines.append(price_line)
    lines.append(f"💵 Cost: {d['cost_price']:.2f}")
    lines.append(f"💳 Payment: {esc(d['payment_method'])}")
    lines.append(f"📅 Duration: {d['duration']} days (ends {end_date.isoformat()})")
    lines.append("")
    lines.append(f"➡️ Net: <b>{sold:.2f}</b>")
    lines.append(f"➡️ Profit: <b>{profit:.2f}</b>")
    return "\n".join(lines)


async def _sub_show_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_to(update, _sub_summary_text(context.user_data), reply_markup=_confirm_keyboard("subconfirm"))
    return SUB_CONFIRM


async def newsub_duration_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ack(update)
    days = int(update.callback_query.data.split(":", 1)[1])
    context.user_data["duration"] = days
    return await _sub_show_confirmation(update, context)


async def newsub_duration_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        days = int(update.message.text.strip())
    except ValueError:
        await reply_to(update, "Please send a whole number of days, or tap a preset above.")
        return SUB_DURATION
    context.user_data["duration"] = days
    return await _sub_show_confirmation(update, context)


async def sub_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ack(update)
    action = update.callback_query.data.split(":", 1)[1]
    if action == "save":
        d = context.user_data
        sold_price = d["gross_price"] - d["discount"]
        sub_id, end_date, profit = db.add_subscription(
            d["customer"], d["item"], sold_price, d["cost_price"], date.today(), d["duration"],
            logged_by=display_name(update), discount=d["discount"],
            gross_price=d["gross_price"], payment_method=d["payment_method"],
        )
        if sheets.is_configured():
            sub_row = dict(db.get_subscription(sub_id))
            asyncio.create_task(asyncio.to_thread(sheets.push_subscription, sub_row))
        await reply_to(
            update,
            f"✅ <b>Subscription #{sub_id} saved!</b>\n"
            f"Ends {end_date.isoformat()} · Profit: <b>{profit:.2f}</b>\n\n"
            f"I'll remind you {REMINDER_DAYS_BEFORE} days before it ends, and on the day.",
        )
        context.user_data.clear()
        return ConversationHandler.END
    elif action == "restart":
        context.user_data.clear()
        await reply_to(update, "📋 <b>New Subscription</b> — Step 1/7\n\n👤 Customer name?")
        return SUB_CUSTOMER
    else:
        context.user_data.clear()
        await reply_to(update, "❌ Cancelled.")
        return ConversationHandler.END


# ================= edit / delete sale =================

async def editsale_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update):
        return ConversationHandler.END
    if not context.args:
        await reply_to(update, "Usage: <code>/editsale &lt;id&gt;</code>  (see IDs via /sales)")
        return ConversationHandler.END
    try:
        sale_id = int(context.args[0])
    except ValueError:
        await reply_to(update, "ID must be a number.")
        return ConversationHandler.END
    sale = db.get_sale(sale_id)
    if not sale:
        await reply_to(update, f"No sale with ID {sale_id}.")
        return ConversationHandler.END
    context.user_data["edit_sale_id"] = sale_id
    await reply_to(
        update,
        f"✏️ Editing sale #{sale_id} (<b>{esc(sale['item'])}</b>). "
        f"Current sold price: {sale['sold_price']:.2f}\nSend the new sold price:",
    )
    return EDIT_SALE_PRICE


async def editsale_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["new_sold_price"] = float(update.message.text.strip())
    except ValueError:
        await reply_to(update, "Please send a valid number.")
        return EDIT_SALE_PRICE
    await reply_to(update, "Send the new cost price:")
    return EDIT_SALE_COST


async def editsale_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        cost_price = float(update.message.text.strip())
    except ValueError:
        await reply_to(update, "Please send a valid number.")
        return EDIT_SALE_COST
    sale_id = context.user_data["edit_sale_id"]
    profit = db.update_sale(sale_id, context.user_data["new_sold_price"], cost_price)
    await reply_to(update, f"✅ Sale #{sale_id} updated. New profit: <b>{profit:.2f}</b>")
    context.user_data.clear()
    return ConversationHandler.END


async def deletesale_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update):
        return
    if not context.args:
        await reply_to(update, "Usage: <code>/deletesale &lt;id&gt;</code>")
        return
    try:
        sale_id = int(context.args[0])
    except ValueError:
        await reply_to(update, "ID must be a number.")
        return
    ok = db.delete_sale(sale_id)
    await reply_to(update, f"✅ Sale #{sale_id} deleted." if ok else f"No sale with ID {sale_id}.")


# ================= edit / delete subscription =================

async def editsub_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update):
        return ConversationHandler.END
    if not context.args:
        await reply_to(update, "Usage: <code>/editsub &lt;id&gt;</code>  (see IDs via /subs)")
        return ConversationHandler.END
    try:
        sub_id = int(context.args[0])
    except ValueError:
        await reply_to(update, "ID must be a number.")
        return ConversationHandler.END
    sub = db.get_subscription(sub_id)
    if not sub:
        await reply_to(update, f"No subscription with ID {sub_id}.")
        return ConversationHandler.END
    context.user_data["edit_sub_id"] = sub_id
    await reply_to(
        update,
        f"✏️ Editing subscription #{sub_id} (<b>{esc(sub['item'])}</b> — {esc(sub['customer'])}). "
        f"Current price: {sub['sold_price']:.2f}\nSend the new price:",
    )
    return EDIT_SUB_PRICE


async def editsub_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["new_sold_price"] = float(update.message.text.strip())
    except ValueError:
        await reply_to(update, "Please send a valid number.")
        return EDIT_SUB_PRICE
    await reply_to(update, "Send the new cost price:")
    return EDIT_SUB_COST


async def editsub_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        cost_price = float(update.message.text.strip())
    except ValueError:
        await reply_to(update, "Please send a valid number.")
        return EDIT_SUB_COST
    sub_id = context.user_data["edit_sub_id"]
    profit = db.update_subscription(sub_id, context.user_data["new_sold_price"], cost_price)
    await reply_to(update, f"✅ Subscription #{sub_id} updated. New profit: <b>{profit:.2f}</b>")
    context.user_data.clear()
    return ConversationHandler.END


async def deletesub_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update):
        return
    if not context.args:
        await reply_to(update, "Usage: <code>/deletesub &lt;id&gt;</code>")
        return
    try:
        sub_id = int(context.args[0])
    except ValueError:
        await reply_to(update, "ID must be a number.")
        return
    ok = db.delete_subscription(sub_id)
    await reply_to(update, f"✅ Subscription #{sub_id} deleted." if ok else f"No subscription with ID {sub_id}.")


# ================= read-only commands =================

async def subs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.list_active_subscriptions()
    if not rows:
        await reply_to(update, "No active subscriptions.")
        return
    lines = ["📋 <b>Active subscriptions</b>"]
    today = date.today()
    for r in rows:
        end = date.fromisoformat(r["end_date"])
        days_left = (end - today).days
        lines.append(
            f"• #{r['id']} <b>{esc(r['customer'])}</b> — {esc(r['item'])} — ends {r['end_date']} "
            f"({days_left} day{'s' if days_left != 1 else ''} left) — profit {r['profit']:.2f}"
        )
    await reply_to(update, "\n".join(lines))


async def sales_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.list_sales(10)
    if not rows:
        await reply_to(update, "No sales logged yet.")
        return
    lines = ["🧾 <b>Last sales</b>"]
    for r in rows:
        cust = f" ({esc(r['customer'])})" if r["customer"] else ""
        lines.append(
            f"• #{r['id']} {r['sale_date']} — <b>{esc(r['item'])}</b>{cust} — "
            f"sold {r['sold_price']:.2f}, profit {r['profit']:.2f}"
        )
    await reply_to(update, "\n".join(lines))


async def monthly_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if args and "-" in args[0]:
        ym = args[0].strip()
        data = db.profit_for_month(ym)
        await reply_to(
            update,
            f"📅 <b>Profit for {ym}</b>\n"
            f"Sales profit: {data['sales_profit']:.2f}\n"
            f"Subscription profit: {data['sub_profit']:.2f}\n"
            f"Total profit: <b>{data['total_profit']:.2f}</b>\n"
            f"Revenue: {data['revenue']:.2f}",
        )
        return

    num_months = 6
    if args:
        try:
            num_months = max(1, min(24, int(args[0])))
        except ValueError:
            pass

    rows = db.monthly_profit_summary(num_months)
    if not rows:
        await reply_to(update, "No sales or subscriptions logged yet.")
        return

    lines = ["📅 <b>Monthly profit</b> (most recent first)"]
    for r in rows:
        lines.append(
            f"• {r['month']} — total <b>{r['total_profit']:.2f}</b> "
            f"(sales {r['sales_profit']:.2f} + subs {r['sub_profit']:.2f}), "
            f"revenue {r['revenue']:.2f}"
        )
    lines.append("\n<i>Tip: /monthly 2026-05 for one specific month, /monthly 12 for last 12 months.</i>")
    await reply_to(update, "\n".join(lines))


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = date.today()
    week_ago = (today - timedelta(days=7)).isoformat()
    month_ago = (today - timedelta(days=30)).isoformat()

    total_all = db.profit_summary()
    total_week = db.profit_summary(week_ago)
    total_month = db.profit_summary(month_ago)

    sales_all = db.sales_summary()
    disc = db.discount_summary()

    lines = [
        "📊 <b>Profit summary</b>",
        f"Last 7 days: {total_week:.2f}",
        f"Last 30 days: {total_month:.2f}",
        f"All-time: <b>{total_all:.2f}</b>",
        "",
        f"Total sales logged: {sales_all['cnt']} (revenue {sales_all['revenue']:.2f})",
    ]

    if disc["total_discount"] > 0:
        lines.append(
            f"\n🏷 Discounts given: {disc['total_discount']:.2f} "
            f"(gross {disc['gross_revenue']:.2f} → net {disc['net_revenue']:.2f})"
        )

    breakdown = db.payment_method_breakdown()
    if breakdown:
        lines.append("\n💳 <b>By payment method</b>")
        for row in breakdown:
            lines.append(
                f"• {esc(row['method'])}: {row['count']}x — revenue {row['revenue']:.2f}, "
                f"profit {row['profit']:.2f}"
            )

    await reply_to(update, "\n".join(lines))


async def customer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await reply_to(update, "Usage: <code>/customer &lt;name&gt;</code>")
        return
    name = " ".join(context.args)
    profile = db.customer_profile(name)

    if not profile["sales"] and not profile["subscriptions"]:
        suggestions = db.find_customer_names(name)
        msg = f"No records found for \"{esc(name)}\"."
        if suggestions:
            msg += "\nDid you mean: " + ", ".join(esc(s) for s in suggestions)
        await reply_to(update, msg)
        return

    lines = [
        f"👤 <b>{esc(name)}</b>",
        f"Total revenue: {profile['total_revenue']:.2f} | Total profit: <b>{profile['total_profit']:.2f}</b>",
    ]

    if profile["active_subscriptions"]:
        lines.append("\n<b>Active subscriptions</b>")
        today = date.today()
        for s in profile["active_subscriptions"]:
            days_left = (date.fromisoformat(s["end_date"]) - today).days
            lines.append(f"• #{s['id']} {esc(s['item'])} — ends {s['end_date']} ({days_left}d left)")

    if profile["sales"]:
        lines.append(f"\n<b>Sales history</b> ({len(profile['sales'])})")
        for s in profile["sales"][:10]:
            lines.append(f"• #{s['id']} {s['sale_date']} — {esc(s['item'])} — profit {s['profit']:.2f}")
        if len(profile["sales"]) > 10:
            lines.append(f"…and {len(profile['sales']) - 10} more")

    await reply_to(update, "\n".join(lines))


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await reply_to(update, "Usage: <code>/search &lt;keyword&gt;</code>")
        return
    keyword = " ".join(context.args)
    sales, subs = db.search_records(keyword)

    if not sales and not subs:
        await reply_to(update, f"No matches for \"{esc(keyword)}\".")
        return

    lines = [f"🔍 <b>Results for \"{esc(keyword)}\"</b>"]
    if sales:
        lines.append("\n🧾 Sales:")
        for r in sales:
            cust = f" ({esc(r['customer'])})" if r["customer"] else ""
            lines.append(f"• #{r['id']} {r['sale_date']} — {esc(r['item'])}{cust} — profit {r['profit']:.2f}")
    if subs:
        lines.append("\n📋 Subscriptions:")
        for r in subs:
            lines.append(f"• #{r['id']} {esc(r['customer'])} — {esc(r['item'])} — ends {r['end_date']}")

    await reply_to(update, "\n".join(lines))


async def top_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    customers = db.top_customers(5)
    products = db.top_products(5)

    lines = ["🏆 <b>Top customers by profit</b>"]
    if customers:
        for i, c in enumerate(customers, 1):
            lines.append(f"{i}. {esc(c['customer'])} — profit {c['total_profit']:.2f}")
    else:
        lines.append("No data yet.")

    lines.append("\n🏆 <b>Top products/items by profit</b>")
    if products:
        for i, p in enumerate(products, 1):
            lines.append(f"{i}. {esc(p['item'])} — profit {p['total_profit']:.2f} ({p['times_sold']}x)")
    else:
        lines.append("No data yet.")

    await reply_to(update, "\n".join(lines))


async def renewal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = db.renewal_churn_stats(grace_days=30)
    if stats["total_ended"] == 0:
        await reply_to(update, "No subscriptions have ended yet, so there's no renewal data to show.")
        return

    lines = [
        "♻️ <b>Renewal & Churn</b>",
        f"Ended subscriptions: {stats['total_ended']}",
        f"Renewed: {stats['renewed']} | Churned: {stats['churned']}",
        f"Renewal rate: <b>{stats['renewal_rate_pct']:.1f}%</b>",
    ]
    if stats["churned_details"]:
        lines.append("\n<b>Didn't come back:</b>")
        for cust, item, end_date in stats["churned_details"][:10]:
            lines.append(f"• {esc(cust)} — {esc(item)} (ended {end_date})")
        if len(stats["churned_details"]) > 10:
            lines.append(f"…and {len(stats['churned_details']) - 10} more")

    await reply_to(update, "\n".join(lines))


# ================= export & backup =================

def _rows_to_csv_bytes(rows, fieldnames) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r[k] for k in fieldnames})
    return buf.getvalue().encode("utf-8")


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update):
        return

    sales = db.all_sales()
    subs = db.all_subscriptions()

    if sales:
        sales_csv = _rows_to_csv_bytes(
            sales, ["id", "sale_date", "item", "customer", "gross_price", "discount",
                    "sold_price", "cost_price", "profit", "payment_method", "logged_by"]
        )
        await reply_document_to(
            update,
            document=InputFile(io.BytesIO(sales_csv), filename="sales_export.csv"),
            caption="🧾 Sales export",
        )
    else:
        await reply_to(update, "No sales to export.")

    if subs:
        subs_csv = _rows_to_csv_bytes(
            subs,
            ["id", "customer", "item", "gross_price", "discount", "sold_price", "cost_price", "profit",
             "start_date", "end_date", "active", "payment_method", "logged_by"],
        )
        await reply_document_to(
            update,
            document=InputFile(io.BytesIO(subs_csv), filename="subscriptions_export.csv"),
            caption="📋 Subscriptions export",
        )
    else:
        await reply_to(update, "No subscriptions to export.")


async def backup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update):
        return
    data = db.full_backup_dict()
    payload = json.dumps(data, indent=2, default=str).encode("utf-8")
    await reply_document_to(
        update,
        document=InputFile(
            io.BytesIO(payload),
            filename=f"business_backup_{date.today().isoformat()}.json",
        ),
        caption=(
            f"💾 Full backup as of {date.today().isoformat()} — "
            f"{len(data['sales'])} sales, {len(data['subscriptions'])} subscriptions"
        ),
    )


async def syncsheets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update):
        return
    if not sheets.is_configured():
        await reply_to(
            update,
            "📊 Google Sheets isn't configured yet. Set "
            "<code>GOOGLE_SHEETS_CREDENTIALS_B64</code> and "
            "<code>GOOGLE_SHEETS_SPREADSHEET_ID</code> to enable this.",
        )
        return
    await reply_to(update, "📊 Syncing everything to Google Sheets, one moment…")
    try:
        sales = [dict(r) for r in db.all_sales()]
        subs = [dict(r) for r in db.all_subscriptions()]
        n_sales, n_subs = await asyncio.to_thread(sheets.full_resync, sales, subs)
        await reply_to(update, f"✅ Synced <b>{n_sales}</b> sales and <b>{n_subs}</b> subscriptions to Sheets.")
    except Exception as e:
        logger.exception("Manual /syncsheets failed")
        await reply_to(update, f"❌ Sync failed: {esc(str(e))}")


# ================= reminder job =================

async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    owner_id = db.get_owner_chat_id()
    if not owner_id:
        return

    today = date.today()
    soon, due = db.subscriptions_due_for_reminder(today, REMINDER_DAYS_BEFORE)

    for sub in soon:
        await context.bot.send_message(
            chat_id=owner_id,
            text=(
                f"⏰ Heads up: <b>{esc(sub['customer'])}</b>'s subscription to \"{esc(sub['item'])}\" "
                f"ends in {REMINDER_DAYS_BEFORE} days ({sub['end_date']})."
            ),
            parse_mode=ParseMode.HTML,
        )
        db.mark_reminded_soon(sub["id"])

    for sub in due:
        await context.bot.send_message(
            chat_id=owner_id,
            text=(
                f"🔴 <b>{esc(sub['customer'])}</b>'s subscription to \"{esc(sub['item'])}\" "
                f"has ended ({sub['end_date']}). Time to renew or follow up!"
            ),
            parse_mode=ParseMode.HTML,
        )
        db.mark_reminded_due(sub["id"])


# ================= app setup =================

async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("menu", "Open the button menu"),
        BotCommand("newsale", "Log a one-time sale"),
        BotCommand("newsub", "Log a customer subscription"),
        BotCommand("subs", "List active subscriptions"),
        BotCommand("sales", "Recent sales"),
        BotCommand("stats", "Profit summary"),
        BotCommand("monthly", "Profit by month"),
        BotCommand("customer", "A customer's history"),
        BotCommand("search", "Search sales & subscriptions"),
        BotCommand("top", "Top customers & products"),
        BotCommand("renewal", "Renewal & churn rate"),
        BotCommand("export", "Download CSV export"),
        BotCommand("backup", "Download database backup"),
        BotCommand("syncsheets", "Push all data to Google Sheets"),
        BotCommand("editsale", "Correct a sale"),
        BotCommand("deletesale", "Remove a sale"),
        BotCommand("editsub", "Correct a subscription"),
        BotCommand("deletesub", "Remove a subscription"),
        BotCommand("myid", "Show your Telegram ID"),
        BotCommand("addstaff", "Authorize a staff member"),
        BotCommand("removestaff", "Revoke staff access"),
        BotCommand("staff", "List authorized staff"),
        BotCommand("cancel", "Cancel current input"),
    ])


def main():
    db.init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("myid", myid_cmd))
    app.add_handler(CommandHandler("addstaff", addstaff_cmd))
    app.add_handler(CommandHandler("removestaff", removestaff_cmd))
    app.add_handler(CommandHandler("staff", staff_cmd))

    app.add_handler(CommandHandler("subs", subs_cmd))
    app.add_handler(CommandHandler("sales", sales_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("monthly", monthly_cmd))
    app.add_handler(CommandHandler("customer", customer_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("top", top_cmd))
    app.add_handler(CommandHandler("renewal", renewal_cmd))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(CommandHandler("backup", backup_cmd))
    app.add_handler(CommandHandler("syncsheets", syncsheets_cmd))
    app.add_handler(CommandHandler("deletesale", deletesale_cmd))
    app.add_handler(CommandHandler("deletesub", deletesub_cmd))

    # menu button dispatch (read-only actions only; newsale/newsub are conversation entry points below)
    app.add_handler(CallbackQueryHandler(
        menu_callback, pattern="^menu:(stats|monthly|subs|sales|top|renewal|export|backup)$"
    ))

    sale_conv = ConversationHandler(
        entry_points=[
            CommandHandler("newsale", newsale_start),
            CallbackQueryHandler(newsale_start, pattern="^menu:newsale$"),
        ],
        states={
            SALE_ITEM: [MessageHandler(filters.TEXT & ~filters.COMMAND, newsale_item)],
            SALE_CUSTOMER: [
                CallbackQueryHandler(newsale_customer_skip, pattern="^cust_skip$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, newsale_customer),
            ],
            SALE_GROSS: [MessageHandler(filters.TEXT & ~filters.COMMAND, newsale_gross)],
            SALE_DISCOUNT: [
                CallbackQueryHandler(newsale_discount_zero, pattern="^disc_zero$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, newsale_discount),
            ],
            SALE_COST: [MessageHandler(filters.TEXT & ~filters.COMMAND, newsale_cost)],
            SALE_PAYMENT: [
                CallbackQueryHandler(newsale_payment_button, pattern="^pay:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, newsale_payment_text),
            ],
            SALE_CONFIRM: [CallbackQueryHandler(sale_confirm_callback, pattern="^saleconfirm:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(sale_conv)

    sub_conv = ConversationHandler(
        entry_points=[
            CommandHandler("newsub", newsub_start),
            CallbackQueryHandler(newsub_start, pattern="^menu:newsub$"),
        ],
        states={
            SUB_CUSTOMER: [MessageHandler(filters.TEXT & ~filters.COMMAND, newsub_customer)],
            SUB_ITEM: [MessageHandler(filters.TEXT & ~filters.COMMAND, newsub_item)],
            SUB_GROSS: [MessageHandler(filters.TEXT & ~filters.COMMAND, newsub_gross)],
            SUB_DISCOUNT: [
                CallbackQueryHandler(newsub_discount_zero, pattern="^disc_zero$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, newsub_discount),
            ],
            SUB_COST: [MessageHandler(filters.TEXT & ~filters.COMMAND, newsub_cost)],
            SUB_PAYMENT: [
                CallbackQueryHandler(newsub_payment_button, pattern="^pay:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, newsub_payment_text),
            ],
            SUB_DURATION: [
                CallbackQueryHandler(newsub_duration_button, pattern="^dur:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, newsub_duration_text),
            ],
            SUB_CONFIRM: [CallbackQueryHandler(sub_confirm_callback, pattern="^subconfirm:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(sub_conv)

    editsale_conv = ConversationHandler(
        entry_points=[CommandHandler("editsale", editsale_start)],
        states={
            EDIT_SALE_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, editsale_price)],
            EDIT_SALE_COST: [MessageHandler(filters.TEXT & ~filters.COMMAND, editsale_cost)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(editsale_conv)

    editsub_conv = ConversationHandler(
        entry_points=[CommandHandler("editsub", editsub_start)],
        states={
            EDIT_SUB_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, editsub_price)],
            EDIT_SUB_COST: [MessageHandler(filters.TEXT & ~filters.COMMAND, editsub_cost)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(editsub_conv)

    # daily reminder check
    app.job_queue.run_daily(
        check_reminders,
        time=dtime(hour=REMINDER_HOUR, minute=REMINDER_MINUTE),
    )

    logger.info("Bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
