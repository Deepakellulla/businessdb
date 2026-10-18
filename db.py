import psycopg2
import psycopg2.extras
from datetime import date
from contextlib import contextmanager

from config import DATABASE_URL


class _ConnWrapper:
    """
    Thin wrapper so call sites can keep using the sqlite3-style convenience API:
    conn.execute(sql, params).fetchall() / .fetchone(), on top of psycopg2.
    """
    def __init__(self, pg_conn):
        self._conn = pg_conn

    def execute(self, sql, params=None):
        cur = self._conn.cursor()
        cur.execute(sql, params or ())
        return cur

    def commit(self):
        self._conn.commit()


@contextmanager
def get_conn():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield _ConnWrapper(conn)
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sales (
                id SERIAL PRIMARY KEY,
                item TEXT NOT NULL,
                customer TEXT,
                sold_price DOUBLE PRECISION NOT NULL,
                cost_price DOUBLE PRECISION NOT NULL,
                profit DOUBLE PRECISION NOT NULL,
                sale_date TEXT NOT NULL,
                logged_by TEXT,
                discount DOUBLE PRECISION DEFAULT 0,
                gross_price DOUBLE PRECISION,
                payment_method TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id SERIAL PRIMARY KEY,
                customer TEXT NOT NULL,
                item TEXT NOT NULL,
                sold_price DOUBLE PRECISION NOT NULL,
                cost_price DOUBLE PRECISION NOT NULL,
                profit DOUBLE PRECISION NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                reminded_soon INTEGER DEFAULT 0,
                reminded_due INTEGER DEFAULT 0,
                active INTEGER DEFAULT 1,
                logged_by TEXT,
                discount DOUBLE PRECISION DEFAULT 0,
                gross_price DOUBLE PRECISION,
                payment_method TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS staff (
                user_id BIGINT PRIMARY KEY,
                name TEXT,
                added_date TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id SERIAL PRIMARY KEY,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                default_price DOUBLE PRECISION,
                default_cost DOUBLE PRECISION,
                default_duration INTEGER,
                default_supplier TEXT
            )
        """)
        conn.commit()

        # migration: add columns for databases created before they existed
        common_cols = [
            ("logged_by", "TEXT"), ("discount", "DOUBLE PRECISION DEFAULT 0"),
            ("gross_price", "DOUBLE PRECISION"), ("payment_method", "TEXT"),
            ("seller", "TEXT"), ("supplier", "TEXT"),
            ("amount_paid", "DOUBLE PRECISION"), ("created_at", "TIMESTAMPTZ"),
        ]
        sub_only_cols = [("phone", "TEXT"), ("renewed_to", "INTEGER")]
        for table, new_cols in (
            ("sales", common_cols),
            ("subscriptions", common_cols + sub_only_cols),
        ):
            for col_name, col_type in new_cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col_name} {col_type}")
            # new rows get a timestamp automatically; old rows stay NULL on purpose
            # (so /undo never touches history it can't date)
            conn.execute(f"ALTER TABLE {table} ALTER COLUMN created_at SET DEFAULT now()")
        conn.execute("UPDATE sales SET gross_price = sold_price + COALESCE(discount,0) WHERE gross_price IS NULL")
        conn.execute(
            "UPDATE subscriptions SET gross_price = sold_price + COALESCE(discount,0) WHERE gross_price IS NULL"
        )
        # everything logged before payment tracking existed is treated as fully paid
        conn.execute("UPDATE sales SET amount_paid = sold_price WHERE amount_paid IS NULL")
        conn.execute("UPDATE subscriptions SET amount_paid = sold_price WHERE amount_paid IS NULL")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS products_kind_name_idx ON products (kind, LOWER(name))"
        )
        conn.commit()


# ---------- settings (owner chat id) ----------

def set_setting(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, value),
        )
        conn.commit()


def get_setting(key: str):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=%s", (key,)).fetchone()
        return row["value"] if row else None


def get_owner_chat_id():
    val = get_setting("owner_chat_id")
    return int(val) if val else None


# ---------- sales ----------

def _iso(d):
    """Accept a date, a 'YYYY-MM-DD' string, or None (-> today) and return an ISO string."""
    if d is None:
        return date.today().isoformat()
    return d.isoformat() if hasattr(d, "isoformat") else str(d)


def add_sale(item, customer, sold_price, cost_price, logged_by=None,
             discount=0.0, gross_price=None, payment_method=None,
             sale_date=None, seller=None, supplier=None, amount_paid=None):
    """
    sold_price = final/net amount actually received (after discount).
    gross_price = list price before discount; if not given, computed as sold_price + discount.
    amount_paid = money actually collected so far; defaults to sold_price (fully paid).
    """
    if gross_price is None:
        gross_price = sold_price + discount
    if amount_paid is None:
        amount_paid = sold_price
    profit = sold_price - cost_price
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO sales "
            "(item, customer, sold_price, cost_price, profit, sale_date, logged_by, "
            " discount, gross_price, payment_method, seller, supplier, amount_paid) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (item, customer, sold_price, cost_price, profit, _iso(sale_date), logged_by,
             discount, gross_price, payment_method, seller, supplier, amount_paid),
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        return new_id, profit


def get_sale(sale_id):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM sales WHERE id=%s", (sale_id,)).fetchone()


def update_sale(sale_id, sold_price, cost_price):
    """Fix price/cost. A fully-paid sale stays fully paid at the new price; a part-paid
    one keeps what was received (capped at the new price)."""
    profit = sold_price - cost_price
    with get_conn() as conn:
        conn.execute(
            "UPDATE sales SET "
            "amount_paid = CASE WHEN COALESCE(amount_paid, sold_price) >= sold_price THEN %s "
            "                   ELSE LEAST(amount_paid, %s) END, "
            "sold_price=%s, cost_price=%s, profit=%s WHERE id=%s",
            (sold_price, sold_price, sold_price, cost_price, profit, sale_id),
        )
        conn.commit()
    return profit


def delete_sale(sale_id):
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM sales WHERE id=%s", (sale_id,))
        conn.commit()
        return cur.rowcount > 0


def list_sales(limit=10):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM sales ORDER BY id DESC LIMIT %s", (limit,)).fetchall()


def sales_summary(since_date: str = None):
    with get_conn() as conn:
        if since_date:
            row = conn.execute(
                "SELECT COUNT(*) as cnt, COALESCE(SUM(sold_price),0) as revenue, "
                "COALESCE(SUM(profit),0) as profit FROM sales WHERE sale_date >= %s",
                (since_date,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) as cnt, COALESCE(SUM(sold_price),0) as revenue, "
                "COALESCE(SUM(profit),0) as profit FROM sales"
            ).fetchone()
        return row


def payment_method_breakdown(since_date: str = None):
    with get_conn() as conn:
        if since_date:
            sales_rows = conn.execute(
                "SELECT COALESCE(payment_method, 'Unspecified') as method, "
                "COUNT(*) as cnt, COALESCE(SUM(sold_price),0) as revenue, COALESCE(SUM(profit),0) as profit "
                "FROM sales WHERE sale_date >= %s GROUP BY method",
                (since_date,),
            ).fetchall()
            sub_rows = conn.execute(
                "SELECT COALESCE(payment_method, 'Unspecified') as method, "
                "COUNT(*) as cnt, COALESCE(SUM(sold_price),0) as revenue, COALESCE(SUM(profit),0) as profit "
                "FROM subscriptions WHERE start_date >= %s GROUP BY method",
                (since_date,),
            ).fetchall()
        else:
            sales_rows = conn.execute(
                "SELECT COALESCE(payment_method, 'Unspecified') as method, "
                "COUNT(*) as cnt, COALESCE(SUM(sold_price),0) as revenue, COALESCE(SUM(profit),0) as profit "
                "FROM sales GROUP BY method"
            ).fetchall()
            sub_rows = conn.execute(
                "SELECT COALESCE(payment_method, 'Unspecified') as method, "
                "COUNT(*) as cnt, COALESCE(SUM(sold_price),0) as revenue, COALESCE(SUM(profit),0) as profit "
                "FROM subscriptions GROUP BY method"
            ).fetchall()

    combined = {}
    for r in list(sales_rows) + list(sub_rows):
        m = r["method"]
        combined.setdefault(m, {"count": 0, "revenue": 0.0, "profit": 0.0})
        combined[m]["count"] += r["cnt"]
        combined[m]["revenue"] += r["revenue"]
        combined[m]["profit"] += r["profit"]

    result = [{"method": m, **v} for m, v in combined.items()]
    result.sort(key=lambda x: x["revenue"], reverse=True)
    return result


def discount_summary(since_date: str = None):
    with get_conn() as conn:
        if since_date:
            s = conn.execute(
                "SELECT COALESCE(SUM(discount),0) as discount, COALESCE(SUM(gross_price),0) as gross, "
                "COALESCE(SUM(sold_price),0) as net FROM sales WHERE sale_date >= %s",
                (since_date,),
            ).fetchone()
            sub = conn.execute(
                "SELECT COALESCE(SUM(discount),0) as discount, COALESCE(SUM(gross_price),0) as gross, "
                "COALESCE(SUM(sold_price),0) as net FROM subscriptions WHERE start_date >= %s",
                (since_date,),
            ).fetchone()
        else:
            s = conn.execute(
                "SELECT COALESCE(SUM(discount),0) as discount, COALESCE(SUM(gross_price),0) as gross, "
                "COALESCE(SUM(sold_price),0) as net FROM sales"
            ).fetchone()
            sub = conn.execute(
                "SELECT COALESCE(SUM(discount),0) as discount, COALESCE(SUM(gross_price),0) as gross, "
                "COALESCE(SUM(sold_price),0) as net FROM subscriptions"
            ).fetchone()
    return {
        "total_discount": s["discount"] + sub["discount"],
        "gross_revenue": s["gross"] + sub["gross"],
        "net_revenue": s["net"] + sub["net"],
    }


# ---------- subscriptions ----------

def add_subscription(customer, item, sold_price, cost_price, start_date: date, duration_days: int,
                      logged_by=None, discount=0.0, gross_price=None, payment_method=None,
                      seller=None, supplier=None, amount_paid=None, phone=None):
    end_date = start_date.fromordinal(start_date.toordinal() + duration_days)
    if gross_price is None:
        gross_price = sold_price + discount
    if amount_paid is None:
        amount_paid = sold_price
    profit = sold_price - cost_price
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO subscriptions "
            "(customer, item, sold_price, cost_price, profit, start_date, end_date, logged_by, "
            " discount, gross_price, payment_method, seller, supplier, amount_paid, phone) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (customer, item, sold_price, cost_price, profit,
             start_date.isoformat(), end_date.isoformat(), logged_by,
             discount, gross_price, payment_method, seller, supplier, amount_paid, phone),
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        return new_id, end_date, profit


def get_subscription(sub_id):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM subscriptions WHERE id=%s", (sub_id,)).fetchone()


def update_subscription(sub_id, sold_price, cost_price):
    profit = sold_price - cost_price
    with get_conn() as conn:
        conn.execute(
            "UPDATE subscriptions SET "
            "amount_paid = CASE WHEN COALESCE(amount_paid, sold_price) >= sold_price THEN %s "
            "                   ELSE LEAST(amount_paid, %s) END, "
            "sold_price=%s, cost_price=%s, profit=%s WHERE id=%s",
            (sold_price, sold_price, sold_price, cost_price, profit, sub_id),
        )
        conn.commit()
    return profit


def delete_subscription(sub_id):
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM subscriptions WHERE id=%s", (sub_id,))
        conn.commit()
        return cur.rowcount > 0


def list_active_subscriptions():
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM subscriptions WHERE active=1 ORDER BY end_date ASC"
        ).fetchall()


def subscriptions_due_for_reminder(today: date, days_before: int):
    """Returns (soon_list, due_today_list) that haven't been reminded yet."""
    with get_conn() as conn:
        soon = conn.execute(
            "SELECT * FROM subscriptions WHERE active=1 AND reminded_soon=0 AND end_date = %s",
            ((today.fromordinal(today.toordinal() + days_before)).isoformat(),),
        ).fetchall()
        due = conn.execute(
            "SELECT * FROM subscriptions WHERE active=1 AND reminded_due=0 AND end_date <= %s",
            (today.isoformat(),),
        ).fetchall()
        return soon, due


def mark_reminded_soon(sub_id):
    with get_conn() as conn:
        conn.execute("UPDATE subscriptions SET reminded_soon=1 WHERE id=%s", (sub_id,))
        conn.commit()


def mark_reminded_due(sub_id):
    with get_conn() as conn:
        conn.execute("UPDATE subscriptions SET reminded_due=1, active=0 WHERE id=%s", (sub_id,))
        conn.commit()


def profit_summary(since_date: str = None):
    with get_conn() as conn:
        if since_date:
            sales_row = conn.execute(
                "SELECT COALESCE(SUM(profit),0) as p FROM sales WHERE sale_date >= %s", (since_date,)
            ).fetchone()
            subs_row = conn.execute(
                "SELECT COALESCE(SUM(profit),0) as p FROM subscriptions WHERE start_date >= %s", (since_date,)
            ).fetchone()
        else:
            sales_row = conn.execute("SELECT COALESCE(SUM(profit),0) as p FROM sales").fetchone()
            subs_row = conn.execute("SELECT COALESCE(SUM(profit),0) as p FROM subscriptions").fetchone()
        return sales_row["p"] + subs_row["p"]


# ---------- monthly profit tracker ----------

def monthly_profit_summary(num_months: int = 6):
    """
    Returns a list of dicts, most recent month first:
    [{month, sales_profit, sub_profit, total_profit, revenue}, ...]
    """
    with get_conn() as conn:
        sales_rows = conn.execute(
            "SELECT LEFT(sale_date,7) as month, COALESCE(SUM(profit),0) as profit, "
            "COALESCE(SUM(sold_price),0) as revenue FROM sales GROUP BY month"
        ).fetchall()
        sub_rows = conn.execute(
            "SELECT LEFT(start_date,7) as month, COALESCE(SUM(profit),0) as profit, "
            "COALESCE(SUM(sold_price),0) as revenue FROM subscriptions GROUP BY month"
        ).fetchall()

    combined = {}
    for r in sales_rows:
        m = r["month"]
        combined.setdefault(m, {"sales_profit": 0.0, "sub_profit": 0.0, "revenue": 0.0})
        combined[m]["sales_profit"] += r["profit"]
        combined[m]["revenue"] += r["revenue"]
    for r in sub_rows:
        m = r["month"]
        combined.setdefault(m, {"sales_profit": 0.0, "sub_profit": 0.0, "revenue": 0.0})
        combined[m]["sub_profit"] += r["profit"]
        combined[m]["revenue"] += r["revenue"]

    result = []
    for month in sorted(combined.keys(), reverse=True)[:num_months]:
        d = combined[month]
        result.append({
            "month": month,
            "sales_profit": d["sales_profit"],
            "sub_profit": d["sub_profit"],
            "total_profit": d["sales_profit"] + d["sub_profit"],
            "revenue": d["revenue"],
        })
    return result


def profit_for_month(year_month: str):
    with get_conn() as conn:
        sales_row = conn.execute(
            "SELECT COALESCE(SUM(profit),0) as p, COALESCE(SUM(sold_price),0) as r "
            "FROM sales WHERE LEFT(sale_date,7) = %s",
            (year_month,),
        ).fetchone()
        sub_row = conn.execute(
            "SELECT COALESCE(SUM(profit),0) as p, COALESCE(SUM(sold_price),0) as r "
            "FROM subscriptions WHERE LEFT(start_date,7) = %s",
            (year_month,),
        ).fetchone()
    return {
        "sales_profit": sales_row["p"],
        "sub_profit": sub_row["p"],
        "total_profit": sales_row["p"] + sub_row["p"],
        "revenue": sales_row["r"] + sub_row["r"],
    }


# ---------- customer profiles ----------

def customer_profile(name: str):
    with get_conn() as conn:
        sales = conn.execute(
            "SELECT * FROM sales WHERE LOWER(customer) = LOWER(%s) ORDER BY sale_date DESC", (name,)
        ).fetchall()
        subs = conn.execute(
            "SELECT * FROM subscriptions WHERE LOWER(customer) = LOWER(%s) ORDER BY start_date DESC", (name,)
        ).fetchall()

    total_sales_revenue = sum(r["sold_price"] for r in sales)
    total_sales_profit = sum(r["profit"] for r in sales)
    total_sub_revenue = sum(r["sold_price"] for r in subs)
    total_sub_profit = sum(r["profit"] for r in subs)
    active_subs = [r for r in subs if r["active"] == 1]

    return {
        "sales": sales,
        "subscriptions": subs,
        "active_subscriptions": active_subs,
        "total_revenue": total_sales_revenue + total_sub_revenue,
        "total_profit": total_sales_profit + total_sub_profit,
    }


def find_customer_names(query: str, limit=8):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT customer FROM ("
            "  SELECT customer FROM sales WHERE customer IS NOT NULL"
            "  UNION"
            "  SELECT customer FROM subscriptions"
            ") t WHERE customer ILIKE %s LIMIT %s",
            (f"%{query}%", limit),
        ).fetchall()
        return [r["customer"] for r in rows]


# ---------- top customers / products ----------

def top_customers(limit=5):
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT MIN(customer) as customer, SUM(profit) as total_profit, SUM(sold_price) as total_revenue
            FROM (
                SELECT customer, profit, sold_price FROM sales WHERE customer IS NOT NULL
                UNION ALL
                SELECT customer, profit, sold_price FROM subscriptions
            ) t
            GROUP BY LOWER(customer)
            ORDER BY total_profit DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()


def top_products(limit=5):
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT MIN(item) as item, SUM(profit) as total_profit, SUM(sold_price) as total_revenue,
                   COUNT(*) as times_sold
            FROM (
                SELECT item, profit, sold_price FROM sales
                UNION ALL
                SELECT item, profit, sold_price FROM subscriptions
            ) t
            GROUP BY LOWER(item)
            ORDER BY total_profit DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()


def abc_analysis():
    """
    Classic ABC analysis of every product/item by profit contribution:
      Tier A = items whose cumulative profit share reaches up to 80%
      Tier B = the next slice up to 95%
      Tier C = the remaining long tail
    Returns a list sorted by profit descending, each entry a dict with
    item, profit, revenue, times_sold, profit_pct, cumulative_pct, tier.
    Returns [] if there's no profit data yet, or if total profit isn't positive
    (e.g. everything logged so far is a net loss -- tiering isn't meaningful then).
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT MIN(item) as item, SUM(profit) as total_profit, SUM(sold_price) as total_revenue,
                   COUNT(*) as times_sold
            FROM (
                SELECT item, profit, sold_price FROM sales
                UNION ALL
                SELECT item, profit, sold_price FROM subscriptions
            ) t
            GROUP BY LOWER(item)
            ORDER BY total_profit DESC
            """
        ).fetchall()

    total_profit = sum(r["total_profit"] for r in rows)
    if total_profit <= 0:
        return []

    result = []
    cumulative_pct = 0.0
    for r in rows:
        profit = r["total_profit"]
        profit_pct = max(profit, 0) / total_profit * 100
        # Classify using the cumulative % *before* this item, so the item that pushes
        # the running total past a threshold is still counted in the tier it started in
        # -- otherwise a single dominant product (e.g. 85% of all profit alone) would
        # incorrectly land in Tier B instead of A.
        if cumulative_pct < 80:
            tier = "A"
        elif cumulative_pct < 95:
            tier = "B"
        else:
            tier = "C"
        cumulative_pct += profit_pct
        result.append({
            "item": r["item"],
            "profit": profit,
            "revenue": r["total_revenue"],
            "times_sold": r["times_sold"],
            "profit_pct": round(profit_pct, 1),
            "cumulative_pct": round(min(cumulative_pct, 100.0), 1),
            "tier": tier,
        })
    return result


