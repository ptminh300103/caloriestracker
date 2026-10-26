"""Deterministic helpers for strength workout sessions."""

import math
import re

from response_parser import classify_response, normalize_response


DURATION_RE = re.compile(
    r"(?:(?P<hours>\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hours?))?\s*"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)\s*(?:m|min|mins|minutes?))?",
    re.IGNORECASE,
)


# ----- read a reported activity duration -----
def parse_duration_minutes(text):
    """Return duration in minutes, or None when no duration is present."""
    value = text.strip().lower()
    match = DURATION_RE.fullmatch(value)
    if not match or not any(match.groupdict().values()):
        return None
    hours = float(match.group("hours") or 0)
    minutes = float(match.group("minutes") or 0)
    total = hours * 60 + minutes
    if not 0 < total <= 1440:
        raise ValueError("Duration must be between 1 minute and 24 hours")
    return total


# ----- recognize an unknown-duration response -----
def is_duration_unknown(text):
    return classify_workout_response(text) == "unknown"


# ----- estimate session duration from recorded activities -----
def infer_duration_minutes(exercises):
    """Fallback duration: strength work/rest plus known cardio duration."""
    total_seconds = 0
    for exercise in exercises:
        if exercise.get("activity_type") == "cardio":
            duration = effective_cardio_duration(exercise)
            if duration:
                total_seconds += float(duration) * 60
            continue
        sets = exercise.get("sets") or 0
        reps = exercise.get("repetitions") or 0
        if sets > 0:
            total_seconds += sets * ((reps or 8) * 3 + 90)
    # Sparse logging still gets a modest, explicit fallback.
    return max(15, math.ceil(total_seconds / 60))


# ----- total cardio time when every activity reports it -----
def known_cardio_session_duration(exercises):
    """Return (minutes, source) when every cardio activity has a known duration.

    Only cardio-only sessions can be auto-totaled. A distance plus reported
    pace is a calculable duration; distance alone requires the final duration
    question (or the existing conservative unknown-duration fallback).
    """
    if not exercises or any(item.get("activity_type") != "cardio" for item in exercises):
        return None
    minutes = 0.0
    all_reported = True
    for item in exercises:
        duration = item.get("duration_minutes")
        if duration is not None:
            minutes += float(duration)
        elif item.get("distance_km") is not None and item.get("pace_min_per_km") is not None:
            minutes += float(item["distance_km"]) * float(item["pace_min_per_km"])
            all_reported = False
        else:
            return None
    if not 0 < minutes <= 1440:
        return None
    return minutes, "reported" if all_reported else "estimated"


# ----- calculate net calories from met, weight, and time -----
def _net_met_calories(weight_kg, duration_minutes, met):
    return (met - 1) * weight_kg * duration_minutes / 60


