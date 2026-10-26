"""Deterministic calorie allowance and short-lived exercise bank."""

from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import sqlite3

import database


TIMEZONE = ZoneInfo("Asia/Singapore")
DEFAULT_BASE_TARGET = 2084


# ----- normalize a calendar date for daily calculations -----
def _date(value):
    return date.fromisoformat(value)


# ----- load confirmed food, exercise, and target source records -----
def _sources(conn, user_id):
    food = {
        row[0]: (int(row[1] or 0), int(row[2] or 0))
        for row in conn.execute(
            """SELECT date, SUM(calories), COUNT(*) FROM food_logs
            WHERE user_id = ? GROUP BY date""", (user_id,)
        )
    }
    exercises = []
    for source_type, table in (("exercise", "exercise_logs"), ("workout", "workout_sessions")):
        exercises.extend(
            {"source_type": source_type, "source_id": int(row[0]),
             "date": row[1], "calories": max(0, int(row[2] or 0))}
            for row in conn.execute(
                f"SELECT id, date, additional_calories FROM {table} WHERE user_id = ?",
                (user_id,),
            )
        )
    targets = [
        (row[0], int(row[1])) for row in conn.execute(
            "SELECT effective_date, base_target FROM calorie_targets WHERE user_id = ? ORDER BY effective_date",
            (user_id,),
        )
    ]
    return food, exercises, targets


