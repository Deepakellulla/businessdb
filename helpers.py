"""Small shared helpers (kept separate so main.py and flow.py can both use them)."""
import asyncio
import re
from datetime import datetime
from html import escape
from urllib.parse import quote

from telegram import Update
from telegram.constants import ParseMode

import db
import sheets
from config import DEFAULT_COUNTRY_CODE, WHATSAPP_MESSAGE

# Lifetime subscriptions are stored as a very-far-future end date rather than NULL,
# so all existing date comparisons/sorting/reminders work unchanged -- a subscription
# with this many days left will never realistically trigger a reminder.
LIFETIME_DAYS = 36500  # 100 years
LIFETIME_THRESHOLD_DAYS = 3650  # 10+ years left is treated as "lifetime" for display


def is_lifetime_duration(days: int) -> bool:
    return days >= LIFETIME_THRESHOLD_DAYS


def esc(x) -> str:
    return escape(str(x)) if x is not None else ""


def money(x) -> str:
    return f"{x:,.2f}"


def display_name(update: Update) -> str:
    user = update.effective_user
    return (user.username and f"@{user.username}") or user.first_name or str(user.id)


async def reply_to(update: Update, text: str, **kwargs):
    kwargs.setdefault("parse_mode", ParseMode.HTML)
    if update.callback_query:
        return await update.callback_query.message.reply_text(text, **kwargs)
    return await update.message.reply_text(text, **kwargs)


async def reply_long(update: Update, text: str, limit: int = 3800, **kwargs):
    """Telegram rejects messages over 4096 characters; split long lists on line boundaries."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    for i, chunk in enumerate(chunks):
        # only the last chunk carries the buttons
        await reply_to(update, chunk, **(kwargs if i == len(chunks) - 1 else {}))


async def reply_document_to(update: Update, **kwargs):
    if update.callback_query:
        return await update.callback_query.message.reply_document(**kwargs)
    return await update.message.reply_document(**kwargs)


async def ack(update: Update, text: str = None, alert: bool = False):
    if update.callback_query:
        try:
            await update.callback_query.answer(text=text, show_alert=alert)
        except Exception:
            pass


async def require_auth(update: Update) -> bool:
    if db.is_authorized(update.effective_user.id):
        return True
    if update.callback_query:
        await ack(update, "Not authorized", alert=True)
    await reply_to(update, "🚫 You're not authorized to use this bot. Ask the owner to add you with /addstaff.")
    return False


def parse_date_input(text: str):
    """Accepts 2026-06-15, 15-06-2026, 15/06/2026 or 2026/06/15. Returns a date or None."""
    text = text.strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def normalize_phone(text: str) -> str:
    """
    Digits only, with the default country code added to bare 10-digit numbers
    (so '98765 43210' -> '919876543210'). Raises ValueError if it can't be a real number.
    """
    digits = re.sub(r"\D", "", text or "")
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 10:
        digits = DEFAULT_COUNTRY_CODE + digits
    if not (8 <= len(digits) <= 15):
        raise ValueError(
            "That doesn't look like a phone number. Send it with country code "
            "(e.g. 919876543210) or just the 10 digits, or tap Skip."
        )
    return digits


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def whatsapp_link(phone: str, customer: str, item: str, end_date: str) -> str:
    """wa.me link that opens WhatsApp with the renewal message already typed out."""
    try:
        text = WHATSAPP_MESSAGE.format_map(_SafeDict(customer=customer, item=item, end_date=end_date))
    except Exception:
        text = f"Hi {customer}, your {item} subscription ends on {end_date}. Would you like to renew?"
    return f"https://wa.me/{phone}?text={quote(text)}"


# ---------- background Google Sheets syncing ----------
# asyncio only keeps a weak reference to tasks, so a fire-and-forget task can be garbage
# collected mid-flight unless we hold on to it ourselves.
_bg_tasks = set()


def spawn(coro):
    task = asyncio.get_running_loop().create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


def push_new_to_sheets(kind: str, record_id: int):
    """Live-sync one new sale ('sale') or subscription ('sub') without delaying the reply."""
    if not sheets.is_configured():
        return
    all_sales = [dict(r) for r in db.all_sales()]
    all_subs = [dict(r) for r in db.all_subscriptions()]
    if kind == "sale":
        row = dict(db.get_sale(record_id))
        spawn(asyncio.to_thread(sheets.push_sale, row, all_sales, all_subs))
    else:
        row = dict(db.get_subscription(record_id))
        spawn(asyncio.to_thread(sheets.push_subscription, row, all_sales, all_subs))


def resync_sheets_in_background():
    """After an edit / delete / payment / undo, rewrite the Sheet tabs so they match the database."""
    if not sheets.is_configured():
        return
    all_sales = [dict(r) for r in db.all_sales()]
    all_subs = [dict(r) for r in db.all_subscriptions()]
    spawn(asyncio.to_thread(sheets.safe_full_resync, all_sales, all_subs))
