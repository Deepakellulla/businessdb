"""
Free trials: a customer tries something before paying.

    /newtrial          log a trial (customer, where they came from, product, how many days, your cost)
    /trials            running trials, ones that need a decision, and how many convert to paid
    /deltrial <id>     remove a trial logged by mistake

The bot reminds you the day before a trial ends and on the last day, with two buttons:
🔁 Convert to paid (starts a new subscription with everything already filled in) and 🙅 Not interested.
A trial also counts as converted automatically if you log a paid subscription for the same customer and
product. Trials nobody decided on for 14 days after they ended are counted as lost.
"""
import logging
from datetime import date, timedelta
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import db
from helpers import ack, message_buttons, money, reply_long, reply_to, require_auth

logger = logging.getLogger(__name__)

STATS_DAYS = 90


def whatsapp_text(t) -> str:
    return (f"Hi {t['customer']}, your free trial of {t['item']} ends on {t['end_date']}. "
            "Would you like to continue with a paid plan?")


def trial_keyboard(t) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("🔁 Convert to paid", callback_data=f"tr:conv:{t['id']}"),
             InlineKeyboardButton("🙅 Not interested", callback_data=f"tr:lost:{t['id']}")]]
    contact = message_buttons(t, whatsapp_text(t))
    if contact:
        rows.append(contact)
    return InlineKeyboardMarkup(rows)


def _left(t, today: date) -> str:
    days = (date.fromisoformat(t["end_date"]) - today).days
    if days > 1:
        return f"ends in {days} days ({t['end_date']})"
    if days == 1:
        return f"ends tomorrow ({t['end_date']})"
    if days == 0:
        return f"ends today ({t['end_date']})"
    return f"ended {-days} day{'s' if days != -1 else ''} ago ({t['end_date']})"


async def send_trial_reminders(bot, owner_id: int, today: date) -> None:
    """Part of the daily reminder job. Each trial is reminded once the day before and once when it's over."""
    closed = db.close_stale_trials(today)
    if closed:
        logger.info("Marked %s long-ended trials as lost", closed)
    soon, due = db.trials_due_for_reminder(today)
    for t in soon:
        await bot.send_message(
            chat_id=owner_id, parse_mode=ParseMode.HTML, reply_markup=trial_keyboard(t),
            text=(f"🧪 <b>{escape(t['customer'])}</b>'s free trial of “{escape(t['item'])}” ends tomorrow "
                  f"({t['end_date']}). Good moment to ask if they'd like to continue."),
        )
        db.mark_trial_reminded(t["id"], due=False)
    for t in due:
        await bot.send_message(
            chat_id=owner_id, parse_mode=ParseMode.HTML, reply_markup=trial_keyboard(t),
            text=(f"🧪 <b>{escape(t['customer'])}</b>'s free trial of “{escape(t['item'])}” "
                  f"{_left(t, today)}. Did they convert?"),
        )
        db.mark_trial_reminded(t["id"], due=True)
        db.mark_trial_reminded(t["id"], due=False)  # no separate "tomorrow" nudge after this


def attention_line(today: date):
    """One line for the morning summary: trials ending soon / waiting for a decision. None if there are none."""
    active = db.list_active_trials()
    soon = [t for t in active if 0 <= (date.fromisoformat(t["end_date"]) - today).days <= 1]
    overdue = [t for t in active if date.fromisoformat(t["end_date"]) < today]
    if not (soon or overdue):
        return None
    bits = []
    if soon:
        bits.append(f"{len(soon)} ending within a day")
    if overdue:
        bits.append(f"{len(overdue)} waiting for a decision")
    return f"🧪 <b>Free trials:</b> {', '.join(bits)} — /trials"


async def trials_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update):
        return
    today = date.today()
    active = db.list_active_trials()
    running = [t for t in active if t["end_date"] >= today.isoformat()]
    decide = [t for t in active if t["end_date"] < today.isoformat()]
    stats = db.trial_stats((today - timedelta(days=STATS_DAYS)).isoformat())

    lines = ["🧪 <b>Free trials</b>", ""]
    if not active:
        lines.append("No trials running. Log one with /newtrial.")

    def row(t):
        contact = f" · 🛒 {escape(t['platform'])}" if t.get("platform") else ""
        return f"• #{t['id']} <b>{escape(t['customer'])}</b> — {escape(t['item'])} — {_left(t, today)}{contact}"

    if decide:
        lines.append(f"⏳ <b>Ended — waiting for a decision ({len(decide)})</b>")
        lines += [row(t) for t in decide[:10]]
        lines.append("")
    if running:
        lines.append(f"▶️ <b>Running ({len(running)})</b>")
        lines += [row(t) for t in running[:12]]
        lines.append("")

    decided = stats["converted"] + stats["lost"]
    lines.append(f"📊 <b>Last {STATS_DAYS} days:</b> {stats['started']} started · "
                 f"{stats['converted']} converted · {stats['lost']} lost · {stats['active']} undecided")
    if decided:
        lines.append(f"Conversion: <b>{stats['converted'] / decided * 100:.0f}%</b> of decided trials "
                     f"({stats['converted']}/{decided})")
    if stats["cost"] > 0.005:
        lines.append(f"Trial cost to you: {money(stats['cost'])}")

    buttons = []
    for t in (decide + running)[:5]:
        buttons.append([InlineKeyboardButton(f"🔁 #{t['id']} {t['customer'][:14]}", callback_data=f"tr:conv:{t['id']}"),
                        InlineKeyboardButton(f"🙅 #{t['id']}", callback_data=f"tr:lost:{t['id']}")])
    await reply_long(update, "\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


async def lost_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update):
        return
    trial_id = int(update.callback_query.data.split(":")[2])
    trial = db.get_trial(trial_id)
    if not trial:
        await ack(update, "That trial no longer exists.", alert=True)
        return
    if not db.set_trial_status(trial_id, "lost"):
        await ack(update, f"Trial #{trial_id} was already decided ({trial['status']}).", alert=True)
        return
    await ack(update)
    try:
        await update.callback_query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await reply_to(update, f"🙅 Trial #{trial_id} ({escape(trial['customer'])} — {escape(trial['item'])}) "
                           "marked as not interested.")


async def deltrial_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update):
        return
    if not context.args or not context.args[0].isdigit():
        await reply_to(update, "Usage: <code>/deltrial &lt;id&gt;</code> (see IDs in /trials)")
        return
    trial_id = int(context.args[0])
    trial = db.get_trial(trial_id)
    if not trial:
        await reply_to(update, f"No trial with ID {trial_id}.")
        return
    db.delete_trial(trial_id)
    await reply_to(update, f"🗑 Trial #{trial_id} ({escape(trial['customer'])} — {escape(trial['item'])}) removed.")
