
import json
import re

from openai import AsyncOpenAI
from strength_training import cardio_met

client = AsyncOpenAI()


STRENGTH_FIELDS = (
    "name", "sets", "repetitions", "weight_kg", "resistance_type",
    "hold_duration_seconds", "duration_minutes", "distance_km",
    "pace_min_per_km", "speed_kmh", "power_watts", "equipment_context",
    "resistance_level", "intensity", "met", "met_source", "activity_type",
    "notes",
)


# ----- extract structured activity details -----
async def extract_strength_exercise(description: str) -> dict:
    """Extract a strength or cardio activity and at most one question."""
    response = await client.chat.completions.create(
        model="gpt-4.1-mini",
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": """
Classify the description as strength, cardio, or other exercise. Extract name,
sets, repetitions, load in kg, resistance type, hold duration seconds,
activity duration minutes, distance km, pace minutes/km, intensity text,
speed km/h, measured power watts, equipment context (stationary/outdoor/other),
and machine resistance level when stated. Preserve machine levels verbatim;
never convert them to watts or kilograms. Distinguish stationary cycling from
outdoor cycling when stated or strongly supported by equipment context. A
stationary bike described with pedals or added resistance is still stationary
cycling; preserve the resistance description separately. Use null for anything
not stated; do not invent duration, speed, cadence, power,
distance, or MET. Set met only when the user explicitly provides a numerical
MET value, and set met_source to "user_reported" only in that case. Do not
infer vigorous intensity from an exercise name or resistance alone.
For cardio such as "run", ask ONE question that accepts either duration or
distance (e.g. "How long did you run, or how far?"). For strength, ask at most
one combined question about useful missing fields. Bodyweight lifts do not
require a load. Return JSON only with activity_type (strength/cardio/other),
all fields, clarification_needed and question.
Example: {"activity_type":"strength","is_strength":true,
"name":"Bench press","sets":4,"repetitions":5,"weight_kg":50,
"resistance_type":"barbell","hold_duration_seconds":null,
"duration_minutes":null,"distance_km":null,"pace_min_per_km":null,
"speed_kmh":null,"power_watts":null,"equipment_context":"other",
"resistance_level":null,"intensity":null,"met":null,"met_source":null,
"notes":null,
"clarification_needed":false,"question":null}
"""},
            {"role": "user", "content": description},
        ],
    )
    content = response.choices[0].message.content
    if not content:
        raise ValueError("OpenAI returned an empty exercise response")
    result = json.loads(content)
    activity_type = str(result.get("activity_type") or (
        "strength" if result.get("is_strength") else "other"
    )).lower()
    if activity_type not in {"strength", "cardio", "other"}:
        activity_type = "other"
    if activity_type == "other":
        return {"is_strength": False, "is_workout_activity": False,
                "activity_type": "other"}
    name = str(result.get("name") or "Strength exercise").strip()
    if not name:
        raise ValueError("Exercise name is required")
    clean = {
        "is_strength": activity_type == "strength",
        "is_workout_activity": True,
        "activity_type": activity_type,
        "name": name,
    }
    for key in ("sets", "repetitions"):
        raw = result.get(key)
        clean[key] = int(raw) if raw is not None else None
        if clean[key] is not None and not 1 <= clean[key] <= 100:
            raise ValueError(f"Invalid exercise {key}")
    raw_weight = result.get("weight_kg")
    clean["weight_kg"] = float(raw_weight) if raw_weight is not None else None
    if clean["weight_kg"] is not None and not 0 < clean["weight_kg"] <= 1000:
        raise ValueError("Invalid exercise weight")
    resistance_type = result.get("resistance_type")
    clean["resistance_type"] = (
        str(resistance_type).strip().lower() if resistance_type else None
    )
    raw_hold = result.get("hold_duration_seconds")
    clean["hold_duration_seconds"] = int(raw_hold) if raw_hold is not None else None
    if clean["hold_duration_seconds"] is not None and not 1 <= clean["hold_duration_seconds"] <= 86400:
        raise ValueError("Invalid hold duration")
    for key in (
        "duration_minutes", "distance_km", "pace_min_per_km", "speed_kmh",
        "power_watts", "met",
    ):
        raw = result.get(key)
        clean[key] = float(raw) if raw is not None else None
    if clean["duration_minutes"] is not None and not 0 < clean["duration_minutes"] <= 1440:
        raise ValueError("Invalid cardio duration")
    if clean["distance_km"] is not None and not 0 < clean["distance_km"] <= 1000:
        raise ValueError("Invalid cardio distance")
    if clean["pace_min_per_km"] is not None and not 0 < clean["pace_min_per_km"] <= 120:
        raise ValueError("Invalid cardio pace")
    if clean["speed_kmh"] is not None and not 0 < clean["speed_kmh"] <= 200:
        raise ValueError("Invalid cardio speed")
    if clean["power_watts"] is not None and not 0 < clean["power_watts"] <= 3000:
        raise ValueError("Invalid cardio power")
    if clean["met"] is not None and not 1 <= clean["met"] <= 25:
        raise ValueError("Invalid cardio MET")
    intensity = result.get("intensity")
    clean["intensity"] = str(intensity).strip().lower() if intensity else None
    equipment_context = str(result.get("equipment_context") or "other").strip().lower()
    clean["equipment_context"] = (
        equipment_context if equipment_context in {"stationary", "outdoor", "other"}
        else "other"
    )
    if (
        activity_type == "cardio"
        and any(word in name.lower() for word in ("bike", "cycle", "cycling"))
        and any(word in (clean["resistance_type"] or "").lower()
                for word in ("weighted pedal", "resistance pedal", "stationary"))
    ):
        clean["equipment_context"] = "stationary"
    resistance_level = result.get("resistance_level")
    clean["resistance_level"] = str(resistance_level).strip() if resistance_level else None
    explicit_met = re.search(r"\b(\d+(?:\.\d+)?)\s*MET\b", description, re.IGNORECASE)
    if explicit_met:
        clean["met"] = float(explicit_met.group(1))
        if not 1 <= clean["met"] <= 25:
            raise ValueError("Invalid cardio MET")
        clean["met_source"] = "user_reported"
    else:
        clean["met"] = None
        clean["met_source"] = None
    notes = result.get("notes")
    clean["notes"] = str(notes).strip() if notes else None
    question = result.get("question")
    clean["clarification_needed"] = result.get("clarification_needed") is True
    clean["question"] = str(question).strip() if question else None
    if clean["clarification_needed"] and not clean["question"]:
        clean["clarification_needed"] = False
    return clean


# ----- merge one clarification into known exercise details -----
async def merge_strength_exercise(original: dict, reply: str) -> dict:
    """Merge one clarification reply with known fields; never ask again."""
    response = await client.chat.completions.create(
        model="gpt-4.1-mini",
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": """
Merge a user's one-time clarification reply into the original workout
activity (strength or cardio). Return fields only: activity_type, name, sets,
repetitions, weight_kg, resistance_type, hold_duration_seconds,
duration_minutes, distance_km, pace_min_per_km, speed_kmh, power_watts,
equipment_context, resistance_level, intensity, met, met_source, notes. Never
ask a question. Preserve all known original values unless the reply explicitly
corrects them. For genuinely unknown values return null; do not infer missing
values. If the user says they do not know, leave those fields null. Convert lb
to kg and distance to km.
Return JSON only.
"""},
            {"role": "user", "content": json.dumps({
                "original_exercise": original,
                "clarification_reply": reply,
            })},
        ],
    )
    content = response.choices[0].message.content
    if not content:
        raise ValueError("OpenAI returned an empty exercise response")
    result = json.loads(content)
    merged = {key: original.get(key) for key in STRENGTH_FIELDS}
    for key in STRENGTH_FIELDS:
        value = result.get(key)
        if value is not None:
            merged[key] = value
    # The merge response is never allowed to create a clarification request.
    merged["clarification_needed"] = False
    merged["question"] = None
    return merged


SYSTEM_PROMPT = """
You interpret exercise descriptions for a calorie tracker.

Extract the activity name, duration in minutes, distance in km, speed in km/h,
pace in minutes/km, measured power in watts, intensity explicitly stated by the
user, and equipment context (stationary/outdoor/other) when available. Return
null for missing values. Never invent speed, power, cadence, distance, or
duration. Do not assign calories or MET values. Do not infer vigorous effort
from an activity name alone. Explicitly described light effort overrides
performance-based assumptions.

If duration is unknown and cannot be inferred
reasonably from distance and activity, request
clarification.

If distance and pace or speed are supplied, duration can be calculated.

Return JSON only:
{
    "activity": "Running",
    "duration_minutes": 30,
    "distance_km": 5,
    "speed_kmh": null,
    "pace_min_per_km": 6,
    "power_watts": null,
    "equipment_context": "outdoor",
    "intensity": null,
    "clarification_needed": false,
    "question": null
}
"""


# ----- estimate calories for a standalone activity -----
async def estimate_exercise(
    description: str,
    weight_kg: float,
    previous_context: dict | None = None,
) -> dict:

    response = await client.chat.completions.create(
        model="gpt-4.1-mini",
        temperature=0.2,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": json.dumps({
                    "description": description,
                    "previous_context": previous_context,
                }),
            },
        ],
    )

    result = json.loads(
        response.choices[0].message.content
    )

    activity = str(result.get("activity") or "Exercise")
    needs_clarification = (
        result.get("clarification_needed") is True
    )

    if needs_clarification:
        question = result.get("question")

        if not isinstance(question, str) or not question.strip():
            raise ValueError("Missing exercise clarification question")

        return {
            "activity": activity,
            "clarification_needed": True,
            "question": question,
        }

    duration_value = result.get("duration_minutes")
    distance_value = result.get("distance_km")
    pace_value = result.get("pace_min_per_km")
    speed_value = result.get("speed_kmh")
    if duration_value is None and distance_value is not None:
        if pace_value is not None:
            duration_value = float(distance_value) * float(pace_value)
        elif speed_value is not None:
            duration_value = float(distance_value) * 60 / float(speed_value)
    if duration_value is None:
        raise ValueError("Exercise duration is required")
    duration = float(duration_value)

    if not 0 < duration <= 1440:
        raise ValueError("Invalid exercise duration")

    activity_info = {
        "name": activity,
        "duration_minutes": duration,
        "distance_km": distance_value,
        "pace_min_per_km": pace_value,
        "speed_kmh": speed_value,
        "power_watts": result.get("power_watts"),
        "equipment_context": result.get("equipment_context"),
        "intensity": result.get("intensity"),
    }
    met = cardio_met(activity_info, weight_kg)

    additional_calories = round(
        (met - 1) * weight_kg * duration / 60
    )

    return {
        "activity": activity,
        "duration_minutes": duration,
        "distance_km": result.get("distance_km"),
        "speed_kmh": speed_value,
        "pace_min_per_km": pace_value,
        "met": met,
        "intensity": result.get("intensity"),
        "weight_kg": float(weight_kg),
        "additional_calories": max(0, additional_calories),
        "clarification_needed": False,
        "question": None,
    }