# ---------- staff management ----------

def add_staff(user_id: int, name: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO staff (user_id, name, added_date) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET name = EXCLUDED.name",
            (user_id, name, date.today().isoformat()),
        )
        conn.commit()


def remove_staff(user_id: int):
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM staff WHERE user_id=%s", (user_id,))
        conn.commit()
        return cur.rowcount > 0


def list_staff():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM staff ORDER BY added_date ASC").fetchall()


def is_staff(user_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM staff WHERE user_id=%s", (user_id,)).fetchone()
        return row is not None


def is_authorized(user_id: int):
    """Owner is always authorized; otherwise must be in the staff table."""
    owner_id = get_owner_chat_id()
    if owner_id is not None and int(user_id) == int(owner_id):
        return True
    return is_staff(user_id)


# ---------- search ----------

def search_records(query: str, limit=15):
    like = f"%{query}%"
    with get_conn() as conn:
        sales = conn.execute(
            "SELECT * FROM sales WHERE item ILIKE %s OR customer ILIKE %s "
            "ORDER BY sale_date DESC LIMIT %s",
            (like, like, limit),
        ).fetchall()
        subs = conn.execute(
            "SELECT * FROM subscriptions WHERE item ILIKE %s OR customer ILIKE %s "
            "ORDER BY start_date DESC LIMIT %s",
            (like, like, limit),
        ).fetchall()
        return sales, subs


# ---------- renewal / churn rate ----------

def renewal_churn_stats(grace_days: int = 30):
    """
    Looks at subscriptions that have ended (active=0). For each, checks whether
    the same customer+item shows up again as a later subscription within
    `grace_days` of the old end date -- if so, it's counted as renewed.
    """
    with get_conn() as conn:
        ended = conn.execute(
            "SELECT * FROM subscriptions WHERE active=0 ORDER BY end_date ASC"
        ).fetchall()
        all_subs = conn.execute("SELECT * FROM subscriptions").fetchall()

    total = len(ended)
    if total == 0:
        return {"total_ended": 0, "renewed": 0, "churned": 0, "renewal_rate_pct": 0.0, "churned_details": []}

    renewed = 0
    churned_details = []
    for old in ended:
        old_end = date.fromisoformat(old["end_date"])
        cutoff = old_end.fromordinal(old_end.toordinal() + grace_days)
        # a one-tap /renew links old -> new directly, which is more reliable than guessing by dates
        found = bool(old.get("renewed_to"))
        for other in ([] if found else all_subs):
            if other["id"] == old["id"]:
                continue
            if (other["customer"] or "").strip().lower() != (old["customer"] or "").strip().lower():
                continue
            if (other["item"] or "").strip().lower() != (old["item"] or "").strip().lower():
                continue
            other_start = date.fromisoformat(other["start_date"])
            if old_end <= other_start <= cutoff:
                found = True
                break
        if found:
            renewed += 1
        else:
            churned_details.append((old["customer"], old["item"], old["end_date"]))

    churned = total - renewed
    rate = round(renewed / total * 100, 1)
    return {
        "total_ended": total,
        "renewed": renewed,
        "churned": churned,
        "renewal_rate_pct": rate,
        "churned_details": churned_details,
    }


# ---------- export & backup ----------

def all_sales():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM sales ORDER BY sale_date ASC").fetchall()


def all_subscriptions():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM subscriptions ORDER BY start_date ASC").fetchall()


def full_backup_dict():
    """Full data export as plain dicts/lists -- used for the /backup command's JSON dump."""
    with get_conn() as conn:
        sales = conn.execute("SELECT * FROM sales ORDER BY id").fetchall()
        subs = conn.execute("SELECT * FROM subscriptions ORDER BY id").fetchall()
        staff = conn.execute("SELECT * FROM staff ORDER BY user_id").fetchall()
        settings = conn.execute("SELECT * FROM settings").fetchall()
        products = conn.execute("SELECT * FROM products ORDER BY id").fetchall()
    return {
        "sales": [dict(r) for r in sales],
        "subscriptions": [dict(r) for r in subs],
        "staff": [dict(r) for r in staff],
        "products": [dict(r) for r in products],
        "settings": {r["key"]: r["value"] for r in settings},
    }


# =====================================================================
# Seller / supplier / dues / expiring / renew / undo / catalog
# =====================================================================

def _recent_values(column: str, limit: int):
    """Most recently used distinct non-empty values of sales/subscriptions.<column>."""
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT v, MAX(d) AS last_used FROM (
                SELECT {column} AS v, sale_date AS d FROM sales WHERE COALESCE({column}, '') <> ''
                UNION ALL
                SELECT {column} AS v, start_date AS d FROM subscriptions WHERE COALESCE({column}, '') <> ''
            ) t
            GROUP BY v
            ORDER BY last_used DESC, v ASC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return [r["v"] for r in rows]


def recent_sellers(limit=6):
    return _recent_values("seller", limit)


def recent_suppliers(limit=6):
    return _recent_values("supplier", limit)


def top_sellers(limit=5):
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT MIN(seller) AS seller, SUM(profit) AS total_profit,
                   SUM(sold_price) AS total_revenue, COUNT(*) AS deals
            FROM (
                SELECT seller, profit, sold_price FROM sales WHERE COALESCE(seller, '') <> ''
                UNION ALL
                SELECT seller, profit, sold_price FROM subscriptions WHERE COALESCE(seller, '') <> ''
            ) t
            GROUP BY LOWER(seller)
            ORDER BY total_profit DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()


def customer_phone(name: str):
    """Most recent phone number saved for this customer (case-insensitive), or None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT phone FROM subscriptions WHERE LOWER(customer) = LOWER(%s) "
            "AND COALESCE(phone, '') <> '' ORDER BY id DESC LIMIT 1",
            (name,),
        ).fetchone()
        return row["phone"] if row else None


def set_customer_phone(name: str, phone: str) -> int:
    """Sets the phone on every subscription of this customer. Returns rows updated."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE subscriptions SET phone=%s WHERE LOWER(customer) = LOWER(%s)", (phone, name)
        )
        conn.commit()
        return cur.rowcount


