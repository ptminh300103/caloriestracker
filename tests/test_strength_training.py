import tempfile
import unittest
import asyncio
import json
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import database
from strength_training import (
    estimate_session_calories,
    infer_duration_minutes,
    known_cardio_session_duration,
    cardio_met,
    strength_session_met,
    is_duration_unknown,
    parse_duration_minutes,
    parse_session_control,
    workout_text_action,
    should_ask_exercise_clarification,
    merge_known_exercise_fields,
    is_unknown_answer,
    classify_workout_response,
    normalize_workout_response,
)
from response_parser import classify_response, normalize_response
from food_conversation import (
    photo_choice_action,
    clarification_action,
    should_ask_food_clarification,
    clarification_context,
    food_route_selection,
    food_state_response,
    is_ambiguous_food_exercise_message,
)
from intent_classifier import classify_message
from main import request_workout_duration
from exercise_estimator import estimate_exercise, extract_strength_exercise


class WorkoutConversationRulesTests(unittest.TestCase):
# ----- verify standalone exercise calories ignore model met and use python default -----
    def test_standalone_exercise_calories_ignore_model_met_and_use_python_default(self):
        raw = {
            "activity": "Walk", "duration_minutes": 30,
            "distance_km": None, "speed_kmh": None,
            "pace_min_per_km": None, "power_watts": None,
            "equipment_context": "outdoor", "intensity": None,
            "met": 12, "clarification_needed": False, "question": None,
        }

# ----- mock the ai response creation call -----
        async def create(**kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(raw))
            )])

        fake_client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)
        ))

# ----- execute the asynchronous test scenario -----
        async def run():
            with patch("exercise_estimator.client", fake_client):
                return await estimate_exercise("Walk 30 min", 80)

        result = asyncio.run(run())
        self.assertEqual(result["met"], 3.0)
        self.assertEqual(result["additional_calories"], 80)

# ----- verify extraction preserves equipment without trusting unstated met -----
    def test_extraction_preserves_equipment_without_trusting_unstated_met(self):
        raw = {
            "activity_type": "cardio", "name": "Bike", "sets": None,
            "repetitions": None, "weight_kg": None,
            "resistance_type": "weighted pedals", "duration_minutes": 15,
            "distance_km": None, "pace_min_per_km": None, "speed_kmh": None,
            "power_watts": None, "equipment_context": "other",
            "resistance_level": "level 4", "intensity": None,
            "met": 8, "met_source": "user_reported", "notes": None,
            "clarification_needed": False, "question": None,
        }

# ----- mock the ai response creation call -----
        async def create(**kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(raw))
            )])

        fake_client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)
        ))

# ----- execute the asynchronous test scenario -----
        async def run():
            with patch("exercise_estimator.client", fake_client):
                return await extract_strength_exercise("Bike 15 min, weighted pedal")

        result = asyncio.run(run())
        self.assertEqual(result["equipment_context"], "stationary")
        self.assertEqual(result["resistance_level"], "level 4")
        self.assertIsNone(result["met"])
        self.assertIsNone(result["met_source"])

# ----- verify request duration auto finishes known cardio only workout -----
    def test_request_duration_auto_finishes_known_cardio_only_workout(self):
        class Message:
# ----- initialize the test helper -----
            def __init__(self):
                self.sent = []
                self.from_user = SimpleNamespace(id=100)

# ----- capture text replies sent by the handler -----
            async def reply_text(self, text, **kwargs):
                self.sent.append(text)

# ----- execute the asynchronous test scenario -----
        async def run():
            message = Message()
            context = SimpleNamespace(user_data={
                "pending_workout": {
                    "id": "known-cardio", "exercises": [
                        {"name": "Run", "activity_type": "cardio",
                         "duration_minutes": 15, "intensity": "easy"},
                        {"name": "Bike", "activity_type": "cardio",
                         "duration_minutes": 20},
                    ],
                },
            })
            with patch("main.weight_for_exercise", return_value=82):
                await request_workout_duration(message, context)
            self.assertEqual(context.user_data["workout_state"], "confirming")
            self.assertIn("Duration: 35 minutes (sum of reported cardio durations)", message.sent[0])
            self.assertNotIn("How long was the whole session", message.sent[0])

        asyncio.run(run())

