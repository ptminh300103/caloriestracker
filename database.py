
import sqlite3
import json
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from pathlib import Path


# Database location
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "calorie_tracker.db"
logger = logging.getLogger(__name__)
UNIFIED_BANK_RULE_VERSION = 1
HISTORICAL_DEBT_SCHEMA_VERSION = 2


# ----- preserve legacy targets during schema migration -----
def _migrate_legacy_calorie_targets(conn, today=None):
    """Anchor legacy bank history before switching the fallback target to 2084."""
    today = today or datetime.now(ZoneInfo("Asia/Singapore")).date()
    legacy = conn.execute(
        """SELECT r.user_id, MIN(r.date),
            (SELECT first.base_target FROM daily_calorie_results AS first
             WHERE first.user_id = r.user_id ORDER BY first.date LIMIT 1),
            MAX(CASE WHEN r.finalized = 1 THEN r.date END)
        FROM daily_calorie_results AS r
        GROUP BY r.user_id
        HAVING NOT EXISTS (
            SELECT 1 FROM calorie_targets AS t WHERE t.user_id = r.user_id
        ) AND MAX(CASE WHEN r.finalized = 1 THEN r.date END) IS NOT NULL"""
    ).fetchall()
    for user_id, first_day, legacy_target, last_finalized in legacy:
        if first_day and legacy_target is not None:
            conn.execute(
                "INSERT OR IGNORE INTO calorie_targets (user_id, effective_date, base_target) VALUES (?, ?, ?)",
                (user_id, first_day, int(legacy_target)),
            )
        starts_new_target = today
        if last_finalized:
            starts_new_target = max(
                today, date.fromisoformat(last_finalized) + timedelta(days=1)
            )
        conn.execute(
            "INSERT OR IGNORE INTO calorie_targets (user_id, effective_date, base_target) VALUES (?, ?, ?)",
            (user_id, starts_new_target.isoformat(), 2084),
        )


# ----- refresh derived bank results after source writes -----
def _refresh_calorie_bank(user_id):
    # Lazy import keeps the source database independent of the derived engine
    # during module initialization while ensuring every write refreshes it.
    try:
        from historical_debt_import import refresh_imported_debts
        from calorie_bank import recalculate_user
        refresh_imported_debts(user_id)
        recalculate_user(user_id)
    except Exception:
        # Source records have already committed; keep their success semantics.
        # /day, /bank and startup catch-up can safely rebuild derived rows.
        logger.exception("Calorie bank refresh failed for user %s", user_id)


