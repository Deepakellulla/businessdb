"""
Field-by-field editor for sales and subscriptions.

    /editsale <id>        /editsub <id>        /edit sale <id>        /edit sub <id>

The bot shows the entry with one button per field. Tap a field, send the new value, and the
entry is updated and shown again, so several fields can be fixed in one go. Tap Done when finished.
"""
import logging
import math
from datetime import timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import db
from helpers import (
    LIFETIME_DAYS, ack, esc, money, normalize_phone, parse_date_input, reply_to,
    require_auth, resync_sheets_in_background,
)

logger = logging.getLogger(__name__)

CHOOSE, VALUE = range(2)
SESSION_TIMEOUT = 15 * 60  # seconds of inactivity before the edit screen closes itself

STATE_KEY = "ed"  # everything this module stores in user_data lives under this one key

# (column, button label, value type)
FIELDS = {
    "sale": [
        ("item", "Item", "text"),
        ("customer", "Customer", "text_opt"),
        ("sale_date", "Date", "date"),
        ("sold_price", "Sold price", "money"),
        ("cost_price", "Cost price", "money"),
        ("discount", "Discount", "money"),
        ("payment_method", "Payment method", "payment"),
        ("amount_paid", "Amount paid", "paid"),
        ("seller", "Seller", "text_opt"),
        ("supplier", "Supplier", "text_opt"),
    ],
    "sub": [
        ("customer", "Customer", "text"),
        ("item", "Item", "text"),
        ("phone", "Phone", "phone"),
        ("start_date", "Start date", "date"),
        ("end_date", "End date", "enddate"),
        ("sold_price", "Sold price", "money"),
        ("cost_price", "Cost price", "money"),
        ("discount", "Discount", "money"),
        ("payment_method", "Payment method", "payment"),
        ("amount_paid", "Amount paid", "paid"),
        ("seller", "Seller", "text_opt"),
        ("supplier", "Supplier", "text_opt"),
    ],
}
KIND_NAME = {"sale": "sale", "sub": "subscription"}
PAYMENT_PRESETS = {"cash": "Cash", "upi": "UPI", "card": "Card"}
USAGE = (
    "✏️ <b>Edit an entry</b>\n\n"
    "• A sale: <code>/editsale &lt;id&gt;</code>  (IDs via /sales)\n"
    "• A subscription: <code>/editsub &lt;id&gt;</code>  (IDs via /subs)\n\n"
    "You can change any field: item, customer, dates, prices, discount, payment, seller, "
    "supplier, phone…"
)


# ------------------------------------------------------------------ helpers

def _field_info(kind: str, column: str):
    for col, label, vtype in FIELDS[kind]:
        if col == column:
            return label, vtype
    return None


def _show(value) -> str:
    return esc(value) if value not in (None, "") else "—"


def _summary(kind: str, row) -> str:
    sold = row["sold_price"] or 0
    paid = row["amount_paid"] if row["amount_paid"] is not None else sold
    due = max(sold - paid, 0)
    lines = [f"✏️ <b>Editing {KIND_NAME[kind]} #{row['id']}</b>", ""]
    lines.append(f"<b>Item:</b> {_show(row['item'])}")
    lines.append(f"<b>Customer:</b> {_show(row['customer'])}")
    if kind == "sub":
        lines.append(f"<b>Phone:</b> {_show(row.get('phone'))}")
        lines.append(f"<b>Period:</b> {_show(row['start_date'])} → {_show(row['end_date'])}")
    else:
        lines.append(f"<b>Date:</b> {_show(row['sale_date'])}")
    lines.append(
        f"<b>Sold price:</b> {money(sold)} · <b>Discount:</b> {money(row.get('discount') or 0)}"
    )
    lines.append(f"<b>Cost:</b> {money(row['cost_price'])} · <b>Profit:</b> {money(row['profit'])}")
    paid_line = f"<b>Paid:</b> {money(paid)}"
    if due > 0.005:
        paid_line += f" · <b>Due:</b> {money(due)}"
    lines.append(paid_line)
    lines.append(f"<b>Payment:</b> {_show(row['payment_method'])}")
    lines.append(f"<b>Seller:</b> {_show(row.get('seller'))} · <b>Supplier:</b> {_show(row.get('supplier'))}")
    lines.append("")
    lines.append("Tap the field you want to change:")
    return "\n".join(lines)


