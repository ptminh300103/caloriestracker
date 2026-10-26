import asyncio
import calendar
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import database
import main
from monthly_report import (
    COLORS,
    classify_day,
    format_caption,
    monthly_data,
    parse_month,
    render_calendar,
)


class MonthlyReportTests(unittest.TestCase):
# ----- prepare isolated test data and storage -----
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_path = database.DB_PATH
        database.DB_PATH = Path(self.tempdir.name) / "month.db"
        database.init_db()

# ----- clean up isolated test data and storage -----
    def tearDown(self):
        database.DB_PATH = self.old_path
        self.tempdir.cleanup()

# ----- verify monday first weekday alignment -----
    def test_monday_first_weekday_alignment(self):
        weeks = calendar.Calendar(firstweekday=calendar.MONDAY).monthdatescalendar(2026, 10)
        self.assertEqual(weeks[0][0], date(2026, 9, 28))
        self.assertEqual(weeks[0][3], date(2026, 10, 1))

# ----- verify leap february has 29 days and five weeks -----
    def test_leap_february_has_29_days_and_five_weeks(self):
        weeks = calendar.Calendar(firstweekday=calendar.MONDAY).monthdatescalendar(2024, 2)
        self.assertEqual(sum(day.month == 2 for week in weeks for day in week), 29)
        self.assertEqual(weeks[0][3], date(2024, 2, 1))

# ----- verify month starting sunday is aligned in sunday column -----
    def test_month_starting_sunday_is_aligned_in_sunday_column(self):
        weeks = calendar.Calendar(firstweekday=calendar.MONDAY).monthdatescalendar(2021, 8)
        sunday = next(day for week in weeks for day in week if day == date(2021, 8, 1))
        self.assertEqual(weeks[0][6], sunday)

# ----- verify day color classification -----
    def test_day_color_classification(self):
        today = date(2026, 10, 22)
        self.assertEqual(classify_day(date(2026, 10, 20), {"finalized": 1, "status": "on_target"}, today), "on_target")
        self.assertEqual(classify_day(date(2026, 10, 20), {"finalized": 1, "status": "over_target"}, today), "over_target")
        self.assertEqual(classify_day(date(2026, 10, 20), None, today), "incomplete")
        self.assertEqual(classify_day(today, {"finalized": 0, "status": "incomplete"}, today), "provisional")
        self.assertEqual(classify_day(date(2026, 10, 23), None, today), "future")
        self.assertEqual(len({COLORS[k] for k in ("on_target", "over_target", "incomplete", "provisional")}), 4)

# ----- verify empty month counts past days incomplete and has no success rate -----
    def test_empty_month_counts_past_days_incomplete_and_has_no_success_rate(self):
        report = monthly_data(1, 2026, 2, as_of="2026-10-22")
        self.assertEqual(report["incomplete"], 28)
        self.assertEqual(report["on_target"], 0)
        self.assertEqual(report["over_target"], 0)
        self.assertIsNone(report["success_rate"])
        self.assertIn("No finalized days yet", format_caption(report))

# ----- verify provisional and future days excluded from statistics -----
    def test_provisional_and_future_days_excluded_from_statistics(self):
        report = monthly_data(1, 2026, 10, as_of="2026-10-22")
        self.assertEqual(report["days"][date(2026, 10, 22)], "provisional")
        self.assertEqual(report["days"][date(2026, 10, 23)], "future")
        self.assertEqual(report["incomplete"], 21)

# ----- verify success rate uses only finalized food days -----
    def test_success_rate_uses_only_finalized_food_days(self):
        today = datetime.now(ZoneInfo("Asia/Singapore")).date()
        first, second = today - timedelta(days=2), today - timedelta(days=1)
        database.save_food(10, first.isoformat(), "12:00", "meal", [], 2000)
        database.save_food(10, second.isoformat(), "12:00", "meal", [], 2200)
        report = monthly_data(10, today.year, today.month, as_of=today)
        self.assertEqual(report["on_target"], 1)
        self.assertEqual(report["over_target"], 1)
        self.assertEqual(report["success_rate"], 50.0)

# ----- verify historical correction refreshes calendar from bank results -----
    def test_historical_correction_refreshes_calendar_from_bank_results(self):
        yesterday = datetime.now(ZoneInfo("Asia/Singapore")).date() - timedelta(days=1)
        entry_id = database.save_food(11, yesterday.isoformat(), "12:00", "meal", [], 2200)
        self.assertEqual(monthly_data(11, yesterday.year, yesterday.month)["days"][yesterday], "over_target")
        database.update_food_calories(11, entry_id, 2000)
        self.assertEqual(monthly_data(11, yesterday.year, yesterday.month)["days"][yesterday], "over_target")

# ----- verify data is isolated between users -----
    def test_data_is_isolated_between_users(self):
        today = datetime.now(ZoneInfo("Asia/Singapore")).date()
        yesterday = today - timedelta(days=1)
        database.save_food(1, yesterday.isoformat(), "12:00", "meal", [], 2000)
        database.save_food(2, yesterday.isoformat(), "12:00", "meal", [], 2200)
        self.assertEqual(monthly_data(1, today.year, today.month)["on_target"], 1)
        self.assertEqual(monthly_data(2, today.year, today.month)["over_target"], 1)

# ----- verify month input validation -----
    def test_month_input_validation(self):
        self.assertEqual(parse_month("2024-02"), (2024, 2))
        for invalid in ("2024-2", "2024-13", "abc", "2024-00"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_month(invalid)

# ----- verify empty month renders png in memory -----
    def test_empty_month_renders_png_in_memory(self):
        report = monthly_data(1, 2026, 2, as_of="2026-10-22")
        image = render_calendar(report)
        self.assertEqual(image.read(8), b"\x89PNG\r\n\x1a\n")
        image.close()

# ----- verify month command rejects invalid input -----
    def test_month_command_rejects_invalid_input(self):
        class Message:
# ----- initialize the test helper -----
            def __init__(self):
                self.replies = []

# ----- capture text replies sent by the handler -----
            async def reply_text(self, text):
                self.replies.append(text)

# ----- execute the asynchronous test scenario -----
        async def run():
            message = Message()
            update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=1))
            await main.month_command(update, SimpleNamespace(args=["2026-13"]))
            return message

        message = asyncio.run(run())
        self.assertIn("invalid", message.replies[0])


if __name__ == "__main__":
    unittest.main()
