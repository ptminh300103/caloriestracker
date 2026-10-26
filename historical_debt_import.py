"""Preview and explicitly import eligible pre-activation calorie debt."""

import argparse
import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import database
from calorie_bank import _calculate as calculate_legacy, _sources
from unified_calorie_bank import _calculate as calculate_unified, recalculate_user


TIMEZONE = ZoneInfo("Asia/Singapore")
IMPORT_RULE_VERSION = 1


# ----- normalize a supplied Singapore calendar date -----
def _as_date(value):
    if isinstance(value, datetime):
        return value.astimezone(TIMEZONE).date() if value.tzinfo else value.date()
    return date.fromisoformat(value) if isinstance(value, str) else value


# ----- test whether the import audit schema is available -----
def _has_import_schema(conn):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'historical_debt_import_batches'"
    ).fetchone() is not None


# ----- build post-activation calculation inputs without writing rows -----
def _unified_inputs(conn, user_id, activation, as_of):
    start_text, end_text = activation.isoformat(), as_of.isoformat()
    food = conn.execute(
        """SELECT date, COALESCE(SUM(calories), 0), COUNT(*) FROM food_logs
        WHERE user_id = ? AND date >= ? AND date <= ? GROUP BY date""",
        (int(user_id), start_text, end_text),
    ).fetchall()
    exercises = []
    for kind, table in (("exercise", "exercise_logs"), ("workout", "workout_sessions")):
        exercises.extend(
            {
                "source_type": kind,
                "source_id": int(row[0]),
                "date": row[1],
                "calories": max(0, int(row[2] or 0)),
            }
            for row in conn.execute(
                f"SELECT id, date, additional_calories FROM {table} "
                "WHERE user_id = ? AND date >= ? AND date <= ?",
                (int(user_id), start_text, end_text),
            )
        )
    targets = conn.execute(
        "SELECT effective_date, base_target FROM calorie_targets WHERE user_id = ?",
        (int(user_id),),
    ).fetchall()
    credits = []
    for row in conn.execute(
        """SELECT id, source_type, source_id, earning_date, available_date,
            expiration_date, original_calories FROM calorie_bank_deposits
        WHERE user_id = ? AND earning_date < ? AND available_date <= ?
            AND expiration_date >= ?""",
        (int(user_id), start_text, start_text, start_text),
    ):
        spent = conn.execute(
            """SELECT COALESCE(SUM(calories), 0) FROM calorie_bank_withdrawals
            WHERE user_id = ? AND deposit_id = ? AND date < ?""",
            (int(user_id), row["id"], start_text),
        ).fetchone()[0]
        opening = int(row["original_calories"]) - int(spent or 0)
        if opening > 0:
            credits.append({**dict(row), "opening_calories": opening})
    return food, exercises, targets, credits


