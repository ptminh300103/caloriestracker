"""Persistent, guarded goal-based daily calorie targets."""

import json
import math
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

import database
from weight_tracking import (
    ACTIVITY_MULTIPLIER,
    AGE_YEARS,
    BMR_SEX,
    DAILY_DEFICIT_KCAL,
    HEIGHT_CM,
    STARTING_TARGET_KCAL,
    STARTING_WEIGHT_KG,
    TARGET_QUANTUM,
    calculate_bmr,
    calculate_daily_target,
    rolling_average,
    validate_weight_kg,
)
from weight_tracking import TIMEZONE


KCAL_PER_KG = Decimal("7700")
MAX_GOAL_DAILY_DEFICIT_KCAL = Decimal("1000")
MIN_GOAL_BASE_TARGET_KCAL = 1500
MAX_GOAL_DAYS = 3650


# ----- select today's singapore calendar date -----
def _today():
    return datetime.now(TIMEZONE).date()


# ----- parse an allowed weight-loss deadline -----
def parse_goal_deadline(argument, today=None):
    current = today or _today()
    if isinstance(current, str):
        current = date.fromisoformat(current)
    if argument.isdecimal():
        days = int(argument)
        if days <= 0 or days > MAX_GOAL_DAYS:
            raise ValueError(
                f"Days must be from 1 to {MAX_GOAL_DAYS}."
            )
        return current + timedelta(days=days)
    if (
        not isinstance(argument, str)
        or len(argument) != 10
        or argument[4] != "-"
        or argument[7] != "-"
    ):
        raise ValueError("Use /goal <target kg> <days> or /goal <target kg> YYYY-MM-DD.")
    try:
        deadline = date.fromisoformat(argument)
    except (TypeError, ValueError):
        raise ValueError("Use /goal <target kg> <days> or /goal <target kg> YYYY-MM-DD.")
    if deadline <= current:
        raise ValueError("The goal deadline must be a future Singapore date.")
    if (deadline - current).days > MAX_GOAL_DAYS:
        raise ValueError(f"The deadline cannot be more than {MAX_GOAL_DAYS} days away.")
    return deadline


# ----- calculate a goal target and apply conservative safeguards -----
def calculate_goal_plan(current_weight, target_weight, deadline, today=None):
    current = today or _today()
    if isinstance(current, str):
        current = date.fromisoformat(current)
    target = Decimal(str(validate_weight_kg(target_weight)))
    weight = Decimal(str(validate_weight_kg(current_weight)))
    if target >= weight:
        raise ValueError(
            "Goal-based targets currently support weight loss only; choose a target below your current seven-day average."
        )
    days = (deadline - current).days
    if days <= 0:
        raise ValueError("The goal deadline must be a future Singapore date.")
    deficit = (weight - target) * KCAL_PER_KG / Decimal(days)
    tdee = calculate_bmr(float(weight)) * Decimal(str(ACTIVITY_MULTIPLIER))
    daily_target = int(
        (tdee - deficit).quantize(TARGET_QUANTUM, rounding=ROUND_HALF_UP)
    )
    maximum_safe_deficit = min(
        MAX_GOAL_DAILY_DEFICIT_KCAL,
        tdee - Decimal(MIN_GOAL_BASE_TARGET_KCAL),
    )
    if deficit > maximum_safe_deficit or daily_target < MIN_GOAL_BASE_TARGET_KCAL:
        suggested_days = max(
            1,
            math.ceil(
                float((weight - target) * KCAL_PER_KG / maximum_safe_deficit)
            ),
        ) if maximum_safe_deficit > 0 else None
        return {
            "feasible": False,
            "current_weight_kg": float(weight),
            "target_weight_kg": float(target),
            "weight_to_lose_kg": float(weight - target),
            "days_remaining": days,
            "deadline": deadline,
            "required_deficit_kcal": float(deficit),
            "daily_target_kcal": daily_target,
            "maximum_safe_deficit_kcal": float(maximum_safe_deficit),
            "suggested_days": suggested_days,
        }
    return {
        "feasible": True,
        "current_weight_kg": float(weight),
        "target_weight_kg": float(target),
        "weight_to_lose_kg": float(weight - target),
        "days_remaining": days,
        "deadline": deadline,
        "required_deficit_kcal": float(deficit),
        "daily_target_kcal": daily_target,
        "maximum_safe_deficit_kcal": float(maximum_safe_deficit),
        "suggested_days": None,
    }


