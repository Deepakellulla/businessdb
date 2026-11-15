"""
Tells the owner on Telegram when something breaks, so problems never go unnoticed.

What triggers an alert
  * any unhandled error while handling a command or button (with what the person was doing)
  * a failed save, a failed Google Sheets sync, a failed daily job
  * the database becoming unreachable (and again when it's back)
  * another copy of the bot running at the same time (Telegram "409 Conflict")
  * an unstable connection to Telegram (only after several errors in a few minutes)
  * the bot crashing on start-up or while running

Alerts are throttled so a repeating error can't flood you: the same error is reported at most once
per 15 minutes (with a "+N more" count), and there's a global cap of 12 alerts per hour.

Alerts are sent with a plain HTTPS call to the Bot API, so they work from any thread, and still work
when the rest of the bot is in a bad state.

The one thing a bot can't tell you is "I'm completely down". For that, set HEALTHCHECK_URL (e.g. a free
healthchecks.io check): the bot pings it regularly and that service alerts you when the pings stop.
"""
import asyncio
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime
from html import escape

from telegram.error import Conflict, NetworkError, RetryAfter

import db
from config import BOT_TOKEN, DATABASE_URL, HEALTHCHECK_URL, OWNER_CHAT_ID, TZINFO

logger = logging.getLogger(__name__)

STARTED_AT = time.time()

COOLDOWN = 15 * 60            # same error: at most one alert per 15 minutes
CONFLICT_COOLDOWN = 60 * 60   # "two copies running" is reported at most hourly
GLOBAL_LIMIT = 12             # alerts per hour, all kinds together
NET_BURST = 5                 # this many network errors ...
NET_WINDOW = 10 * 60          # ... within this many seconds counts as "unstable"

_owner_id = None
_last_sent = {}               # signature -> [time last sent, how many were suppressed since]
_sent_times = deque()         # when each alert went out (for the hourly cap)
_net_errors = deque()         # recent network-error times
_recent = deque(maxlen=50)    # (time, signature, one-line summary) of recent errors, for /health
_db_down = False
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


# ------------------------------------------------------------------ plumbing

def _redact(text: str) -> str:
    """Never let a secret leave in an alert."""
    text = str(text)
    for secret in (BOT_TOKEN, DATABASE_URL):
        if secret:
            text = text.replace(secret, "***")
    text = re.sub(r"(://)[^@\s/]+@", r"\1***@", text)                      # user:password@host
    text = re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b", "***", text)          # bot tokens
    return text


def set_owner(chat_id) -> None:
    global _owner_id
    if chat_id:
        _owner_id = int(chat_id)


def get_owner():
    """Owner's chat id: from the database, else the last one we saw, else the OWNER_CHAT_ID setting."""
    global _owner_id
    try:
        found = db.get_owner_chat_id()
        if found:
            _owner_id = int(found)
    except Exception:
        pass  # database may be the very thing that's broken
    if _owner_id:
        return _owner_id
    return int(OWNER_CHAT_ID) if OWNER_CHAT_ID and OWNER_CHAT_ID.lstrip("-").isdigit() else None


def _post(method: str, payload: dict) -> bool:
    if not BOT_TOKEN:
        return False
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        logger.warning("Alert not delivered (HTTP %s)", e.code)
    except Exception as e:
        logger.warning("Alert not delivered: %s", _redact(e))
    return False


def send_owner_sync(text: str) -> bool:
    """Send a message to the owner. Safe to call from any thread."""
    owner = get_owner()
    if not owner:
        logger.warning("No owner to alert yet (send /start to the bot first)")
        return False
    return _post("sendMessage", {
        "chat_id": owner, "text": text[:3900], "parse_mode": "HTML", "disable_web_page_preview": True,
    })


async def notify_owner(text: str) -> bool:
    return await asyncio.to_thread(send_owner_sync, text)


# ------------------------------------------------------------------ describing an error

def _frame(exc: BaseException):
    """(file, line, function) of the innermost frame in this project (else the innermost frame)."""
    tb, last, mine = exc.__traceback__, None, None
    while tb is not None:
        f = (tb.tb_frame.f_code.co_filename, tb.tb_lineno, tb.tb_frame.f_code.co_name)
        last = f
        if os.path.abspath(f[0]).startswith(_PROJECT_DIR):
            mine = f
        tb = tb.tb_next
    return mine or last


