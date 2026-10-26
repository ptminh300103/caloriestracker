"""Auditable post-activation positive-credit and calorie-debt calculations."""

import sqlite3
from collections import defaultdict
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import database


TIMEZONE = ZoneInfo("Asia/Singapore")
RULE_VERSION = 1


# ----- normalize a date for unified ledger calculations -----
def _as_date(value):
    if isinstance(value, datetime):
        return value.astimezone(TIMEZONE).date() if value.tzinfo else value.date()
    return date.fromisoformat(value) if isinstance(value, str) else value


# ----- calculate the debt and positive-credit ledger for one user -----
def _calculate(
    user_id, activation_date, as_of, food, exercises, targets,
    opening_credits, opening_debts=(),
):
    start = _as_date(activation_date)
    end = _as_date(as_of)
    target_rows = sorted((_as_date(day), int(value)) for day, value in targets)
    food_by_day = {
        _as_date(day): (int(calories or 0), int(count or 0))
        for day, calories, count in food
    }
    exercise_by_day = defaultdict(list)
    for source in exercises:
        exercise_day = _as_date(source["date"])
        if start <= exercise_day <= end:
            exercise_by_day[exercise_day].append(source)

    credits = []
    balances = {}
    for credit in opening_credits:
        if credit["expiration_date"] < start.isoformat():
            continue
        key = ("legacy", int(credit["id"]))
        entry = {
            "key": key,
            "deposit_id": int(credit["id"]),
            "source_type": credit["source_type"],
            "source_id": int(credit["source_id"]),
            "earning_date": credit["earning_date"],
            "available_date": credit["available_date"],
            "expiration_date": credit["expiration_date"],
            "original_calories": int(credit["original_calories"]),
        }
        credits.append(entry)
        balances[key] = max(0, int(credit["opening_calories"]))

    debts = [
        {
            "calculation_key": debt["calculation_key"],
            "source_date": debt["source_date"],
            "original_calories": int(debt["original_calories"]),
            "remaining_calories": int(debt["original_calories"]),
            "recovery_deadline": debt["recovery_deadline"],
            "existing_id": int(debt["id"]),
            "origin": "imported",
            "import_batch_id": debt["import_batch_id"],
            "source_reference": debt["source_reference"],
            "imported_at": debt["imported_at"],
            "audit_snapshot": debt["audit_snapshot"],
        }
        for debt in opening_debts
    ]
    debt_by_day = {}
    repayments = []
    withdrawals = []
    day_results = []
    carry_balance = 0

    for offset in range(max(0, (end - start).days + 1)):
        current = start + timedelta(days=offset)
        day_text = current.isoformat()
        target = 2084
        for effective_date, target_value in target_rows:
            if effective_date <= current:
                target = target_value
            else:
                break

        exercise_bonus = 0
        exercise_total = 0
        day_repaid = 0
        for source in sorted(
            exercise_by_day.get(current, []),
            key=lambda item: (item["source_type"], item["source_id"]),
        ):
            amount = max(0, int(source["calories"]))
            exercise_total += amount
            outstanding = sum(debt["remaining_calories"] for debt in debts)
            repaid_for_source = 0
            if outstanding:
                # Round 75% half-up; the remainder is assigned so each integer
                # calorie is allocated exactly once.
                repayment_budget = (3 * amount + 2) // 4
                for debt in debts:
                    if not repayment_budget:
                        break
                    paid = min(debt["remaining_calories"], repayment_budget)
                    if paid <= 0:
                        continue
                    debt["remaining_calories"] -= paid
                    repayment_budget -= paid
                    repaid_for_source += paid
                    day_repaid += paid
                    repayments.append({
                        "debt_key": debt["calculation_key"],
                        "repayment_date": day_text,
                        "source_type": source["source_type"],
                        "source_id": int(source["source_id"]),
                        "calories": paid,
                    })
                outstanding_after = sum(
                    debt["remaining_calories"] for debt in debts
                )
                if outstanding_after:
                    exercise_bonus += amount - repaid_for_source
                    continue
                remainder = amount - repaid_for_source
                bonus = remainder // 2
                credit_amount = remainder - bonus
            else:
                bonus = amount // 2
                credit_amount = amount - bonus
            exercise_bonus += bonus
            if credit_amount:
                credit = {
                    "key": ("source", source["source_type"], int(source["source_id"])),
                    "deposit_id": None,
                    "source_type": source["source_type"],
                    "source_id": int(source["source_id"]),
                    "earning_date": day_text,
                    "available_date": (current + timedelta(days=1)).isoformat(),
                    "expiration_date": (current + timedelta(days=3)).isoformat(),
                    "original_calories": credit_amount,
                }
                credits.append(credit)
                balances[credit["key"]] = credit_amount

        eligible = [
            credit for credit in credits
            if credit["available_date"] <= day_text <= credit["expiration_date"]
            and balances.get(credit["key"], 0) > 0
        ]
        eligible.sort(key=lambda credit: (
            credit["expiration_date"], credit["earning_date"],
            credit["source_type"], credit["source_id"],
        ))
        bank_available = sum(balances[credit["key"]] for credit in eligible)
        consumed, food_count = food_by_day.get(current, (0, 0))
        allowance = target + exercise_bonus
        need = max(0, consumed - allowance)
        bank_used = 0
        for credit in eligible:
            if not need:
                break
            key = credit["key"]
            spent = min(balances[key], need)
            if spent:
                balances[key] -= spent
                need -= spent
                bank_used += spent
                withdrawals.append({
                    "credit_key": key,
                    "date": day_text,
                    "calories": spent,
                })

        debt_added = need
        if debt_added:
            key = f"daily:{day_text}"
            debt = {
                "calculation_key": key,
                "source_date": day_text,
                "original_calories": debt_added,
                "remaining_calories": debt_added,
                "recovery_deadline": (current + timedelta(days=7)).isoformat(),
                "existing_id": None,
                "origin": "generated",
                "import_batch_id": None,
                "source_reference": f"unified_daily_results:{day_text}",
                "imported_at": None,
                "audit_snapshot": None,
            }
            debts.append(debt)
            debt_by_day[current] = debt

        status = "incomplete" if food_count == 0 else (
            "over_target" if debt_added else "on_target"
        )
        balance_after = sum(
            balances[credit["key"]] for credit in eligible
        )
        carry_balance = balance_after
        day_results.append({
            "user_id": int(user_id),
            "date": day_text,
            "base_target": target,
            "food_consumed": consumed,
            "additional_exercise": exercise_total,
            "exercise_bonus": exercise_bonus,
            "allowance_before_bank": allowance,
            "bank_available": bank_available,
            "bank_used": bank_used,
            "bank_balance_after": balance_after,
            "debt_added": debt_added,
            "debt_repaid": day_repaid,
            "debt_outstanding": sum(
                debt["remaining_calories"] for debt in debts
            ),
            "remaining_allowance": allowance + bank_used - consumed,
            "status": status,
            "finalized": int(current < end),
        })

    for result in day_results:
        source_debt = debt_by_day.get(_as_date(result["date"]))
        if not source_debt or source_debt["remaining_calories"]:
            continue
        last_repayment = max(
            (
                entry["repayment_date"] for entry in repayments
                if entry["debt_key"] == source_debt["calculation_key"]
            ),
            default=None,
        )
        if last_repayment and last_repayment <= source_debt["recovery_deadline"]:
            result["status"] = "recovered_within_7_days"

    for credit in credits:
        remaining = balances.get(credit["key"], 0)
        if credit["expiration_date"] < end.isoformat():
            remaining = 0
        credit["remaining_calories"] = remaining
    return {
        "days": day_results,
        "credits": credits,
        "withdrawals": withdrawals,
        "debts": debts,
        "repayments": repayments,
        "bank_balance": carry_balance,
    }