# ----- recompute finalized daily allowances and bank movements -----
def _calculate(food, exercises, targets, as_of):
    """Pure date-by-date calculation, useful for deterministic regression tests."""
    as_of = _date(as_of) if isinstance(as_of, str) else as_of
    exercise_by_date = defaultdict(list)
    all_dates = set(food)
    for item in exercises:
        exercise_by_date[item["date"]].append(item)
        all_dates.add(item["date"])
    all_dates.update(day for day, _ in targets)
    all_dates.add(as_of.isoformat())

    first_day = min(_date(value) for value in all_dates)
    last_day = max(as_of, max(_date(value) for value in all_dates))
    target_map = sorted((_date(day), value) for day, value in targets)
    deposits = []
    withdrawals = []
    day_results = []
    deposit_remaining = {}

    for offset in range((last_day - first_day).days + 1):
        current = first_day + timedelta(days=offset)
        day_text = current.isoformat()
        effective_target = DEFAULT_BASE_TARGET
        for effective_date, value in target_map:
            if effective_date <= current:
                effective_target = value
            else:
                break

        daily_exercises = sorted(
            exercise_by_date.get(day_text, []),
            key=lambda item: (item["source_type"], item["source_id"]),
        )
        exercise_total = sum(item["calories"] for item in daily_exercises)
        # Odd calories go to the bank per source session/entry. This makes
        # each split sum exactly to that record's additional expenditure.
        exercise_bonus = sum(item["calories"] // 2 for item in daily_exercises)
        for item in daily_exercises:
            amount = item["calories"] - item["calories"] // 2
            if amount <= 0:
                continue
            earned = _date(item["date"])
            deposits.append({
                **item,
                "earning_date": item["date"],
                "available_date": (earned + timedelta(days=1)).isoformat(),
                "expiration_date": (earned + timedelta(days=3)).isoformat(),
                "original_calories": amount,
            })

        eligible = [d for d in deposits
                    if d["available_date"] <= day_text <= d["expiration_date"]]
        eligible.sort(key=lambda d: (d["expiration_date"], d["earning_date"],
                                     d["source_type"], d["source_id"]))
        for deposit in eligible:
            key = (deposit["source_type"], deposit["source_id"])
            deposit_remaining.setdefault(key, deposit["original_calories"])

        bank_available = sum(
            deposit_remaining[(d["source_type"], d["source_id"])] for d in eligible
        )
        allowance = effective_target + exercise_bonus
        consumed, food_count = food.get(day_text, (0, 0))
        need = max(0, consumed - allowance)
        bank_used = 0
        for deposit in eligible:
            if not need:
                break
            key = (deposit["source_type"], deposit["source_id"])
            remaining = deposit_remaining[key]
            spent = min(remaining, need)
            if spent:
                deposit_remaining[key] -= spent
                need -= spent
                bank_used += spent
                withdrawals.append({
                    "date": day_text, "source_type": deposit["source_type"],
                    "source_id": deposit["source_id"], "calories": spent,
                })

        status = "incomplete" if food_count == 0 else (
            "on_target" if consumed <= allowance + bank_used else "over_target"
        )
        bank_balance_after = sum(
            deposit_remaining[(d["source_type"], d["source_id"])]
            for d in eligible
        )
        day_results.append({
            "date": day_text,
            "base_target": effective_target,
            "food_consumed": consumed,
            "additional_exercise": exercise_total,
            "exercise_bonus": exercise_bonus,
            "allowance_before_bank": allowance,
            "bank_available": bank_available,
            "bank_used": bank_used,
            "bank_balance_after": bank_balance_after,
            "remaining_allowance": allowance + bank_used - consumed,
            "status": status,
        })

    for deposit in deposits:
        key = (deposit["source_type"], deposit["source_id"])
        deposit["remaining_calories"] = deposit_remaining.get(
            key, deposit["original_calories"]
        )
        if deposit["expiration_date"] < as_of.isoformat():
            deposit["remaining_calories"] = 0
    return {"days": day_results, "deposits": deposits, "withdrawals": withdrawals}


# ----- rebuild one user’s daily results and calorie bank -----
def recalculate_user(user_id, as_of=None):
    """Rebuild one user's complete ledger atomically from confirmed source rows."""
    now = datetime.now(TIMEZONE)
    as_of_date = as_of or now.date()
    if isinstance(as_of_date, datetime):
        as_of_date = as_of_date.date()
    if isinstance(as_of_date, str):
        as_of_date = date.fromisoformat(as_of_date)
    activation = database.get_calorie_account_activation(user_id)
    if activation is None:
        activation = database.ensure_calorie_account(user_id)
    activation_date = date.fromisoformat(activation)
    if as_of_date >= activation_date:
        from unified_calorie_bank import recalculate_user as recalculate_unified_user

        return recalculate_unified_user(user_id, as_of_date)
    existing_legacy = database.get_legacy_daily_result(
        user_id, as_of_date.isoformat()
    )
    if existing_legacy:
        return existing_legacy
    with sqlite3.connect(database.DB_PATH) as conn:
        has_frozen_history = conn.execute(
            "SELECT 1 FROM daily_calorie_results WHERE user_id = ? AND date < ? LIMIT 1",
            (int(user_id), activation),
        ).fetchone()
    if has_frozen_history:
        return calculate_daily_summary(user_id, as_of_date)
    with sqlite3.connect(database.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        old_finalized = {
            row["date"]: row["finalized_at"] for row in conn.execute(
                "SELECT date, finalized_at FROM daily_calorie_results WHERE user_id = ? AND finalized = 1",
                (user_id,),
            )
        }
        food, exercises, targets = _sources(conn, user_id)
        calculated = _calculate(food, exercises, targets, as_of_date)

        # Withdrawals reference deposit IDs, so remove children before replacing
        # deterministic derived rows. Source food/exercise tables are untouched.
        conn.execute("DELETE FROM calorie_bank_withdrawals WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM calorie_bank_deposits WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM daily_calorie_results WHERE user_id = ?", (user_id,))
        deposit_ids = {}
        for deposit in calculated["deposits"]:
            cur = conn.execute(
                """INSERT INTO calorie_bank_deposits (
                    user_id, source_type, source_id, earning_date, available_date,
                    expiration_date, original_calories, remaining_calories
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, deposit["source_type"], deposit["source_id"],
                 deposit["earning_date"], deposit["available_date"],
                 deposit["expiration_date"], deposit["original_calories"],
                 deposit["remaining_calories"]),
            )
            deposit_ids[(deposit["source_type"], deposit["source_id"])] = cur.lastrowid
        for withdrawal in calculated["withdrawals"]:
            conn.execute(
                """INSERT INTO calorie_bank_withdrawals
                (user_id, date, deposit_id, calories) VALUES (?, ?, ?, ?)""",
                (user_id, withdrawal["date"],
                 deposit_ids[(withdrawal["source_type"], withdrawal["source_id"])],
                 withdrawal["calories"]),
            )
        for result in calculated["days"]:
            is_final = result["date"] < as_of_date.isoformat()
            finalized_at = old_finalized.get(result["date"]) if is_final else None
            if is_final and not finalized_at:
                finalized_at = now.isoformat()
            conn.execute(
                """INSERT INTO daily_calorie_results (
                    user_id, date, base_target, food_consumed, additional_exercise,
                    exercise_bonus, allowance_before_bank, bank_available, bank_used,
                    bank_balance_after, remaining_allowance, status, finalized, finalized_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, result["date"], result["base_target"], result["food_consumed"],
                 result["additional_exercise"], result["exercise_bonus"],
                 result["allowance_before_bank"], result["bank_available"],
                 result["bank_used"], result["bank_balance_after"],
                 result["remaining_allowance"], result["status"], int(is_final),
                 finalized_at),
            )
    return get_daily_result(user_id, as_of_date.isoformat())


# ----- retrieve one finalized daily calculation -----
def get_daily_result(user_id, day):
    activation = database.get_calorie_account_activation(user_id)
    if activation and str(day) >= activation:
        return database.get_unified_daily_result(user_id, day)
    row = database.get_legacy_daily_result(user_id, day)
    if row:
        return _apply_imported_historical_status(user_id, day, row)
    if activation and str(day) < activation:
        return calculate_daily_summary(user_id, day)
    return None


# ----- overlay imported debt status while preserving the legacy result row -----
def _apply_imported_historical_status(user_id, day, result):
    imported = database.get_imported_historical_status(user_id, day)
    if not imported:
        return result
    result["status"] = imported["status"]
    result["historical_debt_added"] = imported["current_excess"]
    result["historical_debt_outstanding"] = imported["remaining_calories"] or 0
    result["historical_recovery_deadline"] = imported["recovery_deadline"]
    return result


# ----- calculate one daily summary without changing stored records -----
def calculate_daily_summary(user_id, day):
    selected_day = _date(day) if isinstance(day, str) else day
    today = datetime.now(TIMEZONE).date()
    activation = database.get_calorie_account_activation(user_id)
    if activation and selected_day >= _date(activation):
        from unified_calorie_bank import recalculate_user as recalculate_unified_user

        recalculate_unified_user(user_id, today)
        result = database.get_unified_daily_result(
            user_id, selected_day.isoformat()
        )
        if result:
            result["finalized"] = int(selected_day < today)
            return result
    if activation and selected_day < _date(activation):
        legacy = database.get_legacy_daily_result(user_id, selected_day.isoformat())
        if legacy:
            return _apply_imported_historical_status(
                user_id, selected_day.isoformat(), legacy
            )
    with sqlite3.connect(database.DB_PATH) as conn:
        food, exercises, targets = _sources(conn, user_id)
    calculated = _calculate(food, exercises, targets, selected_day)
    result = next(
        (
            item for item in calculated["days"]
            if item["date"] == selected_day.isoformat()
        ),
        None,
    )
    if result is None:
        result = {
            "date": selected_day.isoformat(), "base_target": DEFAULT_BASE_TARGET,
            "food_consumed": 0, "additional_exercise": 0, "exercise_bonus": 0,
            "allowance_before_bank": DEFAULT_BASE_TARGET, "bank_available": 0,
            "bank_used": 0, "bank_balance_after": 0,
            "remaining_allowance": DEFAULT_BASE_TARGET, "status": "incomplete",
        }
        for effective_date, target in sorted(
            (_date(effective_date), value) for effective_date, value in targets
        ):
            if effective_date <= selected_day:
                result["base_target"] = target
                result["allowance_before_bank"] = target
                result["remaining_allowance"] = target
    result["finalized"] = int(selected_day < today)
    result.setdefault("finalized_at", None)
    result = _apply_imported_historical_status(
        user_id, selected_day.isoformat(), result
    )
    return result


# ----- retrieve eligible calorie-bank deposits -----
def get_bank_deposits(user_id, day=None):
    day = day or datetime.now(TIMEZONE).date().isoformat()
    with sqlite3.connect(database.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(
            """SELECT * FROM calorie_bank_deposits
            WHERE user_id = ? AND available_date <= ? AND expiration_date >= ?
                AND remaining_calories > 0
            ORDER BY expiration_date, earning_date, id""", (user_id, day, day)
        ).fetchall()]


# ----- rebuild daily results for every user -----
def recalculate_all(as_of=None):
    """Catch up every user known from source or derived database records."""
    with sqlite3.connect(database.DB_PATH) as conn:
        users = {row[0] for row in conn.execute("""
            SELECT user_id FROM food_logs UNION SELECT user_id FROM exercise_logs
            UNION SELECT user_id FROM workout_sessions
            UNION SELECT user_id FROM daily_calorie_results
            UNION SELECT user_id FROM calorie_targets
            UNION SELECT user_id FROM calorie_account_settings
        """)}
    return {user_id: recalculate_user(user_id, as_of) for user_id in users}

