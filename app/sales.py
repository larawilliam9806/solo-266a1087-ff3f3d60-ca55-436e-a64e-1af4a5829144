"""Store, region-table and sales persistence plus attribution logic."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field

from .db import utc_now_iso, write_tx
from .models import NormalisedSale


@dataclass
class ImportOutcome:
    inserted: int = 0
    attributed: int = 0
    pending: int = 0
    duplicates: int = 0
    rejected: int = 0
    results: list[dict] = field(default_factory=list)


def content_hash(sale: NormalisedSale) -> str:
    """Stable hash of the business content of a sale.

    ``imported_at`` and attribution are deliberately excluded: re-sending a
    serial with the same business content must be a no-op even after a region
    table switch changed its attribution.
    """
    payload = json.dumps(
        {
            "serial_no": sale.serial_no,
            "store_id": sale.store_id,
            "business_date": sale.business_date,
            "amount_cents": sale.amount_cents,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ensure_store(conn: sqlite3.Connection, store_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO stores(store_id, created_at) VALUES (?, ?)",
        (store_id, utc_now_iso()),
    )


def active_region_table_id(conn: sqlite3.Connection, store_id: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM region_tables WHERE store_id = ? AND is_active = 1",
        (store_id,),
    ).fetchone()
    return row["id"] if row else None


def match_region(
    conn: sqlite3.Connection, table_id: int, business_date: str
) -> str | None:
    """Return the region whose half-open window contains ``business_date``."""
    row = conn.execute(
        """
        SELECT region_code FROM region_periods
        WHERE table_id = ?
          AND start_date <= ?
          AND ? < end_date
        ORDER BY start_date DESC
        LIMIT 1
        """,
        (table_id, business_date, business_date),
    ).fetchone()
    return row["region_code"] if row else None


def import_sales(conn: sqlite3.Connection, sales: list[NormalisedSale]) -> ImportOutcome:
    outcome = ImportOutcome()
    now = utc_now_iso()

    with write_tx(conn):
        # All-or-nothing on write errors, but per-row validation conflicts are
        # reported without aborting the rest of the batch.  We do it inside a
        # single transaction so the duplicate checks are race-free.
        for sale in sales:
            ensure_store(conn, sale.store_id)
            digest = content_hash(sale)
            existing = conn.execute(
                "SELECT id, content_hash FROM sales WHERE serial_no = ?",
                (sale.serial_no,),
            ).fetchone()
            if existing is not None:
                if existing["content_hash"] == digest:
                    outcome.duplicates += 1
                    outcome.results.append(
                        {
                            "serial_no": sale.serial_no,
                            "status": "duplicate",
                            "region_code": None,
                            "message": "identical sale already imported",
                        }
                    )
                else:
                    outcome.rejected += 1
                    outcome.results.append(
                        {
                            "serial_no": sale.serial_no,
                            "status": "rejected",
                            "region_code": None,
                            "message": "serial_no already exists with different content",
                        }
                    )
                continue

            table_id = active_region_table_id(conn, sale.store_id)
            region = (
                match_region(conn, table_id, sale.business_date) if table_id else None
            )
            status = "attributed" if region else "pending"
            conn.execute(
                """
                INSERT INTO sales(
                    serial_no, store_id, business_date, amount_cents, content_hash,
                    region_code, attribution_status, attribution_table_id,
                    imported_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sale.serial_no,
                    sale.store_id,
                    sale.business_date,
                    sale.amount_cents,
                    digest,
                    region,
                    status,
                    table_id,
                    now,
                    now,
                ),
            )
            outcome.inserted += 1
            if region:
                outcome.attributed += 1
            else:
                outcome.pending += 1
            outcome.results.append(
                {
                    "serial_no": sale.serial_no,
                    "status": (
                        "inserted_attributed" if region else "inserted_pending"
                    ),
                    "region_code": region,
                    "message": None,
                }
            )
    return outcome
