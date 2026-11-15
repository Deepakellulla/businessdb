"""
Step-by-step entry flows: /newsale, /newsub and /addproduct.

All three run on one small engine. A flow is just an ordered list of Steps; the engine asks
them one at a time, validates the answer (typed or tapped), then shows a review screen with
Confirm / Start Over / Cancel before anything is saved.

Every button carries the step it belongs to ("f:<step>:<token>"), so tapping a button left
over from an earlier step is ignored instead of corrupting the current one.
"""
import logging
import math
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import alerts
import compensation
import db
import tgsync
from config import MIN_MARGIN_PCT, REMINDER_DAYS_BEFORE
from helpers import (
    LIFETIME_DAYS,
    ack,
    display_name,
    esc,
    is_lifetime_duration,
    is_owner,
    money,
    normalize_ig_username,
    normalize_phone,
    normalize_platform,
    parse_date_input,
    parse_tg_contact,
    push_new_to_sheets,
    reply_to,
    require_auth,
)

logger = logging.getLogger(__name__)

FLOW, CONFIRM = range(2)  # the only two conversation states; which form is running lives in user_data


class Stay:
    """Returned by a button handler to say 'don't advance, just show this message'."""

    def __init__(self, message: str):
        self.message = message


@dataclass
class Step:
    key: str                                              # id, and the user_data key the answer is stored under
    prompt: Callable[[dict], str]                         # question text
    parse: Optional[Callable[[str, dict], object]] = None  # typed answer -> value (raise ValueError(msg) if bad)
    buttons: Optional[Callable[[dict], list]] = None      # -> rows of (label, token)
    pick: Optional[Callable[[str, dict], object]] = None  # tapped token -> value (or Stay / raise ValueError)
    when: Optional[Callable[[dict], bool]] = None         # step only runs when this is true
    numbered: bool = True                                 # counts toward "Step n/total"
    store: Optional[Callable[[dict, object], None]] = None
    after: Optional[Callable] = None                      # async (update, context, d) run right after the answer is stored


@dataclass
class Flow:
    kind: str
    title: str
    icon: str
    steps: list
    summary: Callable[[dict], str]
    save: Callable[[Update, dict], str]   # does the DB write, returns the success message
    owner_only: bool = False
    # optional abilities
    async_save: Optional[Callable] = None     # async (update, context, d) -> message or (message, keyboard); used instead of save
    dup_check: Optional[Callable] = None      # (update, d) -> warning text if this looks like a duplicate, else None
    confirm_label: str = "✅ Confirm & Save"


# ============================================================ validation helpers

def _num(text: str, *, what="a number", min_value=0.0, strictly_positive=False) -> float:
    try:
        v = float(text.strip().replace(",", ""))
    except ValueError:
        raise ValueError(f"Please send {what} — digits only, like 250 or 99.50.")
    if not math.isfinite(v):
        raise ValueError(f"Please send {what} — digits only, like 250 or 99.50.")
    if v < min_value or (strictly_positive and v <= 0):
        raise ValueError("That can't be negative." if not strictly_positive else "That must be more than zero.")
    return v


def _name(text: str, what: str, maxlen=80) -> str:
    t = text.strip()
    if not t:
        raise ValueError(f"Please send the {what}.")
    if len(t) > maxlen:
        raise ValueError(f"That's too long for a {what} (max {maxlen} characters).")
    return t


def _net(d: dict) -> float:
    return d["gross_price"] - d["discount"]


def _chunk(buttons: list, per_row: int = 2) -> list:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def _opts(d: dict, key: str, values: list) -> list:
    d.setdefault("_opts", {})[key] = values
    return values


# ============================================================ step builders

def s_item(pkind: str) -> Step:
    def buttons(d):
        prods = db.list_products(pkind)[:10]
        _opts(d, "item", [p["id"] for p in prods])
        return _chunk([(p["name"][:26], f"p{p['id']}") for p in prods])

    def prompt(d):
        text = "🏷 What item/product was sold?" if pkind == "sale" else "🏷 What subscription/plan is this?"
        if d.get("_opts", {}).get("item"):
            text += "\n\nTap a saved product (it pre-fills price, cost" + \
                    (" & duration" if pkind == "sub" else "") + ") or just type a name."
        return text

    def pick(token, d):
        try:
            prod = db.get_product(int(token[1:]))
        except ValueError:
            prod = None
        if not prod:
            raise ValueError("That saved product no longer exists — type the name instead.")
        d["defaults"] = {
            "price": prod["default_price"], "cost": prod["default_cost"],
            "duration": prod["default_duration"], "supplier": prod["default_supplier"],
        }
        return prod["name"]

    return Step("item", prompt, parse=lambda t, d: _name(t, "item name"), buttons=buttons, pick=pick)


def s_customer(required: bool) -> Step:
    def parse(text, d):
        if not required and text.strip() == "-":
            return None
        return _name(text, "customer name")

    return Step(
        "customer",
        lambda d: "👤 Customer name?" + ("" if required else " (or tap Skip)"),
        parse=parse,
        buttons=None if required else (lambda d: [[("⏭ Skip", "skip")]]),
        pick=lambda token, d: None,
    )


def s_phone() -> Step:
    def buttons(d):
        saved = db.customer_phone(d["customer"])
        d["_saved_phone"] = saved
        rows = []
        if saved:
            rows.append([(f"✅ Use saved +{saved}", "saved")])
        rows.append([("⏭ Skip", "skip")])
        return rows

    def pick(token, d):
        return d.get("_saved_phone") if token == "saved" else None

    return Step(
        "phone",
        lambda d: "📱 Customer's WhatsApp number? Include the country code (e.g. 919876543210) "
                  "or just the 10 digits.\nRenewal reminders get a one-tap WhatsApp button when this is set.",
        parse=lambda t, d: normalize_phone(t),
        buttons=buttons,
        pick=pick,
    )


def _on(platform: str):
    """Condition for a contact step: only ask when the customer bought via this platform."""
    return lambda d: d.get("platform") == platform


