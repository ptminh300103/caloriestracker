
import os
import json
import base64
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI
from food_conversation import require_product_clarification
from response_parser import normalize_response


# ----- configuration -----

load_dotenv(Path(__file__).resolve().parent / ".env")

API_KEY = os.getenv("OPENAI_API_KEY")

if not API_KEY:
    raise ValueError("OPENAI_API_KEY missing from .env")

client = AsyncOpenAI(api_key=API_KEY)

MODEL = "gpt-4.1-mini"


# ----- ai instructions -----

SYSTEM_PROMPT = """
You are a food calorie estimation assistant familiar
with Singaporean and international foods.

Estimate calories using realistic ingredients,
preparation methods, and serving sizes.

FOOD IDENTIFICATION:
1. Identify the complete meal and its major components.
2. Use the description and image together when available.
3. Assume typical portions when quantities are unspecified.
4. Do not invent major ingredients.
5. Treat calorie values as approximate.
6. Do not claim published nutritional values were verified
   unless an external source was actually checked.
7. Recognize branded foods and beverages from general knowledge and context;
   do not depend on a fixed brand list. Normalize obvious typos, speech-to-text
   errors, and abbreviations only when context makes the intended wording clear.
   For example, "less sweat" or a contextually clear "LS" in a tea order may
   mean "less sweet". Do not silently replace an uncertain brand name with a
   different known brand.

BRANDED PRODUCT IDENTIFICATION:
Return a structured product_identification object when a branded or
standardized product is described, with brand, product_name, variant,
customization, serving_size, serving_assumption, and identity_status
("identified" or "uncertain").
Use null for details the user did not provide and you cannot confidently infer.
Product identity and exact nutrition facts are different questions: if the
brand/product and food or beverage type are identifiable but official calories
are unavailable, estimate from a typical serving and comparable foods or
ingredients. State the serving assumption and that the estimate is approximate;
never invent an official value or imply a lookup occurred. Missing serving size
alone is not a reason to ask if a standard serving estimate is reasonable.
Ask one targeted question only when the product/food identity itself is unclear
enough that a meaningful estimate cannot be made.
A recognizable seller/brand alone is not a specific product identity. If the
user only names a brand and no drink, dish, or product, ask which item they had.
Do not turn brand recognition into an assumed default product.

CLARIFICATION:
Ask at most one question per meal, and only when the identity or major
calorie-relevant components are insufficiently known.
Information sufficiency is separate from calorie-estimate reliability. If an
essential product detail is missing, set clarification_needed=true and provide
one targeted question even when estimate_reliable=false and items is empty.
Return food_identity_status as "sufficient" or "insufficient" separately from
estimate_reliable. Use "insufficient" when the food/drink or its major
calorie-relevant components cannot yet be identified well enough to estimate.
Do not make the caller choose between an estimate and a clarification flag.

Do not ask for exact weights or minor preparation details
when a reasonable standard-serving estimate is possible.

After a clarification, preserve the original meal and
combine it with the user's new information.

PERSONAL FOOD MEMORY:
The previous_context may contain up to three examples from this user's confirmed
food history. These are personal estimates, not verified nutrition facts. Use
them only as supporting evidence and adjust for meaningful changes in portion,
ingredients, brand, preparation, or customization. Repeated use does not make an
estimate more reliable. When an image is attached, treat the current image and
description as primary evidence; compare visible components and portions with
the examples, and do not claim the image verifies a match when visibility is poor.

A clarification is NOT a replacement meal.

When clarification is disabled, do not ask another question. Preserve original
meal components and use only explicit details plus reasonable portion
assumptions. If the food cannot be meaningfully identified without inventing
major ingredients, return estimate_reliable=false, a short reason, and an
empty items list instead of forcing a calorie estimate.

When enough information is available, estimate immediately.
For a recognizable branded or standardized food/drink, distinguish uncertainty
about exact calories from uncertainty about what the item is. If identity is
clear but official nutrition is unavailable, return an approximate estimate
using a typical serving or comparable ingredients, set identity_status to
"identified", and do not ask a clarification merely to obtain official
calories. Never return a verified nutrition claim: this application has no
external lookup. Return structured product_identification fields and an
appropriate calorie_basis in every response.

OUTPUT:
Return valid JSON only:

{
  "items": [
    {"name": "Food", "calories": 300}
  ],
  "total_calories": 300,
  "clarification_needed": false,
  "question": null,
  "food_identity_status": "sufficient",
  "estimate_reliable": true,
  "reliability_reason": null,
  "product_identification": null,
  "calorie_basis": "typical_portion"
}

If clarification is needed:
- Ask one specific question.
- An empty items list is allowed.
- Do not invent ingredients to produce an estimate.

If clarification is not needed:
- Return at least one food item.
- Set clarification_needed to false.
- Set question to null.
- Include product_identification with the structured product fields when applicable.
- Use calorie_basis "approximate_comparable" or "typical_portion" unless an
  external source was actually checked. This application has no lookup feature.

Total calories must equal the sum of item calories.
"""


