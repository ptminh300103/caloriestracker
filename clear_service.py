"""Atomic, user-scoped deletion for tracker data reset requests."""

import sqlite3

import database


# ----- delete activity history and dependent derived records -----
def _delete_logged_history(conn, user_id):
    conn.execute(
        """UPDATE food_memory SET source_food_id = NULL
        WHERE user_id = ? AND source_food_id IN (
            SELECT id FROM food_logs WHERE user_id = ?
        )""",
        (user_id, user_id),
    )
    conn.execute(
        """UPDATE food_memory_events SET source_food_id = NULL
        WHERE user_id = ? AND source_food_id IN (
            SELECT id FROM food_logs WHERE user_id = ?
        )""",
        (user_id, user_id),
    )
    conn.execute("DELETE FROM calorie_bank_withdrawals WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM calorie_bank_deposits WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM daily_calorie_results WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM calorie_debt_repayments WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM calorie_debts WHERE user_id = ?", (user_id,))
    conn.execute(
        "DELETE FROM historical_debt_import_events WHERE user_id = ?", (user_id,)
    )
    conn.execute(
        "DELETE FROM historical_debt_import_items WHERE user_id = ?", (user_id,)
    )
    conn.execute(
        "DELETE FROM historical_debt_import_batches WHERE user_id = ?", (user_id,)
    )
    conn.execute("DELETE FROM unified_daily_results WHERE user_id = ?", (user_id,))
    conn.execute(
        """DELETE FROM workout_exercises WHERE session_id IN (
            SELECT id FROM workout_sessions WHERE user_id = ?
        )""",
        (user_id,),
    )
    conn.execute("DELETE FROM workout_sessions WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM exercise_logs WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM food_logs WHERE user_id = ?", (user_id,))


# ----- reset one user's selected data scope atomically -----
def clear_user_data(user_id, scope):
    if scope not in {"history", "everything"}:
        raise ValueError("Reset scope must be 'history' or 'everything'.")
    user_id = int(user_id)
    connection = sqlite3.connect(database.DB_PATH, timeout=10)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        _delete_logged_history(connection, user_id)
        if scope == "everything":
            connection.execute(
                "DELETE FROM food_memory_events WHERE user_id = ?", (user_id,)
            )
            connection.execute("DELETE FROM food_memory WHERE user_id = ?", (user_id,))
            connection.execute("DELETE FROM weight_logs WHERE user_id = ?", (user_id,))
            connection.execute("DELETE FROM calorie_targets WHERE user_id = ?", (user_id,))
            connection.execute("DELETE FROM calorie_goals WHERE user_id = ?", (user_id,))
            connection.execute(
                "DELETE FROM calorie_account_settings WHERE user_id = ?", (user_id,)
            )
        connection.execute(
            "DELETE FROM pending_date_overrides WHERE user_id = ?", (user_id,)
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
