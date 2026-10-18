import os
import time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

# PostgreSQL connection string, e.g.
# postgresql://user:password@host:5432/dbname?sslmode=require
DATABASE_URL = os.getenv("DATABASE_URL")

# ---------- timezone ----------
# Servers (Railway, Render...) run in UTC. Without this, a sale logged at 2am India time
# gets yesterday's date, and "9am" reminders fire at 9am UTC (2:30pm India time).
# TIMEZONE drives BOTH the dates stamped on entries and the daily job schedule.
# Falls back to the TZ variable if you already set that, then to Asia/Kolkata.
TIMEZONE = os.getenv("TIMEZONE") or os.getenv("TZ") or "Asia/Kolkata"
try:
    TZINFO = ZoneInfo(TIMEZONE)
except Exception as e:  # pragma: no cover - only hit on a typo'd timezone name
    raise RuntimeError(
        f"TIMEZONE '{TIMEZONE}' is not a valid timezone name (use e.g. 'Asia/Kolkata', "
        f"'America/New_York', 'UTC'). Original error: {e}"
    )
os.environ["TZ"] = TIMEZONE
if hasattr(time, "tzset"):
    time.tzset()  # makes date.today() / datetime.now() use TIMEZONE too

# ---------- daily jobs (hours are in TIMEZONE) ----------
REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "9"))
REMINDER_MINUTE = int(os.getenv("REMINDER_MINUTE", "0"))

# How many days before expiry to send the "heads up" reminder
REMINDER_DAYS_BEFORE = int(os.getenv("REMINDER_DAYS_BEFORE", "3"))

# Morning summary sent to the owner. Set MORNING_SUMMARY_HOUR=-1 to turn it off.
MORNING_SUMMARY_HOUR = int(os.getenv("MORNING_SUMMARY_HOUR", "8"))
MORNING_SUMMARY_MINUTE = int(os.getenv("MORNING_SUMMARY_MINUTE", "0"))

# ---------- business rules ----------
# The confirm screen warns when a sale's margin (profit / amount charged) is below this %.
MIN_MARGIN_PCT = float(os.getenv("MIN_MARGIN_PCT", "10"))

# 10-digit phone numbers get this country code prepended for WhatsApp links (91 = India).
DEFAULT_COUNTRY_CODE = os.getenv("DEFAULT_COUNTRY_CODE", "91")

# Message pre-filled in the WhatsApp renewal button. Placeholders: {customer} {item} {end_date}
WHATSAPP_MESSAGE = os.getenv(
    "WHATSAPP_MESSAGE",
    "Hi {customer}, your {item} subscription ends on {end_date}. "
    "Would you like to renew? Reply here and I'll set it up.",
)

# Optional: Google Sheets live sync. Both must be set for the feature to activate;
# if either is missing, the bot runs normally without Sheets sync.
GOOGLE_SHEETS_CREDENTIALS_B64 = os.getenv("GOOGLE_SHEETS_CREDENTIALS_B64")
GOOGLE_SHEETS_SPREADSHEET_ID = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID")

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is not set. Create a .env file (see .env.example) "
        "or set the BOT_TOKEN environment variable."
    )

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Create a .env file (see .env.example) "
        "or set the DATABASE_URL environment variable to your PostgreSQL connection string."
    )
