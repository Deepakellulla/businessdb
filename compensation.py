"""
Outage compensation: "Netflix was down for 2 days -> add 2 days to every running Netflix subscription".

    /compensate                 guided: pick the product, the days, an optional reason, and whether to notify
    /compensate 2 Netflix       same, with days + product already filled in
    /compensations              the last few, each with its ↩️ Undo

Customers who've started the bot are told on Telegram automatically (if you choose to notify). For everyone
else you get one-tap buttons that open WhatsApp / Telegram / Instagram with the message already written.
Every compensation is recorded and can be undone with one tap.
"""
import asyncio
import logging
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import ContextTypes

import db
from helpers import (
    ack, is_owner, message_buttons, reply_to, require_auth, resync_sheets_in_background,
)

logger = logging.getLogger(__name__)

MAX_CONTACT_ROWS = 8


def _days(n: int) -> str:
    return f"{n} day{'s' if n != 1 else ''}"


def notice_text(customer: str, item: str, days: int, new_ends: list, reason) -> str:
    """The message customers get (plain text, so it can go to Telegram or be pasted into WhatsApp)."""
    ends = sorted(set(new_ends))
    lines = [f"Hi {customer}! 👋" if customer else "Hi! 👋",
             f"{item} had a service outage, so we've added {_days(days)} to your subscription."]
    if reason:
        lines.append(f"Reason: {reason}")
    lines.append(f"Your new end date is {ends[0]}." if len(ends) == 1 else f"Your new end dates are {', '.join(ends)}.")
    lines.append("Sorry for the trouble, and thank you for your patience!")
    return "\n".join(lines)


async def _send(bot, chat_id: int, text: str) -> bool:
    """Send a Telegram message to a customer. False if they haven't started the bot / blocked it / etc."""
    for attempt in range(2):
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            return True
        except RetryAfter as e:
            if attempt == 0:
                await asyncio.sleep(min(float(e.retry_after) + 1, 30))
                continue
            return False
        except (Forbidden, BadRequest):
            return False
        except TelegramError:
            logger.exception("Couldn't notify %s", chat_id)
            return False
    return False


async def apply_and_notify(bot, item: str, days: int, reason, notify: bool, logged_by: str):
    """
    Does the compensation and (optionally) the notifying. Returns (message_html, keyboard_or_None).
    The database change is the only step that can raise; notifying problems are reported, never fatal --
    so a failure after the change can never make someone apply it twice.
    """
    result = db.apply_compensation(item, days, reason, logged_by)
    if not result:
        return (f"ℹ️ No running subscriptions for <b>{escape(item)}</b> any more — nothing was changed."), None
    comp_id, rows = result

    customers = {r["customer"].strip().lower() for r in rows}
    lines = [f"✅ <b>Compensation #{comp_id} applied</b>",
             f"Added <b>{_days(days)}</b> to <b>{len(rows)}</b> running {escape(item)} "
             f"subscription{'s' if len(rows) != 1 else ''} ({len(customers)} customer{'s' if len(customers) != 1 else ''}).",
             "Reminders were re-armed for the new end dates."]
    keyboard = []
    try:
        resync_sheets_in_background()
        if notify:
            by_tg = {}
            for r in rows:
                if r.get("tg_id"):
                    by_tg.setdefault(r["tg_id"], []).append(r)
            reached = set()
            for tg_id, group in by_tg.items():
                text = notice_text(group[0]["customer"], item, days, [g["new_end"] for g in group], reason)
                if await _send(bot, tg_id, text):
                    reached.add(tg_id)
                await asyncio.sleep(0.05)
            lines.append(f"📣 Told <b>{len(reached)}</b> customer{'s' if len(reached) != 1 else ''} on Telegram.")

            # everyone we couldn't tell automatically: one row of buttons per customer
            seen, left, no_contact = set(), [], []
            for r in rows:
                key = r["customer"].strip().lower()
                if key in seen or r.get("tg_id") in reached:
                    continue
                seen.add(key)
                ends = [x["new_end"] for x in rows if x["customer"].strip().lower() == key]
                text = notice_text(r["customer"], item, days, ends, reason)
                buttons = message_buttons(r, text, short=True)
                (left if buttons else no_contact).append((r, buttons))
            if left:
                lines.append(f"📲 <b>{len(left)}</b> couldn't be told automatically (no Telegram ID, or they haven't "
                             "started the bot). Tap to message them — the text is already written:")
                for r, buttons in left[:MAX_CONTACT_ROWS]:
                    keyboard.append(buttons)
                if len(left) > MAX_CONTACT_ROWS:
                    lines.append(f"…and {len(left) - MAX_CONTACT_ROWS} more (find them in /subs).")
            if no_contact:
                names = ", ".join(escape(r["customer"]) for r, _ in no_contact[:10])
                lines.append(f"❔ No contact saved for: {names}" + (" …" if len(no_contact) > 10 else ""))
    except Exception:
        logger.exception("Compensation #%s was applied, but notifying failed", comp_id)
        lines.append("⚠️ The days were added, but I hit a problem while notifying customers. Check the logs.")
    keyboard.append([InlineKeyboardButton(f"↩️ Undo compensation #{comp_id}", callback_data=f"comp:undo:{comp_id}")])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


async def undo_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update):
        return
    if not is_owner(update):
        await ack(update, "Only the owner can undo a compensation.", alert=True)
        return
    comp_id = int(update.callback_query.data.split(":")[2])
    status, count = db.undo_compensation(comp_id)
    await ack(update)
    if status == "missing":
        await reply_to(update, "That compensation no longer exists.")
        return
    if status == "already":
        await reply_to(update, f"↩️ Compensation #{comp_id} was already undone.")
        return
    try:
        await update.callback_query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    resync_sheets_in_background()
    comp = db.get_compensation(comp_id)
    await reply_to(
        update,
        f"↩️ <b>Compensation #{comp_id} undone.</b> Took {_days(comp['days'])} back off {count} "
        f"subscription{'s' if count != 1 else ''} ({escape(comp['item'])}).\n"
        "<i>Customers who were already told on Telegram aren't un-told — message them if needed.</i>",
    )


async def compensations_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update):
        return
    rows = db.list_compensations(10)
    if not rows:
        await reply_to(update, "No compensations yet. Use /compensate when a product has an outage.")
        return
    lines = ["🛠 <b>Recent outage compensations</b>", ""]
    buttons = []
    import json
    for c in rows:
        n = len(json.loads(c["affected"]))
        when = c["created_at"].strftime("%d %b") if c["created_at"] else ""
        state = " — <i>undone</i>" if c["undone"] else ""
        lines.append(f"#{c['id']} · {escape(c['item'])} · +{_days(c['days'])} · {n} sub{'s' if n != 1 else ''} · {when}{state}"
                     + (f"\n    “{escape(c['reason'])}”" if c["reason"] else ""))
        if not c["undone"] and len(buttons) < 3 and is_owner(update):
            buttons.append([InlineKeyboardButton(f"↩️ Undo #{c['id']} ({c['item'][:18]})", callback_data=f"comp:undo:{c['id']}")])
    await reply_to(update, "\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)
