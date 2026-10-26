import asyncio
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import database
import main
from calorie_bank import get_daily_result
from historical_logging import (
    MAX_PREV_DAYS,
    previous_logging_date,
)
from monthly_report import monthly_data


TIMEZONE = ZoneInfo("Asia/Singapore")


class HistoricalDateValidationTests(unittest.TestCase):
    # ----- convert one and two days ago in singapore time -----
    def test_previous_day_offsets_use_calendar_dates(self):
        today = date(2026, 10, 23)
        self.assertEqual(previous_logging_date("1", today), "2026-10-22")
        self.assertEqual(previous_logging_date("2", today), "2026-10-21")

    # ----- derive the current date in singapore when given a timestamp -----
    def test_timestamp_is_converted_to_singapore_calendar_date(self):
        utc_time = datetime(2026, 10, 23, 16, 30, tzinfo=timezone.utc)
        self.assertEqual(previous_logging_date("1", utc_time), "2026-10-23")

    # ----- reject invalid and unsupported historical offsets -----
    def test_invalid_and_excessive_offsets_are_rejected(self):
        for value in (None, "", "0", "-1", "+1", "1.5", "one", " " * 2):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    previous_logging_date(value, date(2026, 10, 23))
        with self.assertRaises(ValueError):
            previous_logging_date(str(MAX_PREV_DAYS + 1), date(2026, 10, 23))
        with self.assertRaises(ValueError):
            previous_logging_date("1", date(2000, 1, 1))