# ----- verify strength and mixed sessions still ask for total duration -----
    def test_strength_and_mixed_sessions_still_ask_for_total_duration(self):
        class Message:
# ----- initialize the test helper -----
            def __init__(self):
                self.sent = []
                self.from_user = SimpleNamespace(id=100)

# ----- capture text replies sent by the handler -----
            async def reply_text(self, text, **kwargs):
                self.sent.append(text)

# ----- execute the asynchronous test scenario -----
        async def run(activities):
            message = Message()
            context = SimpleNamespace(user_data={
                "pending_workout": {"id": "duration-needed", "exercises": activities},
            })
            await request_workout_duration(message, context)
            self.assertEqual(context.user_data["workout_state"], "awaiting_duration")
            self.assertIn("How long was the whole session", message.sent[0])

        asyncio.run(run([{"name": "Squat", "activity_type": "strength", "sets": 4}]))
        asyncio.run(run([
            {"name": "Squat", "activity_type": "strength", "sets": 4},
            {"name": "Run", "activity_type": "cardio", "duration_minutes": 15},
        ]))

# ----- verify control responses are deterministic -----
    def test_control_responses_are_deterministic(self):
        self.assertEqual(parse_session_control("yes"), "add")
        self.assertEqual(parse_session_control("no"), "finish")
        self.assertEqual(parse_session_control("that's all"), "finish")
        self.assertEqual(parse_session_control("cancel"), "cancel")
        self.assertIsNone(parse_session_control("lat pulldown 40kg 10x3"))

# ----- verify response classifier normalizes case punctuation and contractions -----
    def test_response_classifier_normalizes_case_punctuation_and_contractions(self):
        self.assertEqual(classify_workout_response("NOPE!!!"), "negative")
        self.assertEqual(classify_workout_response("Nah…"), "negative")
        self.assertEqual(classify_workout_response("That’s all."), "negative")
        self.assertEqual(classify_workout_response("I’m done!"), "negative")
        self.assertEqual(classify_workout_response("YEP."), "affirmative")
        self.assertEqual(classify_workout_response("Sure!"), "affirmative")
        self.assertEqual(classify_workout_response("idk..."), "unknown")
        self.assertEqual(classify_workout_response("Dunno."), "unknown")
        self.assertEqual(classify_workout_response("not sure?"), "unknown")
        self.assertEqual(classify_workout_response("CANCEL!"), "cancel")
        self.assertEqual(normalize_workout_response("  I’m   DONE!!! "), "i am done")

# ----- verify response matching does not consume exercise descriptions -----
    def test_response_matching_does_not_consume_exercise_descriptions(self):
        descriptions = [
            "Bench press, no spotter, 50kg 5x4",
            "Nope, I meant dumbbell press 20kg for 3 sets",
            "Squat 60kg 5x5",
            "I am done with warmup; starting bench press 40kg 8x3",
        ]
        for description in descriptions:
            self.assertEqual(classify_workout_response(description), "other")
            self.assertEqual(workout_text_action("awaiting_more", description), "exercise")

# ----- verify response groups are applied only in matching workout states -----
    def test_response_groups_are_applied_only_in_matching_workout_states(self):
        self.assertEqual(workout_text_action("awaiting_more", "nope"), "finish")
        self.assertEqual(workout_text_action("awaiting_more", "yep"), "add")
        self.assertEqual(workout_text_action("awaiting_more", "cancel"), "cancel")
        self.assertEqual(workout_text_action("awaiting_duration", "idk"), "duration")
        self.assertEqual(workout_text_action("awaiting_exercise_clarification", "idk"), None)

