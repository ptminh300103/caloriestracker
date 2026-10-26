import sqlite3
import tempfile
import unittest
import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import database
import main
from calorie_bank import get_daily_result
from weight_tracking import (
    TIMEZONE,
    STARTING_WEIGHT_KG,
    calculate_bmr,
    calculate_daily_target,
    can_auto_record_weight,
    extract_weight_update,
    record_weight,
    rolling_average,
    weight_for_exercise,
)


class WeightTrackingTests(unittest.TestCase):
# ----- prepare isolated test data and storage -----
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_path = database.DB_PATH
        database.DB_PATH = Path(self.tempdir.name) / "weights.db"
        database.init_db()

# ----- clean up isolated test data and storage -----
    def tearDown(self):
        database.DB_PATH = self.old_path
        self.tempdir.cleanup()

# ----- save a dated test weight measurement -----
    def add_measurement(self, user_id, weight, day, time="08:00:00"):
        return database.save_weight(
            user_id, weight, day, time,
            f"{day}T{time}+08:00",
        )

# ----- verify initial bmr and target formula -----
    def test_initial_bmr_and_target_formula(self):
        self.assertEqual(calculate_bmr(82), 1806.875)
        # Exact formula gives 2084.453125, rounded half-up to 2084.
        self.assertEqual(calculate_daily_target(82), 2084)
        self.assertEqual(calculate_daily_target(80.5), 2064)

# ----- verify rolling average uses latest per day and skips missing days -----
    def test_rolling_average_uses_latest_per_day_and_skips_missing_days(self):
        for day, weight in (
            ("2026-10-14", 90),  # outside the seven-day window
            ("2026-10-15", 82),
            ("2026-10-17", 80),
            ("2026-10-21", 82),
            ("2026-10-21", 81),  # later time wins
        ):
            self.add_measurement(1, weight, day, "07:00:00" if weight == 82 else "08:00:00")
        result = rolling_average(1, "2026-10-21")
        self.assertEqual(result["average_kg"], 81.0)
        self.assertEqual(result["measured_days"], 3)
        self.assertEqual(len(database.get_weight_history(1, 20)), 5)

# ----- verify empty rolling window has no invented measurement -----
    def test_empty_rolling_window_has_no_invented_measurement(self):
        self.add_measurement(1, 79, "2026-10-10")
        result = rolling_average(1, "2026-10-21")
        self.assertIsNone(result["average_kg"])
        self.assertEqual(result["measured_days"], 0)
        self.assertEqual(database.get_effective_calorie_target(1, "2026-10-21"), 2084)

# ----- verify new target is effective tomorrow and keeps today target -----
    def test_new_target_is_effective_tomorrow_and_keeps_today_target(self):
        moment = datetime.now(TIMEZONE).replace(hour=9, minute=30, second=0, microsecond=0)
        today = moment.date()
        tomorrow = today + timedelta(days=1)
        result = record_weight(7, 80.5, moment)
        self.assertEqual(result["current_target"], 2084)
        self.assertTrue(result["target_changed"])
        self.assertEqual(result["target_date"], tomorrow)
        self.assertEqual(database.get_effective_calorie_target(7, today.isoformat()), 2084)
        expected = calculate_daily_target(80.5)
        self.assertEqual(database.get_effective_calorie_target(7, tomorrow.isoformat()), expected)

# ----- verify same day updates upsert tomorrow and avoid duplicate targets -----
    def test_same_day_updates_upsert_tomorrow_and_avoid_duplicate_targets(self):
        moment = datetime.now(TIMEZONE).replace(hour=8, minute=0, second=0, microsecond=0)
        day_after = moment.date() + timedelta(days=1)
        first = record_weight(11, 80.5, moment)
        second = record_weight(11, 80.5, moment + timedelta(hours=2))
        self.assertTrue(first["target_changed"])
        self.assertFalse(second["target_changed"])
        with sqlite3.connect(database.DB_PATH) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM calorie_targets WHERE user_id = ? AND effective_date = ?",
                (11, day_after.isoformat()),
            ).fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM weight_logs WHERE user_id = ?",
                (11,),
            ).fetchone()[0], 2)

        updated = record_weight(11, 81.5, moment + timedelta(hours=3))
        self.assertTrue(updated["target_changed"])
        self.assertEqual(updated["average_kg"], 81.5)
        self.assertEqual(database.get_effective_calorie_target(11, day_after.isoformat()),
                         calculate_daily_target(81.5))
        with sqlite3.connect(database.DB_PATH) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM calorie_targets WHERE user_id = ? AND effective_date = ?",
                (11, day_after.isoformat()),
            ).fetchone()[0], 1)