def s_platform() -> Step:
    """Where did the customer buy from? WhatsApp / Telegram / Instagram / anything else (typed)."""
    names = {"wa": "WhatsApp", "tg": "Telegram", "ig": "Instagram"}

    def buttons(d):
        last = db.customer_platform(d.get("customer"))
        d["_last_platform"] = last
        rows = []
        if last:
            rows.append([(f"✅ Same as before: {last}"[:60], "last")])
        rows.append([("📱 WhatsApp", "wa"), ("✈️ Telegram", "tg"), ("📸 Instagram", "ig")])
        rows.append([("🌐 Other (type it)", "other"), ("⏭ Skip", "skip")])
        return rows

    def pick(token, d):
        if token == "other":
            return Stay("✏️ Type the platform name (e.g. Facebook, Website, Walk-in):")
        if token == "skip":
            return None
        if token == "last":
            return d["_last_platform"]
        return names[token]

    return Step(
        "platform",
        lambda d: "🛒 Which platform did they buy from?\n"
                  "I'll then ask for their contact there — WhatsApp → phone number, Telegram → ID or "
                  "username, Instagram → username.",
        parse=lambda t, d: normalize_platform(t),
        buttons=buttons,
        pick=pick,
    )


def s_ig() -> Step:
    def buttons(d):
        saved = db.customer_ig(d.get("customer"))
        d["_saved_ig"] = saved
        rows = []
        if saved:
            rows.append([(f"✅ Use saved @{saved}"[:60], "saved")])
        rows.append([("⏭ Skip", "skip")])
        return rows

    def parse(text, d):
        if text.strip() == "-":
            return None
        return normalize_ig_username(text)

    return Step(
        "ig_username",
        lambda d: "📸 Customer's Instagram username? (e.g. @alice.shop) — or tap Skip.\n"
                  "Renewal reminders get a button that opens their Instagram profile.",
        parse=parse,
        buttons=buttons,
        pick=lambda token, d: d.get("_saved_ig") if token == "saved" else None,
    )


def _tg_label(d: dict) -> str:
    """'@alice', 'ID 123456789' or 'ID 123456789 (username not found yet)' for review screens."""
    uname, tid = d.get("tg_username"), d.get("tg_id")
    if uname and tid:
        return f"@{esc(uname)} (ID {tid})"
    if uname:
        return f"@{esc(uname)}"
    if tid:
        return f"ID {tid} (username not found yet)"
    return ""


def s_tg() -> Step:
    """Customer's Telegram: a numeric ID (preferred) or a @username. Always optional."""

    def buttons(d):
        saved = db.customer_tg(d.get("customer"))
        d["_saved_tg"] = saved
        rows = []
        if saved:
            label = f"@{saved['tg_username']}" if saved["tg_username"] else f"ID {saved['tg_id']}"
            rows.append([(f"✅ Use saved {label}"[:60], "saved")])
        rows.append([("⏭ Skip", "skip")])
        return rows

    def pick(token, d):
        return d.get("_saved_tg") if token == "saved" else None

    def parse(text, d):
        if text.strip() == "-":
            return None
        return parse_tg_contact(text)

    def store(d, value):
        d["tg_id"] = value["tg_id"] if value else None
        d["tg_username"] = value["tg_username"] if value else None

    async def after(update, context, d):
        """Right after the answer: if we have an ID, fetch the username now and say how it went."""
        if not d.get("tg_id"):
            return
        status, username = await tgsync.lookup(context.bot, d["tg_id"])
        if status == tgsync.OK:
            d["tg_username"] = username
            if username:
                await reply_to(update, f"✅ Found them: <b>@{esc(username)}</b> — I'll keep it updated if they change it.")
            else:
                await reply_to(update, "ℹ️ Found them, but they don't have a public username set. "
                                       "I've saved their ID and will pick the username up if they add one.")
        elif status == tgsync.UNREACHABLE:
            bot_name = context.bot.username
            link = f"\nAsk them to open https://t.me/{bot_name} and press <b>Start</b> once." if bot_name else \
                "\nAsk them to open this bot and press <b>Start</b> once."
            await reply_to(update, f"ℹ️ Saved ID <code>{d['tg_id']}</code>, but I can't see this person on Telegram yet.{link}\n"
                                   "Their username is filled in automatically by the daily check after that.")
        else:
            await reply_to(update, "⚠️ Couldn't reach Telegram just now. The ID is saved and I'll look the "
                                   "username up again in the daily check.")

    return Step(
        "tg",
        lambda d: "✈️ Customer's Telegram — optional.\n"
                  "Best: send their <b>numeric ID</b> (they get it by sending /myid to this bot). I'll fetch their "
                  "username and keep it updated if they change it.\n"
                  "Or just send their @username. Tap Skip to leave it out.",
        parse=parse,
        buttons=buttons,
        pick=pick,
        store=store,
        after=after,
    )


def s_seller() -> Step:
    def buttons(d):
        sellers = _opts(d, "seller", db.recent_sellers(5))
        rows = _chunk([(s[:24], f"s{i}") for i, s in enumerate(sellers)])
        rows.append([("⏭ Skip", "skip")])
        return rows

    def pick(token, d):
        if token == "skip":
            return None
        return d["_opts"]["seller"][int(token[1:])]

    return Step(
        "seller",
        lambda d: "🧑‍💼 Who sold this? Tap a recent seller, type a name, or Skip.",
        parse=lambda t, d: _name(t, "seller name", 60),
        buttons=buttons,
        pick=pick,
    )


def s_date() -> Step:
    def parse(text, d):
        parsed = parse_date_input(text)
        if parsed is None:
            raise ValueError("Couldn't read that date. Try 2026-06-15 or 15-06-2026, or tap Today.")
        if parsed > date.today():
            raise ValueError("That date is in the future — send today's date or an earlier one.")
        return parsed

    return Step(
        "date",
        lambda d: "📅 When was this sold? Tap Today, or type a date (e.g. 2026-06-15 or 15-06-2026) "
                  "to backfill — profit counts toward that month.",
        parse=parse,
        buttons=lambda d: [[("📅 Today", "today")]],
        pick=lambda token, d: date.today(),
    )


def s_gross(kind: str) -> Step:
    def buttons(d):
        price = d.get("defaults", {}).get("price")
        return [[(f"✅ Use {money(price)}", "def")]] if price is not None else []

    return Step(
        "gross_price",
        lambda d: "💰 Price charged (before any discount)?",
        parse=lambda t, d: _num(t, what="the price"),
        buttons=buttons,
        pick=lambda token, d: d["defaults"]["price"],
    )


