import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import database
import main
from calorie_bank import (
    _calculate as calculate_legacy,
    _sources,
    calculate_daily_summary,
    get_daily_result,
)
from historical_debt_import import apply_import, preview_import
from monthly_report import monthly_data
from weight_tracking import TIMEZONE


class HistoricalDebtImportTests(unittest.TestCase):
    # ----- prepare isolated database and activation window -----
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_path = database.DB_PATH
        database.DB_PATH = Path(self.tempdir.name) / "historical_import.db"
        database.init_db()
        self.user_id = 8101
        self.today = datetime.now(TIMEZONE).date()
        self.activation = self.today - timedelta(days=1)
        database.ensure_calorie_account(self.user_id, self.activation.isoformat())
        database.set_calorie_target(
            self.user_id, (self.activation - timedelta(days=8)).isoformat(), 100
        )

    # ----- restore the configured database after each test -----
    def tearDown(self):
        database.DB_PATH = self.old_path
        self.tempdir.cleanup()

    # ----- add raw historical food without triggering recalculation -----
    def add_food(self, day, calories):
        with sqlite3.connect(database.DB_PATH) as connection:
            return connection.execute(
                """INSERT INTO food_logs (
                    user_id, date, time, description, items, calories
                ) VALUES (?, ?, '12:00', 'historical meal', '[]', ?)""",
                (self.user_id, day.isoformat(), calories),
            ).lastrowid

    # ----- add raw historical exercise without triggering recalculation -----
    def add_exercise(self, day, calories):
        with sqlite3.connect(database.DB_PATH) as connection:
            return connection.execute(
                """INSERT INTO exercise_logs (
                    user_id, date, time, description, activity, duration_minutes,
                    met, additional_calories
                ) VALUES (?, ?, '10:00', 'historical run', 'run', 30, 6, ?)""",
                (self.user_id, day.isoformat(), calories),
            ).lastrowid

    # ----- freeze source-based legacy daily results for the import window -----
    def finalize_window(self, through=None):
        last_day = through or (self.activation - timedelta(days=1))
        with sqlite3.connect(database.DB_PATH) as connection:
            connection.row_factory = sqlite3.Row
            food, exercises, targets = _sources(connection, self.user_id)
            calculation = calculate_legacy(food, exercises, targets, last_day)
            by_date = {item["date"]: item for item in calculation["days"]}
            first_day = self.activation - timedelta(days=7)
            for offset in range((last_day - first_day).days + 1):
                day = first_day + timedelta(days=offset)
                item = by_date.get(day.isoformat())
                if not item:
                    continue
                connection.execute(
                    """INSERT OR REPLACE INTO daily_calorie_results (
                        user_id, date, base_target, food_consumed,
                        additional_exercise, exercise_bonus, allowance_before_bank,
                        bank_available, bank_used, bank_balance_after,
                        remaining_allowance, status, finalized, finalized_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'legacy-final')""",
                    (
                        self.user_id, item["date"], item["base_target"],
                        item["food_consumed"], item["additional_exercise"],
                        item["exercise_bonus"], item["allowance_before_bank"],
                        item["bank_available"], item["bank_used"],
                        item["bank_balance_after"], item["remaining_allowance"],
                        item["status"],
                    ),
                )

    # ----- preview multiple eligible days and ignore activation date -----
    def test_preview_uses_seven_days_before_activation_only(self):
        debt_day = self.activation - timedelta(days=2)
        second_day = self.activation - timedelta(days=1)
        self.add_food(debt_day, 250)
        self.add_food(second_day, 300)
        self.add_food(self.activation, 5000)
        self.finalize_window()

        preview = preview_import(self.user_id, self.today)

        self.assertEqual(preview["window_start"], (self.activation - timedelta(days=7)).isoformat())
        self.assertEqual(preview["window_end"], (self.activation - timedelta(days=1)).isoformat())
        self.assertEqual(
            [(item["date"], item["uncovered_excess"]) for item in preview["items"] if item["state"] == "proposed"],
            [(debt_day.isoformat(), 150), (second_day.isoformat(), 200)],
        )
        self.assertNotIn(self.activation.isoformat(), {item["date"] for item in preview["items"]})
        self.assertEqual(preview["total_proposed_debt"], 350)

    # ----- subtract historically used positive credits exactly once -----
    def test_preview_uses_legacy_bank_withdrawal_in_uncovered_excess(self):
        earning_day = self.activation - timedelta(days=3)
        food_day = self.activation - timedelta(days=2)
        self.add_exercise(earning_day, 100)
        self.add_food(food_day, 200)
        self.finalize_window()

        preview = preview_import(self.user_id, self.today)

        candidate = next(item for item in preview["items"] if item["date"] == food_day.isoformat())
        self.assertEqual(candidate["bank_used"], 50)
        self.assertEqual(candidate["uncovered_excess"], 50)

    # ----- carry a newly available legacy credit through the import preview -----
    def test_preview_preserves_positive_legacy_balance_after_import(self):
        day = self.activation - timedelta(days=1)
        exercise_id = self.add_exercise(day, 100)
        self.add_food(day, 250)
        self.finalize_window()
        with sqlite3.connect(database.DB_PATH) as connection:
            connection.execute(
                """INSERT INTO calorie_bank_deposits (
                    user_id, source_type, source_id, earning_date, available_date,
                    expiration_date, original_calories, remaining_calories
                ) VALUES (?, 'exercise', ?, ?, ?, ?, 50, 50)""",
                (
                    self.user_id, exercise_id, day.isoformat(),
                    self.activation.isoformat(),
                    (day + timedelta(days=3)).isoformat(),
                ),
            )

        preview = preview_import(self.user_id, self.today)

        self.assertEqual(preview["total_proposed_debt"], 100)
        self.assertEqual(preview["existing_positive_bank_balance"], 50)
        self.assertEqual(preview["expected_positive_bank_balance"], 50)
        self.assertEqual(preview["expected_outstanding_debt"], 100)

    # ----- exclude incomplete and provisional days from import candidates -----
    def test_incomplete_and_unfinalized_days_are_not_imported(self):
        day = self.activation - timedelta(days=1)
        self.add_food(day, 500)
        self.finalize_window()
        with sqlite3.connect(database.DB_PATH) as connection:
            connection.execute(
                "UPDATE daily_calorie_results SET finalized = 0 WHERE user_id = ? AND date = ?",
                (self.user_id, day.isoformat()),
            )

        preview = preview_import(self.user_id, self.today)

        self.assertFalse(any(item["state"] == "proposed" for item in preview["items"]))
        self.assertEqual(
            next(item for item in preview["items"] if item["date"] == day.isoformat())["reason"],
            "daily result was not finalized",
        )

    # ----- reject historical source and snapshot mismatches -----
    def test_preview_blocks_missing_or_ambiguous_historical_information(self):
        day = self.activation - timedelta(days=1)
        self.add_food(day, 500)

        missing = preview_import(self.user_id, self.today)
        self.assertFalse(missing["ready"])
        self.assertTrue(any("no finalized daily result" in blocker for blocker in missing["blockers"]))

        self.finalize_window()
        with sqlite3.connect(database.DB_PATH) as connection:
            connection.execute(
                "UPDATE daily_calorie_results SET allowance_before_bank = 50 "
                "WHERE user_id = ? AND date = ?",
                (self.user_id, day.isoformat()),
            )
        mismatched = preview_import(self.user_id, self.today)
        self.assertFalse(mismatched["ready"])
        self.assertTrue(any("snapshot differs" in blocker for blocker in mismatched["blockers"]))

    # ----- guarantee preview does not create or alter database records -----
    def test_preview_is_read_only_and_has_a_reviewable_batch_id(self):
        day = self.activation - timedelta(days=1)
        self.add_food(day, 250)
        self.finalize_window()
        with sqlite3.connect(database.DB_PATH) as connection:
            before = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "food_logs", "daily_calorie_results", "calorie_debts",
                    "historical_debt_import_batches", "historical_debt_import_items",
                )
            }

        preview = preview_import(self.user_id, self.today)

        with sqlite3.connect(database.DB_PATH) as connection:
            after = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in before
            }
        self.assertEqual(before, after)
        self.assertTrue(preview["ready"])
        self.assertEqual(len(preview["batch_id"]), 32)
        self.assertEqual(preview["existing_outstanding_debt"], 0)

    # ----- atomically import debt while preserving source and snapshot rows -----
    def test_import_requires_exact_batch_confirmation_and_keeps_original_records(self):
        day = self.activation - timedelta(days=1)
        food_id = self.add_food(day, 250)
        self.finalize_window()
        preview = preview_import(self.user_id, self.today)
        with sqlite3.connect(database.DB_PATH) as connection:
            original_daily = tuple(connection.execute(
                "SELECT * FROM daily_calorie_results WHERE user_id = ? AND date = ?",
                (self.user_id, day.isoformat()),
            ).fetchone())

        with self.assertRaisesRegex(RuntimeError, "stale or.*incorrect"):
            apply_import(self.user_id, "yes", self.today)
        result = apply_import(self.user_id, preview["batch_id"], self.today)

        self.assertTrue(Path(result["backup_path"]).exists())
        self.assertEqual(result["imported_debts"], [{
            "date": day.isoformat(), "calories": 150,
            "recovery_deadline": (day + timedelta(days=7)).isoformat(),
        }])
        with sqlite3.connect(database.DB_PATH) as connection:
            self.assertEqual(connection.execute(
                "SELECT calories FROM food_logs WHERE id = ?", (food_id,)
            ).fetchone()[0], 250)
            self.assertEqual(tuple(connection.execute(
                "SELECT * FROM daily_calorie_results WHERE user_id = ? AND date = ?",
                (self.user_id, day.isoformat()),
            ).fetchone()), original_daily)
            debt = connection.execute(
                """SELECT origin, original_calories, remaining_calories,
                    recovery_deadline, import_batch_id, source_reference,
                    imported_at, rule_version, audit_snapshot
                FROM calorie_debts WHERE user_id = ? AND source_date = ?""",
                (self.user_id, day.isoformat()),
            ).fetchone()
            self.assertEqual(debt[0:5], (
                "imported", 150, 150, (day + timedelta(days=7)).isoformat(),
                preview["batch_id"],
            ))
            self.assertIn("daily_calorie_results", debt[5])
            self.assertTrue(debt[6])
            self.assertEqual(debt[7], 1)
            self.assertTrue(json.loads(debt[8]))
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            apply_import(self.user_id, preview["batch_id"], self.today)
        self.assertFalse(preview_import(self.user_id, self.today)["ready"])

    # ----- repay imported debt oldest first within its original deadline -----
    def test_full_repayment_by_deadline_marks_historical_day_recovered(self):
        day = self.activation - timedelta(days=1)
        self.add_food(day, 250)
        self.finalize_window()
        preview = preview_import(self.user_id, self.today)
        apply_import(self.user_id, preview["batch_id"], self.today)
        database.save_exercise(
            self.user_id, self.activation.isoformat(), "10:00", "long run",
            "run", 60, 6.0, 200,
        )

        result = get_daily_result(self.user_id, day.isoformat())
        summary = calculate_daily_summary(self.user_id, day.isoformat())
        report = monthly_data(self.user_id, day.year, day.month, self.today)

        self.assertEqual(result["status"], "recovered_within_7_days")
        self.assertEqual(summary["status"], "recovered_within_7_days")
        self.assertEqual(report["recovered"], 1)
        self.assertEqual(report["over_target"], 0)
        with sqlite3.connect(database.DB_PATH) as connection:
            self.assertEqual(connection.execute(
                "SELECT SUM(calories) FROM calorie_debt_repayments WHERE user_id = ?",
                (self.user_id,),
            ).fetchone()[0], 150)

    # ----- keep partially repaid imported debt over target -----
    def test_partial_repayment_does_not_mark_day_recovered(self):
        day = self.activation - timedelta(days=1)
        self.add_food(day, 250)
        self.finalize_window()
        preview = preview_import(self.user_id, self.today)
        apply_import(self.user_id, preview["batch_id"], self.today)
        database.save_exercise(
            self.user_id, self.activation.isoformat(), "10:00", "short run",
            "run", 30, 6.0, 100,
        )

        self.assertEqual(get_daily_result(self.user_id, day.isoformat())["status"], "over_target")
        self.assertEqual(database.get_calorie_debts(self.user_id)[0]["remaining_calories"], 75)
        result = get_daily_result(self.user_id, self.activation.isoformat())
        self.assertEqual(result["exercise_bonus"], 25)

    # ----- repay imported debts oldest first across multiple source dates -----
    def test_multiple_imported_debts_repay_oldest_first(self):
        first_day = self.activation - timedelta(days=2)
        second_day = self.activation - timedelta(days=1)
        self.add_food(first_day, 200)
        self.add_food(second_day, 250)
        self.finalize_window()
        preview = preview_import(self.user_id, self.today)
        self.assertEqual(preview["total_proposed_debt"], 250)
        apply_import(self.user_id, preview["batch_id"], self.today)
        database.save_exercise(
            self.user_id, self.activation.isoformat(), "10:00", "long run",
            "run", 60, 6.0, 200,
        )

        debts = database.get_calorie_debts(self.user_id, outstanding_only=False)

        self.assertEqual(
            [(debt["source_date"], debt["remaining_calories"]) for debt in debts],
            [(first_day.isoformat(), 0), (second_day.isoformat(), 100)],
        )

    # ----- update imported debt after editing exercise on its source date -----
    def test_historical_exercise_edit_rebuilds_uncovered_excess(self):
        day = self.activation - timedelta(days=1)
        self.add_food(day, 250)
        self.finalize_window()
        preview = preview_import(self.user_id, self.today)
        apply_import(self.user_id, preview["batch_id"], self.today)

        exercise_id = database.save_exercise(
            self.user_id, day.isoformat(), "10:00", "run", "run", 30, 6.0, 100,
        )
        debt_after_add = database.get_calorie_debts(self.user_id)[0]
        database.delete_exercise(self.user_id, exercise_id)
        debt_after_delete = database.get_calorie_debts(self.user_id)[0]

        self.assertEqual(debt_after_add["original_calories"], 100)
        self.assertEqual(debt_after_delete["original_calories"], 150)
        with sqlite3.connect(database.DB_PATH) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM historical_debt_import_events "
                "WHERE event_type = 'source_edit_recalculation' AND user_id = ?",
                (self.user_id,),
            ).fetchone()[0], 2)

    # ----- roll back debts and audit records if downstream recalculation fails -----
    def test_import_rolls_back_atomically_when_ledger_rebuild_fails(self):
        day = self.activation - timedelta(days=1)
        food_id = self.add_food(day, 250)
        self.finalize_window()
        preview = preview_import(self.user_id, self.today)
        with sqlite3.connect(database.DB_PATH) as connection:
            connection.execute(
                """CREATE TRIGGER reject_imported_rebuild
                BEFORE INSERT ON unified_daily_results
                BEGIN SELECT RAISE(ABORT, 'test rollback'); END"""
            )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "test rollback"):
            apply_import(self.user_id, preview["batch_id"], self.today)

        with sqlite3.connect(database.DB_PATH) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM historical_debt_import_batches WHERE user_id = ?",
                (self.user_id,),
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM calorie_debts WHERE user_id = ? AND origin = 'imported'",
                (self.user_id,),
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT calories FROM food_logs WHERE id = ?", (food_id,)
            ).fetchone()[0], 250)

    # ----- show imported debt in the bank report after explicit import -----
    def test_bank_report_distinguishes_imported_debt(self):
        day = self.activation - timedelta(days=1)
        self.add_food(day, 250)
        self.finalize_window()
        preview = preview_import(self.user_id, self.today)
        apply_import(self.user_id, preview["batch_id"], self.today)
        message = SimpleNamespace(sent=[])

        # ----- capture the telegram response from the bank handler -----
        async def reply_text(text):
            message.sent.append(text)

        message.reply_text = reply_text
        update = SimpleNamespace(
            message=message, effective_user=SimpleNamespace(id=self.user_id)
        )
        import asyncio
        asyncio.run(main.bank_command(update, None))

        self.assertIn("Imported historical debt", message.sent[0])
        self.assertIn((day + timedelta(days=7)).isoformat(), message.sent[0])

    # ----- preserve over-target status when debt is repaid after its deadline -----
    def test_late_repayment_reduces_balance_but_does_not_recover_day(self):
        self.activation = self.today - timedelta(days=12)
        with sqlite3.connect(database.DB_PATH) as connection:
            connection.execute(
                "UPDATE calorie_account_settings SET activation_date = ? WHERE user_id = ?",
                (self.activation.isoformat(), self.user_id),
            )
        database.set_calorie_target(
            self.user_id, (self.activation - timedelta(days=8)).isoformat(), 100
        )
        day = self.activation - timedelta(days=1)
        self.add_food(day, 250)
        self.finalize_window()
        preview = preview_import(self.user_id, self.today)
        apply_import(self.user_id, preview["batch_id"], self.today)
        database.save_exercise(
            self.user_id, self.today.isoformat(), "10:00", "late run",
            "run", 60, 6.0, 200,
        )

        self.assertEqual(get_daily_result(self.user_id, day.isoformat())["status"], "over_target")
        self.assertEqual(
            database.get_calorie_debts(self.user_id, outstanding_only=False)[0]["remaining_calories"],
            0,
        )

    # ----- update imported debt after editing its original food record -----
    def test_historical_food_edit_rebuilds_imported_debt_and_audit_trail(self):
        day = self.activation - timedelta(days=1)
        food_id = self.add_food(day, 250)
        self.finalize_window()
        preview = preview_import(self.user_id, self.today)
        apply_import(self.user_id, preview["batch_id"], self.today)

        database.update_food_calories(self.user_id, food_id, 80)

        self.assertEqual(get_daily_result(self.user_id, day.isoformat())["status"], "on_target")
        self.assertEqual(database.get_calorie_debts(self.user_id), [])
        self.assertTrue(database.delete_food(self.user_id, food_id))
        self.assertEqual(get_daily_result(self.user_id, day.isoformat())["status"], "incomplete")
        with sqlite3.connect(database.DB_PATH) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM historical_debt_import_events "
                "WHERE event_type = 'source_edit_recalculation' AND user_id = ?",
                (self.user_id,),
            ).fetchone()[0], 2)

    # ----- isolate import previews and batches by user -----
    def test_users_have_independent_import_previews(self):
        other = 8102
        database.ensure_calorie_account(other, self.activation.isoformat())
        day = self.activation - timedelta(days=1)
        self.add_food(day, 250)
        self.finalize_window()

        own = preview_import(self.user_id, self.today)
        other_preview = preview_import(other, self.today)

        self.assertTrue(own["ready"])
        self.assertFalse(other_preview["ready"])
        self.assertEqual(other_preview["total_proposed_debt"], 0)


if __name__ == "__main__":
    unittest.main()
