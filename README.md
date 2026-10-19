# Business Tracker Telegram Bot

Tracks your sales and customer subscriptions (price, cost, profit) and
automatically reminds you before — and when — a subscription ends.

### Interface

- **Tap the ☰ menu button** next to the text box (or send `/menu`) for a button-based menu — no need to remember commands.
- **Guided data entry**: `/newsale` and `/newsub` walk you through each field with a step counter ("Step 3/6"), quick-tap buttons for common choices (skip customer, no discount, payment method, subscription duration), and free-text entry wherever a number or name is needed.
- **Review before saving**: every new sale/subscription ends with a summary screen — tap ✅ Confirm & Save, 🔁 Start Over, or ❌ Cancel. Nothing is written to the database until you confirm.

## What it does

**Log**
- `/newsale` — log a one-time sale. Asks: item (or tap a saved product), customer, **seller**, date (tap Today or type a past date to backfill), price, discount, cost, **supplier**, payment method, **paid / part-paid / unpaid** — then a review screen with ✅ Confirm / 🔁 Start Over / ❌ Cancel. Nothing is saved until you confirm. The review screen warns about loss-making or low-margin sales.
- `/newsub` — same, for subscriptions, plus the customer's **phone number** and a duration (30 / 90 / 365 days, any number, or **🔓 Lifetime**). Lifetime subscriptions never trigger renewal reminders.
- `/undo` — took the wrong entry? Deletes the last sale/subscription *you* logged, within the last 24 hours (asks first). Undoing a renewal re-opens the original subscription.

**Money owed**
- `/dues` — everyone who hasn't paid in full, with the balance, and the total owed to you.
- `/receive sale 12 500` / `/receive sub 5` — record a payment against a sale or subscription (leave the amount off for the full remaining balance).

**Renewals**
- `/expiring [days]` — subscriptions ending in the next N days (default 7), with a 💬 WhatsApp button per customer who has a phone number.
- `/renew <id>` (or `/renew <id> due` if they haven't paid yet) — one-tap renewal at the same price. If the old one still has days left, they carry over, so renewing early doesn't cost the customer anything.
- **Reminder messages** (3 days before, and when it ends) now come with 🔁 *Renew (paid)* / 🕓 *Renew (pay later)* buttons and a 💬 *WhatsApp the customer* button that opens a chat with the renewal message already typed.
- `/setphone Alice Smith 9876543210` — add a phone number to a customer's existing subscriptions.

**Adding sellers to older entries** (owner only)
- `/assignseller` — walks through subscriptions that have no seller, oldest first, one at a time: tap a seller, type a new name, or skip. If a customer has several unassigned entries, it asks whether to give them all the same seller. `/assignseller sales` does the same for sales. Your Google Sheet updates when you finish or tap Stop; a session ends by itself after 10 idle minutes.
- `/setseller sub 5,6,9-12 Ravi` — set one seller on specific entries in one go (IDs from `/subs`; `sale` instead of `sub` for sales; use `-` as the name to clear a seller).
- `/subs` tells you how many active subscriptions still have no seller.

**Reports**
- `/summary` — yesterday's sales and profit, this month so far, what's expiring this week, what you're owed. **Also sent to you automatically every morning** (08:00 by default).
- `/stats` · `/monthly` (`/monthly 12`, `/monthly 2026-05`) · `/top` (customers, products, sellers) · `/abc` · `/renewal`
- `/suppliers` — each supplier's total spend, profit and margin, plus "same item, different suppliers" price comparisons so you can see who's cheapest.
- `/subs` · `/sales` · `/customer <name>` · `/search <keyword>`

**Saved products (owner sets up, everyone uses)**
- `/addproduct` — save a regular item or plan with its default price, cost, duration and supplier. It then appears as a tap button in `/newsale` / `/newsub` and pre-fills the price, cost and duration.
- `/products` — list them · `/delproduct <id>` — remove one.

**Data**
- `/export` (CSV) · `/backup` (JSON, includes products) · `/syncsheets` (see Google Sheets below)
- `/editsale <id>` / `/deletesale <id>` / `/editsub <id>` / `/deletesub <id>` — fix price/cost, or remove an entry (find IDs in `/sales`, `/subs`, `/dues`)
- `/menu` — everything above as tappable buttons

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

### Timezone and daily jobs

Servers run in UTC. Set **`TIMEZONE`** (e.g. `Asia/Kolkata`) so that:
- the **date stamped on each entry** is your local date (otherwise a sale logged at 1 am can land on yesterday, which moves profit into the wrong month), and
- **reminders and the morning summary** fire at *your* 9:00 / 8:00, not UTC's.

If you don't set it, it uses `TZ` if present, otherwise `Asia/Kolkata`. (Earlier versions of this README said `TZ` alone was enough for reminders — it wasn't; the scheduler ignored it and ran in UTC.)

| Variable | Default | What it does |
|---|---|---|
| `TIMEZONE` | `Asia/Kolkata` | Timezone for entry dates and the daily jobs |
| `REMINDER_HOUR` / `REMINDER_MINUTE` | `9` / `0` | When subscription reminders go out |
| `REMINDER_DAYS_BEFORE` | `3` | How many days ahead of expiry to warn you |
| `MORNING_SUMMARY_HOUR` / `_MINUTE` | `8` / `0` | When the daily summary is sent (`-1` turns it off) |
| `MIN_MARGIN_PCT` | `10` | The review screen warns when profit ÷ amount charged is below this % |
| `DEFAULT_COUNTRY_CODE` | `91` | Added to 10-digit phone numbers for WhatsApp links |
| `WHATSAPP_MESSAGE` | *(a friendly default)* | Text pre-filled in the WhatsApp button. Placeholders: `{customer}` `{item}` `{end_date}` |

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

Restart the bot. It rewrites the Sheet tabs on every startup, so the columns always match the database. (Run `/syncsheets` any time to force it.) From then on, every `/newsale` and `/newsub` pushes to the Sheet automatically — two tabs are created for you, "Sales" and "Subscriptions."

If Sheets sync ever fails (bad credentials, API quota, etc.), it fails silently in the background and never blocks saving your data to the bot's own database — check the bot's logs if entries stop showing up in the Sheet, and re-run `/syncsheets` to catch up once fixed.

Tabs created: **Sales**, **Subscriptions** (with Seller, Supplier, Amount Paid and Balance Due columns) and **Monthly Summary**. After an edit, delete, `/undo`, `/receive` or `/renew`, the tabs are refreshed in the background so a deleted entry doesn't linger in the Sheet. Because the tabs are regenerated from the database, **don't type notes into them** — they will be overwritten. Put your own analysis in a separate tab.

## Notes / things you may want to extend later

- Reminder time uses the **server's** local time zone, not yours — set `REMINDER_HOUR` accordingly, or set the `TZ` environment variable on your host (e.g. `TZ=Asia/Kolkata`).
- To edit/delete a logged sale or subscription directly at the database level (bypassing `/editsale` etc.), connect with `psql "$DATABASE_URL"` or a GUI tool like TablePlus/pgAdmin.
- `/backup` gives you an application-level JSON export. For database-level backups/point-in-time recovery, check what your Postgres host offers — Neon, Supabase, and Railway all include automated backups on their free/paid tiers.