# ----- verify existing historical targets are preserved -----
    def test_existing_historical_targets_are_preserved(self):
        current = datetime.now(TIMEZONE)
        first_target_day = current.date() - timedelta(days=10)
        old_day = current.date() - timedelta(days=2)
        database.set_calorie_target(5, first_target_day.isoformat(), 2070)
        database.set_calorie_target(5, old_day.isoformat(), 2060)
        record_weight(5, 79.5, current)
        self.assertEqual(database.get_effective_calorie_target(5, first_target_day.isoformat()), 2070)
        self.assertEqual(database.get_effective_calorie_target(5, old_day.isoformat()), 2060)

# ----- verify weight change does not rewrite finalized daily results -----
    def test_weight_change_does_not_rewrite_finalized_daily_results(self):
        today = datetime.now(TIMEZONE).date()
        old_day = today - timedelta(days=3)
        database.set_calorie_target(61, (today - timedelta(days=10)).isoformat(), 2075)
        database.save_food(61, old_day.isoformat(), "12:00:00", "meal", [], 900)
        before = get_daily_result(61, old_day.isoformat())
        self.assertEqual(before["finalized"], 1)
        record_weight(61, 76.5, datetime.now(TIMEZONE))
        after = get_daily_result(61, old_day.isoformat())
        for key in ("base_target", "food_consumed", "status", "finalized", "finalized_at"):
            self.assertEqual(after[key], before[key])

# ----- verify latest measured weight drives future exercise and fallback -----
    def test_latest_measured_weight_drives_future_exercise_and_fallback(self):
        self.assertEqual(weight_for_exercise(20), STARTING_WEIGHT_KG)
        moment = datetime.now(TIMEZONE)
        record_weight(20, 78.4, moment)
        self.assertEqual(weight_for_exercise(20), 78.4)

# ----- verify confirmed exercise calories are not retroactively changed -----
    def test_confirmed_exercise_calories_are_not_retroactively_changed(self):
        exercise_id = database.save_exercise(
            20, "2026-10-21", "09:00:00", "Walk 30 min", "Walk", 30,
            3.0, 80, weight_kg=82,
        )
        session_id = database.save_workout_session(
            "weight-stable-session", 20, "2026-10-21", "10:00:00", 45,
            "reported", 82, 3.5, 160, [{"name": "Squat", "sets": 3}],
        )
        record_weight(20, 75, datetime.now(TIMEZONE))
        exercise = database.get_exercise_by_date(20, "2026-10-21")[0]
        session = database.get_workout_sessions_by_date(20, "2026-10-21")[0]
        self.assertEqual(exercise["id"], exercise_id)
        self.assertEqual(exercise["weight_kg"], 82)
        self.assertEqual(exercise["additional_calories"], 80)
        self.assertEqual(session["id"], session_id)
        self.assertEqual(session["weight_kg"], 82)
        self.assertEqual(session["additional_calories"], 160)

# ----- verify measurements and targets are isolated by user and survive reopen -----
    def test_measurements_and_targets_are_isolated_by_user_and_survive_reopen(self):
        moment = datetime.now(TIMEZONE)
        record_weight(1, 80, moment)
        record_weight(2, 90, moment)
        self.assertEqual(weight_for_exercise(1), 80)
        self.assertEqual(weight_for_exercise(2), 90)
        self.assertEqual(len(database.get_weight_history(1)), 1)
        self.assertEqual(len(database.get_weight_history(2)), 1)
        target = database.get_effective_calorie_target(
            1, (moment.date() + timedelta(days=1)).isoformat()
        )
        with sqlite3.connect(database.DB_PATH) as conn:
            self.assertEqual(conn.execute(
                "SELECT base_target FROM calorie_targets WHERE user_id = ?",
                (1,),
            ).fetchone()[0], target)