def s_discount() -> Step:
    def parse(text, d):
        v = _num(text, what="the discount amount (0 for none)")
        if v > d["gross_price"]:
            raise ValueError(f"The discount can't be more than the price ({money(d['gross_price'])}).")
        return v

    return Step(
        "discount",
        lambda d: "🏷 Any discount given? Send the amount, or tap for none.",
        parse=parse,
        buttons=lambda d: [[("0️⃣ No discount", "none")]],
        pick=lambda token, d: 0.0,
    )


def s_cost(kind: str) -> Step:
    def buttons(d):
        cost = d.get("defaults", {}).get("cost")
        return [[(f"✅ Use {money(cost)}", "def")]] if cost is not None else []

    return Step(
        "cost_price",
        lambda d: "💵 Cost price? (what it cost you)" if kind == "sale"
        else "💵 Your cost price for this subscription?",
        parse=lambda t, d: _num(t, what="the cost price"),
        buttons=buttons,
        pick=lambda token, d: d["defaults"]["cost"],
    )


def s_supplier(key: str = "supplier", use_defaults: bool = True) -> Step:
    def buttons(d):
        rows = []
        default = d.get("defaults", {}).get("supplier") if use_defaults else None
        if default:
            rows.append([(f"✅ {default[:26]}", "def")])
        recent = [s for s in db.recent_suppliers(5) if s != default]
        _opts(d, key, recent)
        rows += _chunk([(s[:24], f"u{i}") for i, s in enumerate(recent)])
        rows.append([("⏭ Skip", "skip")])
        return rows

    def pick(token, d):
        if token == "skip":
            return None
        if token == "def":
            return d["defaults"]["supplier"]
        return d["_opts"][key][int(token[1:])]

    return Step(
        key,
        lambda d: "🏭 Where did you buy it from (supplier)? Tap one, type a name, or Skip.\n"
                  "This lets /suppliers show who's cheapest and who gives the best margin.",
        parse=lambda t, d: _name(t, "supplier name", 60),
        buttons=buttons,
        pick=pick,
    )


def s_payment() -> Step:
    def pick(token, d):
        if token == "other":
            return Stay("✏️ Type the payment method:")
        return token

    return Step(
        "payment_method",
        lambda d: "💳 Payment method?",
        parse=lambda t, d: _name(t, "payment method", 40),
        buttons=lambda d: [
            [("💵 Cash", "Cash"), ("📲 UPI", "UPI"), ("💳 Card", "Card")],
            [("✏️ Other (type it)", "other")],
        ],
        pick=pick,
    )


def s_pstatus() -> Step:
    def store(d, value):
        d["pstatus"] = value
        d.pop("amount_paid", None)
        if value == "paid":
            d["amount_paid"] = _net(d)
        elif value == "unpaid":
            d["amount_paid"] = 0.0

    return Step(
        "pstatus",
        lambda d: f"💰 Has the customer paid? (amount due: <b>{money(_net(d))}</b>)",
        parse=None,
        buttons=lambda d: [
            [("✅ Paid in full", "paid")],
            [("🟡 Paid part", "part"), ("🔴 Not paid yet", "unpaid")],
        ],
        pick=lambda token, d: token,
        store=store,
    )


def s_partial() -> Step:
    def parse(text, d):
        v = _num(text, what="the amount received", strictly_positive=True)
        if v >= _net(d):
            raise ValueError(f"That's the full amount ({money(_net(d))}) — go back and choose “Paid in full”, "
                             f"or send a smaller number.")
        return v

    return Step(
        "amount_paid",
        lambda d: f"🟡 How much have they paid so far? (total is {money(_net(d))})",
        parse=parse,
        when=lambda d: d.get("pstatus") == "part",
        numbered=False,
    )


def s_duration(key: str = "duration", use_defaults: bool = True) -> Step:
    def label(days):
        return "Lifetime" if is_lifetime_duration(days) else f"{days} days"

    def buttons(d):
        rows = []
        saved = d.get("defaults", {}).get("duration") if use_defaults else None
        if saved:
            rows.append([(f"✅ Saved: {label(saved)}", f"d{saved}")])
        rows.append([("30 days", "d30"), ("90 days", "d90"), ("365 days", "d365")])
        rows.append([("🔓 Lifetime (no expiry)", "life")])
        return rows

    def pick(token, d):
        return LIFETIME_DAYS if token == "life" else int(token[1:])

    def parse(text, d):
        t = text.strip().lower()
        if t in ("lifetime", "forever", "never", "no expiry", "no expiration", "unlimited"):
            return LIFETIME_DAYS
        try:
            days = int(t)
        except ValueError:
            raise ValueError('Send a whole number of days, tap a preset, or type "lifetime".')
        if days <= 0:
            raise ValueError("Duration must be at least 1 day.")
        return days

    return Step(
        key,
        lambda d: "📅 How long does it last? Tap a preset, type the number of days, or type \"lifetime\".",
        parse=parse,
        buttons=buttons,
        pick=pick,
    )


# ============================================================ summaries & saving

def _margin_warning(sold: float, profit: float) -> str:
    if sold <= 0:
        return "⚠️ <b>Nothing is being charged</b> — double-check the price."
    if profit < 0:
        return f"🚨 <b>Loss-making:</b> you'd lose {money(-profit)} on this."
    margin = profit / sold * 100
    if margin < MIN_MARGIN_PCT:
        return f"⚠️ <b>Low margin:</b> {margin:.1f}% (your alert level is {MIN_MARGIN_PCT:g}%)."
    return ""


def _payment_line(d: dict) -> str:
    sold = _net(d)
    paid = d["amount_paid"]
    due = max(sold - paid, 0.0)
    pm = esc(d["payment_method"])
    if d["pstatus"] == "paid":
        return f"💳 Payment: {pm} — ✅ paid in full"
    if d["pstatus"] == "part":
        return f"💳 Payment: {pm} — 🟡 {money(paid)} paid, <b>{money(due)} due</b>"
    return f"💳 Payment: {pm} — 🔴 <b>{money(due)} due</b> (nothing paid yet)"


