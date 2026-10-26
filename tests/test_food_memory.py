import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import database
import main
from food_memory import (
    canonical_identity,
    exact_match_ready,
    find_exact_memory,
    find_similar_memories,
    memory_context,
    prepare_memory,
)


class FoodMemoryTests(unittest.TestCase):
# ----- prepare isolated test data and storage -----
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_path = database.DB_PATH
        database.DB_PATH = Path(self.tempdir.name) / "memory.db"
        database.init_db()

# ----- clean up isolated test data and storage -----
    def tearDown(self):
        database.DB_PATH = self.old_path
        self.tempdir.cleanup()

# ----- persist confirmed -----
    def save_confirmed(self, user_id, description, calories=300, *,
                       evidence="ai_confirmed", product=None, photo=False):
        result = {
            "items": [{"name": "Meal", "calories": calories}],
            "total_calories": calories,
            "product_identification": product,
        }
        memory = prepare_memory(
            description, result, evidence_category=evidence,
            source_origin="manual_correction" if evidence == "user_corrected" else "ai_estimated",
            photo_included=photo,
        )
        entry_id = database.save_food(
            user_id, "2026-10-01", "12:00:00", description,
            result["items"], calories, confirmation_key=f"{user_id}-{description}-{calories}",
            memory=memory,
        )
        return entry_id, memory

# ----- verify canonical identity normalizes order units punctuation and number words -----
    def test_canonical_identity_normalizes_order_units_punctuation_and_number_words(self):
        self.assertEqual(
            canonical_identity("I had 150 grams grilled chicken!"),
            canonical_identity("chicken grilled 150 g"),
        )
        self.assertEqual(canonical_identity("two eggs"), canonical_identity("2 eggs"))

# ----- verify exact match requires portion and known branded product -----
    def test_exact_match_requires_portion_and_known_branded_product(self):
        self.assertTrue(exact_match_ready("2 eggs"))
        self.assertFalse(exact_match_ready("eggs"))
        self.assertFalse(exact_match_ready(
            "large Brand Q milk tea", {"brand": "Brand Q", "identity_status": "identified"}
        ))
        self.assertTrue(exact_match_ready(
            "large Brand Q lychee tea",
            {"brand": "Brand Q", "product_name": "Lychee tea", "identity_status": "identified"},
        ))

# ----- verify exact match reuses confirmed calories and is user scoped -----
    def test_exact_match_reuses_confirmed_calories_and_is_user_scoped(self):
        self.save_confirmed(1, "2 eggs", 155)
        memory = find_exact_memory(1, "two eggs!")
        self.assertEqual(memory["calories"], 155)
        self.assertIsNone(find_exact_memory(2, "2 eggs"))

# ----- verify same food different portion is not exact but is similar -----
    def test_same_food_different_portion_is_not_exact_but_is_similar(self):
        self.save_confirmed(1, "grilled chicken breast 150 g", 250)
        self.assertIsNone(find_exact_memory(1, "grilled chicken breast 200 g"))
        similar = find_similar_memories(1, "grilled chicken breast 200 g")
        self.assertEqual(len(similar), 1)

# ----- verify different brands and customizations do not exact match -----
    def test_different_brands_and_customizations_do_not_exact_match(self):
        self.save_confirmed(
            1, "Brand Q oat latte 12 oz less sweet",
            product={"brand": "Brand Q", "product_name": "oat latte", "identity_status": "identified"},
        )
        self.assertIsNone(find_exact_memory(1, "Brand R oat latte 12 oz less sweet"))
        self.assertIsNone(find_exact_memory(1, "Brand Q oat latte 12 oz extra sweet"))
        self.assertEqual(find_similar_memories(1, "Brand R oat latte 12 oz less sweet"), [])

# ----- verify similar retrieval is bounded and does not promote evidence -----
    def test_similar_retrieval_is_bounded_and_does_not_promote_evidence(self):
        for portion in (100, 120, 140, 160):
            self.save_confirmed(1, f"grilled chicken breast {portion} g", 200 + portion)
        examples = find_similar_memories(1, "grilled chicken breast 180 g", limit=10)
        self.assertEqual(len(examples), 3)
        context = memory_context(examples)
        self.assertIn("not verified nutrition facts", context["instruction"])
        self.assertTrue(all(item["evidence_category"] == "ai_confirmed" for item in context["similar_meal_examples"]))

# ----- verify photo memory context treats photo as primary and does not claim verification -----
    def test_photo_memory_context_treats_photo_as_primary_and_does_not_claim_verification(self):
        _, _ = self.save_confirmed(1, "chicken rice 1 serving", photo=True)
        memories = database.get_food_memories(1)
        self.assertEqual(memories[0]["exact_ready"], 1)
        context = memory_context(memories, image_included=True)
        self.assertIn("primary evidence", context["instruction"])
        self.assertIn("does not verify", context["instruction"])