# ----- prepare a read-only historical debt import preview -----
def _preview_in_connection(conn, user_id, as_of=None):
    today = _as_date(as_of or datetime.now(TIMEZONE).date())
    account = conn.execute(
        "SELECT activation_date FROM calorie_account_settings WHERE user_id = ?",
        (int(user_id),),
    ).fetchone()
    if not account:
        return {"ready": False, "user_id": int(user_id), "blockers": [
            "No stored debt-system activation date exists for this user."
        ]}
    activation = date.fromisoformat(account[0])
    window_start = activation - timedelta(days=7)
    window_end = activation - timedelta(days=1)
    blockers = []
    if today < activation:
        blockers.append("The debt-system activation date is still in the future.")
    has_import_schema = _has_import_schema(conn)
    if has_import_schema:
        prior = conn.execute(
            "SELECT batch_id FROM historical_debt_import_batches WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
        if prior:
            blockers.append(f"A historical debt import already exists ({prior[0]}).")

    source_food, source_exercises, source_targets = _sources(conn, int(user_id))
    legacy_calculation = calculate_legacy(
        source_food, source_exercises, source_targets, window_end
    )
    calculated_by_date = {row["date"]: row for row in legacy_calculation["days"]}
    stored_rows = {
        row["date"]: dict(row)
        for row in conn.execute(
            """SELECT * FROM daily_calorie_results
            WHERE user_id = ? AND date BETWEEN ? AND ?""",
            (int(user_id), window_start.isoformat(), window_end.isoformat()),
        )
    }
    source_dates = {
        day for day, (_calories, count) in source_food.items() if count > 0
    }
    items = []
    for offset in range(7):
        day = window_start + timedelta(days=offset)
        day_text = day.isoformat()
        stored = stored_rows.get(day_text)
        current = calculated_by_date.get(day_text)
        if day_text not in source_dates:
            items.append({"date": day_text, "state": "excluded", "reason": "no confirmed food records"})
            continue
        if stored is None:
            blockers.append(f"{day_text}: food exists but no finalized daily result snapshot exists.")
            items.append({"date": day_text, "state": "blocked", "reason": "missing daily result snapshot"})
            continue
        if not stored["finalized"]:
            items.append({"date": day_text, "state": "excluded", "reason": "daily result was not finalized"})
            continue
        if current is None:
            blockers.append(f"{day_text}: source-based legacy calculation is unavailable.")
            items.append({"date": day_text, "state": "blocked", "reason": "missing source calculation"})
            continue
        comparison_fields = (
            ("base_target", "base_target"),
            ("food_consumed", "food_consumed"),
            ("additional_exercise", "additional_exercise"),
            ("exercise_bonus", "exercise_bonus"),
            ("allowance_before_bank", "allowance_before_bank"),
            ("bank_used", "bank_used"),
            ("status", "status"),
        )
        mismatches = [
            stored_field for stored_field, calculated_field in comparison_fields
            if stored.get(stored_field) != current.get(calculated_field)
        ]
        if mismatches:
            blockers.append(
                f"{day_text}: stored snapshot differs from current source calculation "
                f"({', '.join(mismatches)})."
            )
            items.append({"date": day_text, "state": "blocked", "reason": "snapshot/source mismatch"})
            continue
        if stored["food_consumed"] <= 0:
            items.append({"date": day_text, "state": "excluded", "reason": "no positive recorded food intake"})
            continue
        if stored["status"] != "over_target":
            items.append({"date": day_text, "state": "excluded", "reason": f"status is {stored['status']}"})
            continue
        excess = max(
            0,
            int(stored["food_consumed"])
            - int(stored["allowance_before_bank"])
            - int(stored["bank_used"]),
        )
        if excess <= 0:
            blockers.append(f"{day_text}: status is over target but uncovered excess is not positive.")
            items.append({"date": day_text, "state": "blocked", "reason": "inconsistent uncovered excess"})
            continue
        items.append({
            "date": day_text,
            "state": "proposed",
            "food_consumed": int(stored["food_consumed"]),
            "base_target": int(stored["base_target"]),
            "exercise_bonus": int(stored["exercise_bonus"]),
            "allowance_before_bank": int(stored["allowance_before_bank"]),
            "bank_used": int(stored["bank_used"]),
            "uncovered_excess": excess,
            "recovery_deadline": (day + timedelta(days=7)).isoformat(),
            "source_reference": f"daily_calorie_results:{day_text}",
            "source_snapshot": {
                field: stored[field] for field, _ in comparison_fields
            } | {
                "finalized": stored["finalized"],
                "finalized_at": stored["finalized_at"],
                "bank_available": stored["bank_available"],
                "bank_balance_after": stored["bank_balance_after"],
            },
        })

    candidates = [item for item in items if item["state"] == "proposed"]
    activation = date.fromisoformat(account[0])
    food, exercises, targets, opening_credits = _unified_inputs(
        conn, user_id, activation, today
    )
    before = calculate_unified(
        user_id, activation, today, food, exercises, targets, opening_credits
    )
    opening_debts = [
        {
            "id": -index,
            "calculation_key": f"preview:{item['date']}",
            "source_date": item["date"],
            "original_calories": item["uncovered_excess"],
            "recovery_deadline": item["recovery_deadline"],
            "import_batch_id": "preview",
            "source_reference": item["source_reference"],
            "imported_at": "preview",
            "audit_snapshot": json.dumps(item["source_snapshot"], sort_keys=True),
        }
        for index, item in enumerate(candidates, start=1)
    ]
    after = calculate_unified(
        user_id, activation, today, food, exercises, targets,
        opening_credits, opening_debts,
    )
    before_days = {row["date"]: row for row in before["days"]}
    after_days = {row["date"]: row for row in after["days"]}
    affected_allocations = []
    for day_text in sorted(after_days):
        old, new = before_days[day_text], after_days[day_text]
        if new["additional_exercise"] and any(
            old[field] != new[field]
            for field in ("exercise_bonus", "debt_repaid", "debt_outstanding")
        ):
            affected_allocations.append({
                "date": day_text,
                "exercise_calories": new["additional_exercise"],
                "allowance_before_import": old["exercise_bonus"],
                "allowance_after_import": new["exercise_bonus"],
                "debt_repaid_after_import": new["debt_repaid"],
            })

    if has_import_schema:
        existing_balance = conn.execute(
            """SELECT COALESCE(SUM(remaining_calories), 0)
            FROM calorie_bank_deposits
            WHERE user_id = ? AND available_date <= ? AND expiration_date >= ?
                AND remaining_calories > 0""",
            (int(user_id), today.isoformat(), today.isoformat()),
        ).fetchone()[0]
        existing_debt = conn.execute(
            """SELECT COALESCE(SUM(remaining_calories), 0) FROM calorie_debts
            WHERE user_id = ? AND remaining_calories > 0""",
            (int(user_id),),
        ).fetchone()[0]
    else:
        existing_balance = conn.execute(
            """SELECT COALESCE(SUM(remaining_calories), 0)
            FROM calorie_bank_deposits
            WHERE user_id = ? AND available_date <= ? AND expiration_date >= ?
                AND remaining_calories > 0""",
            (int(user_id), today.isoformat(), today.isoformat()),
        ).fetchone()[0]
        existing_debt = conn.execute(
            """SELECT COALESCE(SUM(remaining_calories), 0) FROM calorie_debts
            WHERE user_id = ? AND remaining_calories > 0""",
            (int(user_id),),
        ).fetchone()[0]
    recovered_potential = [
        {"date": item["date"], "deadline": item["recovery_deadline"]}
        for item in candidates if item["recovery_deadline"] >= today.isoformat()
    ]
    payload = {
        "user_id": int(user_id),
        "activation_date": activation.isoformat(),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "as_of": today.isoformat(),
        "items": items,
        "total_proposed_debt": sum(item["uncovered_excess"] for item in candidates),
        "existing_positive_bank_balance": int(existing_balance or 0),
        "expected_positive_bank_balance": int(after["bank_balance"]),
        "existing_outstanding_debt": int(existing_debt or 0),
        "expected_outstanding_debt": sum(
            int(debt["remaining_calories"]) for debt in after["debts"]
        ),
        "affected_exercise_allocations": affected_allocations,
        "historical_days_that_could_become_recoverable": recovered_potential,
        "blockers": blockers,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    preview_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    payload["preview_hash"] = preview_hash
    payload["batch_id"] = hashlib.sha256(
        f"{user_id}:{activation.isoformat()}:{preview_hash}".encode("utf-8")
    ).hexdigest()[:32]
    payload["ready"] = not blockers and bool(candidates)
    if not candidates and not blockers:
        payload["message"] = "No eligible finalized over-target days were found."
    return payload


# ----- preview a user's historical debt import without writing to sqlite -----
def preview_import(user_id, as_of=None):
    database_uri = Path(database.DB_PATH).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return _preview_in_connection(conn, int(user_id), as_of)


# ----- recompute imported historical excess after a source-record edit -----
def refresh_imported_debts(user_id):
    with sqlite3.connect(database.DB_PATH, timeout=20) as conn:
        conn.row_factory = sqlite3.Row
        if not _has_import_schema(conn):
            return False
        batch = conn.execute(
            "SELECT * FROM historical_debt_import_batches WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
        if not batch:
            return False
        activation = date.fromisoformat(batch["activation_date"])
        window_end = date.fromisoformat(batch["window_end"])
        food, exercises, targets = _sources(conn, int(user_id))
        calculated = calculate_legacy(food, exercises, targets, window_end)
        by_date = {row["date"]: row for row in calculated["days"]}
        items = conn.execute(
            "SELECT * FROM historical_debt_import_items WHERE batch_id = ?",
            (batch["batch_id"],),
        ).fetchall()
        now = datetime.now(TIMEZONE).isoformat(timespec="seconds")
        for item in items:
            day = item["source_date"]
            current = by_date.get(day)
            if current is None or current["food_consumed"] <= 0:
                amount = 0
                status = "incomplete"
            else:
                amount = max(
                    0,
                    int(current["food_consumed"])
                    - int(current["allowance_before_bank"])
                    - int(current["bank_used"]),
                )
                status = "over_target" if amount else "on_target"
            previous_amount = int(item["current_excess"])
            previous_status = item["current_status"]
            if amount != previous_amount or status != previous_status:
                conn.execute(
                    """INSERT INTO historical_debt_import_events (
                        batch_id, user_id, source_date, event_type,
                        previous_excess, current_excess, details, created_at
                    ) VALUES (?, ?, ?, 'source_edit_recalculation', ?, ?, ?, ?)""",
                    (
                        batch["batch_id"], int(user_id), day, previous_amount,
                        amount, json.dumps({
                            "previous_status": previous_status,
                            "status": status,
                            "allowance_before_bank": current["allowance_before_bank"] if current else None,
                            "bank_used": current["bank_used"] if current else None,
                        }, sort_keys=True), now,
                    ),
                )
            conn.execute(
                """UPDATE historical_debt_import_items SET current_excess = ?,
                    current_status = ?, updated_at = ? WHERE id = ?""",
                (amount, status, now, item["id"]),
            )
            debt = conn.execute(
                "SELECT id FROM calorie_debts WHERE import_batch_id = ? AND source_date = ?",
                (batch["batch_id"], day),
            ).fetchone()
            if amount <= 0:
                if debt:
                    conn.execute("DELETE FROM calorie_debts WHERE id = ?", (debt["id"],))
            elif debt:
                conn.execute(
                    """UPDATE calorie_debts SET original_calories = ?,
                        remaining_calories = ? WHERE id = ?""",
                    (amount, amount, debt["id"]),
                )
            else:
                snapshot = item["snapshot_json"]
                conn.execute(
                    """INSERT INTO calorie_debts (
                        user_id, source_date, calculation_key, original_calories,
                        remaining_calories, recovery_deadline, rule_version,
                        origin, import_batch_id, source_reference, imported_at,
                        audit_snapshot
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'imported', ?, ?, ?, ?)""",
                    (
                        int(user_id), day,
                        f"import:{batch['batch_id']}:{day}", amount, amount,
                        item["recovery_deadline"], IMPORT_RULE_VERSION,
                        batch["batch_id"], item["source_reference"],
                        batch["imported_at"], snapshot,
                    ),
                )
        conn.commit()
    return True


# ----- create a verified sqlite backup before the confirmed import -----
def _create_verified_backup(connection):
    database_uri = Path(database.DB_PATH).resolve().as_uri() + "?mode=ro"
    source = sqlite3.connect(database_uri, uri=True)
    checked = source.execute("PRAGMA integrity_check").fetchone()[0]
    if checked != "ok":
        source.close()
        raise RuntimeError(f"Source database integrity check failed: {checked}")
    timestamp = datetime.now(TIMEZONE).strftime("%Y%m%dT%H%M%S%f%z")
    path = Path(database.DB_PATH).with_name(
        f"{Path(database.DB_PATH).stem}.historical-debt-import-{timestamp}.sqlite3"
    )
    destination = sqlite3.connect(path)
    try:
        source.backup(destination)
        integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"Backup integrity check failed: {integrity}")
    finally:
        destination.close()
        source.close()
    return str(path)


# ----- atomically apply an explicitly confirmed historical debt import -----
def apply_import(user_id, confirmed_batch_id, as_of=None):
    conn = sqlite3.connect(database.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        if not _has_import_schema(conn):
            raise RuntimeError("Run the application schema migration before importing.")
        conn.execute("BEGIN IMMEDIATE")
        preview = _preview_in_connection(conn, int(user_id), as_of)
        if not preview["ready"]:
            raise RuntimeError("Import is blocked: " + "; ".join(preview["blockers"]))
        if preview["batch_id"] != str(confirmed_batch_id):
            raise RuntimeError("The preview is stale or the confirmation batch ID is incorrect.")
        if conn.execute(
            "SELECT 1 FROM historical_debt_import_batches WHERE user_id = ?",
            (int(user_id),),
        ).fetchone():
            raise RuntimeError("A historical debt import has already been applied for this user.")

        backup_path = _create_verified_backup(conn)
        now = datetime.now(TIMEZONE).isoformat(timespec="seconds")
        conn.execute(
            """INSERT INTO historical_debt_import_batches (
                batch_id, user_id, activation_date, window_start, window_end,
                preview_hash, status, created_at, imported_at, rule_version
            ) VALUES (?, ?, ?, ?, ?, ?, 'imported', ?, ?, ?)""",
            (
                preview["batch_id"], int(user_id), preview["activation_date"],
                preview["window_start"], preview["window_end"],
                preview["preview_hash"], now, now, IMPORT_RULE_VERSION,
            ),
        )
        for item in preview["items"]:
            if item["state"] != "proposed":
                continue
            snapshot = json.dumps(item["source_snapshot"], sort_keys=True)
            conn.execute(
                """INSERT INTO historical_debt_import_items (
                    batch_id, user_id, source_date, source_reference,
                    original_status, base_target, food_consumed, exercise_bonus,
                    allowance_before_bank, bank_used, original_excess,
                    current_excess, recovery_deadline, current_status,
                    snapshot_json, updated_at
                ) VALUES (?, ?, ?, ?, 'over_target', ?, ?, ?, ?, ?, ?, ?, ?,
                    'over_target', ?, ?)""",
                (
                    preview["batch_id"], int(user_id), item["date"],
                    item["source_reference"], item["base_target"],
                    item["food_consumed"], item["exercise_bonus"],
                    item["allowance_before_bank"], item["bank_used"],
                    item["uncovered_excess"], item["uncovered_excess"],
                    item["recovery_deadline"], snapshot, now,
                ),
            )
            conn.execute(
                """INSERT INTO calorie_debts (
                    user_id, source_date, calculation_key, original_calories,
                    remaining_calories, recovery_deadline, rule_version, origin,
                    import_batch_id, source_reference, imported_at, audit_snapshot
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'imported', ?, ?, ?, ?)""",
                (
                    int(user_id), item["date"],
                    f"import:{preview['batch_id']}:{item['date']}",
                    item["uncovered_excess"], item["uncovered_excess"],
                    item["recovery_deadline"], IMPORT_RULE_VERSION,
                    preview["batch_id"], item["source_reference"], now, snapshot,
                ),
            )
            conn.execute(
                """INSERT INTO historical_debt_import_events (
                    batch_id, user_id, source_date, event_type, previous_excess,
                    current_excess, details, created_at
                ) VALUES (?, ?, ?, 'imported', 0, ?, ?, ?)""",
                (
                    preview["batch_id"], int(user_id), item["date"],
                    item["uncovered_excess"], snapshot, now,
                ),
            )
        recalculate_user(int(user_id), as_of, connection=conn)
        conn.execute("UPDATE historical_debt_import_batches SET status = 'reconciled' WHERE batch_id = ?", (preview["batch_id"],))
        conn.commit()
        return {
            "batch_id": preview["batch_id"],
            "imported_debts": [
                {"date": item["date"], "calories": item["uncovered_excess"],
                 "recovery_deadline": item["recovery_deadline"]}
                for item in preview["items"] if item["state"] == "proposed"
            ],
            "backup_path": backup_path,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ----- display a complete debt import preview as formatted json -----
def _print_preview(user_id):
    print(json.dumps(preview_import(user_id), indent=2, sort_keys=True))


# ----- route local preview and explicit batch confirmation commands -----
def main():
    parser = argparse.ArgumentParser(description="Explicit historical debt import tool")
    commands = parser.add_subparsers(dest="action", required=True)
    preview_parser = commands.add_parser("preview", help="show a read-only preview")
    preview_parser.add_argument("user_id", type=int)
    apply_parser = commands.add_parser("apply", help="apply a previously reviewed preview")
    apply_parser.add_argument("user_id", type=int)
    apply_parser.add_argument("confirmed_batch_id")
    args = parser.parse_args()
    if args.action == "preview":
        _print_preview(args.user_id)
        return
    result = apply_import(args.user_id, args.confirmed_batch_id)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
