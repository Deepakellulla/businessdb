import base64
import json
import logging
from collections import defaultdict

import gspread
from google.oauth2.service_account import Credentials

from config import GOOGLE_SHEETS_CREDENTIALS_B64, GOOGLE_SHEETS_SPREADSHEET_ID

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SALES_HEADERS = [
    "ID", "Date", "Item", "Customer", "Gross Price", "Discount", "Sold Price",
    "Cost Price", "Profit", "Payment Method", "Logged By",
]
SUBS_HEADERS = [
    "ID", "Customer", "Item", "Gross Price", "Discount", "Sold Price", "Cost Price",
    "Profit", "Start Date", "End Date", "Active", "Payment Method", "Logged By",
]
MONTHLY_HEADERS = [
    "Month", "Sales Revenue", "Sales Profit", "Subscription Revenue",
    "Subscription Profit", "Total Revenue", "Total Profit",
]

_client = None
_spreadsheet = None


def is_configured() -> bool:
    return bool(GOOGLE_SHEETS_CREDENTIALS_B64 and GOOGLE_SHEETS_SPREADSHEET_ID)


def _get_client():
    global _client
    if _client is None:
        raw = base64.b64decode(GOOGLE_SHEETS_CREDENTIALS_B64).decode("utf-8")
        creds_dict = json.loads(raw)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        _client = gspread.authorize(creds)
    return _client


def _get_spreadsheet():
    global _spreadsheet
    if _spreadsheet is None:
        _spreadsheet = _get_client().open_by_key(GOOGLE_SHEETS_SPREADSHEET_ID)
    return _spreadsheet


def _get_or_create_worksheet(title: str, headers: list):
    ss = _get_spreadsheet()
    try:
        ws = ss.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=title, rows=1000, cols=len(headers))
        ws.append_row(headers)
        return ws
    if not ws.row_values(1):
        ws.append_row(headers)
    return ws


def _sale_row(s: dict) -> list:
    return [
        s["id"], s["sale_date"], s["item"], s["customer"] or "",
        s["gross_price"], s["discount"], s["sold_price"], s["cost_price"], s["profit"],
        s["payment_method"] or "", s["logged_by"] or "",
    ]


def _sub_row(s: dict) -> list:
    return [
        s["id"], s["customer"], s["item"], s["gross_price"], s["discount"],
        s["sold_price"], s["cost_price"], s["profit"], s["start_date"], s["end_date"],
        "Yes" if s["active"] else "No", s["payment_method"] or "", s["logged_by"] or "",
    ]


def _compute_monthly_rows(all_sales, all_subs) -> list:
    """
    Groups sales by sale_date and subscriptions by start_date (both 'YYYY-MM-DD'
    strings), so backfilled subscriptions land in the month they were actually sold.
    Returns rows sorted oldest month first, ready to write under MONTHLY_HEADERS.
    """
    months = defaultdict(lambda: {"sales_rev": 0.0, "sales_profit": 0.0, "sub_rev": 0.0, "sub_profit": 0.0})

    for s in all_sales:
        month = s["sale_date"][:7]
        months[month]["sales_rev"] += s["sold_price"]
        months[month]["sales_profit"] += s["profit"]

    for s in all_subs:
        month = s["start_date"][:7]
        months[month]["sub_rev"] += s["sold_price"]
        months[month]["sub_profit"] += s["profit"]

    rows = []
    for month in sorted(months.keys()):
        m = months[month]
        total_rev = m["sales_rev"] + m["sub_rev"]
        total_profit = m["sales_profit"] + m["sub_profit"]
        rows.append([
            month, round(m["sales_rev"], 2), round(m["sales_profit"], 2),
            round(m["sub_rev"], 2), round(m["sub_profit"], 2),
            round(total_rev, 2), round(total_profit, 2),
        ])
    return rows


def _refresh_monthly_summary(all_sales, all_subs):
    ws = _get_or_create_worksheet("Monthly Summary", MONTHLY_HEADERS)
    ws.clear()
    ws.append_row(MONTHLY_HEADERS)
    rows = _compute_monthly_rows(all_sales, all_subs)
    if rows:
        ws.append_rows(rows)


def push_sale(sale_row: dict, all_sales=None, all_subs=None):
    """
    Best-effort: logs and swallows errors so a Sheets outage never breaks saving data.
    If all_sales/all_subs are provided, also refreshes the Monthly Summary tab so it
    stays in sync with this new entry; pass None to skip that (e.g. for lighter callers).
    """
    if not is_configured():
        return
    try:
        ws = _get_or_create_worksheet("Sales", SALES_HEADERS)
        ws.append_row(_sale_row(sale_row))
        if all_sales is not None and all_subs is not None:
            _refresh_monthly_summary(all_sales, all_subs)
    except Exception:
        logger.exception("Failed to push sale to Google Sheets")


def push_subscription(sub_row: dict, all_sales=None, all_subs=None):
    if not is_configured():
        return
    try:
        ws = _get_or_create_worksheet("Subscriptions", SUBS_HEADERS)
        ws.append_row(_sub_row(sub_row))
        if all_sales is not None and all_subs is not None:
            _refresh_monthly_summary(all_sales, all_subs)
    except Exception:
        logger.exception("Failed to push subscription to Google Sheets")


def full_resync(all_sales, all_subs) -> tuple:
    """Clears and rewrites all three tabs from scratch. Raises on failure (used by
    /syncsheets, where the user should see the error rather than have it silently swallowed)."""
    if not is_configured():
        raise RuntimeError("Google Sheets is not configured.")

    ws_sales = _get_or_create_worksheet("Sales", SALES_HEADERS)
    ws_sales.clear()
    ws_sales.append_row(SALES_HEADERS)
    rows = [_sale_row(s) for s in all_sales]
    if rows:
        ws_sales.append_rows(rows)

    ws_subs = _get_or_create_worksheet("Subscriptions", SUBS_HEADERS)
    ws_subs.clear()
    ws_subs.append_row(SUBS_HEADERS)
    rows2 = [_sub_row(s) for s in all_subs]
    if rows2:
        ws_subs.append_rows(rows2)

    _refresh_monthly_summary(all_sales, all_subs)

    return len(all_sales), len(all_subs)
