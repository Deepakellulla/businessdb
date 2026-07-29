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
        conn.commit()

        # migration: add columns for databases created before they existed
        for table, new_cols in (
            ("sales", [("logged_by", "TEXT"), ("discount", "DOUBLE PRECISION DEFAULT 0"),
                       ("gross_price", "DOUBLE PRECISION"), ("payment_method", "TEXT")]),
            ("subscriptions", [("logged_by", "TEXT"), ("discount", "DOUBLE PRECISION DEFAULT 0"),
                                ("gross_price", "DOUBLE PRECISION"), ("payment_method", "TEXT")]),
        ):
            for col_name, col_type in new_cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col_name} {col_type}")
        conn.execute("UPDATE sales SET gross_price = sold_price + COALESCE(discount,0) WHERE gross_price IS NULL")
        conn.execute(
            "UPDATE subscriptions SET gross_price = sold_price + COALESCE(discount,0) WHERE gross_price IS NULL"
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

def add_sale(item, customer, sold_price, cost_price, logged_by=None,
             discount=0.0, gross_price=None, payment_method=None):
    """
    sold_price = final/net amount actually received (after discount).
    gross_price = list price before discount; if not given, computed as sold_price + discount.
    """
    if gross_price is None:
        gross_price = sold_price + discount
    profit = sold_price - cost_price
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO sales "
            "(item, customer, sold_price, cost_price, profit, sale_date, logged_by, "
            " discount, gross_price, payment_method) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (item, customer, sold_price, cost_price, profit, date.today().isoformat(), logged_by,
             discount, gross_price, payment_method),
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        return new_id, profit


def get_sale(sale_id):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM sales WHERE id=%s", (sale_id,)).fetchone()


def update_sale(sale_id, sold_price, cost_price):
    profit = sold_price - cost_price
    with get_conn() as conn:
        conn.execute(
            "UPDATE sales SET sold_price=%s, cost_price=%s, profit=%s WHERE id=%s",
            (sold_price, cost_price, profit, sale_id),
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
                      logged_by=None, discount=0.0, gross_price=None, payment_method=None):
    end_date = start_date.fromordinal(start_date.toordinal() + duration_days)
    if gross_price is None:
        gross_price = sold_price + discount
    profit = sold_price - cost_price
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO subscriptions "
            "(customer, item, sold_price, cost_price, profit, start_date, end_date, logged_by, "
            " discount, gross_price, payment_method) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (customer, item, sold_price, cost_price, profit,
             start_date.isoformat(), end_date.isoformat(), logged_by,
             discount, gross_price, payment_method),
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
            "UPDATE subscriptions SET sold_price=%s, cost_price=%s, profit=%s WHERE id=%s",
            (sold_price, cost_price, profit, sub_id),
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
        found = False
        for other in all_subs:
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
    return {
        "sales": [dict(r) for r in sales],
        "subscriptions": [dict(r) for r in subs],
        "staff": [dict(r) for r in staff],
        "settings": {r["key"]: r["value"] for r in settings},
    }