# ----- apply the versioned unified bank migration without rewriting history -----
def _apply_unified_bank_migration(conn, today=None):
    today = today or datetime.now(ZoneInfo("Asia/Singapore")).date()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS calorie_account_settings (
            user_id INTEGER PRIMARY KEY,
            activation_date TEXT NOT NULL,
            rule_version INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS calorie_debts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            source_date TEXT NOT NULL,
            calculation_key TEXT NOT NULL,
            original_calories INTEGER NOT NULL CHECK (original_calories > 0),
            remaining_calories INTEGER NOT NULL CHECK (remaining_calories >= 0),
            recovery_deadline TEXT NOT NULL,
            rule_version INTEGER NOT NULL,
            UNIQUE (user_id, calculation_key)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_calorie_debts_open
        ON calorie_debts (user_id, source_date, id, remaining_calories)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS calorie_debt_repayments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            debt_id INTEGER NOT NULL REFERENCES calorie_debts(id) ON DELETE CASCADE,
            repayment_date TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_id INTEGER NOT NULL,
            calories INTEGER NOT NULL CHECK (calories > 0),
            rule_version INTEGER NOT NULL,
            UNIQUE (user_id, debt_id, source_type, source_id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_debt_repayments_user_date
        ON calorie_debt_repayments (user_id, repayment_date, debt_id)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS unified_daily_results (
            user_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            base_target INTEGER NOT NULL,
            food_consumed INTEGER NOT NULL,
            additional_exercise INTEGER NOT NULL,
            exercise_bonus INTEGER NOT NULL,
            allowance_before_bank INTEGER NOT NULL,
            bank_available INTEGER NOT NULL,
            bank_used INTEGER NOT NULL,
            bank_balance_after INTEGER NOT NULL,
            debt_added INTEGER NOT NULL,
            debt_repaid INTEGER NOT NULL,
            debt_outstanding INTEGER NOT NULL,
            remaining_allowance INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('incomplete', 'on_target', 'over_target', 'recovered_within_7_days')
            ),
            finalized INTEGER NOT NULL DEFAULT 0,
            finalized_at TEXT,
            rule_version INTEGER NOT NULL,
            PRIMARY KEY (user_id, date)
        )
    """)

    current_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if current_version >= UNIFIED_BANK_RULE_VERSION:
        return
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    applied_at = datetime.now(ZoneInfo("Asia/Singapore")).isoformat(timespec="seconds")
    legacy_users = conn.execute("""
        SELECT user_id FROM food_logs UNION SELECT user_id FROM exercise_logs
        UNION SELECT user_id FROM workout_sessions UNION SELECT user_id FROM weight_logs
        UNION SELECT user_id FROM calorie_targets UNION SELECT user_id FROM daily_calorie_results
        UNION SELECT user_id FROM calorie_bank_deposits
        UNION SELECT user_id FROM food_memory UNION SELECT user_id FROM food_memory_events
        UNION SELECT user_id FROM calorie_goals UNION SELECT user_id FROM pending_date_overrides
    """).fetchall()
    conn.executemany(
        """INSERT OR IGNORE INTO calorie_account_settings
            (user_id, activation_date, rule_version, created_at)
            VALUES (?, ?, ?, ?)""",
        [
            (int(row[0]), today.isoformat(), UNIFIED_BANK_RULE_VERSION, applied_at)
            for row in legacy_users
        ],
    )
    conn.execute(
        """INSERT OR IGNORE INTO schema_migrations (version, name, applied_at)
        VALUES (?, ?, ?)""",
        (UNIFIED_BANK_RULE_VERSION, "unified_calorie_bank_and_debt", applied_at),
    )
    conn.execute(f"PRAGMA user_version = {UNIFIED_BANK_RULE_VERSION}")
    conn.commit()


# ----- add audit tables for the optional historical debt import -----
def _apply_historical_debt_import_schema(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(calorie_debts)")}
    for name, definition in (
        ("origin", "TEXT NOT NULL DEFAULT 'generated'"),
        ("import_batch_id", "TEXT"),
        ("source_reference", "TEXT"),
        ("imported_at", "TEXT"),
        ("audit_snapshot", "TEXT"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE calorie_debts ADD COLUMN {name} {definition}")
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_imported_debt_batch_date
        ON calorie_debts (import_batch_id, source_date)
        WHERE import_batch_id IS NOT NULL
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS historical_debt_import_batches (
            batch_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL UNIQUE,
            activation_date TEXT NOT NULL,
            window_start TEXT NOT NULL,
            window_end TEXT NOT NULL,
            preview_hash TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('imported', 'reconciled')),
            created_at TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            rule_version INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS historical_debt_import_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL REFERENCES historical_debt_import_batches(batch_id),
            user_id INTEGER NOT NULL,
            source_date TEXT NOT NULL,
            source_reference TEXT NOT NULL,
            original_status TEXT NOT NULL,
            base_target INTEGER NOT NULL,
            food_consumed INTEGER NOT NULL,
            exercise_bonus INTEGER NOT NULL,
            allowance_before_bank INTEGER NOT NULL,
            bank_used INTEGER NOT NULL,
            original_excess INTEGER NOT NULL,
            current_excess INTEGER NOT NULL,
            recovery_deadline TEXT NOT NULL,
            current_status TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (batch_id, source_date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS historical_debt_import_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL REFERENCES historical_debt_import_batches(batch_id),
            user_id INTEGER NOT NULL,
            source_date TEXT NOT NULL,
            event_type TEXT NOT NULL,
            previous_excess INTEGER NOT NULL,
            current_excess INTEGER NOT NULL,
            details TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    current_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if current_version < HISTORICAL_DEBT_SCHEMA_VERSION:
        applied_at = datetime.now(ZoneInfo("Asia/Singapore")).isoformat(timespec="seconds")
        conn.execute(
            """INSERT OR IGNORE INTO schema_migrations (version, name, applied_at)
            VALUES (?, ?, ?)""",
            (HISTORICAL_DEBT_SCHEMA_VERSION, "historical_debt_import_audit", applied_at),
        )
        conn.execute(f"PRAGMA user_version = {HISTORICAL_DEBT_SCHEMA_VERSION}")


# ----- enable unified-bank rules for a user without changing their history -----
def ensure_calorie_account(user_id, activation_date=None):
    effective_date = activation_date or datetime.now(
        ZoneInfo("Asia/Singapore")
    ).date().isoformat()
    created_at = datetime.now(ZoneInfo("Asia/Singapore")).isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT OR IGNORE INTO calorie_account_settings
                (user_id, activation_date, rule_version, created_at)
            VALUES (?, ?, ?, ?)""",
            (int(user_id), str(effective_date), UNIFIED_BANK_RULE_VERSION, created_at),
        )
        row = conn.execute(
            "SELECT activation_date FROM calorie_account_settings WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
    return row[0]


# ----- retrieve one user's unified-bank activation date -----
def get_calorie_account_activation(user_id):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT activation_date FROM calorie_account_settings WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
    return row[0] if row else None


# ----- retrieve dated outstanding calorie debts -----
def get_calorie_debts(user_id, outstanding_only=True):
    query = """SELECT d.id, d.source_date, d.original_calories,
        d.remaining_calories, d.recovery_deadline,
        d.origin, d.import_batch_id, d.source_reference, d.imported_at,
        COALESCE(SUM(r.calories), 0) AS repaid_calories
        FROM calorie_debts AS d
        LEFT JOIN calorie_debt_repayments AS r ON r.debt_id = d.id
        WHERE d.user_id = ?"""
    if outstanding_only:
        query += " AND d.remaining_calories > 0"
    query += " GROUP BY d.id ORDER BY d.source_date, d.id"
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(query, (int(user_id),)).fetchall()]


