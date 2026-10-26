import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import database
from ai_estimator import SYSTEM_PROMPT, estimate_food_calories, validate_estimate
from food_conversation import (
    clarification_action,
    require_product_clarification,
    should_ask_food_clarification,
)
import intent_classifier
from main import show_estimate


class BrandedFoodRecognitionTests(unittest.TestCase):
# ----- verify classifier treats unfamiliar brand plus beverage terms as food -----
    def test_classifier_treats_unfamiliar_brand_plus_beverage_terms_as_food(self):
        captured = {}

# ----- mock the ai response creation call -----
        async def create(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content='{"category":"food"}')
            )])

        fake_client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)
        ))

# ----- execute the asynchronous test scenario -----
        async def run():
            with patch.object(intent_classifier, "client", fake_client):
                return await intent_classifier.classify_message(
                    "Zorli lychee milk tea, LS, medium"
                )

        self.assertEqual(asyncio.run(run()), "food")
        prompt = captured["messages"][0]["content"].lower()
        self.assertIn("do not require", prompt)
        self.assertIn("spelling", prompt)
        self.assertIn("food request", prompt)

# ----- verify product prompt normalizes contextual typos without brand list -----
    def test_product_prompt_normalizes_contextual_typos_without_brand_list(self):
        prompt = SYSTEM_PROMPT.lower()
        self.assertIn("less sweat", prompt)
        self.assertIn("abbreviations", prompt)
        self.assertIn("do not depend on a fixed brand list", prompt)
        self.assertIn("exact nutrition facts are different questions", prompt)

# ----- verify identified brand with unknown official calories gets estimate -----
    def test_identified_brand_with_unknown_official_calories_gets_estimate(self):
        result = validate_estimate({
            "items": [{"name": "Lychee milk tea, medium", "calories": 280}],
            "clarification_needed": True,
            "question": "What size was it?",
            "estimate_reliable": False,
            "reliability_reason": "No published nutrition data",
            "product_identification": {
                "brand": "Chagee", "product_name": "tea",
                "variant": "lychee", "customization": "less sweet",
                "serving_size": "medium", "identity_status": "identified",
            },
            "calorie_basis": "verified_official",
        })
        self.assertFalse(result["clarification_needed"])
        self.assertTrue(result["estimate_reliable"])
        self.assertEqual(result["calorie_basis"], "approximate_comparable")
        self.assertFalse(result["nutrition_verified"])
        self.assertEqual(result["product_identification"]["brand"], "Chagee")
        self.assertEqual(result["product_identification"]["customization"], "less sweet")

# ----- verify standardized product uses structured identity and typical portion -----
    def test_standardized_product_uses_structured_identity_and_typical_portion(self):
        result = validate_estimate({
            "items": [{"name": "Plain Greek yogurt cup", "calories": 150}],
            "clarification_needed": False,
            "product_identification": {
                "brand": None, "product_name": "plain Greek yogurt",
                "variant": None, "customization": None,
                "serving_size": "single cup", "identity_status": "identified",
            },
            "calorie_basis": "typical_portion",
        })
        self.assertFalse(result["clarification_needed"])
        self.assertEqual(result["product_identification"]["product_name"], "plain Greek yogurt")
        self.assertEqual(result["calorie_basis"], "typical_portion")

# ----- verify ambiguous product identity gets only one targeted question -----
    def test_ambiguous_product_identity_gets_only_one_targeted_question(self):
        result = validate_estimate({
            "items": [],
            "clarification_needed": True,
            "question": "Is ‘Apple’ the fruit or a branded drink?",
            "estimate_reliable": True,
            "product_identification": {
                "brand": None, "product_name": "Apple", "variant": None,
                "customization": None, "serving_size": None,
                "identity_status": "uncertain",
            },
            "calorie_basis": "not_applicable",
        })
        self.assertTrue(should_ask_food_clarification(0, result))
        self.assertFalse(should_ask_food_clarification(1, result))
        self.assertIn("fruit or a branded drink", result["question"])

# ----- verify recognized brand without product requests which item -----
    def test_recognized_brand_without_product_requests_which_item(self):
        raw = {
            "items": [], "clarification_needed": False, "question": None,
            "estimate_reliable": True,
            "product_identification": {
                "brand": "Gong Cha", "product_name": None, "variant": None,
                "customization": None, "serving_size": None,
                "identity_status": "identified",
            },
        }
        validated = validate_estimate(raw)
        result = require_product_clarification("gongcha", validated)
        self.assertTrue(result["clarification_needed"])
        self.assertFalse(result["estimate_reliable"])
        self.assertIn("What specific food or drink", result["question"])

# ----- verify incomplete food can ask even when estimate is unreliable -----
    def test_incomplete_food_can_ask_even_when_estimate_is_unreliable(self):
        result = validate_estimate({
            "items": [], "clarification_needed": True,
            "food_identity_status": "insufficient",
            "question": "What filling was in the sandwich?",
            "estimate_reliable": False,
        })
        self.assertTrue(result["clarification_needed"])
        self.assertFalse(result["estimate_reliable"])
        self.assertTrue(should_ask_food_clarification(0, result))

