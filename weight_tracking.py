"""Persistent weight history and future calorie-target calculation.

Weights and target calculations use Asia/Singapore calendar dates. Rolling
averages select the last measurement on each date in the seven-day window and
round to 0.01 kg using decimal ROUND_HALF_UP. Daily targets are rounded to a
whole kcal with ROUND_HALF_UP after applying the configured deficit.
"""

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

import database


TIMEZONE = ZoneInfo("Asia/Singapore")
STARTING_WEIGHT_KG = 82.0
STARTING_TARGET_KCAL = 2084
BMR_SEX = "male"
AGE_YEARS = 23
HEIGHT_CM = 175.5
ACTIVITY_MULTIPLIER = 1.375
DAILY_DEFICIT_KCAL = 400
ROLLING_WINDOW_DAYS = 7
AVERAGE_WEIGHT_QUANTUM = Decimal("0.01")
TARGET_QUANTUM = Decimal("1")
MIN_WEIGHT_KG = 25.0
MAX_WEIGHT_KG = 300.0

_WEIGHT_UPDATE_RE = re.compile(
    r"^\s*(?:i\s+weigh|my\s+weight\s+is|i\s+am|i'm)\s*"
    r"(\d+(?:[.,]\d+)?)\s*(?:kg|kgs|kilograms?)\s*(?:today)?\s*[.!]?\s*$",
    re.IGNORECASE,
)


# ----- reject invalid body-weight measurements -----
def validate_weight_kg(weight_kg):
    try:
        value = float(weight_kg)
    except (TypeError, ValueError):
        raise ValueError("Enter your weight in kilograms, for example /weight 80.5.")
    if not MIN_WEIGHT_KG <= value <= MAX_WEIGHT_KG:
        raise ValueError(
            f"Weight must be between {MIN_WEIGHT_KG:g} and {MAX_WEIGHT_KG:g} kg."
        )
    return value


# ----- recognize an explicit natural-language weigh-in -----
def extract_weight_update(text):
    """Parse only explicit body-weight statements, never arbitrary kg values."""
    match = _WEIGHT_UPDATE_RE.fullmatch(text or "")
    if not match:
        return None
    return float(match.group(1).replace(",", "."))


# ----- check whether automatic weigh-in recording is safe -----
def can_auto_record_weight(user_data):
    """Natural-language weight updates never interrupt a pending entry."""
    active_keys = (
        "pending_food", "food_state", "pending_workout", "workout_state",
        "pending_exercise", "exercise_state",
    )
    return not any(user_data.get(key) for key in active_keys)


# ----- calculate basal metabolic rate from body weight -----
def calculate_bmr(weight_kg):
    weight = Decimal(str(validate_weight_kg(weight_kg)))
    height = Decimal(str(HEIGHT_CM))
    age = Decimal(AGE_YEARS)
    sex_offset = Decimal(5) if BMR_SEX == "male" else Decimal(-161)
    # BMR is not rounded before TDEE.
    return Decimal(10) * weight + Decimal("6.25") * height - Decimal(5) * age + sex_offset


# ----- derive the daily target from the configured deficit -----
def calculate_daily_target(weight_kg):
    tdee = calculate_bmr(weight_kg) * Decimal(str(ACTIVITY_MULTIPLIER))
    target = tdee - Decimal(DAILY_DEFICIT_KCAL)
    return int(target.quantize(TARGET_QUANTUM, rounding=ROUND_HALF_UP))


# ----- average the latest measurements within the seven-day window -----
def rolling_average(user_id, as_of=None):
    """Average available daily readings from as_of and its prior six dates."""
    current = as_of or datetime.now(TIMEZONE).date()
    if isinstance(current, datetime):
        current = current.astimezone(TIMEZONE).date() if current.tzinfo else current.date()
    if isinstance(current, str):
        current = date.fromisoformat(current)
    start = current - timedelta(days=ROLLING_WINDOW_DAYS - 1)
    readings = database.get_weights_between(
        user_id, start.isoformat(), current.isoformat()
    )
    # Rows are sorted newest first, so first encountered measurement per date
    # wins. Same-time ties resolve by descending SQLite record ID.
    daily = {}
    for row in readings:
        daily.setdefault(row["measurement_date"], row)
    values = [Decimal(str(row["weight_kg"])) for row in daily.values()]
    if not values:
        return {"average_kg": None, "measured_days": 0, "readings": []}
    average = (sum(values) / Decimal(len(values))).quantize(
        AVERAGE_WEIGHT_QUANTUM, rounding=ROUND_HALF_UP
    )
    return {
        "average_kg": float(average),
        "measured_days": len(values),
        "readings": list(daily.values()),
    }


# ----- select the latest measured weight for activity estimates -----
def weight_for_exercise(user_id):
    latest = database.get_latest_weight(user_id)
    return float(latest["weight_kg"]) if latest else STARTING_WEIGHT_KG


# ----- save a weigh-in and schedule any target change -----
def record_weight(user_id, weight_kg, measured_at=None):
    """Save a measurement and schedule a changed target for tomorrow only."""
    value = validate_weight_kg(weight_kg)
    now = datetime.now(TIMEZONE)
    moment = measured_at or now
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=TIMEZONE)
    else:
        moment = moment.astimezone(TIMEZONE)
    if moment.date() != now.date():
        raise ValueError(
            "Weight measurements use today's Singapore date. Historical weight edits are not supported."
        )
    created_at = datetime.now(TIMEZONE).isoformat(timespec="seconds")
    record_id = database.save_weight(
        user_id, value, moment.date().isoformat(),
        moment.strftime("%H:%M:%S"), created_at,
    )

    current_date = moment.date()
    average = rolling_average(user_id, current_date)
    current_target = database.get_effective_calorie_target(
        user_id, current_date.isoformat(), STARTING_TARGET_KCAL
    )
    target_date = current_date + timedelta(days=1)
    target_before = database.get_effective_calorie_target(
        user_id, target_date.isoformat(), STARTING_TARGET_KCAL
    )
    active_goal = database.get_active_calorie_goal(user_id)
    goal_update = None
    if active_goal:
        from goal_tracking import refresh_goal

        goal_update = refresh_goal(user_id, current_date)
        changed = bool(goal_update and goal_update.get("target_changed"))
    else:
        new_target = (
            calculate_daily_target(average["average_kg"])
            if average["average_kg"] is not None else None
        )
        changed = False
        if new_target is not None and new_target != target_before:
            changed = database.set_calorie_target(
                user_id, target_date.isoformat(), new_target
            )
    target_after = database.get_effective_calorie_target(
        user_id, target_date.isoformat(), STARTING_TARGET_KCAL
    )
    return {
        "record_id": record_id,
        "weight_kg": value,
        "measured_at": moment,
        "average_kg": average["average_kg"],
        "measured_days": average["measured_days"],
        "current_target": current_target,
        "target_date": target_date,
        "target_changed": changed,
        "new_target": target_after if changed else None,
        "target_tomorrow": target_after,
        "goal_update": goal_update,
    }