def _money_lines(d: dict) -> list:
    sold = _net(d)
    profit = sold - d["cost_price"]
    price_line = f"💰 Price: {money(d['gross_price'])}"
    if d["discount"]:
        price_line += f" (−{money(d['discount'])} discount)"
    lines = [price_line, f"💵 Cost: {money(d['cost_price'])}"]
    if d.get("supplier"):
        lines.append(f"🏭 Supplier: {esc(d['supplier'])}")
    lines.append(_payment_line(d))
    lines += ["", f"➡️ Net: <b>{money(sold)}</b>", f"➡️ Profit: <b>{money(profit)}</b>"]
    warn = _margin_warning(sold, profit)
    if warn:
        lines += ["", warn]
    return lines


def _contact_lines(d: dict) -> list:
    """Platform + whichever contact was collected, for the review screens."""
    lines = []
    if d.get("platform"):
        lines.append(f"🛒 Bought via: <b>{esc(d['platform'])}</b>")
    if d.get("phone"):
        lines.append(f"📱 WhatsApp: +{d['phone']}")
    if _tg_label(d):
        lines.append(f"✈️ Telegram: {_tg_label(d)}")
    if d.get("ig_username"):
        lines.append(f"📸 Instagram: @{esc(d['ig_username'])}")
    return lines


def _sale_summary(d: dict) -> str:
    lines = ["🔎 <b>Review Sale</b>", ""]
    lines.append(f"🏷 Item: <b>{esc(d['item'])}</b>")
    lines.append(f"👤 Customer: {esc(d['customer']) if d['customer'] else '—'}")
    lines += _contact_lines(d)
    if d.get("seller"):
        lines.append(f"🧑‍💼 Seller: {esc(d['seller'])}")
    lines.append(f"📅 Sold on: {d['date'].isoformat()}")
    return "\n".join(lines + _money_lines(d))


def _sub_summary(d: dict) -> str:
    start = d["date"]
    end = start.fromordinal(start.toordinal() + d["duration"])
    lines = ["🔎 <b>Review Subscription</b>", ""]
    lines.append(f"👤 Customer: <b>{esc(d['customer'])}</b>")
    lines += _contact_lines(d)
    lines.append(f"🏷 Item: <b>{esc(d['item'])}</b>")
    if d.get("seller"):
        lines.append(f"🧑‍💼 Seller: {esc(d['seller'])}")
    lines.append(f"📅 Sold on: {start.isoformat()}")
    if is_lifetime_duration(d["duration"]):
        lines.append("⏳ Duration: <b>Lifetime</b> (no expiry, no renewal reminders)")
    else:
        lines.append(f"⏳ Duration: {d['duration']} days (ends {end.isoformat()})")
    return "\n".join(lines + _money_lines(d))


def _save_sale(update: Update, d: dict) -> str:
    sold = _net(d)
    sale_id, profit = db.add_sale(
        d["item"], d["customer"], sold, d["cost_price"], logged_by=display_name(update),
        discount=d["discount"], gross_price=d["gross_price"], payment_method=d["payment_method"],
        sale_date=d["date"], seller=d.get("seller"), supplier=d.get("supplier"),
        amount_paid=d["amount_paid"], tg_username=d.get("tg_username"), tg_id=d.get("tg_id"),
        platform=d.get("platform"), ig_username=d.get("ig_username"), phone=d.get("phone"),
    )
    linked = db.link_customer_tg(d.get("customer"), d.get("tg_id"), d.get("tg_username"))
    push_new_to_sheets("sale", sale_id)
    msg = f"✅ <b>Sale #{sale_id} saved!</b>\nProfit: <b>{money(profit)}</b>"
    if linked:
        msg += f"\n🔗 Also linked their Telegram ID to {linked} older entr{'y' if linked == 1 else 'ies'}."
    due = sold - d["amount_paid"]
    if due > 0.005:
        msg += f"\n\n🔴 {money(due)} still due — record it later with <code>/receive sale {sale_id}</code>."
    return msg


def _save_sub(update: Update, d: dict) -> str:
    sold = _net(d)
    sub_id, end_date, profit = db.add_subscription(
        d["customer"], d["item"], sold, d["cost_price"], d["date"], d["duration"],
        logged_by=display_name(update), discount=d["discount"], gross_price=d["gross_price"],
        payment_method=d["payment_method"], seller=d.get("seller"), supplier=d.get("supplier"),
        amount_paid=d["amount_paid"], phone=d.get("phone"), tg_username=d.get("tg_username"),
        tg_id=d.get("tg_id"), platform=d.get("platform"), ig_username=d.get("ig_username"),
    )
    linked = db.link_customer_tg(d.get("customer"), d.get("tg_id"), d.get("tg_username"))
    push_new_to_sheets("sub", sub_id)
    converted = None
    if d.get("trial_id") and db.set_trial_status(d["trial_id"], "converted", sub_id):
        converted = d["trial_id"]
    elif d.get("customer"):
        converted = db.convert_matching_trial(d["customer"], d["item"], sub_id)

    if is_lifetime_duration(d["duration"]):
        msg = f"✅ <b>Subscription #{sub_id} saved!</b>\nDuration: <b>Lifetime</b> · Profit: <b>{money(profit)}</b>\n\n"
        msg += "🔓 No expiry — this subscription won't trigger any renewal reminders."
    else:
        msg = f"✅ <b>Subscription #{sub_id} saved!</b>\nEnds {end_date.isoformat()} · Profit: <b>{money(profit)}</b>\n\n"
        if end_date <= date.today():
            msg += (
                "📌 This subscription's term already ended based on the date you entered — "
                "it'll show as inactive after today's reminder check runs, and you may get "
                "an \"ended\" notification for it. That's expected for backfilled data."
            )
        else:
            msg += f"I'll remind you {REMINDER_DAYS_BEFORE} days before it ends, and on the day"
            extras = (["💬 WhatsApp"] if d.get("phone") else []) + (["✈️ Telegram"] if d.get("tg_username") else []) \
                + (["📸 Instagram"] if d.get("ig_username") else [])
            msg += " — with 🔁 Renew" + (" and " + " + ".join(extras) + " buttons." if extras else " buttons.")
    if linked:
        msg += f"\n🔗 Also linked their Telegram ID to {linked} older entr{'y' if linked == 1 else 'ies'}."
    if converted:
        msg += f"\n🎉 Free trial #{converted} marked as converted."
    due = sold - d["amount_paid"]
    if due > 0.005:
        msg += f"\n\n🔴 {money(due)} still due — record it later with <code>/receive sub {sub_id}</code>."
    return msg


