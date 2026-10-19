"""
Adding sellers to entries that were logged before the seller field existed.

/assignseller       -> walks through subscriptions with no seller, one at a time
/assignseller sales -> same, for sales
/setseller sub 5,6,9-12 Ravi -> set one seller on specific entries in one go

Owner only: who sold what feeds the seller reports, so it shouldn't be editable by everyone.
"""
import logging

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
from helpers import ack, esc, money, reply_to, resync_sheets_in_background

logger = logging.getLogger(__name__)

AS = 0
SESSION_TIMEOUT = 600  # seconds; stops a stray message hours later being taken as a seller name


def _is_owner(update: Update) -> bool:
    owner = db.get_owner_chat_id()
    return owner is not None and update.effective_user.id == int(owner)


def _describe(kind: str, r) -> str:
    if kind == "sub":
        return (f"📋 <b>#{r['id']} {esc(r['customer'])}</b> — {esc(r['item'])}\n"
                f"Sold {r['start_date']} · profit {money(r['profit'])}")
    who = f" for {esc(r['customer'])}" if r["customer"] else ""
    return (f"🧾 <b>#{r['id']} {esc(r['item'])}</b>{who}\n"
            f"Sold {r['sale_date']} · profit {money(r['profit'])}")


# ------------------------------------------------------------ guided flow

async def start_assign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        await reply_to(update, "Only the owner can assign sellers.")
        return ConversationHandler.END
    if context.user_data.get("_flow"):
        await reply_to(update, "You're in the middle of a form — finish it or /cancel first.")
        return ConversationHandler.END

    kind = "sale" if context.args and context.args[0].lower().startswith("sale") else "sub"
    label = "sales" if kind == "sale" else "subscriptions"
    total = db.count_unassigned(kind)
    if total == 0:
        await reply_to(update, f"✅ Every one of your {label} already has a seller.")
        return ConversationHandler.END

    context.user_data["_as"] = {"kind": kind, "skipped": [], "done": 0, "cur": None, "pending": None, "opts": []}
    await reply_to(
        update,
        f"🧑‍💼 <b>{total} {label}</b> have no seller. I'll show them oldest first — tap a seller for each, "
        f"type a new name, or skip.\n<i>(Your Google Sheet updates when you finish or tap Stop.)</i>",
    )
    return await _show_next(update, context)


async def _show_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = context.user_data["_as"]
    st["pending"] = None
    rec, left = db.next_unassigned(st["kind"], st["skipped"])
    if rec is None:
        lead = "✅ That's the end of the list." if st["skipped"] else "🎉 That's all of them."
        return await _finish(update, context, lead)

    st["cur"], st["customer"] = rec["id"], rec.get("customer")
    sellers = db.recent_sellers(6)
    st["opts"] = sellers
    rows = [[InlineKeyboardButton(s[:24], callback_data=f"as:{rec['id']}:s{i}") for i, s in enumerate(sellers[j:j + 2], j)]
            for j in range(0, len(sellers), 2)]
    rows.append([
        InlineKeyboardButton("⏭ Skip this one", callback_data=f"as:{rec['id']}:skip"),
        InlineKeyboardButton("🛑 Stop", callback_data=f"as:{rec['id']}:stop"),
    ])
    hint = "Tap a seller, or type a new name." if sellers else "Type the seller's name."
    await reply_to(
        update,
        f"🧑‍💼 <b>Assign seller</b> — {left} left\n\n{_describe(st['kind'], rec)}\n\n{hint}",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return AS


async def _finish(update: Update, context: ContextTypes.DEFAULT_TYPE, lead: str):
    st = context.user_data.pop("_as", None) or {}
    done, skipped = st.get("done", 0), len(st.get("skipped", []))
    left = db.count_unassigned(st.get("kind", "sub"))
    msg = f"{lead}\n\n✅ Assigned: <b>{done}</b>"
    if skipped:
        msg += f"\n⏭ Skipped: {skipped}"
    if left:
        msg += f"\n📭 Still without a seller: {left} — run /assignseller again any time."
    if done:
        resync_sheets_in_background()
    await reply_to(update, msg)
    return ConversationHandler.END


async def _assign(update: Update, context: ContextTypes.DEFAULT_TYPE, seller: str):
    st = context.user_data["_as"]
    kind, rid, customer = st["kind"], st["cur"], st.get("customer")
    db.set_seller(kind, [rid], seller)
    st["done"] += 1
    await reply_to(update, f"✅ #{rid} → <b>{esc(seller)}</b>")

    if customer:
        others = db.count_unassigned_for_customer(kind, customer, st["skipped"] + [rid])
        if others:
            st["pending"] = {"customer": customer, "seller": seller}
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"✅ Yes, all {others}", callback_data=f"asall:{rid}:yes"),
                InlineKeyboardButton("No, one by one", callback_data=f"asall:{rid}:no"),
            ]])
            await reply_to(
                update,
                f"{esc(customer)} has <b>{others}</b> more without a seller. Give them to "
                f"<b>{esc(seller)}</b> too?",
                reply_markup=kb,
            )
            return AS
    return await _show_next(update, context)