# ----- resolve imported historical-day statuses without changing snapshots -----
def get_imported_historical_status(user_id, day):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT i.current_excess, i.current_status, i.recovery_deadline,
                d.id AS debt_id, d.remaining_calories,
                (SELECT MAX(r.repayment_date) FROM calorie_debt_repayments AS r
                 WHERE r.debt_id = d.id) AS last_repayment
            FROM historical_debt_import_items AS i
            LEFT JOIN calorie_debts AS d
                ON d.import_batch_id = i.batch_id AND d.source_date = i.source_date
            WHERE i.user_id = ? AND i.source_date = ?
            ORDER BY i.id DESC LIMIT 1""",
            (int(user_id), str(day)),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    if result["current_excess"] <= 0:
        result["status"] = result["current_status"]
        return result
    if result["debt_id"] is not None and result["remaining_calories"] == 0:
        if result["last_repayment"] and result["last_repayment"] <= result["recovery_deadline"]:
            result["status"] = "recovered_within_7_days"
            return result
    result["status"] = "over_target"
    return result


# ----- read a unified daily result for reports -----
def get_unified_daily_result(user_id, day):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM unified_daily_results WHERE user_id = ? AND date = ?",
            (int(user_id), str(day)),
        ).fetchone()
    return dict(row) if row else None


# ----- retrieve an unchanged legacy daily result -----
def get_legacy_daily_result(user_id, day):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM daily_calorie_results WHERE user_id = ? AND date = ?",
            (int(user_id), str(day)),
        ).fetchone()
    return dict(row) if row else None


# ----- freeze a missing pre-activation summary without replacing an old one -----
def save_legacy_daily_result_if_missing(user_id, result, finalized_at):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT OR IGNORE INTO daily_calorie_results (
                user_id, date, base_target, food_consumed, additional_exercise,
                exercise_bonus, allowance_before_bank, bank_available, bank_used,
                bank_balance_after, remaining_allowance, status, finalized,
                finalized_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(user_id), result["date"], result["base_target"],
                result["food_consumed"], result["additional_exercise"],
                result["exercise_bonus"], result["allowance_before_bank"],
                result["bank_available"], result["bank_used"],
                result["bank_balance_after"], result["remaining_allowance"],
                result["status"], int(result.get("finalized", 1)), finalized_at,
            ),
        )
    return get_legacy_daily_result(user_id, result["date"])


# ----- replace a user's unused historical date override -----
def set_pending_date_override(user_id, target_date):
    created_at = datetime.now(ZoneInfo("Asia/Singapore")).isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT INTO pending_date_overrides (user_id, target_date, created_at)
            VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET
            target_date = excluded.target_date, created_at = excluded.created_at""",
            (int(user_id), str(target_date), created_at),
        )


# ----- retrieve a user's unused historical date override -----
def get_pending_date_override(user_id):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT target_date FROM pending_date_overrides WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
    return row[0] if row else None


# ----- atomically consume a user's historical date override -----
def consume_pending_date_override(user_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT target_date FROM pending_date_overrides WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
        if row:
            conn.execute(
                "DELETE FROM pending_date_overrides WHERE user_id = ?",
                (int(user_id),),
            )
    return row[0] if row else None


# ----- clear a user's unused historical date override -----
def clear_pending_date_override(user_id):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            "DELETE FROM pending_date_overrides WHERE user_id = ?",
            (int(user_id),),
        )
    return cursor.rowcount > 0


# ----- allow food memories to outlive their source meal -----
def _allow_detached_food_memories(conn):
    source_column = next(
        row for row in conn.execute("PRAGMA table_info(food_memory)")
        if row[1] == "source_food_id"
    )
    if not source_column[3]:
        return
    conn.execute("SAVEPOINT nullable_food_memory_source")
    try:
        conn.execute("""
        CREATE TABLE food_memory_migration (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            canonical_identity TEXT NOT NULL,
            attributes TEXT NOT NULL,
            description TEXT NOT NULL,
            items TEXT NOT NULL,
            product_info TEXT,
            calories INTEGER NOT NULL CHECK (calories >= 0),
            evidence_category TEXT NOT NULL CHECK (
                evidence_category IN ('verified', 'user_corrected', 'ai_confirmed')
            ),
            source_food_id INTEGER UNIQUE,
            source_origin TEXT NOT NULL CHECK (
                source_origin IN ('external_verified', 'manual_correction', 'ai_estimated')
            ),
            original_source_origin TEXT NOT NULL DEFAULT 'ai_estimated' CHECK (
                original_source_origin IN ('external_verified', 'manual_correction', 'ai_estimated')
            ),
            exact_ready INTEGER NOT NULL DEFAULT 0,
            usage_count INTEGER NOT NULL DEFAULT 1,
            last_used_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        )
        """)
        conn.execute("""
        INSERT INTO food_memory_migration (
            id, user_id, canonical_identity, attributes, description, items,
            product_info, calories, evidence_category, source_food_id,
            source_origin, original_source_origin, exact_ready, usage_count,
            last_used_at, created_at, active
        ) SELECT
            id, user_id, canonical_identity, attributes, description, items,
            product_info, calories, evidence_category, source_food_id,
            source_origin, original_source_origin, exact_ready, usage_count,
            last_used_at, created_at, active
        FROM food_memory
        """)
        conn.execute("DROP TABLE food_memory")
        conn.execute("ALTER TABLE food_memory_migration RENAME TO food_memory")
    except Exception:
        conn.execute("ROLLBACK TO nullable_food_memory_source")
        conn.execute("RELEASE nullable_food_memory_source")
        raise
    conn.execute("RELEASE nullable_food_memory_source")