def _keyboard(kind: str) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(label, callback_data=f"ed:f:{col}") for col, label, _ in FIELDS[kind]]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("✅ Done", callback_data="ed:done")])
    return InlineKeyboardMarkup(rows)


def _clear_state(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop(STATE_KEY, None)


def _state(context: ContextTypes.DEFAULT_TYPE):
    return context.user_data.get(STATE_KEY)


async def _show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix: str = ""):
    st = _state(context)
    row = db.get_entry(st["kind"], st["id"])
    if not row:
        _clear_state(context)
        await reply_to(update, f"That {KIND_NAME[st['kind']]} no longer exists.")
        return ConversationHandler.END
    st["field"] = None
    await reply_to(update, prefix + _summary(st["kind"], row), reply_markup=_keyboard(st["kind"]))
    return CHOOSE


# ------------------------------------------------------------------ entry points

async def _begin(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str, raw_id: str):
    try:
        entry_id = int(raw_id)
    except (TypeError, ValueError):
        await reply_to(update, "ID must be a number.")
        return ConversationHandler.END
    row = db.get_entry(kind, entry_id)
    if not row:
        await reply_to(update, f"No {KIND_NAME[kind]} with ID {entry_id}.")
        return ConversationHandler.END
    context.user_data[STATE_KEY] = {"kind": kind, "id": entry_id, "field": None}
    return await _show_menu(update, context)


async def editsale_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update):
        return ConversationHandler.END
    if not context.args:
        await reply_to(update, "Usage: <code>/editsale &lt;id&gt;</code>  (see IDs via /sales)")
        return ConversationHandler.END
    return await _begin(update, context, "sale", context.args[0])


async def editsub_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update):
        return ConversationHandler.END
    if not context.args:
        await reply_to(update, "Usage: <code>/editsub &lt;id&gt;</code>  (see IDs via /subs)")
        return ConversationHandler.END
    return await _begin(update, context, "sub", context.args[0])