# ---------- payments owed ----------

def dues():
    """Everything not fully paid. Returns (sales_rows, sub_rows, total_balance)."""
    with get_conn() as conn:
        sales = conn.execute(
            "SELECT *, sold_price - COALESCE(amount_paid, sold_price) AS balance FROM sales "
            "WHERE sold_price - COALESCE(amount_paid, sold_price) > 0.005 ORDER BY sale_date ASC, id ASC"
        ).fetchall()
        subs = conn.execute(
            "SELECT *, sold_price - COALESCE(amount_paid, sold_price) AS balance FROM subscriptions "
            "WHERE sold_price - COALESCE(amount_paid, sold_price) > 0.005 ORDER BY start_date ASC, id ASC"
        ).fetchall()
    total = sum(r["balance"] for r in sales) + sum(r["balance"] for r in subs)
    return sales, subs, total


def record_payment(kind: str, record_id: int, amount=None):
    """
    Record money received against a sale ('sale') or subscription ('sub').
    amount=None means "the whole remaining balance". The payment is capped at the balance.
    Returns dict(sold, paid, balance, applied) or None if the record doesn't exist.
    """
    table = "sales" if kind == "sale" else "subscriptions"
    with get_conn() as conn:
        row = conn.execute(f"SELECT sold_price, amount_paid FROM {table} WHERE id=%s", (record_id,)).fetchone()
        if not row:
            return None
        sold = row["sold_price"]
        paid_before = row["amount_paid"] if row["amount_paid"] is not None else sold
        balance_before = max(sold - paid_before, 0.0)
        applied = balance_before if amount is None else min(max(amount, 0.0), balance_before)
        paid_after = paid_before + applied
        conn.execute(f"UPDATE {table} SET amount_paid=%s WHERE id=%s", (paid_after, record_id))
        conn.commit()
    return {"sold": sold, "paid": paid_after, "balance": max(sold - paid_after, 0.0), "applied": applied}