# ----- verify natural language weight parser is explicit and conversation safe -----
    def test_natural_language_weight_parser_is_explicit_and_conversation_safe(self):
        self.assertEqual(extract_weight_update("I weigh 80.5 kg"), 80.5)
        self.assertEqual(extract_weight_update("My weight is 81 kg."), 81.0)
        self.assertIsNone(extract_weight_update("Bench press 80.5 kg x 5"))
        self.assertIsNone(extract_weight_update("Weight 80.5 kg"))
        self.assertIsNone(extract_weight_update("I weigh 80.5 kg after squats"))
        self.assertTrue(can_auto_record_weight({}))
        self.assertFalse(can_auto_record_weight({"workout_state": "awaiting_more"}))
        self.assertFalse(can_auto_record_weight({"food_state": "clarifying"}))
        self.assertFalse(can_auto_record_weight({"exercise_state": "editing"}))

# ----- verify natural language update is saved without ai classification -----
    def test_natural_language_update_is_saved_without_ai_classification(self):
        class Message:
            text = "I weigh 80.5 kg"

# ----- initialize the test helper -----
            def __init__(self):
                self.sent = []
                self.from_user = SimpleNamespace(id=44)

# ----- capture text replies sent by the handler -----
            async def reply_text(self, text, **kwargs):
                self.sent.append(text)

# ----- execute the asynchronous test scenario -----
        async def run():
            message = Message()
            update = SimpleNamespace(message=message,
                                     effective_user=SimpleNamespace(id=44))
            context = SimpleNamespace(user_data={})
            with patch.object(main, "classify_message", side_effect=AssertionError("AI should not classify weigh-ins")):
                await main.handle_text(update, context)
            return message

        message = asyncio.run(run())
        self.assertEqual(len(database.get_weight_history(44)), 1)
        self.assertIn("Weight recorded: 80.5 kg", message.sent[0])

# ----- verify main exercise estimator receives latest measured weight -----
    def test_main_exercise_estimator_receives_latest_measured_weight(self):
        record_weight(51, 77.3, datetime.now(TIMEZONE))
        captured = {}

# ----- mock the activity calorie estimator -----
        async def estimator(**kwargs):
            captured.update(kwargs)
            return {
                "activity": "Walk", "duration_minutes": 30,
                "met": 3.0, "additional_calories": 77,
                "clarification_needed": False,
            }

        class Message:
# ----- initialize the test helper -----
            def __init__(self):
                self.from_user = SimpleNamespace(id=51)
                self.sent = []

# ----- capture text replies sent by the handler -----
            async def reply_text(self, text, **kwargs):
                self.sent.append(text)

# ----- execute the asynchronous test scenario -----
        async def run():
            message = Message()
            context = SimpleNamespace(user_data={})
            with patch.object(main, "estimate_exercise", estimator):
                await main.process_exercise(message, context, "Walk 30 min")

        asyncio.run(run())
        self.assertEqual(captured["weight_kg"], 77.3)

# ----- verify workout session calculation uses latest measured weight -----
    def test_workout_session_calculation_uses_latest_measured_weight(self):
        record_weight(52, 75, datetime.now(TIMEZONE))

        class Message:
# ----- initialize the test helper -----
            def __init__(self):
                self.from_user = SimpleNamespace(id=52)
                self.sent = []

# ----- capture text replies sent by the handler -----
            async def reply_text(self, text, **kwargs):
                self.sent.append(text)

# ----- execute the asynchronous test scenario -----
        async def run():
            message = Message()
            context = SimpleNamespace(user_data={
                "pending_workout": {
                    "id": "latest-weight", "exercises": [
                        {"name": "Squat", "activity_type": "strength", "sets": 3},
                    ],
                },
            })
            await main.finalize_workout_duration(message, context, "30 min")
            return context

        context = asyncio.run(run())
        pending = context.user_data["pending_workout"]
        self.assertEqual(pending["weight_kg"], 75)
        self.assertEqual(pending["estimate"], round((3.5 - 1) * 75 * 30 / 60))

# ----- verify implausible weights are rejected -----
    def test_implausible_weights_are_rejected(self):
        for value in (0, -1, 10, 400, "not a number"):
            with self.assertRaises(ValueError):
                record_weight(1, value)


if __name__ == "__main__":
    unittest.main()