def _signature(exc: BaseException) -> str:
    f = _frame(exc)
    where = f"{os.path.basename(f[0])}:{f[1]}" if f else str(exc)[:60]
    return f"{type(exc).__name__}@{where}"


def _what_was_happening(update) -> str:
    """'/editsub 5' or 'tapped button ed:f:item', plus who did it. Kept short."""
    if update is None:
        return ""
    bits = []
    try:
        msg = getattr(update, "effective_message", None)
        cq = getattr(update, "callback_query", None)
        if cq is not None and getattr(cq, "data", None):
            bits.append(f"tapped a button: <code>{escape(str(cq.data)[:60])}</code>")
        elif msg is not None and getattr(msg, "text", None):
            bits.append(f"sent: <code>{escape(msg.text[:100])}</code>")
        user = getattr(update, "effective_user", None)
        if user is not None:
            name = user.username and f"@{user.username}" or user.first_name or ""
            bits.append(f"by {escape(str(name))} (ID {user.id})")
    except Exception:
        pass
    return "\n".join(f"• {b}" for b in bits)


def _now() -> str:
    return datetime.now(TZINFO).strftime("%d %b %H:%M")


# ------------------------------------------------------------------ throttling

def _allow(sig: str, cooldown: float):
    """-> (send_it, suppressed_since_last, hit_hourly_cap)."""
    now = time.time()
    while _sent_times and now - _sent_times[0] > 3600:
        _sent_times.popleft()
    rec = _last_sent.get(sig)
    if rec and now - rec[0] < cooldown:
        rec[1] += 1
        return False, 0, False
    if len(_sent_times) >= GLOBAL_LIMIT:
        if rec:
            rec[1] += 1
        else:
            _last_sent[sig] = [now - cooldown, 1]  # keep counting; it can go out once the cap clears
        return False, 0, False
    suppressed = rec[1] if rec else 0
    _last_sent[sig] = [now, 0]
    _sent_times.append(now)
    return True, suppressed, len(_sent_times) >= GLOBAL_LIMIT


def _prepare(exc: BaseException, where: str = "", update=None, force: bool = False):
    """Decides whether this error deserves an alert and builds it. Returns the text, or None."""
    if isinstance(exc, RetryAfter):
        return None  # Telegram asking us to slow down is normal
    sig = _signature(exc)
    _recent.append((time.time(), sig, f"{type(exc).__name__}: {_redact(exc)}"[:200]))

    if isinstance(exc, Conflict):
        ok, suppressed, capped = _allow("Conflict", CONFLICT_COOLDOWN)
        if not ok:
            return None
        return ("🚨 <b>Two copies of the bot are running</b>\n\n"
                "Telegram reports a 409 Conflict: another instance is polling with the same bot token, so "
                "commands will randomly go missing.\nStop the extra copy (an old deploy, a local run, "
                f"a second service).\n\n🕒 {_now()}")

    if isinstance(exc, NetworkError):  # includes TimedOut -- normal now and then, a problem in bursts
        now = time.time()
        _net_errors.append(now)
        while _net_errors and now - _net_errors[0] > NET_WINDOW:
            _net_errors.popleft()
        if len(_net_errors) < NET_BURST and not force:
            return None
        ok, suppressed, capped = _allow("NetworkBurst", COOLDOWN)
        if not ok:
            return None
        return (f"⚠️ <b>Telegram connection is unstable</b>\n\n{len(_net_errors)} network errors in the last "
                f"{NET_WINDOW // 60} minutes.\nLatest: <code>{escape(_redact(exc)[:200])}</code>\n\n🕒 {_now()}")

    ok, suppressed, capped = _allow(sig if not force else f"{sig}#{time.time()}", COOLDOWN)
    if not ok:
        return None
    frame = _frame(exc)
    lines = ["🚨 <b>Something broke</b>" + (f" — {escape(where)}" if where else ""), ""]
    lines.append(f"<code>{escape(type(exc).__name__)}: {escape(_redact(exc)[:300])}</code>")
    if frame:
        lines.append(f"📍 {escape(os.path.basename(frame[0]))}:{frame[1]} in <code>{escape(frame[2])}</code>")
    happening = _what_was_happening(update)
    if happening:
        lines += ["", "<b>What was happening</b>", happening]
    if suppressed:
        lines += ["", f"(+{suppressed} identical error{'s' if suppressed != 1 else ''} since the last alert)"]
    if capped:
        lines += ["", "🔇 Alert limit reached — further alerts are muted for up to an hour. Check the logs."]
    lines += ["", f"🕒 {_now()}"]
    return "\n".join(lines)