# ----- create and migrate the sqlite schema -----
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS food_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                description TEXT NOT NULL,
                items TEXT NOT NULL,
                calories INTEGER NOT NULL,
                product_info TEXT
            )
        """)

        food_columns = {row[1] for row in conn.execute("PRAGMA table_info(food_logs)")}
        if "product_info" not in food_columns:
            conn.execute("ALTER TABLE food_logs ADD COLUMN product_info TEXT")
        if "confirmation_key" not in food_columns:
            conn.execute("ALTER TABLE food_logs ADD COLUMN confirmation_key TEXT")

        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_food_user_confirmation
            ON food_logs (user_id, confirmation_key)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_food_user_date
            ON food_logs (user_id, date)
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS weight_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                weight_kg REAL NOT NULL CHECK (weight_kg > 0),
                measurement_date TEXT NOT NULL,
                measurement_time TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_weight_user_measurement
            ON weight_logs (user_id, measurement_date, measurement_time, id)
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_date_overrides (
                user_id INTEGER PRIMARY KEY,
                target_date TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS exercise_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                description TEXT NOT NULL,
                activity TEXT NOT NULL,
                duration_minutes REAL NOT NULL,
                met REAL NOT NULL,
                additional_calories INTEGER NOT NULL,
                weight_kg REAL
            )
        """)
        exercise_log_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(exercise_logs)")
        }
        if "weight_kg" not in exercise_log_columns:
            conn.execute("ALTER TABLE exercise_logs ADD COLUMN weight_kg REAL")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS workout_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_key TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                duration_minutes REAL NOT NULL,
                duration_source TEXT NOT NULL,
                weight_kg REAL NOT NULL,
                met REAL NOT NULL,
                additional_calories INTEGER NOT NULL
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_workout_user_date
            ON workout_sessions (user_id, date)
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS workout_exercises (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL REFERENCES workout_sessions(id)
                    ON DELETE CASCADE,
                position INTEGER NOT NULL,
                name TEXT NOT NULL,
                sets INTEGER,
                repetitions INTEGER,
                weight_kg REAL,
                resistance_type TEXT,
                hold_duration_seconds INTEGER,
                activity_type TEXT NOT NULL DEFAULT 'strength',
                duration_minutes REAL,
                distance_km REAL,
                pace_min_per_km REAL,
                speed_kmh REAL,
                power_watts REAL,
                equipment_context TEXT,
                resistance_level TEXT,
                intensity TEXT,
                met REAL,
                met_source TEXT,
                notes TEXT
            )
        """)

        # Additive migration for databases created before these fields existed.
        exercise_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(workout_exercises)")
        }
        if "resistance_type" not in exercise_columns:
            conn.execute("ALTER TABLE workout_exercises ADD COLUMN resistance_type TEXT")
        if "hold_duration_seconds" not in exercise_columns:
            conn.execute("ALTER TABLE workout_exercises ADD COLUMN hold_duration_seconds INTEGER")
        cardio_columns = {
            "activity_type": "TEXT NOT NULL DEFAULT 'strength'",
            "duration_minutes": "REAL",
            "distance_km": "REAL",
            "pace_min_per_km": "REAL",
            "speed_kmh": "REAL",
            "power_watts": "REAL",
            "equipment_context": "TEXT",
            "resistance_level": "TEXT",
            "intensity": "TEXT",
            "met": "REAL",
            "met_source": "TEXT",
        }
        for column, definition in cardio_columns.items():
            if column not in exercise_columns:
                conn.execute(f"ALTER TABLE workout_exercises ADD COLUMN {column} {definition}")

        # Additive calorie-bank tables. Existing food/exercise records remain
        # the source of truth; these rows are deterministic derived results.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS calorie_targets (
                user_id INTEGER NOT NULL,
                effective_date TEXT NOT NULL,
                base_target INTEGER NOT NULL CHECK (base_target >= 0),
                PRIMARY KEY (user_id, effective_date)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS calorie_goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                start_weight_kg REAL NOT NULL,
                target_weight_kg REAL NOT NULL,
                created_date TEXT NOT NULL,
                deadline TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('active', 'cancelled', 'reached', 'replaced', 'deadline_passed')
                ),
                calculation_status TEXT NOT NULL DEFAULT 'pending',
                current_deficit_kcal REAL,
                current_target_kcal INTEGER,
                last_calculated_date TEXT,
                settings_json TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_calorie_goals_user_status
            ON calorie_goals (user_id, status, id DESC)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS calorie_bank_deposits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                source_type TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                earning_date TEXT NOT NULL,
                available_date TEXT NOT NULL,
                expiration_date TEXT NOT NULL,
                original_calories INTEGER NOT NULL CHECK (original_calories >= 0),
                remaining_calories INTEGER NOT NULL CHECK (remaining_calories >= 0),
                UNIQUE (user_id, source_type, source_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_bank_user_dates
            ON calorie_bank_deposits (user_id, available_date, expiration_date)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS calorie_bank_withdrawals (
                user_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                deposit_id INTEGER NOT NULL REFERENCES calorie_bank_deposits(id)
                    ON DELETE CASCADE,
                calories INTEGER NOT NULL CHECK (calories > 0),
                PRIMARY KEY (user_id, date, deposit_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_calorie_results (
                user_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                base_target INTEGER NOT NULL,
                food_consumed INTEGER NOT NULL,
                additional_exercise INTEGER NOT NULL,
                exercise_bonus INTEGER NOT NULL,
                allowance_before_bank INTEGER NOT NULL,
                bank_available INTEGER NOT NULL,
                bank_used INTEGER NOT NULL,
                bank_balance_after INTEGER NOT NULL,
                remaining_allowance INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('incomplete', 'on_target', 'over_target')),
                finalized INTEGER NOT NULL DEFAULT 0,
                finalized_at TEXT,
                PRIMARY KEY (user_id, date)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS food_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                canonical_identity TEXT NOT NULL,
                attributes TEXT NOT NULL,
                description TEXT NOT NULL,
                items TEXT NOT NULL,
                product_info TEXT,
                calories INTEGER NOT NULL CHECK (calories >= 0),
                evidence_category TEXT NOT NULL CHECK (
                    evidence_category IN ('verified', 'user_corrected', 'ai_confirmed')
                ),
                source_food_id INTEGER UNIQUE,
                source_origin TEXT NOT NULL CHECK (
                    source_origin IN ('external_verified', 'manual_correction', 'ai_estimated')
                ),
                original_source_origin TEXT NOT NULL DEFAULT 'ai_estimated' CHECK (
                    original_source_origin IN ('external_verified', 'manual_correction', 'ai_estimated')
                ),
                exact_ready INTEGER NOT NULL DEFAULT 0,
                usage_count INTEGER NOT NULL DEFAULT 1,
                last_used_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )
        """)
        memory_columns = {row[1] for row in conn.execute("PRAGMA table_info(food_memory)")}
        if "original_source_origin" not in memory_columns:
            conn.execute(
                "ALTER TABLE food_memory ADD COLUMN original_source_origin TEXT NOT NULL DEFAULT 'ai_estimated'"
            )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS food_memory_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                memory_id INTEGER,
                source_food_id INTEGER,
                event_type TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
        """)
        _allow_detached_food_memories(conn)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_food_memory_identity
            ON food_memory (user_id, canonical_identity, exact_ready, active)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_food_memory_recent
            ON food_memory (user_id, active, last_used_at DESC)
        """)

        # Keep existing finalized calorie-bank history stable when changing
        # the fallback target. This only applies to legacy users with no
        # effective-dated targets; existing target rows are never rewritten.
        _migrate_legacy_calorie_targets(conn)
        _apply_unified_bank_migration(conn)
        _apply_historical_debt_import_schema(conn)