# ----- validate and normalize a food-calorie estimate -----
def validate_estimate(result, allow_clarification=True):

    if not isinstance(result, dict):
        raise ValueError("AI returned an invalid response")

    clarification_needed = (
        result.get("clarification_needed") is True
        and allow_clarification
    )

    identity_status = result.get("food_identity_status")
    if identity_status not in {"sufficient", "insufficient"}:
        # Backward compatibility for responses from the previous prompt.
        identity_status = (
            "insufficient" if result.get("clarification_needed") is True
            else "sufficient"
        )

    product = result.get("product_identification")
    clean_product = None
    if isinstance(product, dict):
        clean_product = {}
        for field in (
            "brand", "product_name", "variant", "customization",
            "serving_size", "serving_assumption"
        ):
            value = product.get(field)
            clean_product[field] = (
                str(value).strip()
                if value is not None and str(value).strip() else None
            )
        product_identity_status = product.get("identity_status", "uncertain")
        clean_product["identity_status"] = (
            product_identity_status if product_identity_status in {"identified", "uncertain"}
            else "uncertain"
        )
        # Unknown official calories do not make a known product unidentified.
        # A seller name alone is not a sufficiently identified product.
        product_described = any(
            clean_product[field]
            and "".join(normalize_response(clean_product[field]).split())
            != "".join(normalize_response(clean_product["brand"] or "").split())
            for field in ("product_name", "variant")
        )
        if clean_product["identity_status"] == "identified" and product_described:
            clarification_needed = False
            if "food_identity_status" not in result:
                # Older responses often used clarification_needed only for
                # serving-size questions; a described product is identifiable.
                identity_status = "sufficient"

    items = result.get("items", [])

    if not isinstance(items, list):
        raise ValueError("Invalid food items")

    cleaned_items = []

    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Invalid food item")

        name = str(item["name"]).strip()
        raw_calories = item["calories"]

        if isinstance(raw_calories, bool):
            raise ValueError("Invalid calorie value")

        calories = int(raw_calories)

        if not name or calories < 0:
            raise ValueError("Invalid food item data")

        cleaned_items.append({
            "name": name,
            "calories": calories,
        })

    question = result.get("question")

    # Identity uncertainty may coexist with an unreliable or empty calorie
    # estimate. Keep it actionable while the meal's one clarification remains.
    if identity_status == "insufficient" and allow_clarification:
        clarification_needed = True
        if not isinstance(question, str) or not question.strip():
            if clean_product and clean_product.get("brand") and not any(
                clean_product.get(field) for field in ("product_name", "variant")
            ):
                question = (
                    "What specific food or drink did you have from "
                    f"{clean_product['brand']}?"
                )
            elif clean_product and clean_product.get("product_name"):
                question = (
                    "What main ingredients or serving details should I know "
                    f"about {clean_product['product_name']}?"
                )
            else:
                question = "What food or drink was it, and what were its main ingredients?"

    if clarification_needed:
        if not isinstance(question, str) or not question.strip():
            raise ValueError(
                "Clarification requested without a question"
            )
    else:
        question = None

    estimate_reliable = result.get("estimate_reliable") is not False
    reliability_reason = result.get("reliability_reason")
    if (
        clean_product
        and clean_product["identity_status"] == "identified"
        and cleaned_items
    ):
        estimate_reliable = True
        reliability_reason = None
    calorie_basis = result.get("calorie_basis", "typical_portion")
    if calorie_basis not in {
        "typical_portion", "approximate_comparable", "not_applicable"
    }:
        # Without an external nutrition source, never preserve a claimed
        # official/verified value from the model.
        calorie_basis = (
            "approximate_comparable" if clean_product else "typical_portion"
        )

    brand_without_product = bool(
        clean_product
        and clean_product.get("brand")
        and not clean_product.get("product_name")
        and not clean_product.get("variant")
    )
    if brand_without_product and not cleaned_items:
        estimate_reliable = False
    if (
        not cleaned_items and not clarification_needed and estimate_reliable
        and not brand_without_product
    ):
        raise ValueError("AI returned no food items")

    return {
        "items": cleaned_items,
        "total_calories": sum(
            item["calories"] for item in cleaned_items
        ),
        "clarification_needed": clarification_needed,
        "question": question,
        "food_identity_status": identity_status,
        "estimate_reliable": estimate_reliable,
        "reliability_reason": (
            str(reliability_reason).strip()
            if reliability_reason and not estimate_reliable else None
        ),
        "product_identification": clean_product,
        "calorie_basis": calorie_basis,
        "nutrition_verified": False,
    }


