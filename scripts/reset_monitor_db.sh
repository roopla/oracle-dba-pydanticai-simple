#!/usr/bin/env bash
set -euo pipefail

DB_PATH="${MONITOR_SQLITE_PATH:-./monitor.db}"
BACKUP_DIR="${MONITOR_BACKUP_DIR:-./monitor-db-backups}"
AUTO_CONFIRM="${1:-}"

if [[ ! -f "$DB_PATH" ]]; then
    echo "Monitor database does not exist: $DB_PATH"
    exit 0
fi

if [[ "$AUTO_CONFIRM" != "--yes" ]]; then
    echo "WARNING: This will permanently delete all monitor incidents from:"
    echo "  $DB_PATH"
    echo
    read -r -p "Type RESET to continue: " confirmation

    if [[ "$confirmation" != "RESET" ]]; then
        echo "Cleanup cancelled."
        exit 1
    fi
fi

mkdir -p "$BACKUP_DIR"

timestamp="$(date +%Y%m%d-%H%M%S)"
backup_path="$BACKUP_DIR/monitor.db.before-reset.$timestamp"

DB_PATH="$DB_PATH" BACKUP_PATH="$backup_path" uv run python <<'PY'
import os
import sqlite3
from pathlib import Path

db_path = Path(os.environ["DB_PATH"]).expanduser().resolve()
backup_path = Path(os.environ["BACKUP_PATH"]).expanduser().resolve()

source = sqlite3.connect(db_path, timeout=10)
source.execute("PRAGMA busy_timeout = 10000")

try:
    # Create a consistent SQLite backup before deleting anything.
    backup = sqlite3.connect(backup_path)

    try:
        source.backup(backup)
    finally:
        backup.close()

    table_exists = source.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = 'issues'
        """
    ).fetchone()

    if not table_exists:
        print(f"No issues table found in {db_path}")
        print(f"Backup created: {backup_path}")
        raise SystemExit(0)

    before_count = source.execute(
        "SELECT COUNT(*) FROM issues"
    ).fetchone()[0]

    source.execute("BEGIN IMMEDIATE")

    try:
        source.execute("DELETE FROM issues")

        # Reset AUTOINCREMENT so the next incident starts at ID 1.
        sequence_exists = source.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'sqlite_sequence'
            """
        ).fetchone()

        if sequence_exists:
            source.execute(
                "DELETE FROM sqlite_sequence WHERE name = 'issues'"
            )

        source.commit()
    except Exception:
        source.rollback()
        raise

    # Clear accumulated WAL data and compact the database.
    source.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    source.execute("VACUUM")

    after_count = source.execute(
        "SELECT COUNT(*) FROM issues"
    ).fetchone()[0]

    print(f"Database: {db_path}")
    print(f"Backup:   {backup_path}")
    print(f"Deleted:  {before_count} incident(s)")
    print(f"Remaining incidents: {after_count}")
    print("Schema preserved; incident IDs reset.")
finally:
    source.close()
PY

echo "Monitor database cleanup completed."
