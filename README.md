# Business Tracker Telegram Bot

Tracks your sales and customer subscriptions (price, cost, profit) and
automatically reminds you before — and when — a subscription ends.

### Interface

- **Tap the ☰ menu button** next to the text box (or send `/menu`) for a button-based menu — no need to remember commands.
- **Guided data entry**: `/newsale` and `/newsub` walk you through each field with a step counter ("Step 3/6"), quick-tap buttons for common choices (skip customer, no discount, payment method, subscription duration), and free-text entry wherever a number or name is needed.
- **Review before saving**: every new sale/subscription ends with a summary screen — tap ✅ Confirm & Save, 🔁 Start Over, or ❌ Cancel. Nothing is written to the database until you confirm.

## What it does

- `/newsale` — log a one-time sale (item, customer, sold price, cost price → profit is calculated automatically)
- `/newsub` — log a subscription sold to a customer (price, cost, duration in days → profit + end date calculated automatically). Asks when it was sold — tap "Today" or type a past date to backfill historical subscriptions; profit is attributed to that month in `/monthly`, not the month you're logging it in.
- `/subs` — list active subscriptions with days remaining
- `/sales` — last 10 sales
- `/stats` — profit summary (7 days / 30 days / all-time)
- `/monthly` — profit broken down by month (last 6 months by default). Use `/monthly 12` for the last 12 months, or `/monthly 2026-05` for one specific month.
- `/customer <name>` — a customer's full history: total revenue/profit, active subscriptions, past sales
- `/top` — top 5 customers and top 5 products/items by profit
- `/export` — sends your sales and subscriptions as downloadable CSV files (opens directly in Excel/Google Sheets)
- `/backup` — sends a full JSON export of your database (sales, subscriptions, staff) so you always have an off-server copy
- `/syncsheets` — pushes every sale and subscription to Google Sheets in one go (see setup below); after that, new entries sync automatically as you log them
- `/editsale <id>` / `/deletesale <id>` — correct or remove a sale (find the ID via `/sales`)
- `/editsub <id>` / `/deletesub <id>` — correct or remove a subscription (find the ID via `/subs`)
- `/menu` — tap buttons instead of typing commands (opens an inline keyboard for the most-used actions)
- `/search <keyword>` — find any sale or subscription by item name or customer name
- `/renewal` — renewal & churn rate: what % of ended subscriptions got renewed vs. lapsed, plus a list of customers who didn't come back (a subscription is "resolved" once its end-date reminder has fired; a follow-on subscription for the same customer+item within a few days counts as a renewal)

### Payment method & discounts

`/newsale` and `/newsub` now ask two extra questions:
- **Price before discount, then discount amount** — the bot works out the net amount actually received and the profit off that. `/stats` shows total discounts given, and gross vs. net revenue, whenever any discount has been logged.
- **Payment method** — pick Cash / UPI / Card / Other from the on-screen keyboard (or type your own). `/stats` breaks down revenue and profit per payment method, useful for reconciling against your bank/UPI statements.

Both fields are optional in spirit — just send `0` for no discount, and whichever payment method fits.

### Multi-staff access

By default only the person who first ran `/start` (the "owner") can log or change data.
To let someone else on your team use the bot:

1. They message the bot and send `/myid` to get their Telegram user ID
2. You (the owner) run `/addstaff <their_id> <their name>`
3. They can now use `/newsale`, `/newsub`, `/export`, etc. Everything they log is tagged with who logged it (visible in `/export` and `/sales`).
4. `/staff` lists everyone with access; `/removestaff <id>` revokes it.

Only the owner receives subscription reminders — staff can log data but reminders always go to the owner's chat.
- Daily reminders sent automatically:
  - **3 days before** a subscription ends (configurable)
  - **On the day** it ends (subscription is then marked inactive)

### Storage

Data lives in a **PostgreSQL** database — nothing is stored on the bot's local disk, so it's safe to redeploy or restart without losing data (unlike a local SQLite file). You'll need a Postgres connection string; see setup options below.

## 1. Create your bot

1. Open Telegram, message **@BotFather**
2. Send `/newbot`, follow the prompts
3. Copy the token it gives you (looks like `123456789:AAExample...`)

## 2. Set up PostgreSQL

Pick whichever fits you:

**Already have one?** Skip to step 3 — you just need its connection string in the form `postgresql://user:password@host:5432/dbname`.