# ---------- expiring / renew ----------

def expiring_subs(days: int, today: date = None):
    """Active subscriptions ending within `days` days (including already-overdue ones)."""
    today = today or date.today()
    limit = today.fromordinal(today.toordinal() + days).isoformat()
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM subscriptions WHERE active=1 AND end_date <= %s ORDER BY end_date ASC, id ASC",
            (limit,),
        ).fetchall()


LIFETIME_THRESHOLD_DAYS = 3650  # keep in sync with main.py: 10+ years == "lifetime"


def renew_subscription(sub_id: int, paid: bool = True, logged_by=None, today: date = None):
    """
    Clone a subscription for another term, same price/cost/seller/supplier/phone.
    The new term is sold today. If the old one still has days left, those days are
    added on, so the customer doesn't lose them by renewing early.

    Returns (status, payload):
      ("ok", dict(new_id, old, start, end, duration, extra_days, profit))
      ("missing", None) | ("lifetime", None) | ("already", new_id_of_earlier_renewal)
    """
    today = today or date.today()
    old = get_subscription(sub_id)
    if not old:
        return "missing", None
    if old.get("renewed_to"):
        return "already", old["renewed_to"]

    old_start = date.fromisoformat(old["start_date"])
    old_end = date.fromisoformat(old["end_date"])
    term_days = (old_end - old_start).days
    if term_days >= LIFETIME_THRESHOLD_DAYS:
        return "lifetime", None

    extra = max(0, (old_end - today).days)
    duration = term_days + extra
    sold = old["sold_price"]
    new_id, end_date, profit = add_subscription(
        old["customer"], old["item"], sold, old["cost_price"], today, duration,
        logged_by=logged_by, discount=old["discount"] or 0.0, gross_price=old["gross_price"],
        payment_method=old["payment_method"], seller=old["seller"], supplier=old["supplier"],
        amount_paid=sold if paid else 0.0, phone=old["phone"],
    )
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE subscriptions SET renewed_to=%s, active=0, reminded_due=1 "
            "WHERE id=%s AND renewed_to IS NULL",
            (new_id, sub_id),
        )
        if cur.rowcount == 0:  # someone renewed it a split second earlier -> undo our duplicate
            conn.execute("DELETE FROM subscriptions WHERE id=%s", (new_id,))
            conn.commit()
            return "already", get_subscription(sub_id)["renewed_to"]
        conn.commit()
    return "ok", {
        "new_id": new_id, "old": old, "start": today, "end": end_date,
        "duration": duration, "extra_days": extra, "profit": profit,
    }


