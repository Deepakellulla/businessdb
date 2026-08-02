import base64
import json
import logging

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


def push_sale(sale_row: dict):
    """Best-effort: logs and swallows errors so a Sheets outage never breaks saving data."""
    if not is_configured():
        return
    try:
        ws = _get_or_create_worksheet("Sales", SALES_HEADERS)
        ws.append_row(_sale_row(sale_row))
    except Exception:
        logger.exception("Failed to push sale to Google Sheets")


def push_subscription(sub_row: dict):
    if not is_configured():
        return
    try:
        ws = _get_or_create_worksheet("Subscriptions", SUBS_HEADERS)
        ws.append_row(_sub_row(sub_row))
    except Exception:
        logger.exception("Failed to push subscription to Google Sheets")


def full_resync(all_sales, all_subs) -> tuple:
    """Clears and rewrites both tabs from scratch. Raises on failure (used by /syncsheets,
    where the user should see the error rather than have it silently swallowed)."""
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

    return len(all_sales), len(all_subs)