# ----- verify edit and delete reconcile source memory with audit -----
    def test_edit_and_delete_reconcile_source_memory_with_audit(self):
        entry_id, _ = self.save_confirmed(3, "2 eggs", 155)
        memory = find_exact_memory(3, "2 eggs")
        self.assertTrue(database.update_food_calories(3, entry_id, 180))
        corrected = find_exact_memory(3, "2 eggs")
        self.assertEqual(corrected["evidence_category"], "user_corrected")
        self.assertEqual(corrected["source_origin"], "manual_correction")
        self.assertEqual(corrected["original_source_origin"], "ai_estimated")
        self.assertEqual(corrected["calories"], 180)
        self.assertTrue(database.delete_food(3, entry_id))
        self.assertIsNone(find_exact_memory(3, "2 eggs"))
        events = database.get_food_memory_events(3, entry_id)
        self.assertEqual([event["event_type"] for event in events], ["created", "corrected", "source_deleted"])
        self.assertEqual(events[0]["memory_id"], memory["id"])

# ----- verify repeated exact reuse keeps original evidence and single memory -----
    def test_repeated_exact_reuse_keeps_original_evidence_and_single_memory(self):
        _, _ = self.save_confirmed(4, "2 eggs", 155)
        original = find_exact_memory(4, "2 eggs")
        reused_ids = []
        for suffix in ("first", "second"):
            reused_ids.append(database.save_food(
                4, "2026-10-02", "12:00:00", "2 eggs", original["items"], 155,
                confirmation_key=suffix, memory_reuse_id=original["id"],
            ))
        current = find_exact_memory(4, "2 eggs")
        self.assertEqual(current["usage_count"], 3)
        self.assertEqual(current["evidence_category"], "ai_confirmed")
        self.assertEqual(len(database.get_food_memories(4)), 1)
        self.assertEqual(len([e for e in database.get_food_memory_events(4) if e["event_type"] == "reused"]), 2)
        database.update_food_calories(4, reused_ids[0], 180)
        corrected = find_exact_memory(4, "2 eggs")
        self.assertEqual(corrected["calories"], 180)
        self.assertEqual(corrected["evidence_category"], "user_corrected")
        database.delete_food(4, reused_ids[1])
        self.assertEqual(find_exact_memory(4, "2 eggs")["usage_count"], 2)

# ----- verify unconfirmed food never enters memory -----
    def test_unconfirmed_food_never_enters_memory(self):
        database.save_food(5, "2026-10-01", "12:00", "2 eggs", [], 155)
        self.assertEqual(database.get_food_memories(5), [])

# ----- verify confirmation key prevents duplicate food and memory saves -----
    def test_confirmation_key_prevents_duplicate_food_and_memory_saves(self):
        description = "2 eggs"
        result = {
            "items": [{"name": "Eggs", "calories": 155}],
            "total_calories": 155,
            "product_identification": None,
        }
        memory = prepare_memory(description, result)
        first = database.save_food(
            15, "2026-10-01", "12:00", description, result["items"], 155,
            confirmation_key="same-confirmation", memory=memory,
        )
        second = database.save_food(
            15, "2026-10-01", "12:00", description, result["items"], 155,
            confirmation_key="same-confirmation", memory=memory,
        )
        self.assertEqual(first, second)
        self.assertEqual(len(database.get_food_by_date(15, "2026-10-01")), 1)
        self.assertEqual(len(database.get_food_memories(15)), 1)

# ----- verify database persistence after reopen -----
    def test_database_persistence_after_reopen(self):
        self.save_confirmed(6, "2 eggs", 155)
        database.DB_PATH = Path(self.tempdir.name) / "memory.db"
        self.assertEqual(find_exact_memory(6, "2 eggs")["calories"], 155)

# ----- verify exact food flow skips estimation api and keeps confirmation -----
    def test_exact_food_flow_skips_estimation_api_and_keeps_confirmation(self):
        self.save_confirmed(7, "2 eggs", 155)
        match = find_exact_memory(7, "2 eggs")
        pending = main.new_meal("2 eggs")
        pending["memory_match"] = match
        context = SimpleNamespace(user_data={"pending_food": pending})

        class Message:
            from_user = SimpleNamespace(id=7)

# ----- initialize the test helper -----
            def __init__(self):
                self.sent = []

# ----- capture text replies sent by the handler -----
            async def reply_text(self, text, **kwargs):
                self.sent.append((text, kwargs))