# ---------- undo ----------

def latest_entry_by(logged_by: str, within_hours: int = 24):
    """
    The most recent sale/subscription this person logged in the last `within_hours`.
    Returns ("sale"|"sub", row) or None. Rows logged before timestamps existed are ignored.
    """
    with get_conn() as conn:
        sale = conn.execute(
            "SELECT * FROM sales WHERE logged_by=%s AND created_at IS NOT NULL "
            "AND created_at > now() - make_interval(hours => %s) ORDER BY created_at DESC, id DESC LIMIT 1",
            (logged_by, within_hours),
        ).fetchone()
        sub = conn.execute(
            "SELECT * FROM subscriptions WHERE logged_by=%s AND created_at IS NOT NULL "
            "AND created_at > now() - make_interval(hours => %s) ORDER BY created_at DESC, id DESC LIMIT 1",
            (logged_by, within_hours),
        ).fetchone()
    if sale and sub:
        return ("sale", sale) if sale["created_at"] >= sub["created_at"] else ("sub", sub)
    if sale:
        return "sale", sale
    if sub:
        return "sub", sub
    return None


def is_renewal_target(sub_id: int) -> bool:
    """True if this subscription was created by renewing an earlier one."""
    with get_conn() as conn:
        return conn.execute("SELECT 1 FROM subscriptions WHERE renewed_to=%s", (sub_id,)).fetchone() is not None