class HistoricalLoggingTests(unittest.TestCase):
    # ----- prepare isolated database storage -----
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_path = database.DB_PATH
        database.DB_PATH = Path(self.tempdir.name) / "historical.db"
        database.init_db()
        self.today = datetime.now(TIMEZONE).date()
        self.yesterday = (self.today - timedelta(days=1)).isoformat()

    # ----- restore database storage after each test -----
    def tearDown(self):
        database.DB_PATH = self.old_path
        self.tempdir.cleanup()

    # ----- create a telegram message mock -----
    def message(self, user_id, text=""):
        class Message:
            # ----- initialize message identity and text -----
            def __init__(self, owner_id, body):
                self.from_user = SimpleNamespace(id=owner_id)
                self.text = body
                self.sent = []

            # ----- capture text replies -----
            async def reply_text(self, text, **kwargs):
                self.sent.append((text, kwargs))

        return Message(user_id, text)

    # ----- set a one-time date override through the command handler -----
    def run_prev(self, user_id, args, user_data=None):
        message = self.message(user_id, "/prev")
        update = SimpleNamespace(
            message=message,
            effective_user=SimpleNamespace(id=user_id),
        )
        context = SimpleNamespace(args=args, user_data=user_data or {})
        asyncio.run(main.prev_command(update, context))
        return message, context

    # ----- replace unused overrides and isolate users -----
    def test_override_replacement_and_user_isolation(self):
        first_date = (self.today - timedelta(days=1)).isoformat()
        second_date = (self.today - timedelta(days=2)).isoformat()
        database.set_pending_date_override(10, first_date)
        database.set_pending_date_override(11, second_date)
        database.set_pending_date_override(10, second_date)
        self.assertEqual(database.get_pending_date_override(10), second_date)
        self.assertEqual(database.get_pending_date_override(11), second_date)

    # ----- preserve an unused override across database reconnection -----
    def test_override_survives_a_bot_restart_and_is_consumed_once(self):
        database.set_pending_date_override(12, self.yesterday)
        database.DB_PATH = Path(self.tempdir.name) / "historical.db"
        self.assertEqual(database.get_pending_date_override(12), self.yesterday)
        self.assertEqual(database.consume_pending_date_override(12), self.yesterday)
        self.assertIsNone(database.consume_pending_date_override(12))

    # ----- accept valid command offsets and report their exact date -----
    def test_prev_command_reports_target_date_and_replaces_prior_value(self):
        database.set_pending_date_override(13, "2026-01-01")
        message, _ = self.run_prev(13, ["1"])
        self.assertEqual(database.get_pending_date_override(13), self.yesterday)
        self.assertIn(self.yesterday, message.sent[0][0])
        self.assertIn("Replaced the unused date 2026-01-01", message.sent[0][0])

    # ----- reject malformed commands without changing an existing override -----
    def test_prev_command_rejects_invalid_arguments(self):
        database.set_pending_date_override(14, self.yesterday)
        for args in ([], ["0"], ["-2"], ["1.2"], ["1", "2"]):
            with self.subTest(args=args):
                message, _ = self.run_prev(14, args)
                self.assertTrue(message.sent[0][0])
                self.assertEqual(
                    database.get_pending_date_override(14), self.yesterday
                )

    # ----- prevent an override from changing an active entry -----
    def test_prev_does_not_modify_date_during_an_active_entry(self):
        original_date = self.today.isoformat()
        user_data = {"pending_food": {"logging_date": original_date}}
        message, _ = self.run_prev(15, ["1"], user_data)
        self.assertIn("Finish or cancel", message.sent[0][0])
        self.assertIsNone(database.get_pending_date_override(15))

    # ----- keep informational commands from consuming the override -----
    def test_day_and_bank_leave_override_pending(self):
        database.set_pending_date_override(16, self.yesterday)
        message = self.message(16)
        update = SimpleNamespace(
            message=message,
            effective_user=SimpleNamespace(id=16),
        )
        context = SimpleNamespace(user_data={})
        context.args = []
        asyncio.run(main.day_command(update, context))
        asyncio.run(main.bank_command(update, context))
        self.assertEqual(database.get_pending_date_override(16), self.yesterday)

    # ----- attach the override to a food entry and consume it once -----
    def test_food_entry_consumes_override_and_keeps_its_date(self):
        database.set_pending_date_override(17, self.yesterday)
        message = self.message(17, "meal")
        context = SimpleNamespace(user_data={})
        asyncio.run(main.begin_food_entry(message, context, "rice and eggs"))
        pending = context.user_data["pending_food"]
        self.assertEqual(pending["logging_date"], self.yesterday)
        self.assertIsNone(database.get_pending_date_override(17))

    # ----- preserve food date through photo selection and clarification -----
    def test_food_date_survives_photo_and_clarification_states(self):
        database.set_pending_date_override(18, self.yesterday)
        message = self.message(18, "meal")
        context = SimpleNamespace(user_data={})
        asyncio.run(main.begin_food_entry(message, context, "noodles"))
        pending = context.user_data["pending_food"]
        update = SimpleNamespace(
            message=self.message(18, "yes"),
            effective_user=SimpleNamespace(id=18),
        )
        asyncio.run(main.handle_text(update, context))
        self.assertEqual(context.user_data["food_state"], "awaiting_photo")
        self.assertEqual(pending["logging_date"], self.yesterday)

        class TelegramFile:
            # ----- return mock photo bytes -----
            async def download_as_bytearray(self):
                return bytearray(b"photo")

        class TelegramPhoto:
            # ----- provide a mock telegram file -----
            async def get_file(self):
                return TelegramFile()

        photo_message = self.message(18, "photo caption")
        photo_message.photo = [TelegramPhoto()]
        photo_message.caption = "one bowl"
        photo_update = SimpleNamespace(
            message=photo_message,
            effective_user=SimpleNamespace(id=18),
        )
        with patch.object(main, "analyze_meal", new=AsyncMock()) as analyze:
            asyncio.run(main.handle_photo(photo_update, context))
        analyze.assert_awaited_once()
        self.assertEqual(pending["image_bytes"], b"photo")
        self.assertEqual(pending["logging_date"], self.yesterday)

        context.user_data["food_state"] = "clarifying"
        with patch.object(main, "process_food_clarification", new=AsyncMock()) as process:
            update.message.text = "one serving"
            asyncio.run(main.handle_text(update, context))
        self.assertEqual(pending["logging_date"], self.yesterday)
        process.assert_awaited_once()

    # ----- save confirmed food on its attached historical date -----
    def test_food_confirmation_saves_historical_date(self):
        pending = main.new_meal("rice and eggs")
        pending.update({
            "memory_match": None,
            "logging_date": self.yesterday,
            "estimate_source": "ai_estimated",
            "estimate": {
                "items": [{"name": "Rice and eggs", "calories": 500}],
                "total_calories": 500,
                "product_identification": None,
                "clarification_needed": False,
            },
        })
        pending["memory_basis"] = pending["estimate"]
        user_data = {"pending_food": pending, "food_state": "confirming"}
        query = self.callback_query(
            user_id=19,
            data=f"food_confirm:{pending['id']}",
        )
        asyncio.run(main.handle_food_buttons(
            SimpleNamespace(callback_query=query), SimpleNamespace(user_data=user_data)
        ))
        rows = database.get_food_by_date(19, self.yesterday)
        self.assertEqual(len(rows), 1)
        self.assertIn(self.yesterday, query.message.sent[-1][0])

    # ----- create callback query mocks for confirmation handlers -----
    def callback_query(self, user_id, data):
        class QueryMessage:
            # ----- capture replies and keyboard changes -----
            async def reply_text(self, text, **kwargs):
                self.sent.append((text, kwargs))

            # ----- initialize message reply storage -----
            def __init__(self):
                self.sent = []

            # ----- accept inline keyboard removal -----
            async def edit_reply_markup(self, **kwargs):
                return None

        class Query:
            # ----- acknowledge a button press -----
            async def answer(self):
                return None

            # ----- remove the inline keyboard -----
            async def edit_message_reply_markup(self, **kwargs):
                return None

        query = Query()
        query.data = data
        query.from_user = SimpleNamespace(id=user_id)
        query.message = QueryMessage()
        return query

    # ----- clear an unused override and active entry on cancellation -----
    def test_cancel_clears_unused_override_and_active_food(self):
        database.set_pending_date_override(20, self.yesterday)
        pending = main.new_meal("meal")
        pending["logging_date"] = "2026-01-01"
        context = SimpleNamespace(user_data={
            "pending_food": pending,
            "food_state": "confirming",
        })
        message = self.message(20, "/cancel")
        update = SimpleNamespace(
            message=message,
            effective_user=SimpleNamespace(id=20),
        )
        asyncio.run(main.cancel_command(update, context))
        self.assertIsNone(database.get_pending_date_override(20))
        self.assertNotIn("pending_food", context.user_data)

    # ----- save a standalone exercise on its attached historical date -----
    def test_standalone_exercise_confirmation_uses_historical_date(self):
        database.set_pending_date_override(21, self.yesterday)
        message = self.message(21, "walk 30 minutes")
        context = SimpleNamespace(user_data={})
        estimate = {
            "activity": "Walk",
            "duration_minutes": 30,
            "met": 3.5,
            "weight_kg": 82,
            "additional_calories": 100,
            "clarification_needed": False,
            "question": None,
        }
        with patch.object(main, "estimate_exercise", new=AsyncMock(return_value=estimate)):
            asyncio.run(main.process_exercise(message, context, message.text))
        pending = context.user_data["pending_exercise"]
        self.assertEqual(pending["logging_date"], self.yesterday)
        query = self.callback_query(
            21, f"exercise_confirm:{pending['id']}"
        )
        asyncio.run(main.handle_exercise_buttons(
            SimpleNamespace(callback_query=query), SimpleNamespace(user_data=context.user_data)
        ))
        exercises = database.get_exercise_by_date(21, self.yesterday)
        self.assertEqual(len(exercises), 1)
        self.assertIn(self.yesterday, query.message.sent[-1][0])

    # ----- save a mixed workout on the date captured at session start -----
    def test_mixed_workout_keeps_date_across_activities_and_confirmation(self):
        database.set_pending_date_override(22, self.yesterday)
        message = self.message(22, "squat 3 sets of 8")
        context = SimpleNamespace(user_data={})
        strength = {
            "name": "Squat", "is_workout_activity": True,
            "activity_type": "strength", "sets": 3, "repetitions": 8,
            "clarification_needed": False, "question": None,
        }
        with patch.object(main, "extract_strength_exercise", new=AsyncMock(return_value=strength)):
            asyncio.run(main.route_exercise_description(message, context, message.text))
        pending = context.user_data["pending_workout"]
        cardio = {
            "name": "Run", "is_workout_activity": True,
            "activity_type": "cardio", "duration_minutes": 15,
            "clarification_needed": False, "question": None,
        }
        asyncio.run(main.add_strength_exercise(message, context, "run 15 min", cardio))
        self.assertEqual(pending["logging_date"], self.yesterday)
        self.assertEqual(len(pending["exercises"]), 2)
        asyncio.run(main.finalize_workout_duration(message, context, "40 min"))
        query = self.callback_query(
            22, f"workout_confirm:{pending['button_id']}"
        )
        asyncio.run(main.handle_workout_buttons(
            SimpleNamespace(callback_query=query), SimpleNamespace(user_data=context.user_data)
        ))
        sessions = database.get_workout_sessions_by_date(22, self.yesterday)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(len(sessions[0]["exercises"]), 2)
        self.assertIn(self.yesterday, query.message.sent[-1][0])

    # ----- recalculate historical bank withdrawals and calendar results -----
    def test_backdated_sources_refresh_bank_and_monthly_report(self):
        exercise_day = (self.today - timedelta(days=5)).isoformat()
        food_day = (self.today - timedelta(days=4)).isoformat()
        database.set_calorie_target(23, exercise_day, 100)
        database.save_exercise(
            23, exercise_day, "10:00:00", "run", "run", 30, 8, 100
        )
        database.save_food(
            23, food_day, "12:00:00", "meal", [], 130
        )
        result = get_daily_result(23, food_day)
        self.assertEqual(result["base_target"], 100)
        self.assertEqual(result["bank_used"], 30)
        self.assertEqual(database.get_effective_calorie_target(23, exercise_day), 100)
        self.assertEqual(database.get_effective_calorie_target(23, food_day), 100)
        month = monthly_data(23, self.today.year, self.today.month, self.today)
        self.assertEqual(month["days"][date.fromisoformat(food_day)], "on_target")

    # ----- keep a cancelled workout from reusing its historical date -----
    def test_workout_cancel_consumes_date_and_next_entry_uses_today(self):
        database.set_pending_date_override(24, self.yesterday)
        message = self.message(24, "squat")
        context = SimpleNamespace(user_data={
            "pending_workout": {
                "id": "session", "button_id": "button",
                "logging_date": database.consume_pending_date_override(24),
                "exercises": [],
            },
            "workout_state": "confirming",
        })
        query = self.callback_query(24, "workout_cancel:button")
        asyncio.run(main.handle_workout_buttons(
            SimpleNamespace(callback_query=query), SimpleNamespace(user_data=context.user_data)
        ))
        self.assertIsNone(database.get_pending_date_override(24))
        self.assertEqual(main._consume_logging_date(24), self.today.isoformat())


if __name__ == "__main__":
    unittest.main()