# ----- persist a confirmed meal and its nutrition details -----
def save_food(
    user_id,
    date,
    time,
    description,
    items,
    calories,
    product_info=None,
    confirmation_key=None,
    memory=None,
    memory_reuse_id=None,
):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO food_logs (
                user_id,
                date,
                time,
                description,
                items,
                calories,
                product_info,
                confirmation_key
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, confirmation_key) DO NOTHING
            """,
            (
                user_id,
                date,
                time,
                description,
                json.dumps(items),
                calories,
                json.dumps(product_info) if product_info is not None else None,
                confirmation_key,
            ),
        )

        inserted = cursor.rowcount > 0
        if inserted:
            entry_id = cursor.lastrowid
        else:
            row = conn.execute(
                "SELECT id FROM food_logs WHERE user_id = ? AND confirmation_key = ?",
                (user_id, confirmation_key),
            ).fetchone()
            if not row:
                raise sqlite3.IntegrityError("Food confirmation could not be resolved")
            entry_id = int(row[0])

        if inserted and memory_reuse_id is not None:
            timestamp = datetime.now(ZoneInfo("Asia/Singapore")).isoformat(timespec="seconds")
            source = conn.execute(
                "SELECT evidence_category FROM food_memory WHERE id = ? AND user_id = ? AND active = 1",
                (int(memory_reuse_id), int(user_id)),
            ).fetchone()
            if source:
                conn.execute(
                    "UPDATE food_memory SET usage_count = usage_count + 1, last_used_at = ? WHERE id = ? AND user_id = ?",
                    (timestamp, int(memory_reuse_id), int(user_id)),
                )
                conn.execute(
                    """INSERT INTO food_memory_events
                    (user_id, memory_id, source_food_id, event_type, details, created_at)
                    VALUES (?, ?, ?, 'reused', ?, ?)""",
                    (int(user_id), int(memory_reuse_id), int(entry_id),
                     json.dumps({"evidence_category": source[0]}), timestamp),
                )
        elif inserted and memory:
            timestamp = datetime.now(ZoneInfo("Asia/Singapore")).isoformat(timespec="seconds")
            cur = conn.execute(
                """INSERT INTO food_memory (
                    user_id, canonical_identity, attributes, description, items,
                    product_info, calories, evidence_category, source_food_id,
                    source_origin, original_source_origin, exact_ready, usage_count,
                    last_used_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (int(user_id), memory["canonical_identity"],
                 json.dumps(memory["attributes"]), memory["description"],
                 json.dumps(memory["items"]),
                 json.dumps(memory["product_info"]) if memory.get("product_info") else None,
                 int(memory["calories"]), memory["evidence_category"], int(entry_id),
                 memory["source_origin"], memory.get("original_source_origin", memory["source_origin"]),
                 int(memory["exact_ready"]), timestamp, timestamp),
            )
            memory_id = cur.lastrowid
            conn.execute(
                """INSERT INTO food_memory_events
                (user_id, memory_id, source_food_id, event_type, details, created_at)
                VALUES (?, ?, ?, 'created', ?, ?)""",
                (int(user_id), int(memory_id), int(entry_id),
                 json.dumps({"evidence_category": memory["evidence_category"],
                             "source_origin": memory["source_origin"]}), timestamp),
            )
    _refresh_calorie_bank(user_id)
    return entry_id


# ----- retrieve a user’s meals for one date -----
def get_food_by_date(user_id, date):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        rows = conn.execute(
            """
            SELECT *
            FROM food_logs
            WHERE user_id = ? AND date = ?
            ORDER BY time, id
            """,
            (user_id, date),
        ).fetchall()

        result = []

        for row in rows:
            entry = dict(row)
            entry["items"] = json.loads(entry["items"])
            entry["product_info"] = (
                json.loads(entry["product_info"])
                if entry.get("product_info") else None
            )
            result.append(entry)

        return result


# ----- retrieve a saved exact-match food reference -----
def get_food_memory_exact(user_id, canonical_identity):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT * FROM food_memory
            WHERE user_id = ? AND canonical_identity = ?
                AND exact_ready = 1 AND active = 1
            ORDER BY CASE evidence_category
                WHEN 'verified' THEN 0 WHEN 'user_corrected' THEN 1 ELSE 2 END,
                last_used_at DESC, id DESC LIMIT 1""",
            (int(user_id), canonical_identity),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["attributes"] = json.loads(result["attributes"] or "{}")
        result["items"] = json.loads(result["items"] or "[]")
        result["product_info"] = json.loads(result["product_info"]) if result["product_info"] else None
        return result


# ----- retrieve a user’s confirmed food references -----
def get_food_memories(user_id, limit=200):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM food_memory WHERE user_id = ? AND active = 1
            ORDER BY last_used_at DESC, id DESC LIMIT ?""",
            (int(user_id), max(0, min(int(limit), 500))),
        ).fetchall()
        results = [dict(row) for row in rows]
        for result in results:
            result["attributes"] = json.loads(result["attributes"] or "{}")
            result["items"] = json.loads(result["items"] or "[]")
            result["product_info"] = json.loads(result["product_info"]) if result["product_info"] else None
        return results


