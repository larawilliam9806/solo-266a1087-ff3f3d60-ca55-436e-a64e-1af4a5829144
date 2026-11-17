"""Region-table publishing and resumable background backfill.

Lifecycle
---------
1. ``create_region_table`` validates the new (complete) validity periods,
   stores them as an inactive version, and creates a ``pending`` task with a
   per-store uniqueness guarantee.
2. The worker scans sales in ascending ``sales.id`` order, staging the
   attribution each sale would receive under the new table into
   ``backfill_items``.  Live ``sales`` rows are untouched, so imports and
   queries keep using the old attribution.
3. When no sale is left unscanned (including sales imported while the task
   runs), the task becomes ``ready``.
4. Commit re-checks that nothing new arrived, then in one SQLite
   ``BEGIN IMMEDIATE`` transaction rewrites every affected sale and flips the
   active region table.  A crash before ``COMMIT`` leaves both the old table
   and old attributions intact; the task resumes on restart.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from .db import utc_now_iso, write_tx
from .errors import ConflictError, NotFoundError, TaskInProgressError, ValidationError
from .models import NormalisedPeriod
from .sales import active_region_table_id, ensure_store, match_region


MAX_ATTEMPTS = 5


@dataclass
class StepResult:
    task_id: int
    status: str
    did_work: bool
    completed: bool = False


def _backoff_seconds(failure_count: int) -> int:
    return min(60, 2 ** max(1, failure_count))


def create_region_table(
    conn: sqlite3.Connection, store_id: str, periods: list[NormalisedPeriod]
) -> dict:
    """Publish a new complete region-validity table for ``store_id``.

    The table is not active until its backfill task commits, so callers keep
    seeing old attributions meanwhile.
    """
    if not periods:
        raise ValidationError("at least one region period is required")

    now = utc_now_iso()
    task: sqlite3.Row | None = None
    try:
        with write_tx(conn):
            ensure_store(conn, store_id)
            unfinished = conn.execute(
                """
                SELECT id FROM backfill_tasks
                WHERE store_id = ? AND status IN ('pending','running','ready')
                """,
                (store_id,),
            ).fetchone()
            if unfinished is not None:
                raise TaskInProgressError(
                    "store already has an unfinished backfill task",
                    detail={"store_id": store_id, "task_id": unfinished["id"]},
                )

            source_table_id = active_region_table_id(conn, store_id)
            cursor = conn.execute(
                "INSERT INTO region_tables(store_id, is_active, created_at) "
                "VALUES (?, 0, ?)",
                (store_id, now),
            )
            target_table_id = cursor.lastrowid
            conn.executemany(
                """
                INSERT INTO region_periods(table_id, region_code, start_date, end_date)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (target_table_id, p.region_code, p.start_date, p.end_date)
                    for p in periods
                ],
            )
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM sales WHERE store_id = ?", (store_id,)
            ).fetchone()["n"]
            task_cursor = conn.execute(
                """
                INSERT INTO backfill_tasks(
                    store_id, source_table_id, target_table_id, status,
                    cursor_sales_id, total_sales, processed_sales,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', 0, ?, 0, ?, ?)
                """,
                (store_id, source_table_id, target_table_id, total, now, now),
            )
            task_id = task_cursor.lastrowid
    except sqlite3.IntegrityError as exc:  # unique partial index race
        raise TaskInProgressError(
            "store already has an unfinished backfill task",
            detail={"store_id": store_id},
        ) from exc

    return {
        "store_id": store_id,
        "task_id": task_id,
        "status": "pending",
        "source_table_id": source_table_id,
        "target_table_id": target_table_id,
        "message": (
            "region table stored; backfill task queued. "
            "The table and attributions switch together when the task completes."
        ),
    }


