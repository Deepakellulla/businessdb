import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

# PostgreSQL connection string, e.g.
# postgresql://user:password@host:5432/dbname?sslmode=require
DATABASE_URL = os.getenv("DATABASE_URL")

# Hour (24h, server time) at which subscription reminders are checked daily
REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "9"))
REMINDER_MINUTE = int(os.getenv("REMINDER_MINUTE", "0"))

# How many days before expiry to send the "heads up" reminder
REMINDER_DAYS_BEFORE = int(os.getenv("REMINDER_DAYS_BEFORE", "3"))

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