# ----- retrieve audit events for food memory -----
def get_food_memory_events(user_id, source_food_id=None):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        if source_food_id is None:
            rows = conn.execute(
                "SELECT * FROM food_memory_events WHERE user_id = ? ORDER BY id",
                (int(user_id),),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM food_memory_events WHERE user_id = ?
                AND source_food_id = ? ORDER BY id""",
                (int(user_id), int(source_food_id)),
            ).fetchall()
        results = [dict(row) for row in rows]
        for row in results:
            row["details"] = json.loads(row["details"] or "{}")
        return results


# ----- sum confirmed food calories for one date -----
def get_daily_total(user_id, date):
    with sqlite3.connect(DB_PATH) as conn:
        result = conn.execute(
            """
            SELECT COALESCE(SUM(calories), 0)
            FROM food_logs
            WHERE user_id = ? AND date = ?
            """,
            (user_id, date),
        ).fetchone()

        return result[0]


# ----- update a saved meal and recalculate derived totals -----
def update_food_calories(user_id, food_id, calories):
    if calories < 0:
        raise ValueError("Calories cannot be negative")

    with sqlite3.connect(DB_PATH) as conn:
        correction_source = "source_memory"
        old_memory = conn.execute(
            "SELECT id, calories, evidence_category, source_origin FROM food_memory WHERE user_id = ? AND source_food_id = ? AND active = 1",
            (int(user_id), int(food_id)),
        ).fetchone()
        if not old_memory:
            old_memory = conn.execute(
                """SELECT m.id, m.calories, m.evidence_category, m.source_origin
                FROM food_memory_events AS e JOIN food_memory AS m
                    ON m.id = e.memory_id AND m.user_id = e.user_id
                WHERE e.user_id = ? AND e.source_food_id = ?
                    AND e.event_type = 'reused' AND m.active = 1
                ORDER BY e.id DESC LIMIT 1""",
                (int(user_id), int(food_id)),
            ).fetchone()
            if old_memory:
                correction_source = "reused_log"
        cursor = conn.execute(
            """
            UPDATE food_logs
            SET calories = ?
            WHERE id = ? AND user_id = ?
            """,
            (calories, food_id, user_id),
        )

        changed = cursor.rowcount > 0
        if changed and old_memory:
            timestamp = datetime.now(ZoneInfo("Asia/Singapore")).isoformat(timespec="seconds")
            conn.execute(
                """UPDATE food_memory SET calories = ?, evidence_category = 'user_corrected',
                    source_origin = 'manual_correction', last_used_at = ?
                WHERE id = ? AND user_id = ?""",
                (int(calories), timestamp, int(old_memory[0]), int(user_id)),
            )
            conn.execute(
                """INSERT INTO food_memory_events
                (user_id, memory_id, source_food_id, event_type, details, created_at)
                VALUES (?, ?, ?, 'corrected', ?, ?)""",
                (int(user_id), int(old_memory[0]), int(food_id),
                 json.dumps({"previous_calories": int(old_memory[1]),
                             "calories": int(calories),
                             "previous_evidence": old_memory[2],
                             "previous_source_origin": old_memory[3],
                             "correction_source": correction_source}), timestamp),
            )
    if changed:
        _refresh_calorie_bank(user_id)
    return changed


# ----- delete a saved meal and refresh derived totals -----
def delete_food(user_id, food_id):
    with sqlite3.connect(DB_PATH) as conn:
        memory_rows = conn.execute(
            "SELECT id, evidence_category FROM food_memory WHERE user_id = ? AND source_food_id = ?",
            (int(user_id), int(food_id)),
        ).fetchall()
        cursor = conn.execute(
            """
            DELETE FROM food_logs
            WHERE id = ? AND user_id = ?
            """,
            (food_id, user_id),
        )
        changed = cursor.rowcount > 0
        if changed:
            timestamp = datetime.now(ZoneInfo("Asia/Singapore")).isoformat(timespec="seconds")
            for memory_id, evidence in memory_rows:
                conn.execute(
                    """INSERT INTO food_memory_events
                    (user_id, memory_id, source_food_id, event_type, details, created_at)
                    VALUES (?, ?, ?, 'source_deleted', ?, ?)""",
                    (int(user_id), int(memory_id), int(food_id),
                     json.dumps({"evidence_category": evidence}), timestamp),
                )
            conn.execute(
                "DELETE FROM food_memory WHERE user_id = ? AND source_food_id = ?",
                (int(user_id), int(food_id)),
            )
            reuse_rows = conn.execute(
                """SELECT memory_id FROM food_memory_events
                WHERE user_id = ? AND source_food_id = ? AND event_type = 'reused'""",
                (int(user_id), int(food_id)),
            ).fetchall()
            for (memory_id,) in reuse_rows:
                conn.execute(
                    """UPDATE food_memory SET usage_count = MAX(0, usage_count - 1)
                    WHERE id = ? AND user_id = ? AND active = 1""",
                    (int(memory_id), int(user_id)),
                )
                conn.execute(
                    """INSERT INTO food_memory_events
                    (user_id, memory_id, source_food_id, event_type, details, created_at)
                    VALUES (?, ?, ?, 'reuse_log_deleted', '{}', ?)""",
                    (int(user_id), int(memory_id), int(food_id), timestamp),
                )
    if changed:
        _refresh_calorie_bank(user_id)
    return changed



# ----- persist a confirmed standalone activity -----
def save_exercise(
    user_id,
    date,
    time,
    description,
    activity,
    duration_minutes,
    met,
    additional_calories,
    weight_kg=None,
):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO exercise_logs (
                user_id,
                date,
                time,
                description,
                activity,
                duration_minutes,
                met,
                additional_calories,
                weight_kg
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                date,
                time,
                description,
                activity,
                duration_minutes,
                met,
                additional_calories,
                weight_kg,
            ),
        )

        entry_id = cursor.lastrowid
    _refresh_calorie_bank(user_id)
    return entry_id


# ----- persist a dated body-weight measurement -----
def save_weight(user_id, weight_kg, measurement_date, measurement_time, created_at):
    """Append a weight measurement; multiple measurements per day are retained."""
    if float(weight_kg) <= 0:
        raise ValueError("Weight must be positive")
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """INSERT INTO weight_logs (
                user_id, weight_kg, measurement_date, measurement_time, created_at
            ) VALUES (?, ?, ?, ?, ?)""",
            (int(user_id), float(weight_kg), measurement_date,
             measurement_time, created_at),
        )
        return cursor.lastrowid


# ----- retrieve recent body-weight measurements -----
def get_weight_history(user_id, limit=20):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT id, user_id, weight_kg, measurement_date,
                measurement_time, created_at FROM weight_logs
            WHERE user_id = ? ORDER BY measurement_date DESC,
                measurement_time DESC, id DESC LIMIT ?""",
            (int(user_id), int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]


# ----- retrieve measurements within a date range -----
def get_weights_between(user_id, start_date, end_date):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT id, user_id, weight_kg, measurement_date,
                measurement_time, created_at FROM weight_logs
            WHERE user_id = ? AND measurement_date BETWEEN ? AND ?
            ORDER BY measurement_date DESC, measurement_time DESC, id DESC""",
            (int(user_id), start_date, end_date),
        ).fetchall()
        return [dict(row) for row in rows]


# ----- retrieve the latest body-weight measurement -----
def get_latest_weight(user_id):
    rows = get_weight_history(user_id, limit=1)
    return rows[0] if rows else None


# ----- retrieve the target active on a date -----
def get_effective_calorie_target(user_id, effective_date, default=2084):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """SELECT base_target FROM calorie_targets
            WHERE user_id = ? AND effective_date <= ?
            ORDER BY effective_date DESC LIMIT 1""",
            (int(user_id), effective_date),
        ).fetchone()
    return int(row[0]) if row else int(default)


# ----- store a new persistent calorie goal -----
def create_calorie_goal(
    user_id, start_weight_kg, target_weight_kg, created_date, deadline,
    settings_json,
):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE calorie_goals SET status = 'replaced' "
            "WHERE user_id = ? AND status = 'active'",
            (int(user_id),),
        )
        cursor = conn.execute(
            """INSERT INTO calorie_goals (
                user_id, start_weight_kg, target_weight_kg, created_date,
                deadline, status, settings_json
            ) VALUES (?, ?, ?, ?, ?, 'active', ?)""",
            (int(user_id), float(start_weight_kg), float(target_weight_kg),
             created_date, deadline, settings_json),
        )
        return cursor.lastrowid


# ----- retrieve one user's active calorie goal -----
def get_active_calorie_goal(user_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM calorie_goals WHERE user_id = ? AND status = 'active' "
            "ORDER BY id DESC LIMIT 1",
            (int(user_id),),
        ).fetchone()
    return dict(row) if row else None


# ----- retrieve the latest goal for a user's status display -----
def get_latest_calorie_goal(user_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM calorie_goals WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (int(user_id),),
        ).fetchone()
    return dict(row) if row else None


# ----- list users with active calorie goals -----
def get_users_with_active_calorie_goals():
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT DISTINCT user_id FROM calorie_goals WHERE status = 'active'"
        ).fetchall()
    return [int(row[0]) for row in rows]


# ----- update a goal's latest calculation snapshot -----
def update_calorie_goal_calculation(
    goal_id, calculation_status, calculated_date, deficit_kcal=None,
    target_kcal=None,
):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """UPDATE calorie_goals SET calculation_status = ?,
                last_calculated_date = ?, current_deficit_kcal = ?,
                current_target_kcal = ? WHERE id = ?""",
            (calculation_status, calculated_date, deficit_kcal, target_kcal,
             int(goal_id)),
        )


# ----- finish an active goal without deleting its history -----
def finish_calorie_goal(goal_id, status):
    if status not in {"cancelled", "reached", "deadline_passed"}:
        raise ValueError("Invalid terminal goal status.")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE calorie_goals SET status = ? WHERE id = ? AND status = 'active'",
            (status, int(goal_id)),
        )


# ----- retrieve finalized daily results for a month -----
def get_daily_results_for_month(user_id, start_date, end_date):
    """Read the user's derived daily summaries without recalculating them."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        legacy_rows = conn.execute(
            """SELECT date, base_target, food_consumed, additional_exercise,
                exercise_bonus, bank_available, bank_used, remaining_allowance,
                status, finalized, finalized_at
            FROM daily_calorie_results
            WHERE user_id = ? AND date BETWEEN ? AND ? ORDER BY date""",
            (int(user_id), start_date, end_date),
        ).fetchall()
        activation_row = conn.execute(
            "SELECT activation_date FROM calorie_account_settings WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
        unified_rows = conn.execute(
            """SELECT date, base_target, food_consumed, additional_exercise,
                exercise_bonus, bank_available, bank_used, remaining_allowance,
                status, finalized, finalized_at, debt_added, debt_repaid,
                debt_outstanding, bank_balance_after
            FROM unified_daily_results
            WHERE user_id = ? AND date BETWEEN ? AND ? ORDER BY date""",
            (int(user_id), start_date, end_date),
        ).fetchall()
        imported_dates = {
            row[0] for row in conn.execute(
                """SELECT source_date FROM historical_debt_import_items
                WHERE user_id = ? AND source_date BETWEEN ? AND ?""",
                (int(user_id), start_date, end_date),
            ).fetchall()
        }
    activation = activation_row[0] if activation_row else None
    result_by_date = {
        row["date"]: dict(row) for row in legacy_rows
        if activation is None or row["date"] < activation
    }
    result_by_date.update({row["date"]: dict(row) for row in unified_rows})
    for imported_date in imported_dates:
        imported = get_imported_historical_status(user_id, imported_date)
        if imported and imported_date in result_by_date:
            result_by_date[imported_date]["status"] = imported["status"]
    return [result_by_date[key] for key in sorted(result_by_date)]


# ----- retrieve confirmed exercise for one date -----
def get_exercise_by_date(user_id, date):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        rows = conn.execute(
            """
            SELECT *
            FROM exercise_logs
            WHERE user_id = ? AND date = ?
            ORDER BY time, id
            """,
            (user_id, date),
        ).fetchall()

        return [dict(row) for row in rows]


# ----- persist a confirmed session and its exercises -----
def save_workout_session(
    session_key, user_id, date, time, duration_minutes,
    duration_source, weight_kg, met, additional_calories, exercises,
):
    """Atomically save one workout and its ordered exercise records."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        cursor = conn.execute(
            """INSERT INTO workout_sessions (
                session_key, user_id, date, time, duration_minutes,
                duration_source, weight_kg, met, additional_calories
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_key, user_id, date, time, duration_minutes,
             duration_source, weight_kg, met, additional_calories),
        )
        session_id = cursor.lastrowid
        conn.executemany(
            """INSERT INTO workout_exercises (
                session_id, position, name, sets, repetitions, weight_kg,
                resistance_type, hold_duration_seconds, activity_type,
                duration_minutes, distance_km, pace_min_per_km, speed_kmh,
                power_watts, equipment_context, resistance_level, intensity,
                met, met_source, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (session_id, index, item["name"], item.get("sets"),
                 item.get("repetitions"), item.get("weight_kg"),
                 item.get("resistance_type"), item.get("hold_duration_seconds"),
                 item.get("activity_type", "strength"), item.get("duration_minutes"),
                 item.get("distance_km"), item.get("pace_min_per_km"),
                 item.get("speed_kmh"), item.get("power_watts"),
                 item.get("equipment_context"), item.get("resistance_level"),
                 item.get("intensity"), item.get("met"), item.get("met_source"),
                 item.get("notes"))
                for index, item in enumerate(exercises, start=1)
            ],
        )
    _refresh_calorie_bank(user_id)
    return session_id


# ----- retrieve confirmed workouts for one date -----
def get_workout_sessions_by_date(user_id, date):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        sessions = conn.execute(
            """SELECT * FROM workout_sessions
            WHERE user_id = ? AND date = ? ORDER BY time, id""",
            (user_id, date),
        ).fetchall()
        results = []
        for session in sessions:
            entry = dict(session)
            entry["exercises"] = [dict(row) for row in conn.execute(
                """SELECT name, sets, repetitions, weight_kg,
                resistance_type, hold_duration_seconds, activity_type,
                duration_minutes, distance_km, pace_min_per_km, speed_kmh,
                power_watts, equipment_context, resistance_level, intensity,
                met, met_source, notes
                FROM workout_exercises WHERE session_id = ? ORDER BY position""",
                (entry["id"],),
            ).fetchall()]
            results.append(entry)
        return results


# ----- store a calorie target with its effective date -----
def set_calorie_target(user_id, effective_date, base_target):
    """Set a base target effective from a date; prior dates stay unchanged."""
    if int(base_target) < 0:
        raise ValueError("Target cannot be negative")
    with sqlite3.connect(DB_PATH) as conn:
        previous = conn.execute(
            """SELECT base_target FROM calorie_targets
            WHERE user_id = ? AND effective_date = ?""",
            (int(user_id), effective_date),
        ).fetchone()
        if previous and int(previous[0]) == int(base_target):
            return False
        conn.execute(
            """INSERT INTO calorie_targets (user_id, effective_date, base_target)
            VALUES (?, ?, ?) ON CONFLICT(user_id, effective_date)
            DO UPDATE SET base_target = excluded.base_target""",
            (int(user_id), effective_date, int(base_target)),
        )
    _refresh_calorie_bank(user_id)
    return True


# ----- revise a saved exercise estimate -----
def update_exercise_additional_calories(user_id, exercise_id, calories):
    if int(calories) < 0:
        raise ValueError("Calories cannot be negative")
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            "UPDATE exercise_logs SET additional_calories = ? WHERE id = ? AND user_id = ?",
            (int(calories), exercise_id, user_id),
        )
        changed = cursor.rowcount > 0
    if changed:
        _refresh_calorie_bank(user_id)
    return changed


# ----- remove a saved exercise and refresh daily results -----
def delete_exercise(user_id, exercise_id):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            "DELETE FROM exercise_logs WHERE id = ? AND user_id = ?",
            (exercise_id, user_id),
        )
        changed = cursor.rowcount > 0
    if changed:
        _refresh_calorie_bank(user_id)
    return changed


# ----- revise a saved workout estimate -----
def update_workout_additional_calories(user_id, session_id, calories):
    if int(calories) < 0:
        raise ValueError("Calories cannot be negative")
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            "UPDATE workout_sessions SET additional_calories = ? WHERE id = ? AND user_id = ?",
            (int(calories), session_id, user_id),
        )
        changed = cursor.rowcount > 0
    if changed:
        _refresh_calorie_bank(user_id)
    return changed


# ----- remove a workout and its exercise records -----
def delete_workout_session(user_id, session_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        cursor = conn.execute(
            "DELETE FROM workout_sessions WHERE id = ? AND user_id = ?",
            (session_id, user_id),
        )
        changed = cursor.rowcount > 0
    if changed:
        _refresh_calorie_bank(user_id)
    return changed