async def on_seller_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = context.user_data.get("_as")
    if not st:
        await ack(update, "That session expired — run /assignseller again.", alert=True)
        return ConversationHandler.END
    _, rid, token = update.callback_query.data.split(":")
    if int(rid) != st.get("cur") or st.get("pending"):
        await ack(update, "That button is from an earlier entry.", alert=True)
        return AS
    await ack(update)
    if token == "stop":
        return await _finish(update, context, "🛑 Stopped.")
    if token == "skip":
        st["skipped"].append(int(rid))
        return await _show_next(update, context)
    try:
        seller = st["opts"][int(token[1:])]
    except (ValueError, IndexError):
        await reply_to(update, "That option isn't available — type the name instead.")
        return AS
    return await _assign(update, context, seller)


async def on_all_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = context.user_data.get("_as")
    if not st or not st.get("pending"):
        await ack(update, "That question is no longer open.", alert=True)
        return AS if st else ConversationHandler.END
    _, rid, answer = update.callback_query.data.split(":")
    if int(rid) != st.get("cur"):
        await ack(update, "That button is from an earlier entry.", alert=True)
        return AS
    await ack(update)
    if answer == "yes":
        p = st["pending"]
        ids = db.assign_customer_unassigned(st["kind"], p["customer"], p["seller"], st["skipped"])
        st["done"] += len(ids)
        await reply_to(update, f"✅ Also set <b>{esc(p['seller'])}</b> on {len(ids)} more for {esc(p['customer'])}.")
    return await _show_next(update, context)


async def on_seller_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = context.user_data.get("_as")
    if not st:
        await reply_to(update, "That session expired — run /assignseller again.")
        return ConversationHandler.END
    if st.get("pending"):
        await reply_to(update, "👆 Please tap Yes or No above first.")
        return AS
    name = update.message.text.strip()
    if not name or len(name) > 60:
        await reply_to(update, "⚠️ Send a seller name (up to 60 characters), or tap a button.")
        return AS
    return await _assign(update, context, name)


async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _finish(update, context, "🛑 Stopped.")


async def on_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("_as"):
        await _finish(update, context, "⌛ This session timed out (10 minutes idle) — your progress is saved.")


def build_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("assignseller", start_assign)],
        states={
            AS: [
                CallbackQueryHandler(on_seller_button, pattern=r"^as:\d+:(s\d+|skip|stop)$"),
                CallbackQueryHandler(on_all_button, pattern=r"^asall:\d+:(yes|no)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_seller_text),
            ],
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, on_timeout), CallbackQueryHandler(on_timeout)],
        },
        fallbacks=[CommandHandler("cancel", on_cancel)],
        conversation_timeout=SESSION_TIMEOUT,
        allow_reentry=True,
    )


# ------------------------------------------------------------ one-line version

def _parse_ids(spec: str, cap: int = 500) -> list:
    """'5' / '5,6,9' / '9-12' / '1,4-6' -> sorted unique ints. Raises ValueError on junk."""
    ids = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            lo, hi = int(lo), int(hi)
            if hi < lo or hi - lo > cap:
                raise ValueError("range too large or backwards")
            ids.update(range(lo, hi + 1))
        else:
            ids.add(int(part))
        if len(ids) > cap:
            raise ValueError("too many ids")
    if not ids or min(ids) < 1:
        raise ValueError("no valid ids")
    return sorted(ids)


async def setseller_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        await reply_to(update, "Only the owner can assign sellers.")
        return
    usage = (
        "Usage: <code>/setseller sub 5,6,9-12 Ravi</code>\n"
        "• <code>sub</code> or <code>sale</code>, then the entry IDs (commas and ranges work), then the seller name\n"
        "• use <code>-</code> as the name to clear a seller\n"
        "• for lots of entries, <code>/assignseller</code> is easier"
    )
    args = context.args
    if len(args) < 3 or args[0].lower() not in ("sub", "sale"):
        await reply_to(update, usage)
        return
    kind = args[0].lower()
    try:
        ids = _parse_ids(args[1])
    except ValueError:
        await reply_to(update, "⚠️ I couldn't read those IDs.\n\n" + usage)
        return
    name = " ".join(args[2:]).strip()
    seller = None if name == "-" else name
    if seller and len(seller) > 60:
        await reply_to(update, "⚠️ That seller name is too long (max 60 characters).")
        return

    found = db.set_seller(kind, ids, seller)
    label = "sale" if kind == "sale" else "subscription"
    if not found:
        await reply_to(update, f"No {label}s found with those IDs.")
        return
    resync_sheets_in_background()
    missing = sorted(set(ids) - set(found))
    shown = ", ".join(f"#{i}" for i in found[:15]) + (f" …+{len(found) - 15} more" if len(found) > 15 else "")
    msg = (f"✅ Seller <b>{esc(seller)}</b> set on {len(found)} {label}{'s' if len(found) != 1 else ''}: {shown}"
           if seller else f"✅ Cleared the seller on {len(found)} {label}{'s' if len(found) != 1 else ''}: {shown}")
    if missing:
        msg += f"\n⚠️ Not found: {', '.join('#' + str(i) for i in missing[:15])}"
    await reply_to(update, msg)