# ----- save a validated goal and its calculation settings -----
def create_goal(user_id, target_weight, deadline, today=None):
    current = today or _today()
    if isinstance(current, str):
        current = date.fromisoformat(current)
    target = validate_weight_kg(target_weight)
    average = rolling_average(user_id, current)
    if average["average_kg"] is None:
        raise ValueError(
            "Record a weight during the current seven-day window before setting a goal."
        )
    plan = calculate_goal_plan(
        average["average_kg"], target, deadline, current
    )
    if not plan["feasible"]:
        raise ValueError(format_infeasible_plan(plan))
    settings = {
        "bmr_sex": BMR_SEX,
        "age_years": AGE_YEARS,
        "height_cm": HEIGHT_CM,
        "activity_multiplier": ACTIVITY_MULTIPLIER,
        "kcal_per_kg": str(KCAL_PER_KG),
        "maximum_daily_deficit_kcal": str(MAX_GOAL_DAILY_DEFICIT_KCAL),
        "minimum_base_target_kcal": MIN_GOAL_BASE_TARGET_KCAL,
        "rounding": "decimal round half up to whole kcal",
    }
    goal_id = database.create_calorie_goal(
        user_id,
        plan["current_weight_kg"],
        plan["target_weight_kg"],
        current.isoformat(),
        deadline.isoformat(),
        json.dumps(settings, sort_keys=True),
    )
    _schedule_target(user_id, current + timedelta(days=1), plan["daily_target_kcal"])
    database.update_calorie_goal_calculation(
        goal_id,
        "feasible",
        current.isoformat(),
        plan["required_deficit_kcal"],
        plan["daily_target_kcal"],
    )
    plan.update({"goal_id": goal_id, "target_date": current + timedelta(days=1)})
    return plan


# ----- write a changed target for a future calendar date -----
def _schedule_target(user_id, effective_date, target):
    date_text = effective_date.isoformat()
    previous = database.get_effective_calorie_target(
        user_id, date_text, STARTING_TARGET_KCAL
    )
    if int(target) != previous:
        database.set_calorie_target(user_id, date_text, int(target))
        return True
    return False


# ----- recalculate one active goal without changing today's target -----
def refresh_goal(user_id, today=None):
    current = today or _today()
    if isinstance(current, str):
        current = date.fromisoformat(current)
    goal = database.get_active_calorie_goal(user_id)
    if not goal:
        return None
    next_date = current + timedelta(days=1)
    average = rolling_average(user_id, current)
    deadline = date.fromisoformat(goal["deadline"])
    if average["average_kg"] is None:
        if deadline <= current:
            database.finish_calorie_goal(goal["id"], "deadline_passed")
            fallback_target = calculate_daily_target(STARTING_WEIGHT_KG)
            changed = _schedule_target(user_id, next_date, fallback_target)
            database.update_calorie_goal_calculation(
                goal["id"], "deadline_passed", current.isoformat(),
                float(DAILY_DEFICIT_KCAL), fallback_target,
            )
            goal.update({
                "status": "deadline_passed",
                "calculation_status": "deadline_passed",
            })
            return {
                "goal": goal, "plan": None, "target_date": next_date,
                "target_changed": changed, "target_kcal": fallback_target,
            }
        database.update_calorie_goal_calculation(
            goal["id"], "needs_weight", current.isoformat()
        )
        goal.update({"calculation_status": "needs_weight"})
        return {"goal": goal, "plan": None, "target_date": next_date}

    target_weight = Decimal(str(goal["target_weight_kg"]))
    current_weight = Decimal(str(average["average_kg"]))
    if current_weight <= target_weight:
        database.finish_calorie_goal(goal["id"], "reached")
        fallback_target = calculate_daily_target(float(current_weight))
        changed = _schedule_target(user_id, next_date, fallback_target)
        database.update_calorie_goal_calculation(
            goal["id"], "reached", current.isoformat(),
            float(DAILY_DEFICIT_KCAL), fallback_target,
        )
        goal.update({"status": "reached", "calculation_status": "reached"})
        return {
            "goal": goal, "plan": None, "target_date": next_date,
            "target_changed": changed, "target_kcal": fallback_target,
        }
    if deadline <= current:
        database.finish_calorie_goal(goal["id"], "deadline_passed")
        fallback_target = calculate_daily_target(float(current_weight))
        changed = _schedule_target(user_id, next_date, fallback_target)
        database.update_calorie_goal_calculation(
            goal["id"], "deadline_passed", current.isoformat(),
            float(DAILY_DEFICIT_KCAL), fallback_target,
        )
        goal.update({"status": "deadline_passed", "calculation_status": "deadline_passed"})
        return {
            "goal": goal, "plan": None, "target_date": next_date,
            "target_changed": changed, "target_kcal": fallback_target,
        }
    try:
        plan = calculate_goal_plan(current_weight, target_weight, deadline, current)
    except ValueError:
        plan = {
            "feasible": False,
            "reason": "the remaining goal would require weight gain",
            "suggested_days": None,
        }
    if not plan["feasible"]:
        fallback_target = calculate_daily_target(float(current_weight))
        changed = _schedule_target(user_id, next_date, fallback_target)
        database.update_calorie_goal_calculation(
            goal["id"], "infeasible", current.isoformat(),
            plan.get("required_deficit_kcal"), fallback_target,
        )
        goal.update({"calculation_status": "infeasible"})
        return {
            "goal": goal, "plan": plan, "target_date": next_date,
            "target_changed": changed, "target_kcal": fallback_target,
        }

    changed = _schedule_target(user_id, next_date, plan["daily_target_kcal"])
    database.update_calorie_goal_calculation(
        goal["id"], "feasible", current.isoformat(),
        plan["required_deficit_kcal"], plan["daily_target_kcal"],
    )
    goal.update({
        "calculation_status": "feasible",
        "current_deficit_kcal": plan["required_deficit_kcal"],
        "current_target_kcal": plan["daily_target_kcal"],
        "last_calculated_date": current.isoformat(),
    })
    return {
        "goal": goal, "plan": plan, "target_date": next_date,
        "target_changed": changed,
    }


