import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import database
import main
from calorie_bank import calculate_daily_summary
from historical_logging import day_summary_date


TIMEZONE = ZoneInfo("Asia/Singapore")


class DaySummaryTests(unittest.TestCase):
    # ----- prepare isolated database storage -----
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_path = database.DB_PATH
        database.DB_PATH = Path(self.tempdir.name) / "day_summary.db"
        database.init_db()
        self.today = datetime.now(TIMEZONE).date()

    # ----- restore database storage after each test -----
    def tearDown(self):
        database.DB_PATH = self.old_path
        self.tempdir.cleanup()

    # ----- create a telegram command update and capture its reply -----
    def make_update(self, user_id, args):
        message = SimpleNamespace(sent=[])

        # ----- capture the bot's text response -----
        async def reply_text(text, **kwargs):
            message.sent.append(text)

        message.reply_text = reply_text
        update = SimpleNamespace(
            message=message,
            effective_user=SimpleNamespace(id=user_id),
        )
        context = SimpleNamespace(args=args)
        return update, context

    # ----- return the command reply for an offset -----
    def run_day(self, user_id, args):
        update, context = self.make_update(user_id, args)
        asyncio.run(main.day_command(update, context))
        return update.message.sent[0]

    # ----- verify date offsets use singapore calendar subtraction -----
    def test_day_offsets_include_today_and_historical_dates(self):
        self.assertEqual(day_summary_date(0, self.today), self.today.isoformat())
        self.assertEqual(
            day_summary_date("1", self.today), (self.today - timedelta(days=1)).isoformat()
        )
        self.assertEqual(
            day_summary_date("2", self.today), (self.today - timedelta(days=2)).isoformat()
        )
        self.assertIn(self.today.isoformat(), self.run_day(100, []))
        self.assertIn(self.today.isoformat(), self.run_day(100, ["0"]))
        self.assertIn((self.today - timedelta(days=1)).isoformat(), self.run_day(100, ["1"]))

    # ----- reject malformed and unsupported day offsets -----
    def test_invalid_offsets_return_usage_or_supported_range_message(self):
        for args in (["-1"], ["1.2"], ["abc"], ["1", "2"], ["3651"]):
            with self.subTest(args=args):
                reply = self.run_day(101, args)
                self.assertTrue("Usage:" in reply or "too large" in reply)
        with self.assertRaises(ValueError):
            day_summary_date("1", datetime(2000, 1, 1, tzinfo=TIMEZONE))
        with self.assertRaises(ValueError):
            day_summary_date(-1, self.today)

    # ----- verify historical target and bank values use the shared engine -----
    def test_historical_target_and_bank_calculations_are_applied(self):
        yesterday = (self.today - timedelta(days=1)).isoformat()
        two_days_ago = (self.today - timedelta(days=2)).isoformat()
        database.set_calorie_target(102, two_days_ago, 2100)
        database.set_calorie_target(102, yesterday, 2000)
        database.save_exercise(102, two_days_ago, "10:00", "run", "run", 30, 8, 200, 82)
        database.save_food(102, yesterday, "12:00", "lunch", [], 2050)

        summary = calculate_daily_summary(102, yesterday)
        self.assertEqual(summary["base_target"], 2000)
        self.assertEqual(summary["bank_available"], 100)
        self.assertEqual(summary["bank_used"], 50)
        self.assertEqual(summary["status"], "on_target")
        reply = self.run_day(102, ["1"])
        self.assertIn("Base target: 2000 kcal", reply)
        self.assertIn("Positive credits used: 50 kcal", reply)
        self.assertIn("finalized/recalculated", reply)

    # ----- show foodless days as incomplete and today as provisional -----
    def test_incomplete_days_and_current_day_provisional_status(self):
        today_reply = self.run_day(103, [])
        self.assertIn("incomplete/no-records", today_reply)
        self.assertIn("provisional", today_reply)
        old_reply = self.run_day(104, ["3"])
        self.assertIn("incomplete/no-records", old_reply)
        self.assertIn("finalized/recalculated", old_reply)

    # ----- ensure summary calculation does not mutate ledger or overrides -----
    def test_day_is_read_only_and_does_not_consume_prev_override(self):
        yesterday = (self.today - timedelta(days=1)).isoformat()
        database.set_pending_date_override(105, yesterday)
        before = {}
        with sqlite3.connect(database.DB_PATH) as conn:
            for table in ("daily_calorie_results", "calorie_bank_deposits", "calorie_bank_withdrawals"):
                before[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        self.run_day(105, ["1"])
        with sqlite3.connect(database.DB_PATH) as conn:
            for table, count in before.items():
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], count)
        self.assertEqual(database.get_pending_date_override(105), yesterday)


if __name__ == "__main__":
    unittest.main()