# ----- verify duration parser accepts common formats -----
    def test_duration_parser_accepts_common_formats(self):
        self.assertEqual(parse_duration_minutes("45 min"), 45)
        self.assertEqual(parse_duration_minutes("1 hour 10 minutes"), 70)
        self.assertEqual(parse_duration_minutes("1.5 hrs"), 90)
        self.assertIsNone(parse_duration_minutes("unknown"))

# ----- verify duration unknown and conservative fallback -----
    def test_duration_unknown_and_conservative_fallback(self):
        self.assertTrue(is_duration_unknown("Not sure"))
        duration = infer_duration_minutes([
            {"sets": 4, "repetitions": 5},
            {"sets": 3, "repetitions": 10},
        ])
        self.assertEqual(duration, 15)
        self.assertGreaterEqual(infer_duration_minutes([{}]), 15)

# ----- verify active session state routes controls and exercises -----
    def test_active_session_state_routes_controls_and_exercises(self):
        self.assertEqual(workout_text_action("awaiting_more", "yes"), "add")
        self.assertEqual(workout_text_action("collecting_exercises", "done"), "finish")
        self.assertEqual(workout_text_action("awaiting_more", "cancel"), "cancel")
        self.assertEqual(workout_text_action("awaiting_more", "Squat 60kg 5x5"), "exercise")
        self.assertEqual(workout_text_action("awaiting_duration", "45 min"), "duration")

# ----- verify exercise clarification is limited to one question -----
    def test_exercise_clarification_is_limited_to_one_question(self):
        exercise = {
            "name": "Bench press", "sets": None, "repetitions": None,
            "weight_kg": None, "resistance_type": "barbell",
            "hold_duration_seconds": None, "notes": None,
            "clarification_needed": True, "question": "How many sets and reps, and what load?",
        }
        self.assertTrue(should_ask_exercise_clarification(exercise, 0))
        # Python state marks the question consumed before waiting for the reply.
        self.assertFalse(should_ask_exercise_clarification(exercise, 1))
        self.assertFalse(should_ask_exercise_clarification(exercise, 2))

# ----- verify partial or unknown reply preserves known fields without reasking -----
    def test_partial_or_unknown_reply_preserves_known_fields_without_reasking(self):
        original = {
            "name": "Bench press", "sets": None, "repetitions": None,
            "weight_kg": 50, "resistance_type": "barbell",
            "hold_duration_seconds": None, "notes": None,
            "clarification_needed": True, "question": "How many reps?",
        }
        self.assertTrue(is_unknown_answer("I don't know"))
        partial_reply = merge_known_exercise_fields(original, {"repetitions": 8})
        self.assertEqual(partial_reply["repetitions"], 8)
        self.assertEqual(partial_reply["weight_kg"], 50)
        self.assertIsNone(partial_reply["sets"])
        self.assertFalse(should_ask_exercise_clarification(partial_reply, 1))
        unknown_reply = merge_known_exercise_fields(original, {})
        self.assertEqual(unknown_reply["weight_kg"], 50)
        self.assertIsNone(unknown_reply["repetitions"])
        self.assertFalse(should_ask_exercise_clarification(unknown_reply, 1))

# ----- verify session calories use net met formula -----
    def test_session_calories_use_net_met_formula(self):
        self.assertEqual(estimate_session_calories(82, 60), 205)

# ----- verify food photo choice handles yes no variants deterministically -----
    def test_food_photo_choice_handles_yes_no_variants_deterministically(self):
        for reply in ("yes", "Yep!", "YUP", "sure."):
            self.assertEqual(photo_choice_action("awaiting_photo_choice", reply), "await_photo")
        for reply in ("no", "NOPE", "nah"):
            self.assertEqual(photo_choice_action("awaiting_photo_choice", reply), "estimate_without_photo")
        self.assertEqual(food_state_response("awaiting_photo_choice", "yes"), "await_photo")

# ----- verify typed photo skip responses do not need ai classification -----
    def test_typed_photo_skip_responses_do_not_need_ai_classification(self):
        for reply in ("no", "skip", "never mind", "Never Mind!"):
            self.assertEqual(photo_choice_action("awaiting_photo", reply), "estimate_without_photo")
        self.assertEqual(classify_response("never mind"), "skip")