def _prod_summary(d: dict) -> str:
    lines = ["🔎 <b>Review Product</b>", ""]
    lines.append(f"🏷 Name: <b>{esc(d['pname'])}</b> ({'subscription plan' if d['pkind'] == 'sub' else 'one-time item'})")
    lines.append(f"💰 Default price: {money(d['pprice'])}")
    lines.append(f"💵 Default cost: {money(d['pcost'])}")
    if d["pkind"] == "sub":
        days = d["pduration"]
        lines.append("⏳ Duration: Lifetime" if is_lifetime_duration(days) else f"⏳ Duration: {days} days")
    if d.get("psupplier"):
        lines.append(f"🏭 Supplier: {esc(d['psupplier'])}")
    lines.append("")
    lines.append(f"➡️ Default profit: <b>{money(d['pprice'] - d['pcost'])}</b>")
    return "\n".join(lines)


def _save_prod(update: Update, d: dict) -> str:
    duration = d.get("pduration") if d["pkind"] == "sub" else None
    pid, created = db.save_product(d["pkind"], d["pname"], d["pprice"], d["pcost"], duration, d.get("psupplier"))
    verb = "saved" if created else "updated"
    return f"✅ <b>Product #{pid} {verb}:</b> {esc(d['pname'])}\nIt now appears as a tap button in /{'newsale' if d['pkind'] == 'sale' else 'newsub'}."


# ============================================================ duplicate warnings

def _ago(ts) -> str:
    if not ts:
        return ""
    secs = (datetime.now(ts.tzinfo) - ts).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)} h ago"
    return f"{int(secs // 86400)} day{'s' if secs >= 172800 else ''} ago"


def _dup_warning(kind: str, d: dict, rows: list, update: Update) -> Optional[str]:
    if not rows:
        return None
    who = esc(d["customer"]) if d.get("customer") else "no customer"
    day = d["date"].isoformat()
    lines = ["⚠️ <b>This looks like a duplicate</b>", "",
             f"You're about to save: <b>{esc(d['item'])}</b> for {who} on {day} — {money(_net(d))}",
             "", "Already saved:"]
    for r in rows:
        when = _ago(r.get("created_at"))
        by = f" by {esc(r['logged_by'])}" if r.get("logged_by") else ""
        lines.append(f"• #{r['id']} · {esc(r['customer']) if r.get('customer') else 'no customer'} · {esc(r['item'])} · "
                     f"{r['sale_date'] if kind == 'sale' else r['start_date']} · {money(r['sold_price'])}"
                     + (f" · logged {when}{by}" if when else ""))
    lines += ["", "Save it anyway, or go back?"]
    return "\n".join(lines)


def _dup_sale(update: Update, d: dict) -> Optional[str]:
    rows = db.find_duplicates("sale", d.get("customer"), d["item"], d["date"].isoformat(), _net(d), display_name(update))
    return _dup_warning("sale", d, [dict(r) for r in rows], update)


def _dup_sub(update: Update, d: dict) -> Optional[str]:
    rows = db.find_duplicates("sub", d.get("customer"), d["item"], d["date"].isoformat(), _net(d), display_name(update))
    return _dup_warning("sub", d, [dict(r) for r in rows], update)


def _dup_trial(update: Update, d: dict) -> Optional[str]:
    t = db.find_active_trial(d["customer"], d["item"])
    if not t:
        return None
    return (f"⚠️ <b>{esc(t['customer'])}</b> already has a running trial of <b>{esc(t['item'])}</b> "
            f"(#{t['id']}, ends {t['end_date']}).\n\nStart another one anyway?")


# ============================================================ free trial

def s_trial_days() -> Step:
    def parse(text, d):
        try:
            n = int(text.strip())
        except ValueError:
            raise ValueError("Send a whole number of days, for example 7.")
        if not 1 <= n <= 60:
            raise ValueError("A trial can be 1 to 60 days.")
        return n

    return Step(
        "trial_days",
        lambda d: "⏳ How many days is the trial? Tap one, or type a number.",
        parse=parse,
        buttons=lambda d: [[("1 day", "t1"), ("3 days", "t3")], [("7 days", "t7"), ("14 days", "t14")]],
        pick=lambda token, d: int(token[1:]),
    )


def s_trial_cost() -> Step:
    def buttons(d):
        rows = [[("🆓 Free — costs me nothing", "zero")]]
        cost = d.get("defaults", {}).get("cost")
        if cost:
            rows.append([(f"Use the product cost {money(cost)}", "def")])
        return rows

    return Step(
        "trial_cost",
        lambda d: "💵 What does this trial cost you (for example the account you hand over)? "
                  "Tap Free, or type an amount.",
        parse=lambda t, d: _num(t, what="your cost"),
        buttons=buttons,
        pick=lambda token, d: 0.0 if token == "zero" else d["defaults"]["cost"],
    )


def _trial_summary(d: dict) -> str:
    end = date.today().toordinal() + d["trial_days"]
    lines = ["🔎 <b>Review Free Trial</b>", "", f"👤 Customer: {esc(d['customer'])}"]
    lines += _contact_lines(d)
    lines.append(f"🏷 Item: <b>{esc(d['item'])}</b>")
    if d.get("seller"):
        lines.append(f"🧑‍💼 Seller: {esc(d['seller'])}")
    lines.append(f"⏳ Trial: <b>{d['trial_days']} day{'s' if d['trial_days'] != 1 else ''}</b> "
                 f"(today → {date.fromordinal(end).isoformat()})")
    lines.append(f"💵 Your cost: {money(d['trial_cost'])}" + (" (free)" if d["trial_cost"] <= 0.005 else ""))
    return "\n".join(lines)


