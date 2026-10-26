import asyncio
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import database
import main
from clear_service import clear_user_data
from food_memory import find_exact_memory, find_similar_memories, prepare_memory
from monthly_report import monthly_data


TIMEZONE = ZoneInfo("Asia/Singapore")
USER_TABLES = (
    "food_logs", "exercise_logs", "workout_sessions", "workout_exercises",
    "calorie_bank_deposits", "calorie_bank_withdrawals", "daily_calorie_results",
    "food_memory", "food_memory_events", "weight_logs", "calorie_targets",
    "calorie_goals", "calorie_account_settings", "calorie_debts",
    "calorie_debt_repayments", "unified_daily_results", "pending_date_overrides",
    "historical_debt_import_batches", "historical_debt_import_items",
    "historical_debt_import_events",
)


class ClearCommandTests(unittest.TestCase):
    # ----- prepare isolated database storage -----
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_path = database.DB_PATH
        database.DB_PATH = Path(self.tempdir.name) / "clear_test.db"
        database.init_db()
        self.today = datetime.now(TIMEZONE).date().isoformat()

    # ----- restore database storage after each test -----
    def tearDown(self):
        database.DB_PATH = self.old_path
        self.tempdir.cleanup()

    # ----- insert representative records for one user -----
    def seed_user(self, user_id):
        meal = {
            "items": [{"name": "Meal", "calories": 400}],
            "total_calories": 400,
            "product_identification": None,
        }
        memory = prepare_memory("chicken rice 1 serving", meal)
        food_id = database.save_food(
            user_id, self.today, "12:00:00", "chicken rice 1 serving",
            meal["items"], 400, confirmation_key=f"{user_id}-meal", memory=memory,
        )
        database.save_exercise(
            user_id, self.today, "10:00:00", "run 30 minutes", "run", 30, 8, 200, 82,
        )
        session_id = database.save_workout_session(
            f"session-{user_id}", user_id, self.today, "11:00:00", 20,
            "reported", 82, 3.5, 100,
            [{"name": "Squat", "sets": 3, "activity_type": "strength"}],
        )
        database.save_weight(user_id, 80, self.today, "08:00:00", "created")
        database.set_calorie_target(user_id, "2020-01-01", 1999)
        database.create_calorie_goal(
            user_id, 80, 75, self.today, "2099-01-01", "{}"
        )
        database.ensure_calorie_account(user_id, self.today)
        with sqlite3.connect(database.DB_PATH) as connection:
            debt_cursor = connection.execute(
                """INSERT INTO calorie_debts (
                    user_id, source_date, calculation_key, original_calories,
                    remaining_calories, recovery_deadline, rule_version
                ) VALUES (?, ?, ?, 40, 20, ?, 1)""",
                (user_id, self.today, f"daily:{self.today}", self.today),
            )
            connection.execute(
                """INSERT INTO calorie_debt_repayments (
                    user_id, debt_id, repayment_date, source_type, source_id,
                    calories, rule_version
                ) VALUES (?, ?, ?, 'exercise', 1, 20, 1)""",
                (user_id, debt_cursor.lastrowid, self.today),
            )
            connection.execute(
                """INSERT OR REPLACE INTO unified_daily_results (
                    user_id, date, base_target, food_consumed, additional_exercise,
                    exercise_bonus, allowance_before_bank, bank_available, bank_used,
                    bank_balance_after, debt_added, debt_repaid, debt_outstanding,
                    remaining_allowance, status, finalized, rule_version
                ) VALUES (?, ?, 2084, 2200, 0, 0, 2084, 0, 0, 0, 116, 20, 20,
                    -116, 'over_target', 0, 1)""",
                (user_id, self.today),
            )
            connection.execute(
                """INSERT INTO historical_debt_import_batches (
                    batch_id, user_id, activation_date, window_start, window_end,
                    preview_hash, status, created_at, imported_at, rule_version
                ) VALUES (?, ?, ?, ?, ?, 'hash', 'reconciled', 'created', 'imported', 1)""",
                (f"batch-{user_id}", user_id, self.today, self.today, self.today),
            )
            connection.execute(
                """INSERT INTO historical_debt_import_items (
                    batch_id, user_id, source_date, source_reference, original_status,
                    base_target, food_consumed, exercise_bonus, allowance_before_bank,
                    bank_used, original_excess, current_excess, recovery_deadline,
                    current_status, snapshot_json, updated_at
                ) VALUES (?, ?, ?, 'daily_result', 'over_target', 2084, 2200, 0,
                    2084, 0, 116, 116, ?, 'over_target', '{}', 'updated')""",
                (f"batch-{user_id}", user_id, self.today, self.today),
            )
            connection.execute(
                """INSERT INTO historical_debt_import_events (
                    batch_id, user_id, source_date, event_type, previous_excess,
                    current_excess, details, created_at
                ) VALUES (?, ?, ?, 'imported', 0, 116, '{}', 'created')""",
                (f"batch-{user_id}", user_id, self.today),
            )
        database.set_pending_date_override(user_id, "2020-01-02")
        return food_id, session_id

    # ----- count rows belonging to one user in selected tables -----
    def counts(self, user_id):
        with sqlite3.connect(database.DB_PATH) as conn:
            counts = {}
            for table in USER_TABLES:
                if table == "workout_exercises":
                    query = """SELECT COUNT(*) FROM workout_exercises AS e
                        JOIN workout_sessions AS s ON s.id = e.session_id
                        WHERE s.user_id = ?"""
                else:
                    query = f"SELECT COUNT(*) FROM {table} WHERE user_id = ?"
                counts[table] = conn.execute(query, (user_id,)).fetchone()[0]
            return counts

    # ----- create a bot command update and capture its reply -----
    def command_update(self, user_id, user_data=None):
        message = SimpleNamespace(sent=[])

        # ----- capture the bot's text and keyboard -----
        async def reply_text(text, **kwargs):
            message.sent.append((text, kwargs))

        message.reply_text = reply_text
        update = SimpleNamespace(
            message=message,
            effective_user=SimpleNamespace(id=user_id),
        )
        context = SimpleNamespace(args=[], user_data=user_data if user_data is not None else {})
        return update, context

    # ----- run the clear command and return its state -----
    def begin_clear(self, user_id, user_data=None):
        update, context = self.command_update(user_id, user_data)
        asyncio.run(main.clear_command(update, context))
        return update, context

    # ----- create a callback update for a reset action -----
    def callback_update(self, user_id, user_data, action, request_id=None):
        request_id = request_id or user_data["clear_request"]["id"]
        query = SimpleNamespace(
            data=f"clear:{action}:{request_id}",
            from_user=SimpleNamespace(id=user_id),
            message=SimpleNamespace(sent=[], edits=[]),
            answers=[],
        )

        # ----- record callback acknowledgement arguments -----
        async def answer(*args, **kwargs):
            query.answers.append((args, kwargs))

        # ----- record text sent from an existing callback message -----
        async def reply_text(text, **kwargs):
            query.message.sent.append((text, kwargs))

        # ----- record edits to the callback message -----
        async def edit_message_text(text, **kwargs):
            query.message.edits.append((text, kwargs))

        query.answer = answer
        query.message.reply_text = reply_text
        query.edit_message_text = edit_message_text
        return SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=user_id),
        ), query

    # ----- invoke one reset callback and capture its message -----
    def run_callback(self, user_id, user_data, action, request_id=None):
        update, query = self.callback_update(user_id, user_data, action, request_id)
        asyncio.run(main.handle_clear_buttons(update, SimpleNamespace(user_data=user_data)))
        return query

    # ----- select a reset scope and move to the second stage -----
    def select_scope(self, user_id, user_data, scope):
        return self.run_callback(user_id, user_data, scope)

    # ----- verify the first selection only asks for final confirmation -----
    def test_first_stage_scope_selection_does_not_delete_data(self):
        self.seed_user(201)
        before = self.counts(201)
        update, context = self.begin_clear(201)
        self.assertEqual(self.counts(201), before)
        self.assertIn("permanent", update.message.sent[0][0])
        first_markup = update.message.sent[0][1]["reply_markup"]
        first_buttons = [button.text for row in first_markup.inline_keyboard for button in row]
        self.assertEqual(first_buttons, ["Clear Logged History", "Clear Everything", "Cancel"])
        self.assertIn("clear_request", context.user_data)

        query = self.select_scope(201, context.user_data, "history")
        self.assertIn("cannot be undone", query.message.edits[0][0])
        final_markup = query.message.edits[0][1]["reply_markup"]
        final_buttons = [button.text for row in final_markup.inline_keyboard for button in row]
        self.assertEqual(final_buttons, ["Yes, Delete", "Cancel"])
        self.assertEqual(self.counts(201), before)

    # ----- verify cancellation at both stages preserves persistent data -----
    def test_first_and_second_stage_cancellation_keep_all_records(self):
        self.seed_user(202)
        before = self.counts(202)
        _, first_context = self.begin_clear(202)
        first_cancel = self.run_callback(202, first_context.user_data, "cancel")
        self.assertIn("No data was deleted", first_cancel.message.edits[0][0])
        self.assertNotIn("clear_request", first_context.user_data)
        self.assertEqual(self.counts(202), before)

        _, second_context = self.begin_clear(202)
        self.select_scope(202, second_context.user_data, "everything")
        second_cancel = self.run_callback(202, second_context.user_data, "cancel")
        self.assertIn("No data was deleted", second_cancel.message.edits[0][0])
        self.assertEqual(self.counts(202), before)

    # ----- preserve memory weights and targets while clearing activity history -----
    def test_history_reset_preserves_personal_data_and_removes_activity(self):
        food_id, _ = self.seed_user(203)
        other_food, _ = self.seed_user(204)
        _, context = self.begin_clear(203)
        self.select_scope(203, context.user_data, "history")
        final = self.run_callback(203, context.user_data, "confirm")

        self.assertEqual(final.message.edits[0][0], (
            "Your food and exercise history has been cleared. Your saved meals, "
            "weight history and personal settings have been preserved."
        ))
        counts = self.counts(203)
        for table in (
            "food_logs", "exercise_logs", "workout_sessions", "workout_exercises",
            "calorie_bank_deposits", "calorie_bank_withdrawals", "daily_calorie_results",
            "calorie_debts", "calorie_debt_repayments", "unified_daily_results",
            "pending_date_overrides",
        ):
            self.assertEqual(counts[table], 0, table)
        self.assertEqual(counts["food_memory"], 1)
        self.assertEqual(counts["food_memory_events"], 1)
        self.assertEqual(counts["weight_logs"], 1)
        self.assertEqual(counts["calorie_targets"], 1)
        self.assertEqual(counts["calorie_goals"], 1)
        self.assertEqual(counts["calorie_debts"], 0)
        self.assertEqual(counts["calorie_debt_repayments"], 0)
        self.assertEqual(counts["unified_daily_results"], 0)
        self.assertEqual(counts["calorie_account_settings"], 1)
        self.assertEqual(database.get_effective_calorie_target(203, self.today), 1999)
        self.assertEqual(database.get_weight_history(203)[0]["weight_kg"], 80)
        self.assertEqual(find_exact_memory(203, "chicken rice 1 serving")["calories"], 400)
        self.assertEqual(find_exact_memory(204, "chicken rice 1 serving")["calories"], 400)
        with sqlite3.connect(database.DB_PATH) as conn:
            source_id = conn.execute(
                "SELECT source_food_id FROM food_memory WHERE user_id = ?", (203,)
            ).fetchone()[0]
            event_source = conn.execute(
                "SELECT source_food_id FROM food_memory_events WHERE user_id = ?", (203,)
            ).fetchone()[0]
            other_record = conn.execute(
                "SELECT id FROM food_logs WHERE user_id = ?", (204,)
            ).fetchone()[0]
        self.assertIsNone(source_id)
        self.assertIsNone(event_source)
        self.assertEqual(other_record, other_food)
        self.assertNotEqual(food_id, other_food)
        self.assertEqual(final.answers[0][0], ())
        report = monthly_data(203, int(self.today[:4]), int(self.today[5:7]))
        self.assertEqual(report["on_target"], 0)
        self.assertEqual(report["over_target"], 0)

    # ----- delete memories weights and targets during a full reset -----
    def test_clear_everything_removes_all_user_data_and_leaves_other_users(self):
        self.seed_user(205)
        self.seed_user(206)
        _, context = self.begin_clear(205)
        self.select_scope(205, context.user_data, "everything")
        final = self.run_callback(205, context.user_data, "confirm")

        self.assertIn("reset to its initial settings", final.message.edits[0][0])
        self.assertTrue(all(value == 0 for value in self.counts(205).values()))
        other_counts = self.counts(206)
        self.assertTrue(all(
            other_counts[table] > 0
            for table in (
                "food_logs", "exercise_logs", "workout_sessions", "workout_exercises",
                "calorie_bank_deposits", "food_memory",
                "food_memory_events", "weight_logs", "calorie_targets",
                "calorie_goals", "calorie_account_settings", "calorie_debts",
                "calorie_debt_repayments", "unified_daily_results",
                "pending_date_overrides",
            )
        ))
        self.assertEqual(find_similar_memories(205, "chicken rice"), [])
        self.assertEqual(database.get_effective_calorie_target(205, self.today, 2084), 2084)
        report = monthly_data(205, int(self.today[:4]), int(self.today[5:7]))
        self.assertEqual(report["on_target"], 0)
        self.assertEqual(report["over_target"], 0)
        with sqlite3.connect(database.DB_PATH) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    # ----- clear all ephemeral conversation state and the pending date -----
    def test_success_clears_conversation_cache_and_previous_date_override(self):
        self.seed_user(207)
        state = {
            "pending_food": {"description": "meal"}, "food_state": "clarifying",
            "pending_workout": {"exercises": []}, "workout_state": "awaiting_more",
            "pending_exercise": {"description": "run"}, "exercise_state": "clarifying",
            "pending_route_text": "dips", "food_processing": False,
            "temporary_memory": {"match": "cached"},
        }
        _, context = self.begin_clear(207, state)
        self.select_scope(207, context.user_data, "history")
        self.run_callback(207, context.user_data, "confirm")
        self.assertEqual(context.user_data, {})
        self.assertIsNone(database.get_pending_date_override(207))

    # ----- reject stale and duplicate reset callbacks -----
    def test_new_request_invalidates_old_buttons_and_success_is_single_use(self):
        self.seed_user(208)
        _, context = self.begin_clear(208)
        old_id = context.user_data["clear_request"]["id"]
        self.begin_clear(208, context.user_data)
        new_id = context.user_data["clear_request"]["id"]
        self.assertNotEqual(old_id, new_id)
        before = self.counts(208)
        stale = self.run_callback(208, context.user_data, "confirm", old_id)
        self.assertTrue(stale.answers[-1][1].get("show_alert"))
        self.assertEqual(self.counts(208), before)

        self.select_scope(208, context.user_data, "history")
        self.run_callback(208, context.user_data, "confirm")
        duplicate = self.run_callback(208, context.user_data, "confirm", new_id)
        self.assertTrue(duplicate.answers[-1][1].get("show_alert"))
        self.assertEqual(self.counts(208)["food_logs"], 0)

    # ----- prevent a different telegram user from using reset buttons -----
    def test_callback_owner_must_match_the_request_owner(self):
        self.seed_user(209)
        _, context = self.begin_clear(209)
        request_id = context.user_data["clear_request"]["id"]
        query = self.callback_update(210, context.user_data, "confirm", request_id)[1]
        update = SimpleNamespace(
            callback_query=query, effective_user=SimpleNamespace(id=210)
        )
        asyncio.run(main.handle_clear_buttons(update, SimpleNamespace(user_data=context.user_data)))
        self.assertEqual(self.counts(209)["food_logs"], 1)
        self.assertTrue(query.answers[-1][1].get("show_alert"))

    # ----- reject reset requests while estimation is active -----
    def test_active_ai_processing_blocks_reset_request(self):
        update, context = self.command_update(211, {"food_processing": True})
        asyncio.run(main.clear_command(update, context))
        self.assertNotIn("clear_request", context.user_data)
        self.assertIn("wait", update.message.sent[0][0])

    # ----- ensure ordinary yes text cannot confirm data deletion -----
    def test_text_yes_does_not_confirm_or_delete(self):
        self.seed_user(216)
        _, context = self.begin_clear(216)
        before = self.counts(216)
        message = SimpleNamespace(text="yes", from_user=SimpleNamespace(id=216), sent=[])

        # ----- capture the bot reply to a normal text message -----
        async def reply_text(text, **kwargs):
            message.sent.append(text)

        message.reply_text = reply_text
        update = SimpleNamespace(message=message, effective_user=message.from_user)
        with patch.object(main, "classify_message", new=AsyncMock(return_value="unclear")):
            asyncio.run(main.handle_text(update, context))
        self.assertEqual(self.counts(216), before)
        self.assertIn("clear_request", context.user_data)

    # ----- reject an expired reset callback without deleting records -----
    def test_expired_reset_buttons_are_rejected(self):
        self.seed_user(217)
        _, context = self.begin_clear(217)
        request = context.user_data["clear_request"]
        request["expires_at"] = 0
        before = self.counts(217)
        query = self.run_callback(217, context.user_data, "history")
        self.assertTrue(query.answers[-1][1].get("show_alert"))
        self.assertEqual(self.counts(217), before)
        self.assertNotIn("clear_request", context.user_data)

    # ----- roll back all reset deletions when a dependent delete fails -----
    def test_database_failure_rolls_back_the_entire_reset(self):
        self.seed_user(212)
        before = self.counts(212)
        with sqlite3.connect(database.DB_PATH) as conn:
            conn.execute("""
                CREATE TRIGGER reject_exercise_delete BEFORE DELETE ON exercise_logs
                WHEN OLD.user_id = 212
                BEGIN SELECT RAISE(ABORT, 'injected failure'); END
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            clear_user_data(212, "history")
        self.assertEqual(self.counts(212), before)

    # ----- serialize concurrent idempotent resets and preserve foreign keys -----
    def test_concurrent_reset_attempts_are_safe_and_idempotent(self):
        self.seed_user(213)
        barrier = Barrier(2)

        # ----- wait for both reset workers before deleting -----
        def reset_worker():
            barrier.wait()
            return clear_user_data(213, "history")

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: reset_worker(), range(2)))
        self.assertEqual(len(results), 2)
        counts = self.counts(213)
        self.assertTrue(all(
            counts[table] == 0
            for table in (
                "food_logs", "exercise_logs", "workout_sessions", "workout_exercises",
                "calorie_bank_deposits", "calorie_bank_withdrawals",
                "daily_calorie_results", "pending_date_overrides",
            )
        ))
        self.assertEqual(counts["food_memory"], 1)
        self.assertEqual(counts["weight_logs"], 1)
        self.assertEqual(counts["calorie_targets"], 1)
        with sqlite3.connect(database.DB_PATH) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    # ----- keep a completed reset after reopening and migrating the database -----
    def test_reset_survives_database_restart(self):
        self.seed_user(214)
        clear_user_data(214, "everything")
        database.init_db()
        self.assertTrue(all(value == 0 for value in self.counts(214).values()))
        self.assertEqual(database.get_effective_calorie_target(214, self.today, 2084), 2084)

        # Simulate a callback arriving after process-local confirmation state was lost.
        stale = self.run_callback(214, {}, "confirm", "a" * 16)
        self.assertTrue(stale.answers[-1][1].get("show_alert"))

    # ----- preserve old memory rows while migrating source references nullable -----
    def test_existing_memory_schema_migrates_without_losing_meal_data(self):
        with sqlite3.connect(database.DB_PATH) as conn:
            conn.execute("DROP TABLE food_memory")
            conn.execute("""
                CREATE TABLE food_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    canonical_identity TEXT NOT NULL,
                    attributes TEXT NOT NULL,
                    description TEXT NOT NULL,
                    items TEXT NOT NULL,
                    product_info TEXT,
                    calories INTEGER NOT NULL CHECK (calories >= 0),
                    evidence_category TEXT NOT NULL CHECK (
                        evidence_category IN ('verified', 'user_corrected', 'ai_confirmed')
                    ),
                    source_food_id INTEGER NOT NULL UNIQUE,
                    source_origin TEXT NOT NULL CHECK (
                        source_origin IN ('external_verified', 'manual_correction', 'ai_estimated')
                    ),
                    original_source_origin TEXT NOT NULL DEFAULT 'ai_estimated',
                    exact_ready INTEGER NOT NULL DEFAULT 0,
                    usage_count INTEGER NOT NULL DEFAULT 1,
                    last_used_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                )
            """)
            conn.execute("""
                INSERT INTO food_memory VALUES (
                    1, 215, 'identity', '{}', 'saved meal', '[]', NULL, 321,
                    'ai_confirmed', 98, 'ai_estimated', 'ai_estimated', 1, 2,
                    'created', 'created', 1
                )
            """)
        database.init_db()
        with sqlite3.connect(database.DB_PATH) as conn:
            column = next(
                row for row in conn.execute("PRAGMA table_info(food_memory)")
                if row[1] == "source_food_id"
            )
            record = conn.execute(
                "SELECT description, calories, source_food_id FROM food_memory WHERE id = 1"
            ).fetchone()
        self.assertEqual(column[3], 0)
        self.assertEqual(record, ("saved meal", 321, 98))


if __name__ == "__main__":
    unittest.main()