async def edit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/edit sale 12  or  /edit sub 5  (plain /edit explains how)."""
    if not await require_auth(update):
        return ConversationHandler.END
    args = [a.lower() for a in (context.args or [])]
    if len(args) >= 2 and args[0] in ("sale", "sales", "sub", "subs", "subscription"):
        kind = "sale" if args[0].startswith("sale") else "sub"
        return await _begin(update, context, kind, args[1])
    await reply_to(update, USAGE)
    return ConversationHandler.END


# ------------------------------------------------------------------ buttons

def _prompt_for(kind: str, column: str, row) -> tuple:
    """Returns (text, keyboard rows) asking for the new value of one field."""
    label, vtype = _field_info(kind, column)
    current = row.get(column)
    shown = money(current) if vtype in ("money", "paid") and current is not None else _show(current)
    text = f"✏️ <b>{label}</b> — now: <b>{shown}</b>\n\n"
    rows = []
    if vtype == "text":
        text += "Send the new value:"
    elif vtype == "text_opt":
        text += "Send the new value, or tap Clear to leave it empty:"
        rows.append([InlineKeyboardButton("🗑 Clear", callback_data="ed:clear")])
    elif vtype == "phone":
        text += ("Send the phone number (with country code, or just 10 digits), "
                 "or tap Clear to remove it:")
        rows.append([InlineKeyboardButton("🗑 Clear", callback_data="ed:clear")])
    elif vtype == "money":
        text += "Send the new amount (a number):"
    elif vtype == "paid":
        sold = row["sold_price"] or 0
        text += (f"Send the total amount received so far (0 to {money(sold)}).\n"
                 "Send the full price to mark it as fully paid.")
        rows.append([InlineKeyboardButton(f"✅ Fully paid ({money(sold)})", callback_data="ed:v:full")])
    elif vtype == "date":
        text += "Send the date as <code>2026-06-15</code> or <code>15/06/2026</code>:"
    elif vtype == "enddate":
        text += ("Send the end date as <code>2026-06-15</code> or <code>15/06/2026</code>,\n"
                 "or a length from the start date like <code>30d</code>, or <code>lifetime</code>:")
    elif vtype == "payment":
        text += "Pick one, or tap Other to type it:"
        rows.append([
            InlineKeyboardButton("💵 Cash", callback_data="ed:v:cash"),
            InlineKeyboardButton("📲 UPI", callback_data="ed:v:upi"),
            InlineKeyboardButton("💳 Card", callback_data="ed:v:card"),
        ])
        rows.append([InlineKeyboardButton("✏️ Other (type it)", callback_data="ed:v:other")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="ed:back"),
                 InlineKeyboardButton("✅ Done", callback_data="ed:done")])
    return text, InlineKeyboardMarkup(rows)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    st = _state(context)
    if not st:
        await ack(update, "That screen has expired — send /editsale or /editsub again.", alert=True)
        return ConversationHandler.END
    await ack(update)
    data = query.data  # ed:f:<col> | ed:v:<token> | ed:clear | ed:back | ed:done
    kind = st["kind"]

    if data == "ed:done":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        _clear_state(context)
        await reply_to(update, f"✅ Finished editing {KIND_NAME[kind]} #{st['id']}.")
        return ConversationHandler.END

    if data == "ed:back":
        return await _show_menu(update, context)

    if data.startswith("ed:f:"):
        column = data[5:]
        if not _field_info(kind, column):
            return CHOOSE
        row = db.get_entry(kind, st["id"])
        if not row:
            _clear_state(context)
            await reply_to(update, f"That {KIND_NAME[kind]} no longer exists.")
            return ConversationHandler.END
        st["field"] = column
        st["await_other"] = False
        text, markup = _prompt_for(kind, column, row)
        await reply_to(update, text, reply_markup=markup)
        return VALUE

    # everything below needs a field to be selected
    column = st.get("field")
    if not column:
        return CHOOSE
    _, vtype = _field_info(kind, column)

    if data == "ed:clear":
        if vtype not in ("text_opt", "phone"):
            return VALUE
        return await _apply(update, context, {column: None})

    if data.startswith("ed:v:"):
        token = data[5:]
        if vtype == "payment":
            if token == "other":
                st["await_other"] = True
                await reply_to(update, "✏️ Type the payment method:")
                return VALUE
            if token in PAYMENT_PRESETS:
                return await _apply(update, context, {column: PAYMENT_PRESETS[token]})
        if vtype == "paid" and token == "full":
            row = db.get_entry(kind, st["id"])
            return await _apply(update, context, {column: row["sold_price"]})
    return VALUE


# ------------------------------------------------------------------ typed values

def _parse_text(text: str, what: str, maxlen: int = 100, optional: bool = False):
    t = text.strip()
    if optional and t in ("-", "—"):
        return None
    if not t:
        raise ValueError(f"Please send the {what}.")
    if len(t) > maxlen:
        raise ValueError(f"That's too long for the {what} (max {maxlen} characters).")
    return t


def _parse_money(text: str) -> float:
    try:
        value = float(text.strip().replace(",", ""))
    except ValueError:
        raise ValueError("Please send a valid number (for example 499 or 499.50).")
    if not math.isfinite(value) or value < 0:
        raise ValueError("The amount can't be negative.")
    return round(value, 2)


def _parse_value(kind: str, column: str, vtype: str, text: str, row) -> object:
    if vtype == "text":
        return _parse_text(text, column.replace("_", " "))
    if vtype == "text_opt":
        return _parse_text(text, column.replace("_", " "), optional=True)
    if vtype == "phone":
        if text.strip() in ("-", "—"):
            return None
        try:
            return normalize_phone(text)
        except ValueError:
            raise ValueError(
                "That doesn't look like a phone number. Send it with country code "
                "(e.g. 919876543210) or just the 10 digits, or tap Clear to remove it."
            )
    if vtype == "payment":
        return _parse_text(text, "payment method", maxlen=40)
    if vtype == "money":
        return _parse_money(text)
    if vtype == "paid":
        value = _parse_money(text)
        if value > (row["sold_price"] or 0) + 0.005:
            raise ValueError(
                f"Amount paid can't be more than the sold price ({money(row['sold_price'])}). "
                "Change the sold price first if that's what you want."
            )
        return value
    if vtype == "date":
        d = parse_date_input(text)
        if d is None:
            raise ValueError("I couldn't read that date. Try 2026-06-15 or 15/06/2026.")
        iso = d.isoformat()
        if column == "start_date" and iso > row["end_date"]:
            raise ValueError(
                f"That's after the end date ({row['end_date']}). Change the end date first."
            )
        return iso
    if vtype == "enddate":
        t = text.strip().lower()
        start = parse_date_input(row["start_date"])
        if t == "lifetime":
            return (start + timedelta(days=LIFETIME_DAYS)).isoformat()
        if t.endswith("d") and t[:-1].isdigit():
            days = int(t[:-1])
            if days < 0:
                raise ValueError("The length can't be negative.")
            return (start + timedelta(days=days)).isoformat()
        d = parse_date_input(text)
        if d is None:
            raise ValueError("I couldn't read that. Try 2026-06-15, 15/06/2026, 30d or lifetime.")
        if d.isoformat() < row["start_date"]:
            raise ValueError(f"The end date can't be before the start date ({row['start_date']}).")
        return d.isoformat()
    raise ValueError("Unsupported field.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = _state(context)
    if not st:
        return ConversationHandler.END
    column = st.get("field")
    if not column:
        await reply_to(update, "Tap a field button first (or /cancel).")
        return CHOOSE
    kind = st["kind"]
    _, vtype = _field_info(kind, column)
    row = db.get_entry(kind, st["id"])
    if not row:
        _clear_state(context)
        await reply_to(update, f"That {KIND_NAME[kind]} no longer exists.")
        return ConversationHandler.END
    try:
        value = _parse_value(kind, column, vtype, update.message.text, row)
    except ValueError as e:
        await reply_to(update, f"⚠️ {esc(e)}")
        return VALUE
    return await _apply(update, context, {column: value})


async def _apply(update: Update, context: ContextTypes.DEFAULT_TYPE, changes: dict):
    st = _state(context)
    kind = st["kind"]
    new_row = db.update_entry(kind, st["id"], changes)
    if not new_row:
        _clear_state(context)
        await reply_to(update, f"That {KIND_NAME[kind]} no longer exists.")
        return ConversationHandler.END
    resync_sheets_in_background()
    column = next(iter(changes))
    label, _ = _field_info(kind, column)
    return await _show_menu(update, context, prefix=f"✅ {label} updated.\n\n")


# ------------------------------------------------------------------ cancel / timeout / stale

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _clear_state(context)
    await reply_to(update, "❌ Cancelled — nothing more was changed.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def on_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _state(context):
        _clear_state(context)
        await reply_to(update, "⌛ Edit screen closed after 15 minutes idle. Changes you made are saved.")
    return ConversationHandler.END


async def stale_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buttons from an edit screen that has already finished (or from before a restart)."""
    await ack(update, "That edit screen has closed — send /editsale or /editsub again.", alert=True)


# ------------------------------------------------------------------ wiring

def build_conversation() -> ConversationHandler:
    button = CallbackQueryHandler(on_button, pattern=r"^ed:")
    return ConversationHandler(
        entry_points=[
            CommandHandler("editsale", editsale_cmd),
            CommandHandler("editsub", editsub_cmd),
            CommandHandler("edit", edit_cmd),
        ],
        states={
            CHOOSE: [button],
            VALUE: [button, MessageHandler(filters.TEXT & ~filters.COMMAND, on_text)],
            ConversationHandler.TIMEOUT: [
                MessageHandler(filters.ALL, on_timeout), CallbackQueryHandler(on_timeout),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=SESSION_TIMEOUT,
        allow_reentry=True,
    )


def stale_handler() -> CallbackQueryHandler:
    return CallbackQueryHandler(stale_button, pattern=r"^ed:")