def _save_trial(update: Update, d: dict) -> str:
    trial_id, end = db.add_trial(
        d["customer"], d["item"], d["trial_days"], cost_price=d["trial_cost"], seller=d.get("seller"),
        logged_by=display_name(update), platform=d.get("platform"), phone=d.get("phone"),
        tg_id=d.get("tg_id"), tg_username=d.get("tg_username"), ig_username=d.get("ig_username"),
    )
    linked = db.link_customer_tg(d.get("customer"), d.get("tg_id"), d.get("tg_username"))
    msg = (f"✅ <b>Trial #{trial_id} saved!</b>\nEnds {end.isoformat()}.\n\n"
           "I'll remind you the day before and on the last day, with 🔁 Convert to paid and 🙅 Not interested buttons. "
           "If you log a paid subscription for them meanwhile, it counts as converted automatically.")
    if linked:
        msg += f"\n🔗 Also linked their Telegram ID to {linked} older entr{'y' if linked == 1 else 'ies'}."
    return msg


# ============================================================ outage compensation

def _match_comp_item(text: str) -> str:
    """Typed product name -> the exact name of a product that has running subscriptions."""
    items = db.compensation_items()
    if not items:
        raise ValueError("There are no running subscriptions right now, so there's nothing to extend.")
    t = text.strip().lower()
    exact = [name for name, _ in items if name.lower() == t]
    if exact:
        return exact[0]
    partial = [name for name, _ in items if t and t in name.lower()]
    if len(partial) == 1:
        return partial[0]
    listing = ", ".join(f"{name} ({n})" for name, n in items[:12])
    raise ValueError(f"I couldn't match “{text.strip()}” to one product. Running now: {listing}")


def s_comp_item() -> Step:
    def buttons(d):
        items = db.compensation_items()[:10]
        _opts(d, "citem", [name for name, _ in items])
        return _chunk([(f"{name[:20]} ({n})", f"i{i}") for i, (name, n) in enumerate(items)])

    return Step(
        "citem",
        lambda d: "📦 Which product had the outage? Tap one (the number is how many running subscriptions it has), "
                  "or type the name.",
        parse=lambda t, d: _match_comp_item(t),
        buttons=buttons,
        pick=lambda token, d: d["_opts"]["citem"][int(token[1:])],
    )


def s_comp_days() -> Step:
    def parse(text, d):
        try:
            n = int(text.strip())
        except ValueError:
            raise ValueError("Send a whole number of days, for example 2.")
        if not 1 <= n <= 90:
            raise ValueError("Add between 1 and 90 days.")
        return n

    return Step(
        "days",
        lambda d: f"➕ How many days to add to every running {esc(d['citem'])} subscription?",
        parse=parse,
        buttons=lambda d: [[("1", "n1"), ("2", "n2"), ("3", "n3")], [("5", "n5"), ("7", "n7"), ("14", "n14")]],
        pick=lambda token, d: int(token[1:]),
    )


def s_comp_reason() -> Step:
    return Step(
        "reason",
        lambda d: "📝 Reason, to include in the message customers get? (e.g. “server outage on 10 Nov”) — or tap Skip.",
        parse=lambda t, d: _name(t, "reason", 100),
        buttons=lambda d: [[("⏭ Skip", "skip")]],
        pick=lambda token, d: None,
    )


def s_comp_notify() -> Step:
    def prompt(d):
        rows = db.compensation_targets(d["citem"])
        customers = {r["customer"].strip().lower() for r in rows}
        with_tg = {r["customer"].strip().lower() for r in rows if r.get("tg_id")}
        return (f"📣 Tell the customers? {len(with_tg)} of {len(customers)} have a Telegram ID, so I can message "
                "them myself (if they've started this bot). For everyone else I'll give you one-tap buttons "
                "with the message already written.")

    return Step(
        "notify", prompt,
        buttons=lambda d: [[("📣 Yes, notify them", "yes")], [("🤫 No, just extend", "no")]],
        pick=lambda token, d: token == "yes",
    )


def _comp_summary(d: dict) -> str:
    rows = db.compensation_targets(d["citem"])
    customers = {r["customer"].strip().lower() for r in rows}
    lines = ["🔎 <b>Review Outage Compensation</b>", "",
             f"📦 Product: <b>{esc(d['citem'])}</b>",
             f"➕ Days added: <b>{d['days']}</b>",
             f"👥 Running subscriptions affected: <b>{len(rows)}</b> ({len(customers)} customer"
             f"{'s' if len(customers) != 1 else ''})"]
    for r in rows[:5]:
        new = date.fromisoformat(r["end_date"]).toordinal() + d["days"]
        lines.append(f"   • {esc(r['customer'])}: {r['end_date']} → {date.fromordinal(new).isoformat()}")
    if len(rows) > 5:
        lines.append(f"   …and {len(rows) - 5} more")
    if d.get("reason"):
        lines.append(f"📝 Reason: {esc(d['reason'])}")
    lines.append(f"📣 Notify customers: <b>{'yes' if d.get('notify') else 'no'}</b>")
    lines += ["", "⚠️ This changes the end date of every subscription above. You can undo it afterwards."]
    return "\n".join(lines)


def _save_comp(update: Update, d: dict) -> str:  # never used: Flow.async_save does the work
    raise RuntimeError("compensation is saved through async_save")


async def _async_save_comp(update: Update, context: ContextTypes.DEFAULT_TYPE, d: dict):
    return await compensation.apply_and_notify(
        context.bot, d["citem"], d["days"], d.get("reason"), bool(d.get("notify")), display_name(update),
    )


# ============================================================ flow definitions

def _pkind_step() -> Step:
    return Step(
        "pkind",
        lambda d: "What kind of product is this?",
        parse=None,
        buttons=lambda d: [[("🧾 One-time item", "sale")], [("📋 Subscription plan", "sub")]],
        pick=lambda token, d: token,
    )


