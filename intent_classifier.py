
import json

from food_conversation import is_ambiguous_food_exercise_message

from openai import AsyncOpenAI

client = None


# ----- classify a new message for food or exercise routing -----
async def classify_message(text: str) -> str:
    """
    Classify a new message as food, exercise, ambiguous, or unclear.
    """

    if is_ambiguous_food_exercise_message(text):
        return "ambiguous"

    global client
    if client is None:
        client = AsyncOpenAI()

    response = await client.chat.completions.create(
        model="gpt-4.1-mini",
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": """
Classify the user's message for a calorie-tracking bot.

Categories:
- food: Describes eating, drinking, meals, or food.
- exercise: Describes physical exercise or a workout.
- unclear: Neither category is clear, or both are present.
- ambiguous: A short name plausibly means either food or exercise.

Examples of ambiguous short terms include "dip" or "dips" without context.

Use the intended meaning, not isolated keywords.
Treat a recognizable restaurant, packaged-food, or beverage brand/product name
as a potential food logging request even when it is short or contains no
calorie details. A brand name followed by flavor, drink, milk, sweetness,
ice, size, or serving customization language is a food request. Do not require
the brand to be in a fixed vocabulary; use general knowledge and context.
Interpret likely spelling, speech-to-text, and common abbreviation variants
in context (for example, a beverage customization may contain a misspelled
"sweet"), without changing the intended category based on a literal typo.
Clear exercise verbs and workout descriptions still take priority.
Do not calculate calories.

Return JSON only:
{"category": "food"}
"""
            },
            {
                "role": "user",
                "content": text
            }
        ]
    )

    result = json.loads(
        response.choices[0].message.content
    )

    category = result.get("category", "unclear")

    if category not in {"food", "exercise", "unclear", "ambiguous"}:
        return "unclear"

    return category
