"""Database connections, schema and transaction helpers."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS stores (
    store_id   TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

-- A region table is one complete, immutable version of a store's region
-- validity periods.  Exactly one version per store may be active; the active
-- version is the one used for imports and for all read queries.
CREATE TABLE IF NOT EXISTS region_tables (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id     TEXT NOT NULL REFERENCES stores(store_id),
    is_active    INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1)),
    created_at   TEXT NOT NULL,
    active_since TEXT,
    CHECK (is_active = 0 OR active_since IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_region_tables_one_active
    ON region_tables(store_id) WHERE is_active = 1;

CREATE INDEX IF NOT EXISTS ix_region_tables_store
    ON region_tables(store_id, id);

-- Half-open windows [start_date, end_date).  The publishing service rejects
-- overlap before insert; SQLite has no native exclusion constraint.
CREATE TABLE IF NOT EXISTS region_periods (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    table_id    INTEGER NOT NULL REFERENCES region_tables(id) ON DELETE CASCADE,
    region_code TEXT NOT NULL,
    start_date  TEXT NOT NULL,
    end_date    TEXT NOT NULL,
    CHECK (start_date < end_date)
);

CREATE INDEX IF NOT EXISTS ix_region_periods_lookup
    ON region_periods(table_id, start_date, end_date);

-- serial_no is the idempotency key.  A repeated serial with identical
-- content is a no-op; the same serial with different content is rejected.
CREATE TABLE IF NOT EXISTS sales (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    serial_no            TEXT NOT NULL UNIQUE,
    store_id             TEXT NOT NULL REFERENCES stores(store_id),
    business_date        TEXT NOT NULL,
    amount_cents         INTEGER NOT NULL,
    -- Hash of serial/store/date/amount; used to tell an identical retry from
    -- a conflicting reuse of the same serial number.
    content_hash         TEXT NOT NULL,
    region_code          TEXT,
    attribution_status   TEXT NOT NULL CHECK (
        attribution_status IN ('attributed', 'pending')
    ),
    -- The region table version against which the current attribution was
    -- evaluated.  NULL when no region table existed at import time.
    attribution_table_id INTEGER REFERENCES region_tables(id),
    imported_at          TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    CHECK (
        (attribution_status = 'attributed' AND region_code IS NOT NULL)
        OR
        (attribution_status = 'pending' AND region_code IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS ix_sales_store_scan
    ON sales(store_id, id);
CREATE INDEX IF NOT EXISTS ix_sales_store_date
    ON sales(store_id, business_date);
CREATE INDEX IF NOT EXISTS ix_sales_pending
    ON sales(store_id) WHERE attribution_status = 'pending';

-- A replacement or first attribution task.  sales rows keep their published
-- attribution while the task scans; staged results live in backfill_items.
CREATE TABLE IF NOT EXISTS backfill_tasks (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id           TEXT NOT NULL REFERENCES stores(store_id),
    source_table_id    INTEGER REFERENCES region_tables(id),
    target_table_id    INTEGER NOT NULL REFERENCES region_tables(id),
    status             TEXT NOT NULL CHECK (
        status IN ('pending', 'running', 'ready', 'completed', 'failed')
    ),
    cursor_sales_id    INTEGER NOT NULL DEFAULT 0,
    total_sales        INTEGER NOT NULL DEFAULT 0,
    processed_sales    INTEGER NOT NULL DEFAULT 0,
    failure_count      INTEGER NOT NULL DEFAULT 0,
    last_error         TEXT,
    next_attempt_at    INTEGER,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    started_at         TEXT,
    finished_at        TEXT,
    CHECK (status <> 'completed' OR finished_at IS NOT NULL)
);

-- At most one unfinished task per store.
CREATE UNIQUE INDEX IF NOT EXISTS ux_backfill_one_unfinished_per_store
    ON backfill_tasks(store_id)
    WHERE status IN ('pending', 'running', 'ready');

CREATE INDEX IF NOT EXISTS ix_backfill_work
    ON backfill_tasks(status, next_attempt_at, id);

CREATE TABLE IF NOT EXISTS backfill_items (
    task_id            INTEGER NOT NULL REFERENCES backfill_tasks(id) ON DELETE CASCADE,
    sales_id           INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
    sales_updated_at   TEXT NOT NULL,
    region_code        TEXT,
    attribution_status TEXT NOT NULL CHECK (
        attribution_status IN ('attributed', 'pending')
    ),
    PRIMARY KEY (task_id, sales_id)
);

PRAGMA user_version = 1;
"""


def utc_now_iso() -> str:
    """UTC timestamp with millisecond precision, sortable as text."""
    return (
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    )


def connect(path: str) -> sqlite3.Connection:
    """Open a connection with the pragmas the service relies on."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    # check_same_thread=False: FastAPI may run a sync dependency's teardown on
    # a different threadpool worker than its setup.  Each connection is still
    # used by one request at a time.
    conn = sqlite3.connect(
        path, timeout=30.0, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a serialized write transaction.

    ``BEGIN IMMEDIATE`` takes the write lock up front, so checks made inside
    the transaction cannot race with another writer.  Any exception rolls the
    whole unit back, which is what keeps table/attribution switches atomic.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