# ----- verify deterministic food state actions route without ai classification -----
    def test_deterministic_food_state_actions_route_without_ai_classification(self):
        self.assertEqual(food_state_response("awaiting_photo_choice", "Yep"), "await_photo")
        self.assertEqual(food_state_response("awaiting_photo_choice", "nope"), "estimate_without_photo")
        self.assertEqual(food_state_response("awaiting_photo", "skip"), "estimate_without_photo")
        self.assertEqual(food_state_response("awaiting_photo", "cancel"), "cancel")
        self.assertEqual(food_state_response("clarifying", "idk"), "estimate_from_original")

# ----- verify food clarification is limited to one question -----
    def test_food_clarification_is_limited_to_one_question(self):
        result = {"clarification_needed": True}
        self.assertTrue(should_ask_food_clarification(0, result))
        self.assertFalse(should_ask_food_clarification(1, result))

# ----- verify food refusals use original meal instead of becoming ingredients -----
    def test_food_refusals_use_original_meal_instead_of_becoming_ingredients(self):
        for reply in ("no", "nope", "idk", "dunno", "not sure"):
            self.assertEqual(clarification_action(reply), "estimate_from_original")
        self.assertEqual(clarification_action("cancel"), "cancel")
        pending = {
            "description": "pasta with chicken",
            "estimate": {"items": [{"name": "Pasta", "calories": 300}]},
            "history": [],
            "last_question": "What sauce was used?",
        }
        context = clarification_context(pending, "cream sauce")
        self.assertEqual(context["original_description"], "pasta with chicken")
        self.assertIn("Preserve every original food component", context["instruction"])
        self.assertEqual(context["clarification_history"][0]["answer"], "cream sauce")

# ----- verify ambiguous dips routes to explicit food or exercise choice -----
    def test_ambiguous_dips_routes_to_explicit_food_or_exercise_choice(self):
        self.assertTrue(is_ambiguous_food_exercise_message("DIPS!"))
        self.assertEqual(food_route_selection(" FOOD "), "food")
        self.assertEqual(food_route_selection("Exercise."), "exercise")
        self.assertIsNone(food_route_selection("dips"))
        self.assertEqual(food_state_response("awaiting_route_choice", "exercise"), "exercise")

# ----- verify dips is ambiguous without constructing an ai client -----
    def test_dips_is_ambiguous_without_constructing_an_ai_client(self):
# ----- run the ambiguous-message routing check -----
        async def run_check():
            with patch("intent_classifier.AsyncOpenAI", side_effect=AssertionError("AI client should not be created")):
                self.assertEqual(await classify_message("Dips!"), "ambiguous")
        import asyncio
        asyncio.run(run_check())

# ----- verify shared response parser normalizes and matches whole messages -----
    def test_shared_response_parser_normalizes_and_matches_whole_messages(self):
        self.assertEqual(normalize_response("  I’m   DONE!!! "), "i am done")
        self.assertEqual(classify_response("Nope!!"), "negative")
        self.assertEqual(classify_response("Bench press, no spotter"), "other")

# ----- verify strength only session uses session duration -----
    def test_strength_only_session_uses_session_duration(self):
        activities = [{"activity_type": "strength", "sets": 4, "repetitions": 8}]
        self.assertEqual(
            estimate_session_calories(82, 60, activities=activities), 205
        )

# ----- verify cardio duration is auto totaled only when all are known -----
    def test_cardio_duration_is_auto_totaled_only_when_all_are_known(self):
        self.assertEqual(known_cardio_session_duration([
            {"activity_type": "cardio", "duration_minutes": 15},
        ]), (15.0, "reported"))
        self.assertEqual(known_cardio_session_duration([
            {"activity_type": "cardio", "duration_minutes": 15},
            {"activity_type": "cardio", "duration_minutes": 20},
        ]), (35.0, "reported"))
        self.assertEqual(known_cardio_session_duration([
            {"activity_type": "cardio", "distance_km": 5, "pace_min_per_km": 6},
        ]), (30.0, "estimated"))
        self.assertIsNone(known_cardio_session_duration([
            {"activity_type": "cardio", "distance_km": 5},
        ]))
        self.assertIsNone(known_cardio_session_duration([
            {"activity_type": "strength", "sets": 4},
            {"activity_type": "cardio", "duration_minutes": 15},
        ]))