# ----- choose a conservative activity-specific cardio intensity -----
def _default_cardio_met(exercise, weight_kg=None):
    """Select a conservative, activity-specific MET from stated evidence."""
    activity = (exercise.get("name") or "").lower()
    equipment_terms = " ".join((exercise.get("resistance_type") or "",
                                 exercise.get("resistance_level") or "")).lower()
    stationary = (
        exercise.get("equipment_context") == "stationary"
        or (any(word in activity for word in ("bike", "cycle", "cycling"))
            and any(word in equipment_terms for word in (
                "pedal", "weighted", "resistance", "stationary",
            )))
    )
    intensity_text = (exercise.get("intensity") or "").lower()
    explicitly_low = any(word in intensity_text for word in (
        "light", "easy", "low", "gentle", "slow", "recovery",
    ))
    explicitly_high = any(word in intensity_text for word in (
        "vigorous", "hard", "intense", "sprint", "hiit", "high effort",
    ))

    if any(word in activity for word in ("run", "jog")):
        baseline = 6.0
        speed = exercise.get("speed_kmh")
        pace = exercise.get("pace_min_per_km")
        duration = exercise.get("duration_minutes")
        distance = exercise.get("distance_km")
        measured_speed = float(speed) if speed else (
            60 / float(pace) if pace else (
                float(distance) * 60 / float(duration)
                if distance and duration else None
            )
        )
        if measured_speed is not None:
            if measured_speed >= 11.0:
                baseline = 10.0
            elif measured_speed >= 8.5:
                baseline = 8.0
    elif any(word in activity for word in ("cycle", "bike", "cycling")):
        baseline = 4.0 if stationary else 5.5
        power = exercise.get("power_watts")
        weight = weight_kg
        if power and weight:
            baseline = min(15.0, max(2.0, (10.8 * float(power) / float(weight) + 7) / 3.5))
        elif not stationary:
            speed = exercise.get("speed_kmh")
            if speed is None and exercise.get("distance_km") and exercise.get("duration_minutes"):
                speed = float(exercise["distance_km"]) * 60 / float(exercise["duration_minutes"])
            if speed is not None:
                speed = float(speed)
                baseline = 8.0 if speed >= 20 else (6.8 if speed >= 16 else 4.5)
    elif "swim" in activity:
        baseline = 4.5
    elif any(word in activity for word in ("yoga", "pilates", "stretch")):
        baseline = 2.5
    elif "dance" in activity:
        baseline = 4.0
    elif any(word in activity for word in ("row", "elliptical")):
        baseline = 4.5
    elif "walk" in activity:
        baseline = 3.0
    elif "hike" in activity:
        baseline = 4.0
    else:
        baseline = 3.5

    resistance = " ".join((exercise.get("resistance_level") or "",
                            exercise.get("resistance_type") or "")).lower()
    numeric_level = re.search(r"\blevel\s*(\d+)\b", resistance)
    higher_machine_level = bool(numeric_level and int(numeric_level.group(1)) > 1)
    resistance_increased = higher_machine_level or any(
        term in resistance for term in (
            "high", "heavy", "increased", "weighted", "resistance",
        )
    )
    if resistance_increased and stationary and not explicitly_low and not explicitly_high:
        # Machine settings are not comparable measurements. Use a modest
        # moderate-cycling assumption without translating the setting.
        baseline = max(baseline, 5.0)
    if explicitly_low:
        baseline = min(baseline, 3.0)
    elif explicitly_high:
        if "run" in activity or "jog" in activity:
            high_default = 8.0
        elif any(word in activity for word in ("bike", "cycle", "swim")):
            high_default = 7.0
        elif "walk" in activity or "hike" in activity:
            high_default = 5.0
        else:
            high_default = 6.0
        baseline = max(baseline, high_default)
    return min(15.0, max(2.0, baseline))


# ----- choose the session intensity for recorded activities -----
def strength_session_met(activities):
    """Session-level resistance MET includes the logged rest periods."""
    labels = [(item.get("intensity") or "").lower() for item in activities
              if item.get("activity_type") == "strength"]
    if any(any(word in text for word in ("light", "easy", "low", "gentle")) for text in labels):
        return 2.8
    if any(any(word in text for word in ("vigorous", "hard", "intense", "hiit")) for text in labels):
        return 5.0
    return 3.5


# ----- estimate cardio intensity from available activity details -----
def cardio_met(exercise, weight_kg=None):
    if exercise.get("met_source") == "user_reported" and exercise.get("met") is not None:
        return min(25.0, max(1.0, float(exercise["met"])))
    return _default_cardio_met(exercise, weight_kg)


# ----- cap credited cardio time to avoid session overlap -----
def effective_cardio_duration(exercise):
    """Use reported time/pace, or conservative named speed for distance only."""
    duration = exercise.get("duration_minutes")
    if duration:
        return float(duration)
    distance = exercise.get("distance_km")
    if not distance:
        return None
    pace = exercise.get("pace_min_per_km")
    if pace:
        return float(distance) * float(pace)
    name = exercise.get("name", "").lower()
    if any(word in name for word in ("run", "jog")):
        assumed_pace = 8.0
    elif any(word in name for word in ("cycle", "bike")):
        assumed_pace = 3.75
    elif any(word in name for word in ("swim",)):
        assumed_pace = 24.0
    elif any(word in name for word in ("walk", "hike")):
        assumed_pace = 13.33
    else:
        assumed_pace = 10.0
    return float(distance) * assumed_pace