def undo_subscription(sub_id: int) -> bool:
    """Delete a subscription; if it was created by a one-tap renewal, reopen the original."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE subscriptions SET renewed_to=NULL, active=1, reminded_due=0 WHERE renewed_to=%s",
            (sub_id,),
        )
        cur = conn.execute("DELETE FROM subscriptions WHERE id=%s", (sub_id,))
        conn.commit()
        return cur.rowcount > 0


# ---------- suppliers ----------

def supplier_report():
    """
    Returns dict(suppliers=[...], comparisons=[...]).
    suppliers: per supplier -> deals, total_cost, revenue, profit, margin_pct (profit / revenue).
    comparisons: items bought from 2+ suppliers -> avg cost per supplier, cheapest first.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT MIN(supplier) AS supplier, COUNT(*) AS deals, SUM(cost_price) AS total_cost,
                   SUM(sold_price) AS revenue, SUM(profit) AS profit
            FROM (
                SELECT supplier, cost_price, sold_price, profit FROM sales WHERE COALESCE(supplier,'') <> ''
                UNION ALL
                SELECT supplier, cost_price, sold_price, profit FROM subscriptions WHERE COALESCE(supplier,'') <> ''
            ) t
            GROUP BY LOWER(supplier)
            ORDER BY profit DESC
            """
        ).fetchall()
        per_item = conn.execute(
            """
            SELECT MIN(item) AS item, MIN(supplier) AS supplier, COUNT(*) AS deals, AVG(cost_price) AS avg_cost
            FROM (
                SELECT item, supplier, cost_price FROM sales WHERE COALESCE(supplier,'') <> ''
                UNION ALL
                SELECT item, supplier, cost_price FROM subscriptions WHERE COALESCE(supplier,'') <> ''
            ) t
            GROUP BY LOWER(item), LOWER(supplier)
            """
        ).fetchall()

    suppliers = []
    for r in rows:
        revenue = r["revenue"] or 0.0
        suppliers.append({
            "supplier": r["supplier"], "deals": r["deals"], "total_cost": r["total_cost"],
            "revenue": revenue, "profit": r["profit"],
            "margin_pct": (r["profit"] / revenue * 100) if revenue > 0 else 0.0,
        })

    by_item = {}
    for r in per_item:
        by_item.setdefault(r["item"].lower(), {"item": r["item"], "options": []})["options"].append(
            {"supplier": r["supplier"], "avg_cost": r["avg_cost"], "deals": r["deals"]}
        )
    comparisons = []
    for entry in by_item.values():
        if len(entry["options"]) >= 2:
            entry["options"].sort(key=lambda o: o["avg_cost"])
            comparisons.append(entry)
    comparisons.sort(key=lambda e: e["item"].lower())
    return {"suppliers": suppliers, "comparisons": comparisons}


# ---------- period totals (morning summary) ----------

def period_totals(start_iso: str, end_iso: str):
    """Sales by sale_date and subscriptions by start_date, inclusive range of ISO date strings."""
    with get_conn() as conn:
        s = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(sold_price),0) AS revenue, COALESCE(SUM(profit),0) AS profit "
            "FROM sales WHERE sale_date >= %s AND sale_date <= %s", (start_iso, end_iso)
        ).fetchone()
        u = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(sold_price),0) AS revenue, COALESCE(SUM(profit),0) AS profit "
            "FROM subscriptions WHERE start_date >= %s AND start_date <= %s", (start_iso, end_iso)
        ).fetchone()
    return {
        "sales_n": s["n"], "subs_n": u["n"],
        "revenue": s["revenue"] + u["revenue"], "profit": s["profit"] + u["profit"],
    }


# ---------- product catalog ----------

def save_product(kind: str, name: str, price, cost, duration, supplier):
    """Create, or update if a product with the same kind+name already exists. Returns (id, created)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM products WHERE kind=%s AND LOWER(name)=LOWER(%s)", (kind, name)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE products SET name=%s, default_price=%s, default_cost=%s, default_duration=%s, "
                "default_supplier=%s WHERE id=%s",
                (name, price, cost, duration, supplier, row["id"]),
            )
            conn.commit()
            return row["id"], False
        cur = conn.execute(
            "INSERT INTO products (kind, name, default_price, default_cost, default_duration, default_supplier) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (kind, name, price, cost, duration, supplier),
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        return new_id, True


def list_products(kind=None):
    with get_conn() as conn:
        if kind:
            return conn.execute(
                "SELECT * FROM products WHERE kind=%s ORDER BY LOWER(name)", (kind,)
            ).fetchall()
        return conn.execute("SELECT * FROM products ORDER BY kind, LOWER(name)").fetchall()


def get_product(product_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM products WHERE id=%s", (product_id,)).fetchone()


def delete_product(product_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM products WHERE id=%s", (product_id,))
        conn.commit()
        return cur.rowcount > 0
