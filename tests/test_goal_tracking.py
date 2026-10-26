import asyncio
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import database
import main
from calorie_bank import get_daily_result
from goal_tracking import (
    calculate_goal_plan,
    cancel_goal,
    create_goal,
    goal_overview,
    parse_goal_deadline,
    refresh_all_goals,
    refresh_goal,
)
from weight_tracking import TIMEZONE, calculate_daily_target, record_weight


class GoalTrackingTests(unittest.TestCase):
    # ----- prepare an isolated sqlite database -----
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_path = database.DB_PATH
        database.DB_PATH = Path(self.tempdir.name) / "goals.db"
        database.init_db()
        self.today = datetime.now(TIMEZONE).date()

    # ----- restore the configured database after each test -----
    def tearDown(self):
        database.DB_PATH = self.old_path
        self.tempdir.cleanup()

    # ----- insert a dated measurement for a test user -----
    def add_weight(self, user_id, weight, day=None, time="08:00:00"):
        measured_day = day or self.today
        return database.save_weight(
            user_id,
            weight,
            measured_day.isoformat(),
            time,
            f"{measured_day.isoformat()}T{time}+08:00",
        )

    # ----- verify numeric and calendar-date deadline inputs -----
    def test_deadline_accepts_days_and_iso_date(self):
        self.assertEqual(
            parse_goal_deadline("120", self.today),
            self.today + timedelta(days=120),
        )
        specified = self.today + timedelta(days=45)
        self.assertEqual(
            parse_goal_deadline(specified.isoformat(), self.today), specified
        )
        for argument in ("0", "-1", "3651", "1.5", "2027/03/01", "2037-01-01"):
            with self.subTest(argument=argument):
                with self.assertRaises(ValueError):
                    parse_goal_deadline(argument, self.today)

    # ----- verify the goal deficit uses the seven thousand seven hundred conversion -----
    def test_goal_deficit_and_target_calculation(self):
        deadline = self.today + timedelta(days=120)
        plan = calculate_goal_plan(80, 75, deadline, self.today)
        self.assertTrue(plan["feasible"])
        self.assertAlmostEqual(plan["required_deficit_kcal"], 5 * 7700 / 120)
        self.assertEqual(plan["days_remaining"], 120)
        self.assertGreaterEqual(plan["daily_target_kcal"], 1500)

    # ----- reject weight-gain goals and goals without a current average -----
    def test_goal_requires_weight_loss_and_a_recent_measurement(self):
        deadline = self.today + timedelta(days=120)
        with self.assertRaisesRegex(ValueError, "Record a weight"):
            create_goal(30, 75, deadline, self.today)
        self.add_weight(30, 80)
        with self.assertRaisesRegex(ValueError, "weight loss only"):
            create_goal(30, 82, deadline, self.today)

    # ----- verify goal creation preserves today's target and schedules tomorrow -----
    def test_goal_creation_uses_rolling_average_and_is_effective_tomorrow(self):
        self.add_weight(31, 81)
        self.add_weight(31, 80, time="09:00:00")
        previous_date = self.today - timedelta(days=1)
        database.set_calorie_target(31, previous_date.isoformat(), 2070)
        today_target = database.get_effective_calorie_target(31, self.today.isoformat())
        deadline = self.today + timedelta(days=120)
        plan = create_goal(31, 75, deadline, self.today)
        self.assertEqual(plan["current_weight_kg"], 80)
        self.assertEqual(database.get_effective_calorie_target(31, self.today.isoformat()), today_target)
        self.assertEqual(
            database.get_effective_calorie_target(31, (self.today + timedelta(days=1)).isoformat()),
            plan["daily_target_kcal"],
        )
        goal = database.get_active_calorie_goal(31)
        self.assertEqual(goal["deadline"], deadline.isoformat())
        self.assertEqual(goal["status"], "active")

    # ----- reject unsafe timelines without creating a goal or lowering the target -----
    def test_unrealistic_deadline_is_rejected_without_target_change(self):
        self.add_weight(32, 100)
        tomorrow = self.today + timedelta(days=1)
        before = database.get_effective_calorie_target(32, tomorrow.isoformat())
        with self.assertRaisesRegex(ValueError, "Try at least"):
            create_goal(32, 70, tomorrow, self.today)
        self.assertIsNone(database.get_active_calorie_goal(32))
        self.assertEqual(database.get_effective_calorie_target(32, tomorrow.isoformat()), before)

    # ----- verify weight changes update the future goal target -----
    def test_new_weight_recalculates_an_active_goal(self):
        self.add_weight(33, 80)
        deadline = self.today + timedelta(days=150)
        create_goal(33, 75, deadline, self.today)
        record_weight(33, 79, datetime.now(TIMEZONE))
        expected = calculate_goal_plan(79, 75, deadline, self.today)
        future_date = (self.today + timedelta(days=1)).isoformat()
        self.assertEqual(
            database.get_effective_calorie_target(33, future_date),
            expected["daily_target_kcal"],
        )
        self.assertEqual(database.get_active_calorie_goal(33)["last_calculated_date"], self.today.isoformat())

    # ----- verify daily catch-up recalculates remaining days without extending the deadline -----
    def test_missed_midnight_refresh_uses_shorter_remaining_timeline(self):
        self.add_weight(34, 80)
        deadline = self.today + timedelta(days=150)
        create_goal(34, 75, deadline, self.today)
        next_day = self.today + timedelta(days=1)
        results = refresh_all_goals(next_day)
        result = next(item for item in results if item["goal"]["user_id"] == 34)
        self.assertEqual(result["plan"]["days_remaining"], 149)
        self.assertEqual(result["goal"]["deadline"], deadline.isoformat())
        self.assertEqual(result["goal"]["last_calculated_date"], next_day.isoformat())

    # ----- mark a reached target and restore the configured fixed deficit -----
    def test_reaching_target_finishes_goal_and_restores_fixed_deficit(self):
        self.add_weight(35, 80)
        deadline = self.today + timedelta(days=120)
        create_goal(35, 75, deadline, self.today)
        record_weight(35, 75, datetime.now(TIMEZONE))
        goal = database.get_latest_calorie_goal(35)
        self.assertEqual(goal["status"], "reached")
        expected = calculate_daily_target(75)
        self.assertEqual(
            database.get_effective_calorie_target(35, (self.today + timedelta(days=1)).isoformat()),
            expected,
        )

    # ----- preserve the chosen deadline when time expires -----
    def test_expired_goal_is_not_extended(self):
        self.add_weight(36, 80)
        deadline = self.today + timedelta(days=1)
        create_goal(36, 79.9, deadline, self.today)
        result = refresh_goal(36, deadline)
        self.assertEqual(result["goal"]["status"], "deadline_passed")
        self.assertEqual(result["goal"]["deadline"], deadline.isoformat())

    # ----- expire a goal on time even when its rolling average is unavailable -----
    def test_deadline_expires_without_a_recent_weight(self):
        self.add_weight(43, 80)
        deadline = self.today + timedelta(days=1)
        create_goal(43, 79.9, deadline, self.today)
        refresh_date = self.today + timedelta(days=10)
        result = refresh_goal(43, refresh_date)
        self.assertEqual(result["goal"]["status"], "deadline_passed")
        self.assertIsNone(result["plan"])
        self.assertEqual(result["goal"]["deadline"], deadline.isoformat())

    # ----- cancel a goal and preserve its history -----
    def test_cancel_restores_fixed_deficit_and_keeps_goal_record(self):
        self.add_weight(37, 80)
        create_goal(37, 75, self.today + timedelta(days=120), self.today)
        result = cancel_goal(37, self.today)
        self.assertEqual(result["goal"]["status"], "cancelled")
        self.assertEqual(database.get_latest_calorie_goal(37)["status"], "cancelled")
        self.assertEqual(result["target_kcal"], calculate_daily_target(80))

    # ----- keep goals and target updates isolated by user -----
    def test_goals_are_user_scoped_and_survive_database_reopen(self):
        self.add_weight(38, 80)
        self.add_weight(39, 90)
        deadline = self.today + timedelta(days=120)
        create_goal(38, 75, deadline, self.today)
        create_goal(39, 85, deadline, self.today)
        self.assertEqual(database.get_active_calorie_goal(38)["target_weight_kg"], 75)
        self.assertEqual(database.get_active_calorie_goal(39)["target_weight_kg"], 85)
        with sqlite3.connect(database.DB_PATH) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM calorie_goals").fetchone()[0], 2)

    # ----- preserve exercise bank credits when future goal targets change -----
    def test_future_goal_target_change_preserves_same_day_bank_credit(self):
        self.add_weight(40, 80)
        database.save_exercise(
            40, self.today.isoformat(), "10:00:00", "run", "run", 30, 8, 201,
            weight_kg=80,
        )
        before = get_daily_result(40, self.today.isoformat())
        create_goal(40, 75, self.today + timedelta(days=120), self.today)
        after = get_daily_result(40, self.today.isoformat())
        self.assertEqual(after["exercise_bonus"], before["exercise_bonus"])
        self.assertEqual(after["additional_exercise"], before["additional_exercise"])

    # ----- show goal progress and report an inactive goal clearly -----
    def test_goal_overview_reports_progress_and_target(self):
        self.add_weight(41, 80)
        create_goal(41, 75, self.today + timedelta(days=120), self.today)
        overview = goal_overview(41, self.today)
        self.assertTrue(overview["active"])
        self.assertEqual(overview["average_kg"], 80)
        self.assertEqual(overview["days_remaining"], 120)
        self.assertEqual(overview["progress_percent"], 0)

    # ----- dispatch a goal command and verify its user-facing response -----
    def test_goal_command_sets_goal_and_cancellation(self):
        self.add_weight(42, 80)
        sent = []

        class Message:
            # ----- capture telegram text responses -----
            async def reply_text(self, text):
                sent.append(text)

        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=42), message=Message()
        )
        context = SimpleNamespace(args=["75", "120"])
        asyncio.run(main.goal_command(update, context))
        self.assertIn("Weight-loss goal set", sent[-1])
        self.assertIn("Effective from:", sent[-1])
        context.args = ["cancel"]
        asyncio.run(main.goal_command(update, context))
        self.assertIn("Goal cancelled", sent[-1])


if __name__ == "__main__":
    unittest.main()