# ----- verify default intensity is light to moderate and activity specific -----
    def test_default_intensity_is_light_to_moderate_and_activity_specific(self):
        self.assertEqual(cardio_met({"name": "Run"}), 6.0)
        self.assertEqual(cardio_met({"name": "Walk"}), 3.0)
        self.assertEqual(cardio_met({"name": "Bike", "equipment_context": "stationary"}), 4.0)
        self.assertEqual(strength_session_met([{"activity_type": "strength"}]), 3.5)

# ----- verify explicit intensity and objective speed affect cardio met -----
    def test_explicit_intensity_and_objective_speed_affect_cardio_met(self):
        self.assertEqual(cardio_met({"name": "Run", "intensity": "hard"}), 8.0)
        self.assertEqual(cardio_met({"name": "Run", "intensity": "easy", "speed_kmh": 12}), 3.0)
        self.assertEqual(cardio_met({"name": "Run", "speed_kmh": 12}), 10.0)

# ----- verify resistance and power are contextual not vigorous by default -----
    def test_resistance_and_power_are_contextual_not_vigorous_by_default(self):
        plain = cardio_met({"name": "Bike", "equipment_context": "stationary"})
        added = cardio_met({
            "name": "Bike", "equipment_context": "stationary",
            "resistance_type": "weighted pedals",
        })
        self.assertEqual(plain, 4.0)
        self.assertEqual(added, 5.0)
        self.assertLess(added, 7.0)
        self.assertEqual(cardio_met({
            "name": "Bike", "equipment_context": "stationary",
            "resistance_level": "level 1",
        }), 4.0)
        self.assertEqual(cardio_met({
            "name": "Bike", "equipment_context": "stationary",
            "resistance_level": "level 4",
        }), 5.0)
        self.assertAlmostEqual(cardio_met({
            "name": "Bike", "power_watts": 150, "met_source": None,
        }, weight_kg=82), (10.8 * 150 / 82 + 7) / 3.5)

# ----- verify conflicting reported cardio time is capped without scaling -----
    def test_conflicting_reported_cardio_time_is_capped_without_scaling(self):
        activities = [
            {"activity_type": "cardio", "name": "Run", "duration_minutes": 40},
            {"activity_type": "cardio", "name": "Bike", "duration_minutes": 30},
        ]
        # Only 50 minutes fit in the reported session; the second activity is
        # credited for the remaining 10 minutes rather than proportionally
        # shrinking both reported activity durations.
        expected = round(((6 - 1) * 82 * 40 + (5.5 - 1) * 82 * 10) / 60)
        self.assertEqual(estimate_session_calories(82, 50, activities=activities), expected)

# ----- verify cardio only session uses cardio duration and intensity -----
    def test_cardio_only_session_uses_cardio_duration_and_intensity(self):
        activities = [{
            "activity_type": "cardio", "name": "Run",
            "duration_minutes": 30, "distance_km": 5, "intensity": "hard",
        }]
        self.assertEqual(
            estimate_session_calories(82, 30, activities=activities), 287
        )

# ----- verify mixed session does not double count overlapping minutes -----
    def test_mixed_session_does_not_double_count_overlapping_minutes(self):
        activities = [
            {"activity_type": "strength", "name": "Bench press", "sets": 4},
            {"activity_type": "cardio", "name": "Run", "duration_minutes": 60, "intensity": "hard"},
        ]
        # The 60 minutes of cardio consume the full session; strength gets zero
        # overlapping minutes instead of adding another 60-minute estimate.
        self.assertEqual(
            estimate_session_calories(82, 60, activities=activities), 574
        )