# ----- estimate meal calories from its description and photo -----
async def estimate_food_calories(
    description: str,
    previous_context: dict | None = None,
    allow_clarification: bool = True,
    image_bytes: bytes | None = None,
) -> dict:

    user_content = {
        "description": description,
        "previous_context": previous_context,
        "allow_clarification": allow_clarification,
    }

    instructions = SYSTEM_PROMPT

    if not allow_clarification:
        instructions += """
For this request, clarification is disabled.
Use reasonable assumptions and return a complete
estimate rather than another question.
"""

    content = [
        {
            "type": "text",
            "text": json.dumps(user_content),
        }
    ]

    # Include image only when provided
    if image_bytes is not None:

        encoded_image = base64.b64encode(
            image_bytes
        ).decode("utf-8")

        content.append({
            "type": "image_url",
            "image_url": {
                "url": (
                    "data:image/jpeg;base64,"
                    + encoded_image
                ),
                "detail": "low",
            },
        })

    response = await client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": instructions,
            },
            {
                "role": "user",
                "content": content,
            },
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
    )

    raw_content = response.choices[0].message.content

    if not raw_content:
        raise ValueError("OpenAI returned an empty response")

    result = json.loads(raw_content)

    validated = validate_estimate(
        result,
        allow_clarification=allow_clarification,
    )
    return require_product_clarification(
        description, validated, allow_clarification=allow_clarification
    )


# ----- classify a reply to a pending food estimate -----
async def classify_food_reply(
    original_description: str,
    question: str,
    reply: str,
) -> str:

    response = await client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": """
Classify a user's response to a food clarification.

Return exactly one category:

clarification:
The user answers the question or adds information
to the existing meal.

correction:
The user modifies details of the existing meal.

replacement:
The user abandons the original meal and describes
a different meal.

Return JSON only:
{"type": "clarification"}
""",
            },
            {
                "role": "user",
                "content": json.dumps({
                    "original_meal": original_description,
                    "question": question,
                    "reply": reply,
                }),
            },
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )

    result = json.loads(
        response.choices[0].message.content
    )

    reply_type = result.get("type", "clarification")

    if reply_type not in {
        "clarification",
        "correction",
        "replacement",
    }:
        return "clarification"

    return reply_type
