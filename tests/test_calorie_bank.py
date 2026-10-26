import tempfile
import unittest
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import database
from calorie_bank import (
    get_bank_deposits,
    get_daily_result,
    recalculate_all,
    recalculate_user,
)


class CalorieBankTests(unittest.TestCase):
# ----- prepare isolated test data and storage -----
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_path = database.DB_PATH
        database.DB_PATH = Path(self.tempdir.name) / "bank_test.db"
        database.init_db()
        database.ensure_calorie_account(1, "2025-12-31")
        database.ensure_calorie_account(2, "2025-12-31")

# ----- clean up isolated test data and storage -----
    def tearDown(self):
        database.DB_PATH = self.old_path
        self.tempdir.cleanup()

# ----- create a confirmed test food record -----
    def food(self, user, day, calories):
        return database.save_food(user, day, "12:00:00", "test meal", [], calories)

# ----- create a confirmed test exercise record -----
    def exercise(self, user, day, calories):
        return database.save_exercise(
            user, day, "10:00:00", "test activity", "run", 30, 8, calories
        )

# ----- retrieve a test user’s daily calorie result -----
    def result(self, user, day):
        return recalculate_user(user, date.fromisoformat(day))

# ----- verify split and odd rounding sum to each source calories -----
    def test_split_and_odd_rounding_sum_to_each_source_calories(self):
        self.exercise(1, "2026-01-01", 5)
        result = self.result(1, "2026-01-01")
        deposit = get_bank_deposits(1, "2026-01-02")[0]
        self.assertEqual(result["exercise_bonus"], 2)
        self.assertEqual(deposit["original_calories"], 3)
        self.assertEqual(result["exercise_bonus"] + deposit["original_calories"], 5)

# ----- verify deposit is not available on earning day and expires after three days -----
    def test_deposit_is_not_available_on_earning_day_and_expires_after_three_days(self):
        self.exercise(1, "2026-01-01", 100)
        self.result(1, "2026-01-01")
        self.assertEqual(get_bank_deposits(1, "2026-01-01"), [])
        self.assertEqual(len(get_bank_deposits(1, "2026-01-02")), 1)
        self.assertEqual(len(get_bank_deposits(1, "2026-01-04")), 1)
        self.assertEqual(get_bank_deposits(1, "2026-01-05"), [])

# ----- verify earliest expiring deposit is spent first -----
    def test_earliest_expiring_deposit_is_spent_first(self):
        first_id = self.exercise(1, "2026-01-01", 100)
        second_id = self.exercise(1, "2026-01-02", 100)
        self.food(1, "2026-01-03", 2149)
        result = self.result(1, "2026-01-03")
        self.assertEqual(result["bank_used"], 65)
        deposits = get_bank_deposits(1, "2026-01-03")
        self.assertEqual(deposits[0]["source_id"], second_id)
        self.assertEqual(deposits[0]["remaining_calories"], 35)
        with sqlite3.connect(database.DB_PATH) as conn:
            first_spent = conn.execute("""
                SELECT SUM(w.calories) FROM calorie_bank_withdrawals w
                JOIN calorie_bank_deposits d ON d.id = w.deposit_id
                WHERE d.source_type = 'exercise' AND d.source_id = ?
            """, (first_id,)).fetchone()[0]
        self.assertEqual(first_spent, 50)

# ----- verify multiple deposits and no normal allowance rollover -----
    def test_multiple_deposits_and_no_normal_allowance_rollover(self):
        self.exercise(1, "2026-01-01", 101)
        self.exercise(1, "2026-01-02", 81)
        self.food(1, "2026-01-01", 1000)
        self.food(1, "2026-01-03", 2085)
        self.result(1, "2026-01-03")
        day3 = get_daily_result(1, "2026-01-03")
        self.assertEqual(day3["bank_used"], 1)
        # Only exercise deposits are eligible; unused Jan 1 food allowance
        # contributes nothing to the bank.
        self.assertEqual(day3["bank_available"], 92)

# ----- verify repeated calculation is idempotent -----
    def test_repeated_calculation_is_idempotent(self):
        self.exercise(1, "2026-01-01", 100)
        self.food(1, "2026-01-02", 2135)
        first = self.result(1, "2026-01-02")
        deposits1 = get_bank_deposits(1, "2026-01-02")
        second = self.result(1, "2026-01-02")
        deposits2 = get_bank_deposits(1, "2026-01-02")
        self.assertEqual(first, second)
        self.assertEqual(deposits1, deposits2)
        self.assertEqual(second["bank_used"], 50)