# ----- verify identity insufficiency is separate from calorie reliability -----
    def test_identity_insufficiency_is_separate_from_calorie_reliability(self):
        result = validate_estimate({
            "items": [], "clarification_needed": False,
            "food_identity_status": "insufficient",
            "estimate_reliable": False,
        })
        self.assertEqual(result["food_identity_status"], "insufficient")
        self.assertFalse(result["estimate_reliable"])
        self.assertTrue(result["clarification_needed"])
        self.assertTrue(result["question"])
        self.assertTrue(should_ask_food_clarification(0, result))
        self.assertFalse(should_ask_food_clarification(1, result))

# ----- verify complete product description does not need clarification -----
    def test_complete_product_description_does_not_need_clarification(self):
        result = validate_estimate({
            "items": [{"name": "Milk tea with tapioca pearls", "calories": 320}],
            "clarification_needed": False,
            "product_identification": {
                "brand": "Gong Cha", "product_name": "milk tea",
                "variant": "tapioca pearls", "customization": "less sweet",
                "serving_size": "medium", "identity_status": "identified",
            },
        })
        result = require_product_clarification(
            "Gong Cha milk tea with tapioca pearls, medium, less sweet", result
        )
        self.assertFalse(result["clarification_needed"])
        self.assertTrue(result["estimate_reliable"])

# ----- verify unknown answers proceed without a second question -----
    def test_unknown_answers_proceed_without_a_second_question(self):
        for reply in ("idk", "no", "not sure"):
            self.assertEqual(clarification_action(reply), "estimate_from_original")
        self.assertFalse(should_ask_food_clarification(1, {
            "clarification_needed": True,
        }))

# ----- verify unreliable estimate still asks allowed clarification once -----
    def test_unreliable_estimate_still_asks_allowed_clarification_once(self):
        class Message:
# ----- initialize the test helper -----
            def __init__(self):
                self.sent = []

# ----- capture text replies sent by the handler -----
            async def reply_text(self, text, **kwargs):
                self.sent.append(text)

# ----- exercise the clarification conversation path -----
        async def exercise_flow():
            message = Message()
            pending = {"id": "meal-1", "clarifications": 0}
            context = SimpleNamespace(user_data={"pending_food": pending})
            result = {
                "items": [], "total_calories": 0,
                "clarification_needed": True,
                "question": "Which drink did you order?",
                "estimate_reliable": False,
                "reliability_reason": None,
            }
            await show_estimate(message, context, pending, result)
            self.assertEqual(context.user_data["food_state"], "clarifying")
            self.assertEqual(pending["clarifications"], 1)
            self.assertEqual(message.sent, ["Which drink did you order?"])

            second_result = dict(result)
            await show_estimate(message, context, pending, second_result)
            self.assertNotIn("pending_food", context.user_data)
            self.assertEqual(pending["clarifications"], 1)
            self.assertEqual(len(message.sent), 2)
            self.assertIn("can't estimate this meal reliably", message.sent[-1])

        asyncio.run(exercise_flow())

# ----- verify estimator applies brand only clarification after validation -----
    def test_estimator_applies_brand_only_clarification_after_validation(self):
        raw = {
            "items": [], "clarification_needed": False, "question": None,
            "estimate_reliable": True,
            "product_identification": {
                "brand": "Gong Cha", "product_name": None, "variant": None,
                "customization": None, "serving_size": None,
                "identity_status": "identified",
            },
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
            import ai_estimator
            with patch.object(ai_estimator, "client", fake_client):
                return await estimate_food_calories("gongcha")

        result = asyncio.run(run())
        self.assertTrue(result["clarification_needed"])
        self.assertEqual(result["items"], [])


class ProductMetadataPersistenceTests(unittest.TestCase):
# ----- prepare isolated test data and storage -----
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_path = database.DB_PATH
        database.DB_PATH = Path(self.tempdir.name) / "products.db"
        database.init_db()

# ----- clean up isolated test data and storage -----
    def tearDown(self):
        database.DB_PATH = self.old_path
        self.tempdir.cleanup()

# ----- verify confirmed food can store structured product metadata -----
    def test_confirmed_food_can_store_structured_product_metadata(self):
        product = {
            "brand": "Chagee", "product_name": "tea", "variant": "lychee",
            "customization": "less sweet", "serving_size": "medium",
            "identity_status": "identified",
        }
        database.save_food(8, "2026-01-01", "12:00:00", "chagee lychee tea",
                           [{"name": "Lychee tea", "calories": 280}], 280,
                           product_info=product)
        entry = database.get_food_by_date(8, "2026-01-01")[0]
        self.assertEqual(entry["product_info"], product)


if __name__ == "__main__":
    unittest.main()