# ----- execute the asynchronous test scenario -----
        async def run():
            message = Message()
            with patch.object(main, "estimate_food_calories", new=AsyncMock(side_effect=AssertionError("should skip estimator"))):
                await main.analyze_meal(message, context, pending)
            return message

        message = asyncio.run(run())
        self.assertEqual(pending["estimate"]["total_calories"], 155)
        self.assertEqual(context.user_data["food_state"], "confirming")
        self.assertTrue(any("Reused from your confirmed meal history" in text for text, _ in message.sent))
        self.assertTrue(any(kwargs.get("reply_markup") for _, kwargs in message.sent))

# ----- verify exact new message routes without classifier api -----
    def test_exact_new_message_routes_without_classifier_api(self):
        self.save_confirmed(17, "2 eggs", 155)

        class Message:
            text = "two eggs!"
            from_user = SimpleNamespace(id=17)

# ----- initialize the test helper -----
            def __init__(self):
                self.sent = []

# ----- capture text replies sent by the handler -----
            async def reply_text(self, text, **kwargs):
                self.sent.append(text)

# ----- execute the asynchronous test scenario -----
        async def run():
            message = Message()
            context = SimpleNamespace(user_data={})
            update = SimpleNamespace(message=message, effective_user=message.from_user)
            with patch.object(main, "classify_message", new=AsyncMock(side_effect=AssertionError("exact meals should route locally"))):
                await main.handle_text(update, context)
            return message, context

        message, context = asyncio.run(run())
        self.assertEqual(context.user_data["food_state"], "awaiting_photo_choice")
        self.assertEqual(context.user_data["pending_food"]["memory_match"]["calories"], 155)
        self.assertEqual(message.sent[0], "Would you like to add a photo?")

# ----- verify food confirmation saves memory atomically and clears pending -----
    def test_food_confirmation_saves_memory_atomically_and_clears_pending(self):
        pending = main.new_meal("2 eggs")
        pending["memory_match"] = None
        pending["estimate"] = {
            "items": [{"name": "Eggs", "calories": 155}],
            "total_calories": 155,
            "product_identification": None,
            "clarification_needed": False,
        }
        pending["memory_basis"] = pending["estimate"]
        pending["estimate_source"] = "ai_estimated"
        user_data = {"pending_food": pending, "food_state": "confirming"}

        class QueryMessage:
# ----- initialize the test helper -----
            def __init__(self):
                self.sent = []

# ----- capture text replies sent by the handler -----
            async def reply_text(self, text, **kwargs):
                self.sent.append(text)

# ----- capture removal of the inline keyboard -----
            async def edit_reply_markup(self, **kwargs):
                return None

        class Query:
            data = f"food_confirm:{pending['id']}"
            from_user = SimpleNamespace(id=18)
            message = QueryMessage()

# ----- capture acknowledgement of the callback query -----
            async def answer(self):
                return None

# ----- capture removal of the callback message keyboard -----
            async def edit_message_reply_markup(self, **kwargs):
                return None

# ----- execute the asynchronous test scenario -----
        async def run():
            query = Query()
            update = SimpleNamespace(callback_query=query)
            await main.handle_food_buttons(update, SimpleNamespace(user_data=user_data))
            return query

        query = asyncio.run(run())
        memories = database.get_food_memories(18)
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["evidence_category"], "ai_confirmed")
        self.assertEqual(memories[0]["source_origin"], "ai_estimated")
        self.assertNotIn("pending_food", user_data)
        self.assertIn("Food recorded successfully", query.message.sent[0])

# ----- verify photo forces ai review even for an exact text match -----
    def test_photo_forces_ai_review_even_for_an_exact_text_match(self):
        self.save_confirmed(8, "2 eggs", 155)
        pending = main.new_meal("2 eggs")
        pending["memory_match"] = find_exact_memory(8, "2 eggs")
        pending["image_bytes"] = b"photo"
        context = SimpleNamespace(user_data={"pending_food": pending})

        class Message:
            from_user = SimpleNamespace(id=8)

# ----- capture text replies sent by the handler -----
            async def reply_text(self, text, **kwargs):
                return None

        estimate = {
            "items": [{"name": "Eggs", "calories": 200}],
            "total_calories": 200,
            "clarification_needed": False,
            "question": None,
            "estimate_reliable": True,
            "product_identification": None,
        }

# ----- execute the asynchronous test scenario -----
        async def run():
            with patch.object(main, "estimate_food_calories", new=AsyncMock(return_value=estimate)) as model:
                await main.analyze_meal(Message(), context, pending)
                return model

        model = asyncio.run(run())
        self.assertTrue(model.await_count)
        kwargs = model.await_args.kwargs
        self.assertEqual(kwargs["image_bytes"], b"photo")
        self.assertIn("primary evidence", kwargs["previous_context"]["instruction"])


if __name__ == "__main__":
    unittest.main()
