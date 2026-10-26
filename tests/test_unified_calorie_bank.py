import asyncio
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import database
import main
from calorie_bank import get_bank_deposits, get_daily_result, recalculate_all
from monthly_report import monthly_data
from unified_calorie_bank import account_summary, recalculate_user
from weight_tracking import TIMEZONE


class UnifiedCalorieBankTests(unittest.TestCase):
    # ----- prepare an isolated database with an active unified ledger -----
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_path = database.DB_PATH
        database.DB_PATH = Path(self.tempdir.name) / "unified.db"
        database.init_db()
        self.user_id = 701
        self.today = datetime.now(TIMEZONE).date()
        self.activation = self.today - timedelta(days=12)
        database.ensure_calorie_account(self.user_id, self.activation.isoformat())

    # ----- restore the configured database after each test -----
    def tearDown(self):
        database.DB_PATH = self.old_path
        self.tempdir.cleanup()

    # ----- set a deterministic base target for the test account -----
    def set_target(self, target, effective_date=None):
        database.set_calorie_target(
            self.user_id,
            (effective_date or self.activation).isoformat(),
            target,
        )

    # ----- save confirmed test food on a Singapore calendar date -----
    def add_food(self, day, calories):
        return database.save_food(
            self.user_id, day.isoformat(), "12:00:00", "test meal", [], calories
        )

    # ----- save confirmed test exercise on a Singapore calendar date -----
    def add_exercise(self, day, calories):
        return database.save_exercise(
            self.user_id,
            day.isoformat(),
            "10:00:00",
            "test activity",
            "run",
            30,
            8,
            calories,
            80,
        )

    # ----- calculate one post-activation date for assertions -----
    def result(self, day):
        return recalculate_user(self.user_id, day)

    # ----- verify migration preserves existing rows and positive bank balance -----
    def test_versioned_migration_preserves_rows_and_legacy_credits(self):
        user_id = 702
        previous_day = self.today - timedelta(days=1)
        expiry = self.today + timedelta(days=2)
        with sqlite3.connect(database.DB_PATH) as connection:
            connection.execute(
                "INSERT INTO calorie_targets VALUES (?, ?, ?)",
                (user_id, (self.today - timedelta(days=10)).isoformat(), 2000),
            )
            connection.execute(
                """INSERT INTO food_logs (
                    user_id, date, time, description, items, calories
                ) VALUES (?, ?, '12:00:00', 'legacy meal', '[]', 500)""",
                (user_id, previous_day.isoformat()),
            )
            connection.execute(
                """INSERT INTO daily_calorie_results (
                    user_id, date, base_target, food_consumed, additional_exercise,
                    exercise_bonus, allowance_before_bank, bank_available, bank_used,
                    bank_balance_after, remaining_allowance, status, finalized, finalized_at
                ) VALUES (?, ?, 2000, 500, 0, 0, 2000, 50, 0, 50, 1500,
                    'on_target', 1, 'frozen-before-migration')""",
                (user_id, previous_day.isoformat()),
            )
            credit_id = connection.execute(
                """INSERT INTO calorie_bank_deposits (
                    user_id, source_type, source_id, earning_date, available_date,
                    expiration_date, original_calories, remaining_calories
                ) VALUES (?, 'exercise', 777, ?, ?, ?, 50, 30)""",
                (
                    user_id,
                    (self.today - timedelta(days=3)).isoformat(),
                    (self.today - timedelta(days=2)).isoformat(),
                    expiry.isoformat(),
                ),
            ).lastrowid
            connection.execute(
                """INSERT INTO calorie_bank_withdrawals
                    (user_id, date, deposit_id, calories) VALUES (?, ?, ?, 20)""",
                (user_id, previous_day.isoformat(), credit_id),
            )
            connection.execute("PRAGMA user_version = 0")
            connection.execute("DELETE FROM schema_migrations")
            connection.execute(
                "DELETE FROM calorie_account_settings WHERE user_id = ?", (user_id,)
            )

        database.init_db()
        self.assertEqual(
            database.get_calorie_account_activation(user_id), self.today.isoformat()
        )
        self.assertEqual(get_daily_result(user_id, previous_day.isoformat())["finalized_at"], "frozen-before-migration")
        with sqlite3.connect(database.DB_PATH) as connection:
            credit = connection.execute(
                """SELECT original_calories, remaining_calories, expiration_date
                FROM calorie_bank_deposits WHERE id = ?""",
                (credit_id,),
            ).fetchone()
            self.assertEqual(credit, (50, 30, expiry.isoformat()))
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0],
                2,
            )
        activation = database.get_calorie_account_activation(user_id)
        database.init_db()
        self.assertEqual(database.get_calorie_account_activation(user_id), activation)
        with sqlite3.connect(database.DB_PATH) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 2)

    # ----- carry an unexpired legacy credit into the unified ledger -----
    def test_unexpired_legacy_credit_remains_available_after_activation(self):
        self.set_target(100)
        activation = self.activation
        earning_day = activation - timedelta(days=2)
        credit_expiry = activation + timedelta(days=1)
        with sqlite3.connect(database.DB_PATH) as connection:
            credit_id = connection.execute(
                """INSERT INTO calorie_bank_deposits (
                    user_id, source_type, source_id, earning_date, available_date,
                    expiration_date, original_calories, remaining_calories
                ) VALUES (?, 'exercise', 778, ?, ?, ?, 50, 50)""",
                (
                    self.user_id,
                    earning_day.isoformat(),
                    (earning_day + timedelta(days=1)).isoformat(),
                    credit_expiry.isoformat(),
                ),
            ).lastrowid
        self.add_food(activation, 130)

        result = self.result(activation)

        self.assertEqual(result["bank_used"], 30)
        self.assertEqual(result["debt_added"], 0)
        with sqlite3.connect(database.DB_PATH) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT remaining_calories FROM calorie_bank_deposits WHERE id = ?",
                    (credit_id,),
                ).fetchone()[0],
                20,
            )

    # ----- verify sqlite backup api creates a consistent integrity-checked copy -----
    def test_sqlite_backup_api_produces_an_integrity_checked_copy(self):
        backup_path = Path(self.tempdir.name) / "verified-backup.sqlite3"
        with sqlite3.connect(database.DB_PATH) as source:
            with sqlite3.connect(backup_path) as backup:
                source.backup(backup)
                self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(
                    backup.execute("PRAGMA user_version").fetchone()[0],
                    source.execute("PRAGMA user_version").fetchone()[0],
                )

    # ----- verify 50/50 exercise allocation when no debt exists -----
    def test_no_debt_uses_fifty_fifty_exercise_allocation(self):
        self.set_target(1000)
        day = self.today - timedelta(days=2)
        exercise_id = self.add_exercise(day, 101)
        result = self.result(day)
        self.assertEqual(result["exercise_bonus"], 50)
        with sqlite3.connect(database.DB_PATH) as connection:
            credit = connection.execute(
                """SELECT original_calories, available_date, expiration_date
                FROM calorie_bank_deposits WHERE user_id = ? AND source_type = 'exercise'
                    AND source_id = ?""",
                (self.user_id, exercise_id),
            ).fetchone()
        self.assertEqual(credit, (
            51,
            (day + timedelta(days=1)).isoformat(),
            (day + timedelta(days=3)).isoformat(),
        ))

    # ----- verify food excess becomes a dated debt after available credits -----
    def test_uncovered_food_excess_creates_one_dated_debt(self):
        self.set_target(100)
        day = self.today - timedelta(days=3)
        self.add_food(day, 150)
        result = self.result(day)
        self.assertEqual(result["debt_added"], 50)
        self.assertEqual(result["status"], "over_target")
        debt = database.get_calorie_debts(self.user_id)[0]
        self.assertEqual(debt["source_date"], day.isoformat())
        self.assertEqual(debt["remaining_calories"], 50)
        self.assertEqual(debt["recovery_deadline"], (day + timedelta(days=7)).isoformat())

    # ----- verify exercise repays 75 percent toward outstanding debt -----
    def test_debt_exercise_allocation_uses_seventy_five_twenty_five_split(self):
        self.set_target(100)
        debt_day = self.today - timedelta(days=4)
        exercise_day = debt_day + timedelta(days=1)
        self.add_food(debt_day, 200)
        self.add_exercise(exercise_day, 100)
        result = self.result(exercise_day)
        self.assertEqual(result["debt_repaid"], 75)
        self.assertEqual(result["exercise_bonus"], 25)
        self.assertEqual(result["debt_outstanding"], 25)
        self.assertEqual(get_bank_deposits(self.user_id, exercise_day.isoformat()), [])

    # ----- verify excess exercise after full repayment returns to 50/50 -----
    def test_debt_fully_repaid_mid_exercise_splits_remainder_normally(self):
        self.set_target(100)
        debt_day = self.today - timedelta(days=4)
        exercise_day = debt_day + timedelta(days=1)
        self.add_food(debt_day, 150)
        exercise_id = self.add_exercise(exercise_day, 100)
        result = self.result(exercise_day)
        self.assertEqual(result["debt_repaid"], 50)
        self.assertEqual(result["exercise_bonus"], 25)
        with sqlite3.connect(database.DB_PATH) as connection:
            credit = connection.execute(
                "SELECT original_calories FROM calorie_bank_deposits "
                "WHERE user_id = ? AND source_id = ? AND source_type = 'exercise'",
                (self.user_id, exercise_id),
            ).fetchone()
        self.assertEqual(credit[0], 25)
        self.assertEqual(result["debt_outstanding"], 0)

    # ----- verify full repayment by the seventh-day deadline recovers the day -----
    def test_full_repayment_within_seven_days_recovers_original_day(self):
        self.set_target(100)
        debt_day = self.today - timedelta(days=8)
        self.add_food(debt_day, 150)
        recovery_day = debt_day + timedelta(days=7)
        self.add_exercise(recovery_day, 100)
        recovered = self.result(self.today)
        self.assertEqual(
            get_daily_result(self.user_id, debt_day.isoformat())["status"],
            "recovered_within_7_days",
        )
        self.assertGreaterEqual(recovered["debt_repaid"], 0)
        self.assertEqual(monthly_data(self.user_id, debt_day.year, debt_day.month)["recovered"], 1)

    # ----- keep an over-target day over when repayment misses its deadline -----
    def test_late_repayment_reduces_debt_without_recovering_the_day(self):
        self.set_target(100)
        debt_day = self.today - timedelta(days=10)
        self.add_food(debt_day, 150)
        late_day = debt_day + timedelta(days=8)
        self.add_exercise(late_day, 100)
        self.result(self.today)
        self.assertEqual(
            get_daily_result(self.user_id, debt_day.isoformat())["status"],
            "over_target",
        )
        debt = database.get_calorie_debts(self.user_id, outstanding_only=False)[0]
        self.assertEqual(debt["remaining_calories"], 0)

    # ----- repay multiple debts oldest first and preserve each payment -----
    def test_multiple_debts_repay_oldest_first(self):
        self.set_target(100)
        first_day = self.today - timedelta(days=5)
        second_day = first_day + timedelta(days=1)
        pay_day = second_day + timedelta(days=1)
        self.add_food(first_day, 150)
        self.add_food(second_day, 160)
        self.add_exercise(pay_day, 100)
        self.result(pay_day)
        debts = database.get_calorie_debts(self.user_id, outstanding_only=False)
        self.assertEqual([debt["remaining_calories"] for debt in debts], [0, 35])
        with sqlite3.connect(database.DB_PATH) as connection:
            repayments = connection.execute(
                """SELECT d.source_date, r.calories FROM calorie_debt_repayments r
                JOIN calorie_debts d ON d.id = r.debt_id WHERE r.user_id = ?
                ORDER BY d.source_date""",
                (self.user_id,),
            ).fetchall()
        self.assertEqual(repayments, [(first_day.isoformat(), 50), (second_day.isoformat(), 25)])

    # ----- verify positive credits expire after their third available day -----
    def test_positive_credit_expires_after_three_following_days(self):
        self.set_target(1000)
        earning_day = self.today - timedelta(days=5)
        self.add_exercise(earning_day, 100)
        self.result(self.today)
        deposits = get_bank_deposits(
            self.user_id, (earning_day + timedelta(days=4)).isoformat()
        )
        self.assertEqual(deposits, [])
        with sqlite3.connect(database.DB_PATH) as connection:
            remaining = connection.execute(
                "SELECT remaining_calories FROM calorie_bank_deposits "
                "WHERE user_id = ? AND earning_date = ?",
                (self.user_id, earning_day.isoformat()),
            ).fetchone()[0]
        self.assertEqual(remaining, 0)

    # ----- do not create new-system debt for a date before activation -----
    def test_pre_activation_food_does_not_create_debt(self):
        prior_user = 703
        activation = self.today - timedelta(days=1)
        database.ensure_calorie_account(prior_user, activation.isoformat())
        pre_activation = activation - timedelta(days=1)
        database.set_calorie_target(prior_user, pre_activation.isoformat(), 100)
        database.save_food(prior_user, pre_activation.isoformat(), "12:00", "meal", [], 500)
        recalculate_user(prior_user, self.today)
        with sqlite3.connect(database.DB_PATH) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM calorie_debts WHERE user_id = ?",
                    (prior_user,),
                ).fetchone()[0],
                0,
            )
            self.assertIsNone(connection.execute(
                "SELECT date FROM unified_daily_results WHERE user_id = ? AND date = ?",
                (prior_user, pre_activation.isoformat()),
            ).fetchone())

    # ----- preserve frozen pre-activation finalized results after source edits -----
    def test_pre_activation_finalized_result_is_not_reclassified(self):
        previous_day = self.today - timedelta(days=1)
        self.set_target(100)
        entry_id = self.add_food(previous_day, 200)
        legacy = database.save_legacy_daily_result_if_missing(
            self.user_id,
            {
                "date": previous_day.isoformat(), "base_target": 100,
                "food_consumed": 200, "additional_exercise": 0,
                "exercise_bonus": 0, "allowance_before_bank": 100,
                "bank_available": 0, "bank_used": 0, "bank_balance_after": 0,
                "remaining_allowance": -100, "status": "over_target", "finalized": 1,
            },
            "locked",
        )
        database.update_food_calories(self.user_id, entry_id, 100)
        frozen = database.get_legacy_daily_result(self.user_id, previous_day.isoformat())
        self.assertEqual(frozen["status"], "over_target")
        self.assertEqual(frozen["finalized_at"], "locked")
        self.assertEqual(legacy["food_consumed"], 200)

    # ----- recalculate post-activation historical corrections from source rows -----
    def test_post_activation_food_correction_rebuilds_debt(self):
        self.set_target(100)
        day = self.today - timedelta(days=2)
        entry_id = self.add_food(day, 150)
        self.assertEqual(self.result(day)["debt_added"], 50)
        database.update_food_calories(self.user_id, entry_id, 100)
        updated = self.result(day)
        self.assertEqual(updated["debt_added"], 0)
        self.assertEqual(updated["status"], "on_target")
        self.assertEqual(database.get_calorie_debts(self.user_id), [])

    # ----- verify user debt and bank balances are isolated -----
    def test_separate_users_have_separate_debt_and_credit_ledgers(self):
        other = 704
        database.ensure_calorie_account(other, self.activation.isoformat())
        database.set_calorie_target(other, self.activation.isoformat(), 100)
        self.set_target(100)
        debt_day = self.today - timedelta(days=2)
        self.add_food(debt_day, 150)
        database.save_food(other, debt_day.isoformat(), "12:00", "meal", [], 100)
        self.assertEqual(len(database.get_calorie_debts(self.user_id)), 1)
        self.assertEqual(database.get_calorie_debts(other), [])

    # ----- keep a failed ledger transaction from deleting previous results -----
    def test_calculation_failure_rolls_back_all_derived_ledger_changes(self):
        self.set_target(100)
        day = self.today - timedelta(days=1)
        self.add_food(day, 150)
        before = get_daily_result(self.user_id, day.isoformat())
        with sqlite3.connect(database.DB_PATH) as connection:
            connection.execute(
                f"""CREATE TRIGGER reject_unified_result BEFORE INSERT ON unified_daily_results
                WHEN NEW.user_id = {self.user_id} AND NEW.date = '{self.today.isoformat()}'
                BEGIN SELECT RAISE(ABORT, 'forced transaction failure'); END"""
            )
        with self.assertRaises(sqlite3.IntegrityError):
            recalculate_user(self.user_id, self.today)
        after = get_daily_result(self.user_id, day.isoformat())
        self.assertEqual(after, before)

    # ----- show new debt details in the daily telegram summary -----
    def test_day_command_reports_positive_credits_and_debt_fields(self):
        user = 705
        database.ensure_calorie_account(user, self.today.isoformat())
        database.set_calorie_target(user, self.today.isoformat(), 100)
        database.save_food(user, self.today.isoformat(), "12:00", "meal", [], 150)
        message = SimpleNamespace(sent=[])

        # ----- capture telegram text for the daily report -----
        async def reply_text(text):
            message.sent.append(text)

        message.reply_text = reply_text
        update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=user))
        asyncio.run(main.day_command(update, SimpleNamespace(args=[])))
        self.assertIn("Positive credits used:", message.sent[0])
        self.assertIn("New calorie debt: 50 kcal", message.sent[0])
        self.assertIn("Outstanding calorie debt: 50 kcal", message.sent[0])

    # ----- display available credits, debt, and recovery dates in bank report -----
    def test_bank_command_reports_outstanding_debt_and_recovery_deadline(self):
        user = 706
        database.ensure_calorie_account(user, self.today.isoformat())
        database.set_calorie_target(user, self.today.isoformat(), 100)
        database.save_food(user, self.today.isoformat(), "12:00", "meal", [], 150)
        message = SimpleNamespace(sent=[])

        # ----- capture telegram text for the bank report -----
        async def reply_text(text):
            message.sent.append(text)

        message.reply_text = reply_text
        update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=user))
        asyncio.run(main.bank_command(update, None))
        self.assertIn("Outstanding calorie debt: 50 kcal", message.sent[0])
        self.assertIn("recovery deadline", message.sent[0])

    # ----- catch up all active users after a restart without duplicate ledger rows -----
    def test_restart_catchup_is_idempotent(self):
        self.set_target(100)
        self.add_food(self.today - timedelta(days=1), 150)
        first = recalculate_all(self.today)[self.user_id]
        second = recalculate_all(self.today)[self.user_id]
        self.assertEqual(first, second)
        with sqlite3.connect(database.DB_PATH) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM calorie_debts WHERE user_id = ?",
                (self.user_id,),
            ).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
