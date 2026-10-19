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
from datetime import date
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

import db
from config import MIN_MARGIN_PCT, REMINDER_DAYS_BEFORE
from helpers import (
    LIFETIME_DAYS,
    ack,
    display_name,
    esc,
    is_lifetime_duration,
    money,
    normalize_phone,
    parse_date_input,
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


@dataclass
class Flow:
    kind: str
    title: str
    icon: str
    steps: list
    summary: Callable[[dict], str]
    save: Callable[[Update, dict], str]   # does the DB write, returns the success message
    owner_only: bool = False


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
        lambda d: "📞 Customer's WhatsApp/phone number? Include the country code (e.g. 919876543210) "
                  "or just the 10 digits.\nRenewal reminders get a one-tap WhatsApp button when this is set.",
        parse=lambda t, d: normalize_phone(t),
        buttons=buttons,
        pick=pick,
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


def _sale_summary(d: dict) -> str:
    lines = ["🔎 <b>Review Sale</b>", ""]
    lines.append(f"🏷 Item: <b>{esc(d['item'])}</b>")
    lines.append(f"👤 Customer: {esc(d['customer']) if d['customer'] else '—'}")
    if d.get("seller"):
        lines.append(f"🧑‍💼 Seller: {esc(d['seller'])}")
    lines.append(f"📅 Sold on: {d['date'].isoformat()}")
    return "\n".join(lines + _money_lines(d))


def _sub_summary(d: dict) -> str:
    start = d["date"]
    end = start.fromordinal(start.toordinal() + d["duration"])
    lines = ["🔎 <b>Review Subscription</b>", ""]
    lines.append(f"👤 Customer: <b>{esc(d['customer'])}</b>")
    if d.get("phone"):
        lines.append(f"📞 Phone: +{d['phone']}")
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
        amount_paid=d["amount_paid"],
    )
    push_new_to_sheets("sale", sale_id)
    msg = f"✅ <b>Sale #{sale_id} saved!</b>\nProfit: <b>{money(profit)}</b>"
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
        amount_paid=d["amount_paid"], phone=d.get("phone"),
    )
    push_new_to_sheets("sub", sub_id)

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
            msg += " — with 🔁 Renew" + (" and 💬 WhatsApp buttons." if d.get("phone") else " buttons.")
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
        [s_item("sale"), s_customer(False), s_seller(), s_date(), s_gross("sale"), s_discount(),
         s_cost("sale"), s_supplier(), s_payment(), s_pstatus(), s_partial()],
        _sale_summary, _save_sale,
    ),
    "sub": Flow(
        "sub", "New Subscription", "📋",
        [s_customer(True), s_phone(), s_item("sub"), s_seller(), s_date(), s_gross("sub"), s_discount(),
         s_cost("sub"), s_supplier(), s_payment(), s_pstatus(), s_partial(), s_duration()],
        _sub_summary, _save_sub,
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
    return [s for s in flow.steps if s.when is None or s.when(d)]


def _next_step(flow: Flow, d: dict, key: str):
    keys = [s.key for s in flow.steps]
    for s in flow.steps[keys.index(key) + 1:]:
        if s.when is None or s.when(d):
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
        [InlineKeyboardButton("✅ Confirm & Save", callback_data=f"cf:{kind}:save")],
        [
            InlineKeyboardButton("🔁 Start Over", callback_data=f"cf:{kind}:restart"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"cf:{kind}:cancel"),
        ],
    ])


async def _begin(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: Flow) -> int:
    d = context.user_data
    d.clear()
    d["_flow"] = flow.kind
    d["_step"] = flow.steps[0].key
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


async def _advance(update, context, flow: Flow, step: Step, value) -> int:
    d = context.user_data
    if step.store:
        step.store(d, value)
    else:
        d[step.key] = value
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


async def flow_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data
    _, kind, action = update.callback_query.data.split(":")
    flow = FLOWS[kind]
    if d.get("_flow") != kind:
        await ack(update, "This form has already finished or expired.", alert=True)
        return ConversationHandler.END
    await ack(update)

    if action == "save":
        try:
            message = flow.save(update, d)
        except Exception:
            logger.exception("Saving %s failed", kind)
            await reply_to(update, "❌ Couldn't save that — nothing was recorded. Please tap Confirm again, "
                                   "or Cancel and retry.")
            return CONFIRM
        d.clear()  # clear BEFORE replying so a failed send can never lead to a duplicate save
        await reply_to(update, message)
        return ConversationHandler.END
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