async def report(exc: BaseException, where: str = "", update=None, force: bool = False) -> None:
    """Log an error and (subject to throttling) alert the owner. Never raises."""
    try:
        logger.error("%s: %s", where or "Error", _redact(exc), exc_info=exc)
        text = _prepare(exc, where, update, force)
        if text:
            await notify_owner(text)
    except Exception:
        logger.exception("Alerting itself failed")


def report_sync(exc: BaseException, where: str = "", force: bool = False) -> None:
    """Same as report(), for threads and for the moment before the bot has an event loop."""
    try:
        logger.error("%s: %s", where or "Error", _redact(exc), exc_info=exc)
        text = _prepare(exc, where, None, force)
        if text:
            send_owner_sync(text)
    except Exception:
        logger.exception("Alerting itself failed")


# ------------------------------------------------------------------ health

def _check_db():
    """-> (ok, milliseconds, error text)."""
    started = time.time()
    try:
        with db.get_conn() as conn:
            conn.execute("SELECT 1").fetchone()
        return True, int((time.time() - started) * 1000), ""
    except Exception as e:
        return False, int((time.time() - started) * 1000), _redact(e)[:300]


def _ping_healthcheck(ok: bool) -> None:
    if not HEALTHCHECK_URL:
        return
    url = HEALTHCHECK_URL.rstrip("/") + ("" if ok else "/fail")
    try:
        urllib.request.urlopen(url, timeout=10).read()
    except Exception as e:
        logger.warning("Health-check ping failed: %s", _redact(e))


async def health_job(context=None):
    """Every few minutes: is the database reachable? Alert once when it goes down and once when it's back."""
    global _db_down
    ok, ms, err = await asyncio.to_thread(_check_db)
    if not ok and not _db_down:
        _db_down = True
        await notify_owner(f"🔴 <b>Database unreachable</b>\n\n<code>{escape(err)}</code>\n\n"
                           f"The bot can't save or read anything until it's back.\n🕒 {_now()}")
    elif ok and _db_down:
        _db_down = False
        await notify_owner(f"🟢 <b>Database is back</b> ({ms} ms)\n🕒 {_now()}")
    await asyncio.to_thread(_ping_healthcheck, ok)


def _uptime() -> str:
    s = int(time.time() - STARTED_AT)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    return (f"{d}d " if d else "") + (f"{h}h " if h or d else "") + f"{m}m"


async def health_text() -> str:
    ok, ms, err = await asyncio.to_thread(_check_db)
    day_ago = time.time() - 86400
    errs = [e for e in _recent if e[0] >= day_ago]
    lines = ["🩺 <b>Bot health</b>", ""]
    lines.append(f"• Database: {'✅ OK (' + str(ms) + ' ms)' if ok else '🔴 DOWN — ' + escape(err)}")
    lines.append(f"• Running for: {_uptime()}")
    if errs:
        t, sig, summary = errs[-1]
        when = datetime.fromtimestamp(t, TZINFO).strftime("%H:%M")
        lines.append(f"• Errors in the last 24h: <b>{len(errs)}</b> (latest {when}: <code>{escape(summary[:120])}</code>)")
    else:
        lines.append("• Errors in the last 24h: none ✅")
    lines.append("• Owner alerts: " + ("✅ on" if get_owner() else "⚠️ no owner set"))
    lines.append("• Down-detection monitor: " + ("✅ pinging every few minutes" if HEALTHCHECK_URL
                 else "➖ not set up (add HEALTHCHECK_URL to be told if the whole bot goes down)"))
    return "\n".join(lines)