# ----- refresh every user's active goal after restart or at midnight -----
def refresh_all_goals(today=None):
    current = today or _today()
    return [
        refresh_goal(user_id, current)
        for user_id in database.get_users_with_active_calorie_goals()
    ]


# ----- cancel a goal and restore the configured fixed deficit -----
def cancel_goal(user_id, today=None):
    current = today or _today()
    if isinstance(current, str):
        current = date.fromisoformat(current)
    goal = database.get_active_calorie_goal(user_id)
    if not goal:
        return None
    database.finish_calorie_goal(goal["id"], "cancelled")
    goal["status"] = "cancelled"
    average = rolling_average(user_id, current)
    weight = average["average_kg"] or STARTING_WEIGHT_KG
    target = calculate_daily_target(weight)
    effective_date = current + timedelta(days=1)
    changed = _schedule_target(user_id, effective_date, target)
    database.update_calorie_goal_calculation(
        goal["id"], "cancelled", current.isoformat(),
        float(DAILY_DEFICIT_KCAL), target,
    )
    return {
        "goal": goal,
        "target_kcal": target,
        "target_date": effective_date,
        "target_changed": changed,
        "used_starting_weight": average["average_kg"] is None,
    }


# ----- format a clear explanation for an unsafe requested deadline -----
def format_infeasible_plan(plan):
    if "required_deficit_kcal" not in plan:
        return (
            "This weight-loss goal is no longer feasible because reaching the "
            "target would require weight gain. Weight-gain goals are not supported."
        )
    message = (
        f"That deadline requires about {plan['required_deficit_kcal']:.1f} kcal/day "
        f"of deficit, beyond the configured safe limit. The minimum base target "
        f"is {MIN_GOAL_BASE_TARGET_KCAL} kcal/day."
    )
    if plan.get("suggested_days"):
        message += (
            f" Try at least {plan['suggested_days']} days from today; "
            "the requested deadline was not changed."
        )
    return message


# ----- describe goal progress using current stored measurements -----
def goal_overview(user_id, today=None):
    current = today or _today()
    if isinstance(current, str):
        current = date.fromisoformat(current)
    result = refresh_goal(user_id, current)
    goal = database.get_active_calorie_goal(user_id)
    if goal is None:
        goal = database.get_latest_calorie_goal(user_id)
    average = rolling_average(user_id, current)
    active = database.get_active_calorie_goal(user_id) is not None
    current_target = database.get_effective_calorie_target(
        user_id, current.isoformat(), STARTING_TARGET_KCAL
    )
    if not goal:
        return {
            "goal": None, "average_kg": average["average_kg"],
            "current_target": current_target, "active": False,
        }
    plan = result["plan"] if result else None
    days_remaining = max(0, (date.fromisoformat(goal["deadline"]) - current).days)
    start_weight = float(goal["start_weight_kg"])
    target_weight = float(goal["target_weight_kg"])
    progress = 100.0 if goal["status"] == "reached" else 0.0
    if average["average_kg"] is not None and start_weight > target_weight:
        progress = min(100.0, max(
            0.0,
            (start_weight - average["average_kg"])
            / (start_weight - target_weight) * 100,
        ))
    return {
        "goal": goal,
        "average_kg": average["average_kg"],
        "current_target": current_target,
        "active": active,
        "plan": plan,
        "days_remaining": days_remaining,
        "progress_percent": progress,
    }