# ----- estimate additional calories for the whole session -----
def estimate_session_calories(weight_kg, duration_minutes, met=3.5, activities=None):
    """Estimate net session calories without overlapping activity minutes.

    Cardio durations are credited first and capped to total session duration.
    When strength is also present, it receives only the remaining minutes.
    """
    if not 25 <= weight_kg <= 300:
        raise ValueError("Weight must be between 25 and 300 kg")
    if not 0 < duration_minutes <= 1440:
        raise ValueError("Duration must be between 1 minute and 24 hours")
    if not 1 <= met <= 25:
        raise ValueError("MET must be between 1 and 25")
    activities = activities or []
    cardio = [item for item in activities if item.get("activity_type") == "cardio"]
    has_strength = any(item.get("activity_type") == "strength" for item in activities)
    cardio_durations = []
    for item in cardio:
        active_minutes = effective_cardio_duration(item)
        cardio_durations.append(float(active_minutes) if active_minutes else None)

    known_total = sum(value or 0 for value in cardio_durations)
    unspecified = [index for index, value in enumerate(cardio_durations) if value is None]
    if unspecified and not has_strength:
        remainder = max(0, duration_minutes - known_total)
        share = remainder / len(unspecified)
        for index in unspecified:
            cardio_durations[index] = share

    cardio_total = sum(value or 0 for value in cardio_durations)
    if cardio_total > duration_minutes and cardio_total > 0:
        # Keep each reported duration intact in the record, but credit only
        # the portion that fits within the user-reported whole-session time.
        remaining = float(duration_minutes)
        capped = []
        for value in cardio_durations:
            credited = min(float(value or 0), remaining)
            capped.append(credited)
            remaining -= credited
        cardio_durations = capped
        cardio_total = float(duration_minutes)

    calories = 0
    for item, active_minutes in zip(cardio, cardio_durations):
        if active_minutes:
            intensity = cardio_met(item, weight_kg)
            intensity = min(25, max(1, float(intensity)))
            calories += _net_met_calories(weight_kg, active_minutes, intensity)
    if has_strength:
        strength_minutes = max(0, duration_minutes - cardio_total)
        calories += _net_met_calories(weight_kg, strength_minutes, met)
    elif not cardio:
        calories = _net_met_calories(weight_kg, duration_minutes, met)
    return max(0, round(calories))


normalize_workout_response = normalize_response
classify_workout_response = classify_response


# ----- interpret a workout control response -----
def parse_session_control(text):
    """Backward-compatible mapping for the active exercise collection state."""
    return {
        "affirmative": "add",
        "negative": "finish",
        "cancel": "cancel",
        "skip": "cancel",
    }.get(classify_workout_response(text))


# ----- route workout text according to the active state -----
def workout_text_action(state, text):
    """Resolve active strength-session text without an AI call."""
    if state in {"awaiting_more", "collecting_exercises"}:
        response = classify_workout_response(text)
        if response == "negative":
            return "finish"
        if response == "affirmative":
            return "add"
        if response == "cancel":
            return "cancel"
        if response == "skip":
            return "cancel"
        return "exercise"
    if state == "awaiting_duration":
        return "duration"
    if state == "editing":
        return "edit"
    if state == "confirming":
        return "confirming"
    return None


# ----- enforce the one-question exercise limit -----
def should_ask_exercise_clarification(exercise, clarification_count):
    """Python-enforced one-question maximum for an exercise record."""
    return (
        clarification_count < 1
        and exercise.get("clarification_needed") is True
        and bool(exercise.get("question"))
    )


# ----- merge reported fields while preserving known values -----
def merge_known_exercise_fields(original, reply_fields):
    """Retain known values; only replace them with explicit reply values."""
    fields = (
        "name", "sets", "repetitions", "weight_kg", "resistance_type",
        "hold_duration_seconds", "duration_minutes", "distance_km",
        "pace_min_per_km", "intensity", "met", "activity_type", "notes",
    )
    merged = {key: original.get(key) for key in fields}
    for key in fields:
        value = reply_fields.get(key)
        if value is not None:
            merged[key] = value
    merged["clarification_needed"] = False
    merged["question"] = None
    return merged


# ----- recognize an unknown-information response -----
def is_unknown_answer(text):
    return classify_workout_response(text) == "unknown"