# ----- verify food edits and deletions recalculate historical spending -----
    def test_food_edits_and_deletions_recalculate_historical_spending(self):
        self.exercise(1, "2026-01-01", 100)
        food_id = self.food(1, "2026-01-02", 2200)
        result = self.result(1, "2026-01-02")
        self.assertEqual(result["bank_used"], 50)
        database.update_food_calories(1, food_id, 2084)
        self.assertEqual(self.result(1, "2026-01-02")["bank_used"], 0)
        database.delete_food(1, food_id)
        self.assertEqual(self.result(1, "2026-01-02")["status"], "incomplete")

# ----- verify exercise edits and deletions recalculate deposits -----
    def test_exercise_edits_and_deletions_recalculate_deposits(self):
        exercise_id = self.exercise(1, "2026-01-01", 100)
        before = self.result(1, "2026-01-02")
        self.assertEqual(before["bank_available"], 50)
        database.update_exercise_additional_calories(1, exercise_id, 61)
        self.assertEqual(self.result(1, "2026-01-02")["bank_available"], 31)
        database.delete_exercise(1, exercise_id)
        self.assertEqual(self.result(1, "2026-01-02")["bank_available"], 0)

# ----- verify workout sessions are sources and can be revised or deleted -----
    def test_workout_sessions_are_sources_and_can_be_revised_or_deleted(self):
        session_id = database.save_workout_session(
            "session", 1, "2026-01-01", "10:00:00", 30, "reported", 82, 3.5,
            101, [{"name": "Squat", "sets": 3}],
        )
        self.assertEqual(self.result(1, "2026-01-02")["bank_available"], 51)
        database.update_workout_additional_calories(1, session_id, 80)
        self.assertEqual(self.result(1, "2026-01-02")["bank_available"], 40)
        database.delete_workout_session(1, session_id)
        self.assertEqual(self.result(1, "2026-01-02")["bank_available"], 0)

# ----- verify restart catchup and midnight finalization are idempotent -----
    def test_restart_catchup_and_midnight_finalization_are_idempotent(self):
        self.food(1, "2026-01-01", 1500)
        self.food(1, "2026-01-03", 1500)
        recalculate_all(date(2026, 1, 4))
        day1 = get_daily_result(1, "2026-01-01")
        self.assertEqual(day1["finalized"], 1)
        self.assertEqual(get_daily_result(1, "2026-01-02")["status"], "incomplete")
        finalized_at = day1["finalized_at"]
        recalculate_all(date(2026, 1, 4))
        self.assertEqual(get_daily_result(1, "2026-01-01")["finalized_at"], finalized_at)
        self.assertEqual(get_daily_result(1, "2026-01-03")["finalized"], 1)

# ----- verify historical edit recalculates later bank use and target is effective dated -----
    def test_historical_edit_recalculates_later_bank_use_and_target_is_effective_dated(self):
        self.exercise(1, "2026-01-01", 100)
        food_id = self.food(1, "2026-01-03", 2050)
        database.set_calorie_target(1, "2026-01-02", 2000)
        result = self.result(1, "2026-01-03")
        self.assertEqual(result["base_target"], 2000)
        self.assertEqual(result["bank_used"], 50)
        database.update_food_calories(1, food_id, 2000)
        self.assertEqual(self.result(1, "2026-01-03")["bank_used"], 0)
        self.assertEqual(get_daily_result(1, "2026-01-01")["base_target"], 2084)

# ----- verify legacy finalized target is anchored before new default -----
    def test_legacy_finalized_target_is_anchored_before_new_default(self):
        yesterday = datetime.now(ZoneInfo("Asia/Singapore")).date() - timedelta(days=1)
        day = yesterday.isoformat()
        with sqlite3.connect(database.DB_PATH) as conn:
            conn.execute(
                """INSERT INTO daily_calorie_results (
                    user_id, date, base_target, food_consumed, additional_exercise,
                    exercise_bonus, allowance_before_bank, bank_available, bank_used,
                    bank_balance_after, remaining_allowance, status, finalized, finalized_at
                ) VALUES (99, ?, 2085, 0, 0, 0, 2085, 0, 0, 0, 2085,
                    'on_target', 1, 'legacy')""", (day,)
            )
        database.init_db()
        today = yesterday + timedelta(days=1)
        self.assertEqual(database.get_effective_calorie_target(99, day), 2085)
        self.assertEqual(database.get_effective_calorie_target(99, today.isoformat()), 2084)
        rebuilt = recalculate_user(99, today)
        self.assertEqual(get_daily_result(99, day)["base_target"], 2085)
        self.assertEqual(rebuilt["base_target"], 2084)