**Free hosted Postgres (easiest, no server to manage):**
- [Neon](https://neon.tech) or [Supabase](https://supabase.com) — both have a free tier, sign up, create a project, and copy the connection string it gives you (include `?sslmode=require`).
- Railway also offers a Postgres add-on if you're already deploying there — one click from your project dashboard.

**Local Docker (for testing on your own machine):**
```bash
docker run --name business-bot-db -e POSTGRES_PASSWORD=devpass \
  -e POSTGRES_DB=businessbot -p 5432:5432 -d postgres:16
```
Connection string: `postgresql://postgres:devpass@localhost:5432/businessbot`

The bot creates all its tables automatically on first run — no manual schema setup needed.

## 3. Configure

```bash
cp .env.example .env
```

Edit `.env`: paste your bot token into `BOT_TOKEN`, and your connection string into `DATABASE_URL`. Adjust `REMINDER_HOUR` /
`REMINDER_DAYS_BEFORE` if you want reminders at a different time or lead time
(server's local time zone is used).

## 4. Run locally (optional, for testing)

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

Then message your bot on Telegram and send `/start` — this registers your
chat as the one that receives reminders.

## 5. Deploy to a cloud host

### Railway
1. Push this folder to a GitHub repo
2. On [railway.app](https://railway.app) → New Project → Deploy from GitHub repo
3. Add environment variables `BOT_TOKEN` and `DATABASE_URL` (Railway → Variables tab). If you added Railway's Postgres plugin to the same project, it gives you a `DATABASE_URL` automatically — just reference it.
4. Railway auto-detects the `Procfile` and runs `python main.py` as a worker
5. No disk/volume needed — your data lives in Postgres, not on the bot's filesystem.

### Render
1. Push to GitHub, then on [render.com](https://render.com) → New → **Background Worker** (not "Web Service" — this bot uses polling, not a web port)
2. Build command: `pip install -r requirements.txt`
3. Start command: `python main.py`
4. Add `BOT_TOKEN` and `DATABASE_URL` under Environment
5. No Render Disk needed — same reason as above.

### Any VPS (Ubuntu, etc.)
```bash
git clone <your-repo>
cd telegram_business_bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit BOT_TOKEN and DATABASE_URL
# run permanently with systemd or:
nohup python main.py &
```

## Google Sheets sync (optional)

Every sale and subscription can automatically appear in a Google Sheet the moment you log it — good for sharing with an accountant, or just viewing your data outside Telegram.

**1. Create a Google Cloud service account**
- [console.cloud.google.com](https://console.cloud.google.com) → create a project → **APIs & Services → Library** → enable **Google Sheets API**
- **APIs & Services → Credentials → Create Credentials → Service Account** → give it any name
- Open the service account → **Keys** tab → **Add Key → Create new key → JSON** → downloads a `.json` file
- Copy the service account's email (shown on its page, looks like `xxx@your-project.iam.gserviceaccount.com`)

**2. Share your Sheet**
- Create or open a Google Sheet
- **Share** → paste in the service account email → give it **Editor** access
- Copy the spreadsheet ID from the URL: `docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`

**3. Encode the credentials file**

The bot reads credentials from one environment variable, so the JSON key needs to become a single base64 string:
```bash
# Mac/Linux
base64 -i your-key-file.json | tr -d '\n'
```
```powershell
# Windows PowerShell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("your-key-file.json"))
```

**4. Set the environment variables**

Add to your `.env` (or your host's Variables tab):
- `GOOGLE_SHEETS_CREDENTIALS_B64` = the base64 string from step 3
- `GOOGLE_SHEETS_SPREADSHEET_ID` = the ID from step 2

Restart the bot, then run `/syncsheets` once to backfill anything you logged before this was set up. From then on, every `/newsale` and `/newsub` pushes to the Sheet automatically — two tabs are created for you, "Sales" and "Subscriptions."

If Sheets sync ever fails (bad credentials, API quota, etc.), it fails silently in the background and never blocks saving your data to the bot's own database — check the bot's logs if entries stop showing up in the Sheet, and re-run `/syncsheets` to catch up once fixed.

## Notes / things you may want to extend later

- Reminder time uses the **server's** local time zone, not yours — set `REMINDER_HOUR` accordingly, or set the `TZ` environment variable on your host (e.g. `TZ=Asia/Kolkata`).
- To edit/delete a logged sale or subscription directly at the database level (bypassing `/editsale` etc.), connect with `psql "$DATABASE_URL"` or a GUI tool like TablePlus/pgAdmin.
- `/backup` gives you an application-level JSON export. For database-level backups/point-in-time recovery, check what your Postgres host offers — Neon, Supabase, and Railway all include automated backups on their free/paid tiers.
