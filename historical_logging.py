"""Date validation for one-time historical logging."""

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


TIMEZONE = ZoneInfo("Asia/Singapore")
SUPPORTED_HISTORY_START = date(2000, 1, 1)
MAX_PREV_DAYS = 3650


# ----- validate a day offset and calculate its singapore date -----
def previous_logging_date(value, today=None):
    if isinstance(value, bool):
        raise ValueError("Usage: /prev N, where N is a positive whole number of days.")
    if isinstance(value, int):
        days = value
    elif isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        try:
            days = int(value.strip())
        except ValueError as error:
            raise ValueError("Enter a whole number of days from 1 to 3650.") from error
    else:
        raise ValueError("Usage: /prev N, where N is a positive whole number of days.")

    if days < 1:
        raise ValueError("N must be a positive number of days.")
    if days > MAX_PREV_DAYS:
        raise ValueError(f"N is too large. Choose 1 to {MAX_PREV_DAYS} days ago.")

    if today is None:
        today = datetime.now(TIMEZONE).date()
    elif isinstance(today, datetime):
        today = today.astimezone(TIMEZONE).date() if today.tzinfo else today.date()
    elif isinstance(today, str):
        today = date.fromisoformat(today)

    target = today - timedelta(days=days)
    if target < SUPPORTED_HISTORY_START:
        raise ValueError(
            f"Dates before {SUPPORTED_HISTORY_START.isoformat()} are not supported."
        )
    return target.isoformat()


# ----- calculate a supported date from a nonnegative day offset -----
def day_summary_date(value, today=None):
    if isinstance(value, bool):
        raise ValueError("Usage: /day [N], where N is a whole number from 0 to 3650.")
    if isinstance(value, int):
        days = value
    elif isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        try:
            days = int(value.strip())
        except ValueError as error:
            raise ValueError(
                "Usage: /day [N], where N is a whole number from 0 to 3650."
            ) from error
    else:
        raise ValueError("Usage: /day [N], where N is a whole number from 0 to 3650.")

    if days < 0:
        raise ValueError("N cannot be negative. Use /day 0 for today.")
    if days > MAX_PREV_DAYS:
        raise ValueError(f"N is too large. Choose 0 to {MAX_PREV_DAYS} days ago.")
    if today is None:
        today = datetime.now(TIMEZONE).date()
    elif isinstance(today, datetime):
        today = today.astimezone(TIMEZONE).date() if today.tzinfo else today.date()
    elif isinstance(today, str):
        today = date.fromisoformat(today)

    target = today - timedelta(days=days)
    if target < SUPPORTED_HISTORY_START:
        raise ValueError(
            f"Dates before {SUPPORTED_HISTORY_START.isoformat()} are not supported."
        )
    return target.isoformat()