FLOWS = {
    "sale": Flow(
        "sale", "New Sale", "🧾",
        [s_item("sale"), s_customer(False), s_platform(),
         replace(s_phone(), when=_on("WhatsApp")), replace(s_tg(), when=_on("Telegram")),
         replace(s_ig(), when=_on("Instagram")), s_seller(), s_date(),
         s_gross("sale"), s_discount(),
         s_cost("sale"), s_supplier(), s_payment(), s_pstatus(), s_partial()],
        _sale_summary, _save_sale, dup_check=_dup_sale,
    ),
    "sub": Flow(
        "sub", "New Subscription", "📋",
        [s_customer(True), s_platform(),
         replace(s_phone(), when=_on("WhatsApp")), replace(s_tg(), when=_on("Telegram")),
         replace(s_ig(), when=_on("Instagram")), s_item("sub"), s_seller(), s_date(), s_gross("sub"),
         s_discount(),
         s_cost("sub"), s_supplier(), s_payment(), s_pstatus(), s_partial(), s_duration()],
        _sub_summary, _save_sub, dup_check=_dup_sub,
    ),
    "trial": Flow(
        "trial", "Free Trial", "🧪",
        [s_customer(True), s_platform(),
         replace(s_phone(), when=_on("WhatsApp")), replace(s_tg(), when=_on("Telegram")),
         replace(s_ig(), when=_on("Instagram")), s_item("sub"), s_seller(), s_trial_days(), s_trial_cost()],
        _trial_summary, _save_trial, dup_check=_dup_trial,
    ),
    "comp": Flow(
        "comp", "Outage Compensation", "🛠",
        [s_comp_item(), s_comp_days(), s_comp_reason(), s_comp_notify()],
        _comp_summary, _save_comp, owner_only=True, async_save=_async_save_comp,
        confirm_label="✅ Apply compensation",
    ),
    "prod": Flow(
        "prod", "Save Product", "📦",
        [
            _pkind_step(),
            Step("pname", lambda d: "🏷 Product / plan name?", parse=lambda t, d: _name(t, "product name")),
            Step("pprice", lambda d: "💰 Default price?", parse=lambda t, d: _num(t, what="the price")),
            Step("pcost", lambda d: "💵 Default cost?", parse=lambda t, d: _num(t, what="the cost")),
            replace(s_duration("pduration", use_defaults=False), when=lambda d: d.get("pkind") == "sub"),
            s_supplier("psupplier", use_defaults=False),
        ],
        _prod_summary, _save_prod, owner_only=True,
    ),
}


# ============================================================ engine

def _active_steps(flow: Flow, d: dict) -> list:
    skip = d.get("_skip", ())   # steps already answered up front (e.g. converting a trial, /compensate 2 Netflix)
    return [s for s in flow.steps if s.key not in skip and (s.when is None or s.when(d))]


def _next_step(flow: Flow, d: dict, key: str):
    keys = [s.key for s in flow.steps]
    skip = d.get("_skip", ())
    for s in flow.steps[keys.index(key) + 1:]:
        if s.key not in skip and (s.when is None or s.when(d)):
            return s
    return None


def _header(flow: Flow, d: dict, step: Step) -> str:
    if not step.numbered:
        return f"{flow.icon} <b>{flow.title}</b> — Part payment"
    keys = [s.key for s in _active_steps(flow, d) if s.numbered]
    n = keys.index(step.key) + 1 if step.key in keys else 1
    return f"{flow.icon} <b>{flow.title}</b> — Step {n}/{len(keys)}"


def _keyboard(flow: Flow, step: Step, rows: list) -> InlineKeyboardMarkup:
    kb = [[InlineKeyboardButton(label, callback_data=f"f:{step.key}:{token}") for label, token in row]
          for row in rows]
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="f:_:cancel")])
    return InlineKeyboardMarkup(kb)


async def _ask(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: Flow):
    d = context.user_data
    step = next(s for s in flow.steps if s.key == d["_step"])
    rows = step.buttons(d) if step.buttons else []      # buttons first: they may stash options the prompt uses
    text = f"{_header(flow, d, step)}\n\n{step.prompt(d)}"
    await reply_to(update, text, reply_markup=_keyboard(flow, step, rows))


def _confirm_keyboard(kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(FLOWS[kind].confirm_label, callback_data=f"cf:{kind}:save")],
        [
            InlineKeyboardButton("🔁 Start Over", callback_data=f"cf:{kind}:restart"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"cf:{kind}:cancel"),
        ],
    ])


async def _begin(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: Flow,
                 prefill: Optional[dict] = None, skip=()) -> int:
    """Start a flow. `prefill` values are put in place up front and the steps named in `skip` are not asked."""
    d = context.user_data
    d.clear()
    d["_flow"] = flow.kind
    if prefill:
        d.update(prefill)
        d["_skip"] = set(skip)
    d["_step"] = _active_steps(flow, d)[0].key
    await _ask(update, context, flow)
    return FLOW


def _make_start(kind: str):
    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        flow = FLOWS[kind]
        if not await require_auth(update):
            return ConversationHandler.END
        if flow.owner_only:
            owner = db.get_owner_chat_id()
            if owner is None or update.effective_user.id != int(owner):
                await ack(update, "Owner only", alert=True)
                await reply_to(update, "Only the owner can manage the product list.")
                return ConversationHandler.END
        await ack(update)
        return await _begin(update, context, flow)
    return start


start_sale = _make_start("sale")
start_sub = _make_start("sub")
start_product = _make_start("prod")
start_trial = _make_start("trial")


async def start_comp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/compensate [days] [product]   e.g. /compensate 2 Netflix  (anything left out is asked for)."""
    if not await require_auth(update):
        return ConversationHandler.END
    if not is_owner(update):
        await reply_to(update, "Only the owner can add compensation days.")
        return ConversationHandler.END
    tokens = list(context.args or [])
    days, item_text = None, ""
    if tokens and tokens[0].isdigit():
        days, item_text = int(tokens[0]), " ".join(tokens[1:])
    elif len(tokens) > 1 and tokens[-1].isdigit():
        days, item_text = int(tokens[-1]), " ".join(tokens[:-1])
    else:
        item_text = " ".join(tokens)
    prefill = {}
    try:
        if days is not None:
            if not 1 <= days <= 90:
                raise ValueError("Add between 1 and 90 days.")
            prefill["days"] = days
        if item_text.strip():
            prefill["citem"] = _match_comp_item(item_text)
        elif not db.compensation_items():
            raise ValueError("There are no running subscriptions right now, so there's nothing to extend.")
    except ValueError as e:
        await reply_to(update, f"⚠️ {esc(e)}\n\nUsage: <code>/compensate 2 Netflix</code> (or just /compensate).")
        return ConversationHandler.END
    return await _begin(update, context, FLOWS["comp"], prefill, skip=set(prefill))


async def start_convert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🔁 Convert to paid: a new-subscription form with everything from the trial already filled in."""
    if not await require_auth(update):
        return ConversationHandler.END
    trial_id = int(update.callback_query.data.split(":")[2])
    trial = db.get_trial(trial_id)
    if not trial or trial["status"] != "active":
        await ack(update, "That trial was already decided (or removed).", alert=True)
        return ConversationHandler.END
    await ack(update)
    prefill = {
        "customer": trial["customer"], "platform": trial["platform"], "phone": trial["phone"],
        "tg_id": trial["tg_id"], "tg_username": trial["tg_username"], "ig_username": trial["ig_username"],
        "item": trial["item"], "seller": trial["seller"], "trial_id": trial_id,
    }
    prod = db.find_product("sub", trial["item"])
    if prod:  # saved product -> price / cost / duration come pre-filled as one-tap buttons
        prefill["defaults"] = {
            "price": prod["default_price"], "cost": prod["default_cost"],
            "duration": prod["default_duration"], "supplier": prod["default_supplier"],
        }
    await reply_to(update, f"🔁 Converting <b>{esc(trial['customer'])}</b>'s trial of {esc(trial['item'])} — "
                           "their details are already filled in.")
    return await _begin(update, context, FLOWS["sub"], prefill,
                        skip={"customer", "platform", "phone", "tg", "ig_username", "item", "seller"})