# ----- rebuild post-activation ledgers while preserving earlier rows -----
def recalculate_user(user_id, as_of=None, connection=None):
    today = _as_date(as_of or datetime.now(TIMEZONE).date())
    activation = database.get_calorie_account_activation(user_id)
    if activation is None:
        activation = database.ensure_calorie_account(user_id)
    start = _as_date(activation)
    if today < start:
        return None

    owns_connection = connection is None
    connection_context = (
        sqlite3.connect(database.DB_PATH, timeout=20)
        if owns_connection else nullcontext(connection)
    )
    with connection_context as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if owns_connection:
            conn.execute("BEGIN IMMEDIATE")
        food = conn.execute(
            """SELECT date, COALESCE(SUM(calories), 0), COUNT(*) FROM food_logs
            WHERE user_id = ? AND date >= ? AND date <= ? GROUP BY date""",
            (int(user_id), start.isoformat(), today.isoformat()),
        ).fetchall()
        exercise_rows = []
        for source_type, table in (
            ("exercise", "exercise_logs"), ("workout", "workout_sessions")
        ):
            exercise_rows.extend(
                {
                    "source_type": source_type,
                    "source_id": int(row[0]),
                    "date": row[1],
                    "calories": max(0, int(row[2] or 0)),
                }
                for row in conn.execute(
                    f"SELECT id, date, additional_calories FROM {table} "
                    "WHERE user_id = ? AND date >= ? AND date <= ?",
                    (int(user_id), start.isoformat(), today.isoformat()),
                )
            )
        targets = conn.execute(
            "SELECT effective_date, base_target FROM calorie_targets WHERE user_id = ?",
            (int(user_id),),
        ).fetchall()
        legacy_credits = conn.execute(
            """SELECT id, source_type, source_id, earning_date, available_date,
                expiration_date, original_calories FROM calorie_bank_deposits
            WHERE user_id = ? AND earning_date < ? AND available_date <= ?
                AND expiration_date >= ?""",
            (int(user_id), start.isoformat(), start.isoformat(), start.isoformat()),
        ).fetchall()
        opening_credits = []
        for credit in legacy_credits:
            spent_before = conn.execute(
                """SELECT COALESCE(SUM(calories), 0) FROM calorie_bank_withdrawals
                WHERE user_id = ? AND deposit_id = ? AND date < ?""",
                (int(user_id), credit["id"], start.isoformat()),
            ).fetchone()[0]
            opening = int(credit["original_calories"]) - int(spent_before or 0)
            if opening > 0:
                opening_credits.append({**dict(credit), "opening_calories": opening})
        opening_debts = conn.execute(
            """SELECT id, calculation_key, source_date, original_calories,
                recovery_deadline, import_batch_id, source_reference, imported_at,
                audit_snapshot
            FROM calorie_debts WHERE user_id = ? AND origin = 'imported'
            ORDER BY source_date, id""",
            (int(user_id),),
        ).fetchall()

        old_finalized = {
            row["date"]: row["finalized_at"] for row in conn.execute(
                """SELECT date, finalized_at FROM unified_daily_results
                WHERE user_id = ? AND date >= ? AND finalized = 1""",
                (int(user_id), start.isoformat()),
            )
        }
        calculated = _calculate(
            user_id, start, today, food, exercise_rows, targets,
            opening_credits, opening_debts,
        )

        conn.execute(
            "DELETE FROM calorie_bank_withdrawals WHERE user_id = ? AND date >= ?",
            (int(user_id), start.isoformat()),
        )
        conn.execute(
            "DELETE FROM calorie_bank_deposits WHERE user_id = ? AND earning_date >= ?",
            (int(user_id), start.isoformat()),
        )
        conn.execute(
            "DELETE FROM calorie_debt_repayments WHERE user_id = ? AND repayment_date >= ?",
            (int(user_id), start.isoformat()),
        )
        conn.execute(
            "DELETE FROM calorie_debts WHERE user_id = ? AND source_date >= ? "
            "AND origin = 'generated'",
            (int(user_id), start.isoformat()),
        )
        conn.execute(
            "DELETE FROM unified_daily_results WHERE user_id = ? AND date >= ?",
            (int(user_id), start.isoformat()),
        )

        credit_ids = {}
        for credit in calculated["credits"]:
            key = credit["key"]
            deposit_id = credit["deposit_id"]
            if deposit_id is None:
                cursor = conn.execute(
                    """INSERT INTO calorie_bank_deposits (
                        user_id, source_type, source_id, earning_date,
                        available_date, expiration_date, original_calories,
                        remaining_calories
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        int(user_id), credit["source_type"], credit["source_id"],
                        credit["earning_date"], credit["available_date"],
                        credit["expiration_date"], credit["original_calories"],
                        credit["remaining_calories"],
                    ),
                )
                deposit_id = cursor.lastrowid
            else:
                conn.execute(
                    "UPDATE calorie_bank_deposits SET remaining_calories = ? "
                    "WHERE user_id = ? AND id = ?",
                    (credit["remaining_calories"], int(user_id), int(deposit_id)),
                )
            credit_ids[key] = deposit_id
        for withdrawal in calculated["withdrawals"]:
            conn.execute(
                """INSERT INTO calorie_bank_withdrawals
                    (user_id, date, deposit_id, calories) VALUES (?, ?, ?, ?)""",
                (
                    int(user_id), withdrawal["date"],
                    credit_ids[withdrawal["credit_key"]], withdrawal["calories"],
                ),
            )

        debt_ids = {}
        for debt in calculated["debts"]:
            if debt.get("existing_id"):
                debt_id = debt["existing_id"]
                conn.execute(
                    "UPDATE calorie_debts SET remaining_calories = ? WHERE id = ?",
                    (debt["remaining_calories"], debt_id),
                )
            else:
                cursor = conn.execute(
                    """INSERT INTO calorie_debts (
                        user_id, source_date, calculation_key, original_calories,
                        remaining_calories, recovery_deadline, rule_version,
                        origin, import_batch_id, source_reference, imported_at,
                        audit_snapshot
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        int(user_id), debt["source_date"], debt["calculation_key"],
                        debt["original_calories"], debt["remaining_calories"],
                        debt["recovery_deadline"], RULE_VERSION,
                        debt.get("origin", "generated"), debt.get("import_batch_id"),
                        debt.get("source_reference"), debt.get("imported_at"),
                        debt.get("audit_snapshot"),
                    ),
                )
                debt_id = cursor.lastrowid
            debt_ids[debt["calculation_key"]] = debt_id
        for repayment in calculated["repayments"]:
            conn.execute(
                """INSERT INTO calorie_debt_repayments (
                    user_id, debt_id, repayment_date, source_type, source_id,
                    calories, rule_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(user_id), debt_ids[repayment["debt_key"]],
                    repayment["repayment_date"], repayment["source_type"],
                    repayment["source_id"], repayment["calories"], RULE_VERSION,
                ),
            )
        for result in calculated["days"]:
            is_final = result["date"] < today.isoformat()
            finalized_at = old_finalized.get(result["date"]) if is_final else None
            if is_final and not finalized_at:
                finalized_at = datetime.now(TIMEZONE).isoformat(timespec="seconds")
            conn.execute(
                """INSERT INTO unified_daily_results (
                    user_id, date, base_target, food_consumed, additional_exercise,
                    exercise_bonus, allowance_before_bank, bank_available, bank_used,
                    bank_balance_after, debt_added, debt_repaid, debt_outstanding,
                    remaining_allowance, status, finalized, finalized_at, rule_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(user_id), result["date"], result["base_target"],
                    result["food_consumed"], result["additional_exercise"],
                    result["exercise_bonus"], result["allowance_before_bank"],
                    result["bank_available"], result["bank_used"],
                    result["bank_balance_after"], result["debt_added"],
                    result["debt_repaid"], result["debt_outstanding"],
                    result["remaining_allowance"], result["status"],
                    int(is_final), finalized_at, RULE_VERSION,
                ),
            )
        if owns_connection:
            conn.commit()
        row = conn.execute(
            "SELECT * FROM unified_daily_results WHERE user_id = ? AND date = ?",
            (int(user_id), today.isoformat()),
        ).fetchone()
        return dict(row) if row else None


# ----- retrieve one user's debt and credit summary -----
def account_summary(user_id, day=None):
    selected = _as_date(day or datetime.now(TIMEZONE).date())
    activation = database.get_calorie_account_activation(user_id)
    if activation is None or selected < _as_date(activation):
        return None
    result = recalculate_user(user_id, selected)
    debts = database.get_calorie_debts(user_id)
    deposits = []
    with sqlite3.connect(database.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        deposits = [dict(row) for row in conn.execute(
            """SELECT * FROM calorie_bank_deposits
            WHERE user_id = ? AND available_date <= ? AND expiration_date >= ?
                AND remaining_calories > 0
            ORDER BY expiration_date, earning_date, id""",
            (int(user_id), selected.isoformat(), selected.isoformat()),
        ).fetchall()]
    return {
        "result": result,
        "debts": debts,
        "deposits": deposits,
        "bank_balance": sum(item["remaining_calories"] for item in deposits),
        "outstanding_debt": sum(item["remaining_calories"] for item in debts),
    }
