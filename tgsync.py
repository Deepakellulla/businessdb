"""
Keeps customers' Telegram usernames up to date from their numeric Telegram IDs.

A numeric ID never changes, but a username can. Given an ID, the bot asks Telegram for the
person's current username (Bot API `getChat`). Telegram only allows that for people who have
opened this bot and pressed Start at least once -- for anyone else the lookup fails with
"chat not found", and we simply try again on the next daily check.
"""
import asyncio
import logging

from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import ContextTypes

import db
from helpers import esc, resync_sheets_in_background

logger = logging.getLogger(__name__)

OK, UNREACHABLE, ERROR = "ok", "unreachable", "error"


async def lookup(bot, tg_id: int):
    """
    Returns (status, username):
      ("ok", "alice")  found, has a username
      ("ok", None)     found, but they have no public username
      ("unreachable", None)  they haven't started this bot (or blocked it) -- can't see them
      ("error", None)  Telegram/network problem -- try again later
    """
    for attempt in range(2):
        try:
            chat = await bot.get_chat(tg_id)
        except RetryAfter as e:
            if attempt == 0:
                await asyncio.sleep(min(float(e.retry_after) + 1, 30))
                continue
            return ERROR, None
        except (BadRequest, Forbidden):
            return UNREACHABLE, None
        except TelegramError:
            logger.exception("Telegram lookup failed for %s", tg_id)
            return ERROR, None
        if getattr(chat, "type", "private") != "private":
            return UNREACHABLE, None  # an ID that belongs to a group/channel, not a person
        return OK, (chat.username or None)
    return ERROR, None


def _changes_text(changes: list) -> str:
    lines = ["🔄 <b>Telegram username update</b>", ""]
    for c in changes:
        who = ", ".join(esc(n) for n in sorted(c["customers"])) or "—"
        old = ", ".join(f"@{esc(o)}" for o in sorted(c["old"])) or "none"
        if c["new"]:
            lines.append(f"• {who}: {old} → <b>@{esc(c['new'])}</b> ({c['entries']} entr{'y' if c['entries'] == 1 else 'ies'})")
        else:
            lines.append(f"• {who}: {old} → <b>no username</b> (they removed it) — the Telegram button is hidden until they set one")
    return "\n".join(lines)


async def sync_all(bot) -> dict:
    """
    Re-checks every saved ID and updates the username wherever it changed.
    Returns {"checked", "unreachable", "errors", "changes": [...]}; `changes` only lists real
    changes (a username that just differs in capital letters is updated silently).
    """
    result = {"checked": 0, "unreachable": 0, "errors": 0, "changes": []}
    for tg_id in db.tg_ids_to_check():
        status, username = await lookup(bot, tg_id)
        result["checked"] += 1
        if status == UNREACHABLE:
            result["unreachable"] += 1
        elif status == ERROR:
            result["errors"] += 1
        else:
            changed = db.set_tg_username_for_id(tg_id, username)
            real = [(c, o) for c, o in changed if (o or "").lower() != (username or "").lower()]
            if real:
                result["changes"].append({
                    "tg_id": tg_id,
                    "new": username,
                    "old": {o for _, o in real if o},
                    "customers": {c for c, _ in real if c},
                    "entries": len(real),
                })
        await asyncio.sleep(0.05)  # stay far below Telegram's rate limits
    return result


async def sync_job(context: ContextTypes.DEFAULT_TYPE):
    """Daily job: refresh usernames, and message the owner only when something changed."""
    try:
        result = await sync_all(context.bot)
    except Exception:
        logger.exception("Telegram username sync failed")
        return
    logger.info(
        "Telegram username sync: %s checked, %s changed, %s can't be seen yet, %s errors",
        result["checked"], len(result["changes"]), result["unreachable"], result["errors"],
    )
    if not result["changes"]:
        return
    resync_sheets_in_background()
    owner_id = db.get_owner_chat_id()
    if owner_id:
        try:
            await context.bot.send_message(
                chat_id=owner_id, text=_changes_text(result["changes"]), parse_mode=ParseMode.HTML
            )
        except Exception:
            logger.exception("Couldn't send the username-change notice")