def get_task(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM backfill_tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError("backfill task not found", detail={"task_id": task_id})
    return row


def task_to_dict(row: sqlite3.Row) -> dict:
    remaining = max(0, row["total_sales"] - row["processed_sales"])
    return {
        "task_id": row["id"],
        "store_id": row["store_id"],
        "source_table_id": row["source_table_id"],
        "target_table_id": row["target_table_id"],
        "status": row["status"],
        "cursor_sales_id": row["cursor_sales_id"],
        "total_sales": row["total_sales"],
        "processed_sales": row["processed_sales"],
        "remaining_sales": remaining,
        "failure_count": row["failure_count"],
        "last_error": row["last_error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


class RewindNeeded(Exception):
    """A scanned sale changed since staging; re-scan from just before it."""

    def __init__(self, sales_id: int) -> None:
        super().__init__(f"sales row {sales_id} changed during backfill")
        self.sales_id = sales_id


def _refresh_counts(conn: sqlite3.Connection, task_id: int, store_id: str) -> None:
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM sales WHERE store_id = ?", (store_id,)
    ).fetchone()["n"]
    processed = conn.execute(
        "SELECT COUNT(*) AS n FROM backfill_items WHERE task_id = ?", (task_id,)
    ).fetchone()["n"]
    conn.execute(
        "UPDATE backfill_tasks SET total_sales = ?, processed_sales = ? WHERE id = ?",
        (total, processed, task_id),
    )


def scan_batch(
    conn: sqlite3.Connection, task_id: int, batch_size: int
) -> StepResult:
    """Stage one batch of sales under the target table."""
    with write_tx(conn):
        task = conn.execute(
            "SELECT * FROM backfill_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if task is None:
            raise NotFoundError("backfill task not found", detail={"task_id": task_id})
        if task["status"] == "completed":
            return StepResult(task_id, "completed", did_work=False, completed=True)
        if task["status"] == "failed":
            return StepResult(task_id, "failed", did_work=False)
        if task["status"] == "ready":
            return StepResult(task_id, "ready", did_work=False)

        if task["status"] == "pending":
            conn.execute(
                "UPDATE backfill_tasks SET status = 'running', "
                "started_at = COALESCE(started_at, ?), updated_at = ? WHERE id = ?",
                (utc_now_iso(), utc_now_iso(), task_id),
            )

        rows = conn.execute(
            """
            SELECT id, business_date, updated_at FROM sales
            WHERE store_id = ? AND id > ?
            ORDER BY id
            LIMIT ?
            """,
            (task["store_id"], task["cursor_sales_id"], batch_size),
        ).fetchall()

        now = utc_now_iso()
        for row in rows:
            region = match_region(conn, task["target_table_id"], row["business_date"])
            status = "attributed" if region else "pending"
            conn.execute(
                """
                INSERT INTO backfill_items(
                    task_id, sales_id, sales_updated_at, region_code, attribution_status
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(task_id, sales_id) DO UPDATE SET
                    sales_updated_at = excluded.sales_updated_at,
                    region_code = excluded.region_code,
                    attribution_status = excluded.attribution_status
                """,
                (task_id, row["id"], row["updated_at"], region, status),
            )
        if rows:
            new_cursor = rows[-1]["id"]
            conn.execute(
                "UPDATE backfill_tasks SET cursor_sales_id = ?, updated_at = ?, "
                "failure_count = 0, next_attempt_at = NULL WHERE id = ?",
                (new_cursor, now, task_id),
            )
        _refresh_counts(conn, task_id, task["store_id"])

        remaining = conn.execute(
            """
            SELECT COUNT(*) AS n FROM sales
            WHERE store_id = ? AND id > ?
            """,
            (task["store_id"], rows[-1]["id"] if rows else task["cursor_sales_id"]),
        ).fetchone()["n"]

        if rows and remaining > 0:
            return StepResult(task_id, "running", did_work=True)

        # No sale beyond the cursor: safe to mark ready.  Commit re-checks
        # under the write lock before switching anything.
        conn.execute(
            "UPDATE backfill_tasks SET status = 'ready', updated_at = ? WHERE id = ?",
            (now, task_id),
        )
        return StepResult(task_id, "ready", did_work=bool(rows))


def commit_task(conn: sqlite3.Connection, task_id: int) -> StepResult:
    """Atomically apply staged attributions and activate the target table."""
    try:
        with write_tx(conn):
            task = conn.execute(
                "SELECT * FROM backfill_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise NotFoundError(
                    "backfill task not found", detail={"task_id": task_id}
                )
            if task["status"] == "completed":
                return StepResult(task_id, "completed", False, completed=True)
            if task["status"] != "ready":
                # Another worker may be scanning or a new sale reopened the
                # task between our scan and this commit: not an error.
                return StepResult(task_id, task["status"], False)

            max_id = conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS m FROM sales WHERE store_id = ?",
                (task["store_id"],),
            ).fetchone()["m"]
            if max_id > task["cursor_sales_id"]:
                conn.execute(
                    "UPDATE backfill_tasks SET status = 'running', updated_at = ? "
                    "WHERE id = ?",
                    (utc_now_iso(), task_id),
                )
                return StepResult(task_id, "running", False)

            mismatch = conn.execute(
                """
                SELECT b.sales_id
                FROM backfill_items b
                JOIN sales s ON s.id = b.sales_id
                WHERE b.task_id = ? AND s.updated_at <> b.sales_updated_at
                ORDER BY b.sales_id
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if mismatch is not None:
                raise RewindNeeded(mismatch["sales_id"])

            staged = conn.execute(
                "SELECT COUNT(*) AS n FROM backfill_items WHERE task_id = ?",
                (task_id,),
            ).fetchone()["n"]
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM sales WHERE store_id = ?",
                (task["store_id"],),
            ).fetchone()["n"]
            if staged != total:
                # A sale exists without a staged row (e.g. imported in a
                # window without the write lock): go back to scanning.
                conn.execute(
                    "UPDATE backfill_tasks SET status = 'running', updated_at = ? "
                    "WHERE id = ?",
                    (utc_now_iso(), task_id),
                )
                return StepResult(task_id, "running", False)

            now = utc_now_iso()
            conn.execute(
                """
                UPDATE sales
                SET region_code = (
                        SELECT region_code FROM backfill_items
                        WHERE task_id = ? AND sales_id = sales.id
                    ),
                    attribution_status = (
                        SELECT attribution_status FROM backfill_items
                        WHERE task_id = ? AND sales_id = sales.id
                    ),
                    attribution_table_id = ?,
                    updated_at = ?
                WHERE id IN (
                    SELECT sales_id FROM backfill_items WHERE task_id = ?
                )
                """,
                (task_id, task_id, task["target_table_id"], now, task_id),
            )
            conn.execute(
                "UPDATE region_tables SET is_active = 0 "
                "WHERE store_id = ? AND is_active = 1 AND id <> ?",
                (task["store_id"], task["target_table_id"]),
            )
            conn.execute(
                "UPDATE region_tables SET is_active = 1, active_since = ? "
                "WHERE id = ? AND store_id = ?",
                (now, task["target_table_id"], task["store_id"]),
            )
            conn.execute(
                """
                UPDATE backfill_tasks
                SET status = 'completed', processed_sales = total_sales,
                    updated_at = ?, finished_at = ?, last_error = NULL
                WHERE id = ?
                """,
                (now, now, task_id),
            )
            _refresh_counts(conn, task_id, task["store_id"])
    except RewindNeeded as exc:
        _rewind(conn, task_id, exc.sales_id)
        return StepResult(task_id, "running", did_work=True)
    return StepResult(task_id, "completed", did_work=True, completed=True)


def _rewind(conn: sqlite3.Connection, task_id: int, sales_id: int) -> None:
    """Drop staged rows from a changed sale onwards and re-scan it."""
    with write_tx(conn):
        task = conn.execute(
            "SELECT store_id FROM backfill_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if task is None:
            return
        conn.execute(
            "DELETE FROM backfill_items WHERE task_id = ? AND sales_id >= ?",
            (task_id, sales_id),
        )
        conn.execute(
            "UPDATE backfill_tasks SET status = 'running', cursor_sales_id = ?, "
            "updated_at = ? WHERE id = ?",
            (max(0, sales_id - 1), utc_now_iso(), task_id),
        )
        _refresh_counts(conn, task_id, task["store_id"])


def record_failure(conn: sqlite3.Connection, task_id: int, error: str) -> None:
    with write_tx(conn):
        task = conn.execute(
            "SELECT failure_count FROM backfill_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if task is None:
            return
        count = task["failure_count"] + 1
        now = utc_now_iso()
        if count >= MAX_ATTEMPTS:
            conn.execute(
                "UPDATE backfill_tasks SET status = 'failed', failure_count = ?, "
                "last_error = ?, updated_at = ?, finished_at = ? WHERE id = ?",
                (count, error, now, now, task_id),
            )
        else:
            conn.execute(
                "UPDATE backfill_tasks SET failure_count = ?, last_error = ?, "
                "next_attempt_at = ?, updated_at = ? WHERE id = ?",
                (count, error, int(time.time()) + _backoff_seconds(count), now, task_id),
            )


def retry_task(conn: sqlite3.Connection, task_id: int) -> dict:
    try:
        with write_tx(conn):
            task = conn.execute(
                "SELECT * FROM backfill_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise NotFoundError(
                    "backfill task not found", detail={"task_id": task_id}
                )
            if task["status"] != "failed":
                raise ConflictError(
                    "only failed tasks can be retried",
                    detail={"status": task["status"]},
                )
            newer = conn.execute(
                "SELECT id FROM backfill_tasks WHERE store_id = ? AND id > ? "
                "ORDER BY id LIMIT 1",
                (task["store_id"], task_id),
            ).fetchone()
            if newer is not None:
                raise ConflictError(
                    "a newer backfill task exists; retrying this one would "
                    "roll attributions back to an outdated region table",
                    detail={"task_id": task_id, "newer_task_id": newer["id"]},
                )
            conn.execute(
                "UPDATE backfill_tasks SET status = 'pending', failure_count = 0, "
                "last_error = NULL, next_attempt_at = NULL, finished_at = NULL, "
                "updated_at = ? WHERE id = ?",
                (utc_now_iso(), task_id),
            )
    except sqlite3.IntegrityError as exc:
        raise TaskInProgressError(
            "another unfinished backfill task exists for this store",
            detail={"store_id": task["store_id"] if task is not None else None},
        ) from exc
    return task_to_dict(get_task(conn, task_id))