# ----- verify mixed session splits non overlapping cardio and strength time -----
    def test_mixed_session_splits_non_overlapping_cardio_and_strength_time(self):
        activities = [
            {"activity_type": "strength", "name": "Squat", "sets": 4},
            {"activity_type": "cardio", "name": "Run", "duration_minutes": 20, "intensity": "hard"},
        ]
        self.assertEqual(
            estimate_session_calories(82, 60, activities=activities), 328
        )

# ----- verify distance only cardio uses labeled conservative pace assumption -----
    def test_distance_only_cardio_uses_labeled_conservative_pace_assumption(self):
        activities = [{
            "activity_type": "cardio", "name": "Run", "distance_km": 5,
            "duration_minutes": None, "pace_min_per_km": None,
        }]
        self.assertEqual(infer_duration_minutes(activities), 40)
        # With no separate strength activity, cardio uses the overall session
        # duration and a conservative light-to-moderate running MET.
        self.assertEqual(
            estimate_session_calories(82, 40, activities=activities), 273
        )


class WorkoutPersistenceTests(unittest.TestCase):
# ----- prepare isolated test data and storage -----
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_path = database.DB_PATH
        database.DB_PATH = Path(self.tempdir.name) / "test.db"
        database.init_db()

# ----- clean up isolated test data and storage -----
    def tearDown(self):
        database.DB_PATH = self.old_db_path
        self.tempdir.cleanup()

# ----- verify session and exercises are persisted together -----
    def test_session_and_exercises_are_persisted_together(self):
        session_id = database.save_workout_session(
            session_key="abc123",
            user_id=100,
            date="2026-10-21",
            time="12:00:00",
            duration_minutes=45,
            duration_source="reported",
            weight_kg=82,
            met=3.5,
            additional_calories=154,
            exercises=[
                {"name": "Bench press", "sets": 4, "repetitions": 5,
                 "weight_kg": 50, "resistance_type": "barbell",
                 "hold_duration_seconds": None, "notes": None},
                {"name": "Row", "sets": 3, "repetitions": 10,
                 "weight_kg": 35, "resistance_type": "dumbbell",
                 "hold_duration_seconds": None, "notes": None},
                {"name": "Run", "activity_type": "cardio",
                 "duration_minutes": 20, "distance_km": 3,
                 "pace_min_per_km": 6.67, "intensity": "moderate",
                "equipment_context": "stationary", "resistance_level": "level 4",
                "power_watts": 120, "met": 5.2, "met_source": "estimated",
                "notes": None},
            ],
        )
        sessions = database.get_workout_sessions_by_date(100, "2026-10-21")
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["id"], session_id)
        self.assertEqual(sessions[0]["additional_calories"], 154)
        self.assertEqual([item["name"] for item in sessions[0]["exercises"]],
                         ["Bench press", "Row", "Run"])
        self.assertEqual(sessions[0]["exercises"][0]["resistance_type"], "barbell")
        self.assertEqual(sessions[0]["exercises"][2]["activity_type"], "cardio")
        self.assertEqual(sessions[0]["exercises"][2]["distance_km"], 3)
        self.assertEqual(sessions[0]["exercises"][2]["equipment_context"], "stationary")
        self.assertEqual(sessions[0]["exercises"][2]["resistance_level"], "level 4")
        self.assertEqual(sessions[0]["exercises"][2]["power_watts"], 120)

# ----- verify session key prevents duplicate confirmation saves -----
    def test_session_key_prevents_duplicate_confirmation_saves(self):
        args = dict(
            session_key="same-key", user_id=100, date="2026-10-21",
            time="12:00:00", duration_minutes=30, duration_source="reported",
            weight_kg=82, met=3.5, additional_calories=103,
            exercises=[{"name": "Squat"}],
        )
        database.save_workout_session(**args)
        with self.assertRaises(Exception):
            database.save_workout_session(**args)
        self.assertEqual(len(database.get_workout_sessions_by_date(100, "2026-10-21")), 1)


if __name__ == "__main__":
    unittest.main()
