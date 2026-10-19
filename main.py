import asyncio
import csv
import io
import json
import logging
from datetime import date, timedelta, time as dtime

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

import assign
import db
import flow
import sheets
from config import (
    BOT_TOKEN, REMINDER_HOUR, REMINDER_MINUTE, REMINDER_DAYS_BEFORE,
    MORNING_SUMMARY_HOUR, MORNING_SUMMARY_MINUTE, TIMEZONE, TZINFO,
)
from helpers import (
    ack, display_name, esc, is_lifetime_duration, money, normalize_phone,
    reply_document_to, reply_long, reply_to, require_auth, resync_sheets_in_background, whatsapp_link,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------- conversation states for the (separate) edit flows ----------
EDIT_SALE_PRICE, EDIT_SALE_COST = range(2)
EDIT_SUB_PRICE, EDIT_SUB_COST = range(2, 4)

_is_lifetime_duration = is_lifetime_duration  # short alias used by the list views below

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
        "<b>Log</b>\n"
        "🧾 /newsale · 📋 /newsub · ↩️ /undo (take back your last entry)\n\n"
        "<b>Money owed &amp; renewals</b>\n"
        "🔴 /dues · 💰 /receive · ⏳ /expiring · 🔁 /renew · 📞 /setphone\n\n"
        "<b>Reports</b>\n"
        "☀️ /summary · 📊 /stats · 📅 /monthly · 🏆 /top · 📈 /abc · ♻️ /renewal · 🏭 /suppliers\n"
        "👤 /customer &lt;name&gt; · 🔍 /search &lt;keyword&gt;\n\n"
        "<b>Setup</b>\n"
        "📦 /products · /addproduct · /delproduct\n"
        "🧑‍💼 /assignseller · /setseller (add sellers to older entries)\n"
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
            InlineKeyboardButton("☀️ Summary", callback_data="menu:summary"),
            InlineKeyboardButton("🔴 Dues", callback_data="menu:dues"),
        ],
        [
            InlineKeyboardButton("⏳ Expiring", callback_data="menu:expiring"),
            InlineKeyboardButton("↩️ Undo last", callback_data="menu:undo"),
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
            InlineKeyboardButton("📈 ABC Analysis", callback_data="menu:abc"),
        ],
        [
            InlineKeyboardButton("🏭 Suppliers", callback_data="menu:suppliers"),
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
        "top": top_cmd, "abc": abc_cmd, "renewal": renewal_cmd, "export": export_cmd,
        "backup": backup_cmd, "summary": summary_cmd, "dues": dues_cmd, "expiring": expiring_cmd,
        "undo": undo_cmd, "suppliers": suppliers_cmd,
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


async def cancel_idle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cancel when no form is open (open forms handle /cancel themselves)."""
    context.user_data.clear()
    await reply_to(update, "👍 Nothing in progress — you're all clear.", reply_markup=ReplyKeyboardRemove())


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
    resync_sheets_in_background()
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
    if ok:
        resync_sheets_in_background()
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
    resync_sheets_in_background()
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
    ok = db.undo_subscription(sub_id)  # deletes it; if it was a one-tap renewal, reopens the original
    if ok:
        resync_sheets_in_background()
    await reply_to(update, f"✅ Subscription #{sub_id} deleted." if ok else f"No subscription with ID {sub_id}.")


# ================= read-only commands =================

def _balance(r) -> float:
    """What the customer still owes on a sale/subscription row."""
    paid = r["amount_paid"] if r.get("amount_paid") is not None else r["sold_price"]
    return max(r["sold_price"] - paid, 0.0)


def _tags(r) -> str:
    """' · 🧑‍💼 Ravi · 🔴 due 600.00' style suffix for list lines."""
    out = ""
    if r.get("seller"):
        out += f" · 🧑‍💼 {esc(r['seller'])}"
    bal = _balance(r)
    if bal > 0.005:
        out += f" · 🔴 due {money(bal)}"
    return out


async def subs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.list_active_subscriptions()
    if not rows:
        await reply_to(update, "No active subscriptions.")
        return
    lines = [f"📋 <b>Active subscriptions</b> ({len(rows)})"]
    today = date.today()
    for r in rows:
        end = date.fromisoformat(r["end_date"])
        days_left = (end - today).days
        when = "🔓 Lifetime" if _is_lifetime_duration(days_left) else (
            f"ends {r['end_date']} ({days_left} day{'s' if days_left != 1 else ''} left)"
        )
        lines.append(
            f"• #{r['id']} <b>{esc(r['customer'])}</b> — {esc(r['item'])} — {when} — "
            f"profit {r['profit']:.2f}{_tags(r)}"
        )
    missing = sum(1 for r in rows if not r.get("seller"))
    if missing:
        lines.append(f"\n🧑‍💼 {missing} of these have no seller yet — /assignseller")
    await reply_long(update, "\n".join(lines))


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
            f"sold {r['sold_price']:.2f}, profit {r['profit']:.2f}{_tags(r)}"
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

    _, _, owed = db.dues()
    if owed > 0.005:
        lines.append(f"\n🔴 <b>Outstanding dues:</b> {money(owed)} — see /dues")

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

    phone = db.customer_phone(name)
    if phone:
        lines.append(f"📞 +{phone}")
    owed = sum(_balance(r) for r in profile["sales"]) + sum(_balance(r) for r in profile["subscriptions"])
    if owed > 0.005:
        lines.append(f"🔴 <b>Owes you {money(owed)}</b>")

    if profile["active_subscriptions"]:
        lines.append("\n<b>Active subscriptions</b>")
        today = date.today()
        for sub in profile["active_subscriptions"]:
            days_left = (date.fromisoformat(sub["end_date"]) - today).days
            when = "🔓 Lifetime" if _is_lifetime_duration(days_left) else f"ends {sub['end_date']} ({days_left}d left)"
            lines.append(f"• #{sub['id']} {esc(sub['item'])} — {when}{_tags(sub)}")

    if profile["sales"]:
        lines.append(f"\n<b>Sales history</b> ({len(profile['sales'])})")
        for sale in profile["sales"][:10]:
            lines.append(f"• #{sale['id']} {sale['sale_date']} — {esc(sale['item'])} — profit {sale['profit']:.2f}{_tags(sale)}")
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
            lines.append(f"• #{r['id']} {r['sale_date']} — {esc(r['item'])}{cust} — profit {r['profit']:.2f}{_tags(r)}")
    if subs:
        lines.append("\n📋 Subscriptions:")
        for r in subs:
            lines.append(f"• #{r['id']} {esc(r['customer'])} — {esc(r['item'])} — ends {r['end_date']}{_tags(r)}")

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

    sellers = db.top_sellers(5)
    if sellers:
        lines.append("\n🧑‍💼 <b>Top sellers by profit</b>")
        for i, r in enumerate(sellers, 1):
            lines.append(
                f"{i}. {esc(r['seller'])} — profit {r['total_profit']:.2f} "
                f"({r['deals']} deal{'s' if r['deals'] != 1 else ''}, revenue {r['total_revenue']:.2f})"
            )

    await reply_to(update, "\n".join(lines))


async def abc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = db.abc_analysis()
    if not result:
        await reply_to(update, "Not enough profit data yet for an ABC analysis — log a few sales first.")
        return

    tiers = {"A": [], "B": [], "C": []}
    for r in result:
        tiers[r["tier"]].append(r)

    tier_labels = {
        "A": ("🅰️", "Top performers", "focus here — these drive most of your profit"),
        "B": ("🅱️", "Steady contributors", "solid, worth keeping an eye on"),
        "C": ("🆑", "Long tail", "small individually — consider trimming or bundling"),
    }

    lines = ["📈 <b>ABC Analysis</b> — products ranked by profit contribution"]
    for tier in ("A", "B", "C"):
        items = tiers[tier]
        if not items:
            continue
        icon, label, note = tier_labels[tier]
        tier_profit_pct = sum(i["profit_pct"] for i in items)
        lines.append(f"\n{icon} <b>Tier {tier} — {label}</b> ({len(items)} item{'s' if len(items) != 1 else ''}, {tier_profit_pct:.1f}% of profit)")
        lines.append(f"<i>{note}</i>")
        shown = items if tier != "C" else items[:15]
        for i in shown:
            lines.append(f"• {esc(i['item'])} — profit {i['profit']:.2f} ({i['profit_pct']:.1f}%), sold {i['times_sold']}x")
        if tier == "C" and len(items) > 15:
            lines.append(f"…and {len(items) - 15} more")

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
        # "balance_due" isn't stored -- it's worked out from sold_price and amount_paid
        writer.writerow({k: (round(_balance(r), 2) if k == "balance_due" else r[k]) for k in fieldnames})
    return buf.getvalue().encode("utf-8")


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update):
        return

    sales = db.all_sales()
    subs = db.all_subscriptions()

    if sales:
        sales_csv = _rows_to_csv_bytes(
            sales, ["id", "sale_date", "item", "customer", "seller", "supplier", "gross_price", "discount",
                    "sold_price", "cost_price", "profit", "amount_paid", "balance_due",
                    "payment_method", "logged_by"]
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
            ["id", "customer", "phone", "item", "seller", "supplier", "gross_price", "discount",
             "sold_price", "cost_price", "profit", "amount_paid", "balance_due",
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


# ================= dues & payments =================

async def dues_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sales, subs, total = db.dues()
    if not sales and not subs:
        await reply_to(update, "🎉 Nobody owes you anything right now.")
        return
    lines = [f"🔴 <b>Owed to you: {money(total)}</b> ({len(sales) + len(subs)} entries)"]
    if sales:
        lines.append("\n🧾 <b>Sales</b>")
        for r in sales:
            who = esc(r["customer"]) if r["customer"] else "—"
            lines.append(
                f"• #{r['id']} {who} — {esc(r['item'])} ({r['sale_date']}) — "
                f"<b>{money(r['balance'])}</b> of {money(r['sold_price'])}"
            )
    if subs:
        lines.append("\n📋 <b>Subscriptions</b>")
        for r in subs:
            phone = f" · 📞 +{r['phone']}" if r.get("phone") else ""
            lines.append(
                f"• #{r['id']} {esc(r['customer'])} — {esc(r['item'])} ({r['start_date']}) — "
                f"<b>{money(r['balance'])}</b> of {money(r['sold_price'])}{phone}"
            )
    lines.append(
        "\n💰 Record a payment: <code>/receive sale 12 500</code> or <code>/receive sub 5</code> "
        "(leave the amount off for the full balance)."
    )
    await reply_long(update, "\n".join(lines))


async def receive_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update):
        return
    usage = ("Usage: <code>/receive sale 12 500</code> or <code>/receive sub 5</code>\n"
             "(leave the amount off to record the full remaining balance — find IDs with /dues)")
    args = context.args
    if len(args) < 2 or args[0].lower() not in ("sale", "sub"):
        await reply_to(update, usage)
        return
    kind = args[0].lower()
    try:
        record_id = int(args[1])
    except ValueError:
        await reply_to(update, usage)
        return
    amount = None
    if len(args) >= 3:
        try:
            amount = float(args[2].replace(",", ""))
        except ValueError:
            await reply_to(update, usage)
            return
        if not (amount > 0) or amount == float("inf"):
            await reply_to(update, "The amount must be a positive number.")
            return

    res = db.record_payment(kind, record_id, amount)
    label = "sale" if kind == "sale" else "subscription"
    if res is None:
        await reply_to(update, f"No {label} with ID {record_id}.")
        return
    if res["applied"] <= 0.005:
        await reply_to(update, f"✅ {label.capitalize()} #{record_id} is already paid in full — nothing to record.")
        return
    resync_sheets_in_background()
    msg = f"💰 Recorded <b>{money(res['applied'])}</b> on {label} #{record_id}."
    if amount is not None and amount > res["applied"] + 0.005:
        msg += f" (You entered {money(amount)}, but only {money(res['applied'])} was owed.)"
    msg += "\n✅ Fully paid now." if res["balance"] < 0.005 else f"\n🔴 Still due: <b>{money(res['balance'])}</b>"
    await reply_to(update, msg)


# ================= expiring / renew =================

async def expiring_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    days = 7
    if context.args:
        try:
            days = max(1, min(365, int(context.args[0])))
        except ValueError:
            await reply_to(update, "Usage: <code>/expiring 14</code> (days ahead, default 7)")
            return
    rows = db.expiring_subs(days)
    if not rows:
        await reply_to(update, f"✅ Nothing ends in the next {days} days.")
        return
    today = date.today()
    lines = [f"⏳ <b>Ending within {days} days</b> ({len(rows)})"]
    buttons = []
    for r in rows:
        left = (date.fromisoformat(r["end_date"]) - today).days
        if left < 0:
            when = f"⚠️ ended {-left} day{'s' if left != -1 else ''} ago"
        elif left == 0:
            when = "ends <b>today</b>"
        else:
            when = f"ends {r['end_date']} ({left}d)"
        phone = f" · 📞 +{r['phone']}" if r.get("phone") else ""
        lines.append(f"• #{r['id']} <b>{esc(r['customer'])}</b> — {esc(r['item'])} — {when}{phone}{_tags(r)}")
        if r.get("phone") and len(buttons) < 8:
            buttons.append([InlineKeyboardButton(
                f"💬 {r['customer'][:22]}",
                url=whatsapp_link(r["phone"], r["customer"], r["item"], r["end_date"]),
            )])
    lines.append("\n🔁 Renew with <code>/renew &lt;id&gt;</code> (add <code>due</code> if they haven't paid yet).")
    await reply_long(update, "\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


def _renew(update: Update, sub_id: int, paid: bool) -> str:
    status, payload = db.renew_subscription(sub_id, paid=paid, logged_by=display_name(update))
    if status == "missing":
        return f"No subscription with ID {sub_id}."
    if status == "lifetime":
        return "🔓 That's a lifetime subscription — there's nothing to renew."
    if status == "already":
        return f"ℹ️ Subscription #{sub_id} was already renewed (as #{payload})."
    old, new_id = payload["old"], payload["new_id"]
    resync_sheets_in_background()
    carry = f" (includes {payload['extra_days']} leftover days)" if payload["extra_days"] else ""
    msg = (
        f"🔁 <b>Renewed!</b> New subscription <b>#{new_id}</b>\n"
        f"👤 {esc(old['customer'])} — {esc(old['item'])}\n"
        f"📅 {payload['start'].isoformat()} → {payload['end'].isoformat()} · {payload['duration']} days{carry}\n"
        f"💰 {money(old['sold_price'])} · profit {money(payload['profit'])} · "
    )
    msg += "✅ paid" if paid else f"🔴 <b>{money(old['sold_price'])} due</b> (/receive sub {new_id})"
    msg += "\n\nWrong? <code>/undo</code> takes it back."
    return msg


async def renew_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update):
        return
    usage = "Usage: <code>/renew 12</code> — or <code>/renew 12 due</code> if they haven't paid yet (see /expiring)"
    if not context.args:
        await reply_to(update, usage)
        return
    try:
        sub_id = int(context.args[0])
    except ValueError:
        await reply_to(update, usage)
        return
    paid = not (len(context.args) > 1 and context.args[1].lower() in ("due", "unpaid", "later"))
    await reply_to(update, _renew(update, sub_id, paid))


async def renew_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update):
        return
    _, sub_id, mode = update.callback_query.data.split(":")
    text = _renew(update, int(sub_id), paid=(mode == "paid"))
    await ack(update, "Renewed ✅" if text.startswith("🔁") else None)
    if text.startswith("🔁") or text.startswith("ℹ️"):
        try:  # take the buttons off the reminder so it can't be renewed twice by accident
            await update.callback_query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
    await reply_to(update, text)


# ================= undo =================

async def undo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update):
        return
    me = display_name(update)
    found = db.latest_entry_by(me, 24)
    if not found:
        await reply_to(update, "↩️ Nothing to undo — I only undo entries <b>you</b> logged in the last 24 hours.")
        return
    kind, r = found
    if kind == "sale":
        what = (f"🧾 <b>Sale #{r['id']}</b> — {esc(r['item'])}"
                f"{' for ' + esc(r['customer']) if r['customer'] else ''}\n"
                f"Sold {money(r['sold_price'])} · profit {money(r['profit'])} · {r['sale_date']}")
    else:
        what = (f"📋 <b>Subscription #{r['id']}</b> — {esc(r['item'])} for {esc(r['customer'])}\n"
                f"Sold {money(r['sold_price'])} · profit {money(r['profit'])} · {r['start_date']}")
        if db.get_subscription(r["id"]) and db.is_renewal_target(r["id"]):
            what += "\n🔁 This was a renewal — undoing it re-opens the original subscription."
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, delete it", callback_data=f"undo:{kind}:{r['id']}:yes"),
        InlineKeyboardButton("Keep it", callback_data=f"undo:{kind}:{r['id']}:no"),
    ]])
    await reply_to(update, f"↩️ <b>Undo your last entry?</b>\n\n{what}", reply_markup=kb)


async def undo_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update):
        return
    _, kind, rid, answer = update.callback_query.data.split(":")
    rid = int(rid)
    try:
        await update.callback_query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    if answer != "yes":
        await ack(update, "Kept.")
        await reply_to(update, "👍 Kept — nothing was deleted.")
        return
    row = db.get_sale(rid) if kind == "sale" else db.get_subscription(rid)
    if not row:
        await ack(update, "Already gone.")
        await reply_to(update, "That entry has already been removed.")
        return
    if row["logged_by"] != display_name(update):
        await ack(update, "Not yours.", alert=True)
        await reply_to(update, "🚫 You can only undo entries you logged yourself.")
        return
    ok = db.delete_sale(rid) if kind == "sale" else db.undo_subscription(rid)
    await ack(update, "Undone ↩️")
    if ok:
        resync_sheets_in_background()
    await reply_to(update, f"↩️ Undone — {'sale' if kind == 'sale' else 'subscription'} #{rid} was deleted." if ok
                   else "Nothing was deleted.")


# ================= suppliers =================

async def suppliers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rep = db.supplier_report()
    if not rep["suppliers"]:
        await reply_to(
            update,
            "🏭 No supplier data yet. When logging a sale or subscription, tap or type the supplier "
            "at the “Where did you buy it from?” step — then this report compares them.",
        )
        return
    lines = ["🏭 <b>Suppliers</b> — by profit they've generated"]
    for r in rep["suppliers"]:
        lines.append(
            f"• <b>{esc(r['supplier'])}</b> — {r['deals']} deal{'s' if r['deals'] != 1 else ''}, "
            f"spent {money(r['total_cost'])}, profit {money(r['profit'])} "
            f"(margin {r['margin_pct']:.1f}%)"
        )
    if rep["comparisons"]:
        lines.append("\n💡 <b>Same item, different suppliers</b> (cheapest first)")
        for c in rep["comparisons"]:
            opts = " → ".join(f"{esc(o['supplier'])} {money(o['avg_cost'])}" for o in c["options"])
            saving = c["options"][-1]["avg_cost"] - c["options"][0]["avg_cost"]
            lines.append(f"• {esc(c['item'])}: {opts}  <i>(up to {money(saving)} less each)</i>")
    await reply_long(update, "\n".join(lines))


# ================= product catalog =================

async def products_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update):
        return
    prods = db.list_products()
    if not prods:
        await reply_to(
            update,
            "📦 No saved products yet.\nSave your regular items with /addproduct — then /newsale and "
            "/newsub show them as tap buttons that pre-fill price, cost and duration.",
        )
        return
    lines = ["📦 <b>Saved products</b>"]
    for kind, title in (("sale", "🧾 One-time items"), ("sub", "📋 Subscription plans")):
        group = [p for p in prods if p["kind"] == kind]
        if not group:
            continue
        lines.append(f"\n{title}")
        for p in group:
            bits = [f"{money(p['default_price'])} / cost {money(p['default_cost'])}"]
            if kind == "sub" and p["default_duration"]:
                bits.append("Lifetime" if is_lifetime_duration(p["default_duration"]) else f"{p['default_duration']}d")
            if p["default_supplier"]:
                bits.append(esc(p["default_supplier"]))
            lines.append(f"• #{p['id']} <b>{esc(p['name'])}</b> — " + " · ".join(bits))
    lines.append("\nAdd or update: /addproduct · Remove: <code>/delproduct &lt;id&gt;</code>")
    await reply_long(update, "\n".join(lines))


async def delproduct_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner = db.get_owner_chat_id()
    if owner is None or update.effective_user.id != int(owner):
        await reply_to(update, "Only the owner can manage the product list.")
        return
    try:
        pid = int(context.args[0])
    except (IndexError, ValueError):
        await reply_to(update, "Usage: <code>/delproduct &lt;id&gt;</code> (see /products)")
        return
    prod = db.get_product(pid)
    ok = db.delete_product(pid)
    await reply_to(update, f"🗑 Removed <b>{esc(prod['name'])}</b> from your saved products. "
                           "(Past sales are untouched.)" if ok else f"No product with ID {pid}.")


# ================= customer phone =================

async def setphone_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update):
        return
    usage = ("Usage: <code>/setphone Alice Smith 9876543210</code>\n"
             "(the name first, the number last — add the country code for non-local numbers)")
    if len(context.args) < 2:
        await reply_to(update, usage)
        return
    name = " ".join(context.args[:-1])
    try:
        phone = normalize_phone(context.args[-1])
    except ValueError as e:
        await reply_to(update, f"⚠️ {esc(e)}")
        return
    n = db.set_customer_phone(name, phone)
    if not n:
        await reply_to(update, f"No subscriptions found for “{esc(name)}”. The name has to match — try /search {esc(name)}.")
        return
    resync_sheets_in_background()
    await reply_to(update, f"📞 Saved +{phone} on {n} subscription{'s' if n != 1 else ''} for <b>{esc(name)}</b>. "
                           "Their reminders will now have a WhatsApp button.")


# ================= daily summary =================

def build_summary(today: date) -> str:
    yesterday = today - timedelta(days=1)
    y = db.period_totals(yesterday.isoformat(), yesterday.isoformat())
    t = db.period_totals(today.isoformat(), today.isoformat())
    m = db.period_totals(today.replace(day=1).isoformat(), today.isoformat())
    sales_due, subs_due, owed = db.dues()
    expiring = db.expiring_subs(7, today)

    lines = [f"☀️ <b>Daily summary</b> — {today.strftime('%a %d %b %Y')}", ""]
    lines.append(
        f"<b>Yesterday</b>: {y['sales_n']} sale{'s' if y['sales_n'] != 1 else ''}, "
        f"{y['subs_n']} subscription{'s' if y['subs_n'] != 1 else ''} · "
        f"revenue {money(y['revenue'])} · profit <b>{money(y['profit'])}</b>"
    )
    if t["sales_n"] or t["subs_n"]:
        lines.append(
            f"<b>Today so far</b>: {t['sales_n'] + t['subs_n']} deal(s) · profit <b>{money(t['profit'])}</b>"
        )
    lines.append(
        f"<b>{today.strftime('%B')} so far</b>: {m['sales_n'] + m['subs_n']} deals · "
        f"revenue {money(m['revenue'])} · profit <b>{money(m['profit'])}</b>"
    )
    if expiring:
        lines.append(f"\n⏳ <b>Ending in the next 7 days ({len(expiring)})</b>")
        for r in expiring[:6]:
            left = (date.fromisoformat(r["end_date"]) - today).days
            when = "overdue" if left < 0 else ("today" if left == 0 else f"in {left}d")
            lines.append(f"• #{r['id']} {esc(r['customer'])} — {esc(r['item'])} — {when}")
        if len(expiring) > 6:
            lines.append(f"…and {len(expiring) - 6} more — /expiring")
    if owed > 0.005:
        lines.append(f"\n🔴 <b>Owed to you:</b> {money(owed)} across {len(sales_due) + len(subs_due)} entries — /dues")
    if not (expiring or owed > 0.005):
        lines.append("\n✅ Nothing expiring this week and nobody owes you money.")
    return "\n".join(lines)


async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update):
        return
    await reply_long(update, build_summary(date.today()))


async def morning_summary(context: ContextTypes.DEFAULT_TYPE):
    owner_id = db.get_owner_chat_id()
    if not owner_id:
        return
    try:
        await context.bot.send_message(
            chat_id=owner_id, text=build_summary(date.today()), parse_mode=ParseMode.HTML
        )
    except Exception:
        logger.exception("Morning summary failed")


# ================= reminder job =================

def _reminder_keyboard(sub) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton("🔁 Renew (paid)", callback_data=f"renew:{sub['id']}:paid"),
        InlineKeyboardButton("🕓 Renew (pay later)", callback_data=f"renew:{sub['id']}:due"),
    ]]
    if sub.get("phone"):
        rows.append([InlineKeyboardButton(
            "💬 WhatsApp the customer",
            url=whatsapp_link(sub["phone"], sub["customer"], sub["item"], sub["end_date"]),
        )])
    return InlineKeyboardMarkup(rows)


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
            reply_markup=_reminder_keyboard(sub),
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
            reply_markup=_reminder_keyboard(sub),
        )
        db.mark_reminded_due(sub["id"])


# ================= app setup =================

COMMAND_MENU = [
    ("menu", "Open the button menu"),
    ("newsale", "Log a one-time sale"),
    ("newsub", "Log a customer subscription"),
    ("undo", "Take back your last entry"),
    ("summary", "Yesterday, this month, what's due"),
    ("dues", "Who owes you money"),
    ("receive", "Record a payment received"),
    ("expiring", "Subscriptions ending soon"),
    ("renew", "Renew a subscription"),
    ("subs", "List active subscriptions"),
    ("sales", "Recent sales"),
    ("stats", "Profit summary"),
    ("monthly", "Profit by month"),
    ("customer", "A customer's history"),
    ("search", "Search sales & subscriptions"),
    ("top", "Top customers, products & sellers"),
    ("abc", "ABC analysis of products by profit"),
    ("suppliers", "Compare suppliers by cost & margin"),
    ("renewal", "Renewal & churn rate"),
    ("products", "Saved products"),
    ("addproduct", "Save a product/plan (owner)"),
    ("setphone", "Save a customer's phone number"),
    ("assignseller", "Add sellers to older entries (owner)"),
    ("setseller", "Set a seller on specific entries (owner)"),
    ("export", "Download CSV export"),
    ("backup", "Download database backup"),
    ("syncsheets", "Push all data to Google Sheets"),
    ("editsale", "Correct a sale"),
    ("deletesale", "Remove a sale"),
    ("editsub", "Correct a subscription"),
    ("deletesub", "Remove a subscription"),
    ("myid", "Show your Telegram ID"),
    ("addstaff", "Authorize a staff member"),
    ("removestaff", "Revoke staff access"),
    ("staff", "List authorized staff"),
    ("cancel", "Cancel current input"),
]


async def post_init(app: Application):
    await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMAND_MENU])
    logger.info("Timezone: %s | reminders %02d:%02d | morning summary %s",
                TIMEZONE, REMINDER_HOUR, REMINDER_MINUTE,
                "off" if MORNING_SUMMARY_HOUR < 0 else f"{MORNING_SUMMARY_HOUR:02d}:{MORNING_SUMMARY_MINUTE:02d}")
    # The Sheet's columns changed in this version; rewriting the tabs on startup keeps them
    # matching the database (and picks up anything a previous sync missed).
    if sheets.is_configured():
        resync_sheets_in_background()


async def _stale_form_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await flow.stale_form_button(update, context)


def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("myid", myid_cmd))
    app.add_handler(CommandHandler("addstaff", addstaff_cmd))
    app.add_handler(CommandHandler("removestaff", removestaff_cmd))
    app.add_handler(CommandHandler("staff", staff_cmd))

    # the three step-by-step forms (/newsale, /newsub, /addproduct) share one conversation
    app.add_handler(flow.build_conversation())
    app.add_handler(assign.build_conversation())
    app.add_handler(CommandHandler("setseller", assign.setseller_cmd))

    app.add_handler(CommandHandler("subs", subs_cmd))
    app.add_handler(CommandHandler("sales", sales_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("monthly", monthly_cmd))
    app.add_handler(CommandHandler("customer", customer_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("top", top_cmd))
    app.add_handler(CommandHandler("abc", abc_cmd))
    app.add_handler(CommandHandler("renewal", renewal_cmd))
    app.add_handler(CommandHandler("suppliers", suppliers_cmd))
    app.add_handler(CommandHandler("summary", summary_cmd))
    app.add_handler(CommandHandler("dues", dues_cmd))
    app.add_handler(CommandHandler("receive", receive_cmd))
    app.add_handler(CommandHandler("expiring", expiring_cmd))
    app.add_handler(CommandHandler("renew", renew_cmd))
    app.add_handler(CommandHandler("undo", undo_cmd))
    app.add_handler(CommandHandler("products", products_cmd))
    app.add_handler(CommandHandler("delproduct", delproduct_cmd))
    app.add_handler(CommandHandler("setphone", setphone_cmd))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(CommandHandler("backup", backup_cmd))
    app.add_handler(CommandHandler("syncsheets", syncsheets_cmd))
    app.add_handler(CommandHandler("deletesale", deletesale_cmd))
    app.add_handler(CommandHandler("deletesub", deletesub_cmd))

    # one-tap buttons
    app.add_handler(CallbackQueryHandler(renew_button, pattern=r"^renew:\d+:(paid|due)$"))
    app.add_handler(CallbackQueryHandler(undo_button, pattern=r"^undo:(sale|sub):\d+:(yes|no)$"))
    app.add_handler(CallbackQueryHandler(
        menu_callback,
        pattern="^menu:(stats|monthly|subs|sales|top|abc|renewal|export|backup|summary|dues|expiring|undo|suppliers)$",
    ))

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

    app.add_handler(CommandHandler("cancel", cancel_idle))

    # last resort: taps on buttons of a form that already finished (must come after the conversation)
    app.add_handler(CallbackQueryHandler(_stale_form_button, pattern=r"^(f|cf|as|asall):"))

    # daily jobs -- times are in TIMEZONE (the scheduler itself defaults to UTC, so be explicit)
    app.job_queue.run_daily(
        check_reminders, time=dtime(hour=REMINDER_HOUR, minute=REMINDER_MINUTE, tzinfo=TZINFO),
    )
    if MORNING_SUMMARY_HOUR >= 0:
        app.job_queue.run_daily(
            morning_summary,
            time=dtime(hour=MORNING_SUMMARY_HOUR, minute=MORNING_SUMMARY_MINUTE, tzinfo=TZINFO),
        )

    return app


def main():
    db.init_db()
    app = build_app()
    logger.info("Bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
