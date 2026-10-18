"""
One-off script to copy ALL data (settings, staff, sales, subscriptions) from your
old PostgreSQL database to a new one -- used when moving the bot to a new
Railway account/database before the old one is shut down.

Setup:
    pip install psycopg2-binary

Usage:
    python migrate_db.py "<OLD_DATABASE_URL>" "<NEW_DATABASE_URL>"

Get each connection string from Railway: click your Postgres service -> "Connect"
tab -> copy the "Postgres Connection URL". Use the OLD account's one first, then
the NEW account's one, in that order.

This script is safe to re-run: existing rows in the destination are left alone,
and rows are only inserted, never deleted from either side.
"""
import sys

import psycopg2
import psycopg2.extras

# Order matters: staff/settings have no foreign keys, sales/subscriptions are independent tables too,
# but we keep a stable order for clean log output.
TABLES = ["settings", "staff", "products", "sales", "subscriptions"]

# These tables use a SERIAL "id" primary key whose sequence must be re-synced after migrating.
SERIAL_ID_TABLES = ["sales", "subscriptions", "products"]


def fetch_all(url: str, table: str):
    conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur = conn.cursor()
        try:
            cur.execute(f"SELECT * FROM {table}")
        except psycopg2.errors.UndefinedTable:
            return []  # e.g. an old database from before the products table existed
        return cur.fetchall()
    finally:
        conn.close()


def ensure_schema(new_url: str):
    """Creates all tables/columns on the new database using the bot's own init_db()."""
    import os
    os.environ["DATABASE_URL"] = new_url
    os.environ.setdefault("BOT_TOKEN", "placeholder-not-used-by-init-db")
    import db  # local import so DATABASE_URL is set before config.py reads it
    db.init_db()


def main():
    if len(sys.argv) != 3:
        print("Usage: python migrate_db.py <OLD_DATABASE_URL> <NEW_DATABASE_URL>")
        sys.exit(1)

    old_url, new_url = sys.argv[1], sys.argv[2]

    print("Creating schema on the new database (safe if it already exists)...")
    ensure_schema(new_url)

    new_conn = psycopg2.connect(new_url)
    new_cur = new_conn.cursor()

    for table in TABLES:
        rows = fetch_all(old_url, table)
        if not rows:
            print(f"{table}: nothing to copy")
            continue

        cols = list(rows[0].keys())
        col_list = ", ".join(cols)
        placeholders = ", ".join(["%s"] * len(cols))
        insert_sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"

        copied = 0
        for row in rows:
            values = [row[c] for c in cols]
            try:
                new_cur.execute(insert_sql, values)
                copied += 1
            except Exception as e:
                print(f"  ! failed on one row in {table}: {e}")
                new_conn.rollback()
                continue
        new_conn.commit()
        print(f"{table}: copied {copied}/{len(rows)} rows")

    # Re-sync the auto-increment sequences so new sales/subscriptions logged after
    # migration don't collide with the migrated IDs.
    for table in SERIAL_ID_TABLES:
        new_cur.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
            f"COALESCE((SELECT MAX(id) FROM {table}), 1))"
        )
    new_conn.commit()
    new_conn.close()

    print("\nMigration complete. Point your bot's DATABASE_URL at the new database and restart it.")


if __name__ == "__main__":
    main()