# ----- verify finalized days retain their timestamp after recalculation -----
    def test_edit_rebuilds_finalized_day_without_duplicate_finalization(self):
        self.exercise(1, "2026-01-01", 100)
        food_id = self.food(1, "2026-01-02", 2200)
        self.result(1, "2026-01-04")
        before = get_daily_result(1, "2026-01-02")
        self.assertEqual(before["finalized"], 1)
        database.update_food_calories(1, food_id, 2099)
        after = get_daily_result(1, "2026-01-02")
        self.assertEqual(after["bank_used"], 15)
        self.assertEqual(after["finalized"], 1)
        self.assertEqual(after["finalized_at"], before["finalized_at"])
        with sqlite3.connect(database.DB_PATH) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM unified_daily_results WHERE user_id = 1 AND date = '2026-01-02'"
            ).fetchone()[0], 1)

# ----- verify users have independent banks -----
    def test_users_have_independent_banks(self):
        self.exercise(1, "2026-01-01", 100)
        self.food(2, "2026-01-02", 2200)
        recalculate_all(date(2026, 1, 2))
        self.assertEqual(get_daily_result(2, "2026-01-02")["bank_used"], 0)
        self.assertEqual(get_daily_result(2, "2026-01-02")["status"], "over_target")
        self.assertEqual(get_daily_result(1, "2026-01-02")["bank_available"], 50)

# ----- verify days without food are incomplete -----
    def test_days_without_food_are_incomplete(self):
        self.exercise(1, "2026-01-01", 100)
        self.assertEqual(self.result(1, "2026-01-03")["status"], "incomplete")


class DatabaseMigrationTests(unittest.TestCase):
# ----- verify additive migration preserves existing food rows -----
    def test_additive_migration_preserves_existing_food_rows(self):
        with tempfile.TemporaryDirectory() as tempdir:
            old_path = database.DB_PATH
            database.DB_PATH = Path(tempdir) / "legacy.db"
            try:
                with sqlite3.connect(database.DB_PATH) as conn:
                    conn.execute("""CREATE TABLE food_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                        date TEXT NOT NULL, time TEXT NOT NULL, description TEXT NOT NULL,
                        items TEXT NOT NULL, calories INTEGER NOT NULL)""")
                    conn.execute("""INSERT INTO food_logs
                        (user_id, date, time, description, items, calories)
                        VALUES (7, '2026-01-01', '12:00:00', 'legacy meal', '[]', 500)""")
                    conn.execute("""CREATE TABLE workout_exercises (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id INTEGER NOT NULL, position INTEGER NOT NULL,
                        name TEXT NOT NULL, sets INTEGER, repetitions INTEGER,
                        weight_kg REAL, resistance_type TEXT,
                        hold_duration_seconds INTEGER,
                        activity_type TEXT NOT NULL DEFAULT 'strength',
                        duration_minutes REAL, distance_km REAL,
                        pace_min_per_km REAL, intensity TEXT, met REAL, notes TEXT
                    )""")
                    conn.execute("""CREATE TABLE exercise_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL, date TEXT NOT NULL,
                        time TEXT NOT NULL, description TEXT NOT NULL,
                        activity TEXT NOT NULL, duration_minutes REAL NOT NULL,
                        met REAL NOT NULL, additional_calories INTEGER NOT NULL
                    )""")
                database.init_db()
                with sqlite3.connect(database.DB_PATH) as conn:
                    self.assertEqual(conn.execute(
                        "SELECT description, calories FROM food_logs WHERE user_id = 7"
                    ).fetchone(), ("legacy meal", 500))
                    tables = {row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )}
                    self.assertIn("calorie_bank_deposits", tables)
                    self.assertIn("daily_calorie_results", tables)
                    self.assertIn("weight_logs", tables)
                    self.assertIn("weight_kg", {
                        row[1] for row in conn.execute("PRAGMA table_info(exercise_logs)")
                    })
                    exercise_columns = {
                        row[1] for row in conn.execute(
                            "PRAGMA table_info(workout_exercises)"
                        )
                    }
                    self.assertTrue({
                        "speed_kmh", "power_watts", "equipment_context",
                        "resistance_level", "met_source",
                    }.issubset(exercise_columns))
            finally:
                database.DB_PATH = old_path


if __name__ == "__main__":
    unittest.main()