async def _advance(update, context, flow: Flow, step: Step, value) -> int:
    d = context.user_data
    if step.store:
        step.store(d, value)
    else:
        d[step.key] = value
    if step.after:
        await step.after(update, context, d)
    nxt = _next_step(flow, d, step.key)
    if nxt:
        d["_step"] = nxt.key
        await _ask(update, context, flow)
        return FLOW
    await reply_to(update, flow.summary(d), reply_markup=_confirm_keyboard(flow.kind))
    return CONFIRM


async def flow_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data
    flow = FLOWS.get(d.get("_flow"))
    if not flow or "_step" not in d:
        await reply_to(update, "That form expired — start again with /newsale or /newsub.")
        return ConversationHandler.END
    step = next(s for s in flow.steps if s.key == d["_step"])
    if step.parse is None:
        await reply_to(update, "👆 Please tap one of the buttons above.")
        return FLOW
    try:
        value = step.parse(update.message.text, d)
    except ValueError as e:
        await reply_to(update, f"⚠️ {esc(e)}")
        return FLOW
    return await _advance(update, context, flow, step, value)


async def flow_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data
    _, key, token = update.callback_query.data.split(":", 2)

    if token == "cancel":
        await ack(update)
        d.clear()
        await reply_to(update, "❌ Cancelled.")
        return ConversationHandler.END

    flow = FLOWS.get(d.get("_flow"))
    if not flow or "_step" not in d:
        await ack(update, "That form expired — start again.", alert=True)
        return ConversationHandler.END
    if key != d["_step"]:
        await ack(update, "That button is from an earlier step.", alert=True)
        return FLOW

    step = next(s for s in flow.steps if s.key == key)
    if step.pick is None:
        await ack(update)
        return FLOW
    try:
        value = step.pick(token, d)
    except (ValueError, KeyError, IndexError, TypeError) as e:
        await ack(update, str(e)[:180] or "That option isn't available.", alert=True)
        return FLOW
    await ack(update)
    if isinstance(value, Stay):
        await reply_to(update, value.message)
        return FLOW
    return await _advance(update, context, flow, step, value)


def _dup_keyboard(kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Save anyway", callback_data=f"cf:{kind}:force")],
        [InlineKeyboardButton("📝 Back to review", callback_data=f"cf:{kind}:review"),
         InlineKeyboardButton("❌ Cancel", callback_data=f"cf:{kind}:cancel")],
    ])


async def flow_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data
    _, kind, action = update.callback_query.data.split(":")
    flow = FLOWS[kind]
    if d.get("_flow") != kind:
        await ack(update, "This form has already finished or expired.", alert=True)
        return ConversationHandler.END
    await ack(update)

    if action in ("save", "force"):
        # "looks like a duplicate?" -- asked once, before saving; "Save anyway" skips the check
        if action == "save" and flow.dup_check:
            try:
                warning = flow.dup_check(update, d)
            except Exception:
                logger.exception("Duplicate check failed -- saving without it")
                warning = None
            if warning:
                await reply_to(update, warning, reply_markup=_dup_keyboard(kind))
                return CONFIRM
        try:
            if flow.async_save:
                result = await flow.async_save(update, context, d)
            else:
                result = flow.save(update, d)
        except Exception as e:
            logger.exception("Saving %s failed", kind)
            await alerts.report(e, f"Saving: {flow.title}", update)
            await reply_to(update, "❌ Couldn't save that — nothing was recorded. Please tap Confirm again, "
                                   "or Cancel and retry. (The owner has been alerted.)")
            return CONFIRM
        message, markup = result if isinstance(result, tuple) else (result, None)
        d.clear()  # clear BEFORE replying so a failed send can never lead to a duplicate save
        await reply_to(update, message, reply_markup=markup)
        return ConversationHandler.END
    if action == "review":
        await reply_to(update, flow.summary(d), reply_markup=_confirm_keyboard(kind))
        return CONFIRM
    if action == "restart":
        return await _begin(update, context, flow)
    d.clear()
    await reply_to(update, "❌ Cancelled.")
    return ConversationHandler.END


async def flow_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await reply_to(update, "❌ Cancelled.")
    return ConversationHandler.END


async def stale_form_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches taps on buttons from a form that already finished (or from before a restart)."""
    await ack(update, "That screen has expired — start again from /menu.", alert=True)


def build_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("newsale", start_sale),
            CommandHandler("newsub", start_sub),
            CommandHandler("addproduct", start_product),
            CommandHandler("newtrial", start_trial),
            CommandHandler("compensate", start_comp),
            CallbackQueryHandler(start_convert, pattern=r"^tr:conv:\d+$"),
            CallbackQueryHandler(start_sale, pattern="^menu:newsale$"),
            CallbackQueryHandler(start_sub, pattern="^menu:newsub$"),
        ],
        states={
            FLOW: [
                CallbackQueryHandler(flow_button, pattern="^f:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, flow_text),
            ],
            CONFIRM: [CallbackQueryHandler(flow_confirm, pattern="^cf:")],
        },
        fallbacks=[CommandHandler("cancel", flow_cancel_command)],
        allow_reentry=True,   # /newsub in the middle of /newsale just restarts cleanly
    )
