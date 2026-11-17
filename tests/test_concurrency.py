"""Race-oriented tests: scans vs. concurrent imports and concurrent committers."""

from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from app import backfill
from app.db import connect

from .conftest import (
    advance,
    import_sales,
    replace_table,
    sale,
    sales_by_serial,
)

PERIODS_V1 = [
    {"region_code": "N", "start_date": "2024-01-01", "end_date": "2024-04-01"},
    {"region_code": "S", "start_date": "2024-04-01", "end_date": "2024-07-01"},
]
PERIODS_V2 = [
    {"region_code": "N2", "start_date": "2024-01-01", "end_date": "2024-04-01"},
    {"region_code": "S2", "start_date": "2024-04-01", "end_date": "2024-08-01"},
]


def seed(client: TestClient) -> None:
    task = replace_table(client, "S1", PERIODS_V1)
    advance(client, task["task_id"])
    import_sales(
        client,
        *[sale(f"R-{i:03d}", f"2024-0{1 + i % 6}-1{i % 9}") for i in range(40)],
    )


def test_sales_imported_mid_scan_are_staged_before_commit(client: TestClient) -> None:
    seed(client)
    task = replace_table(client, "S1", PERIODS_V2)

    # Interleave imports and advances; every batch restarts from the cursor.
    for i in range(5):
        import_sales(client, sale(f"MID-{i}", "2024-05-05", "1.00"))
        state = advance(client, task["task_id"], max_batches=1)
        assert state["status"] in {"running", "ready", "completed"}

    done = advance(client, task["task_id"])
    assert done["status"] == "completed"
    # total/processed must agree and cover the sales imported during the run.
    assert done["total_sales"] == done["processed_sales"] == 45
    rows = sales_by_serial(client)
    assert len(rows) == 45
    assert all(row["attribution_table_id"] == task["target_table_id"] for row in rows.values())
    assert rows["MID-0"]["region_code"] == "S2"
    assert rows["R-000"]["region_code"] == "N2"


def test_scan_restarts_when_it_reaches_a_newly_imported_sale(
    client: TestClient, db_path
) -> None:
    """A sale imported after the scan cursor must still be staged."""
    seed(client)
    task = replace_table(client, "S1", PERIODS_V2)
    conn = connect(str(db_path))
    try:
        # Scan everything currently present -> ready.
        assert backfill.scan_batch(conn, task["task_id"], 1000).status == "ready"
        # Import arrives between the final scan and commit.
        import_sales(client, sale("LATE", "2024-05-05", "3.00"))

        # Commit notices the new id, refuses to switch, and reopens scanning.
        result = backfill.commit_task(conn, task["task_id"])
        assert result.status == "running"

        assert backfill.scan_batch(conn, task["task_id"], 1000).status == "ready"
        assert backfill.commit_task(conn, task["task_id"]).status == "completed"
    finally:
        conn.close()

    rows = sales_by_serial(client)
    assert rows["LATE"]["region_code"] == "S2"
    assert rows["LATE"]["attribution_table_id"] == task["target_table_id"]


def test_concurrent_scan_and_commit_from_two_connections(client: TestClient, db_path) -> None:
    seed(client)
    task = replace_table(client, "S1", PERIODS_V2)

    errors: list[BaseException] = []

    def scan() -> None:
        conn = connect(str(db_path))
        try:
            for _ in range(50):
                if backfill.scan_batch(conn, task["task_id"], 3).status != "running":
                    break
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)
        finally:
            conn.close()

    def commit() -> None:
        conn = connect(str(db_path))
        try:
            for _ in range(500):
                state = backfill.get_task(conn, task["task_id"])
                if state["status"] == "completed":
                    return
                if state["status"] == "ready":
                    backfill.commit_task(conn, task["task_id"])
                else:
                    time.sleep(0.002)
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=scan), threading.Thread(target=commit)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors
    assert backfill.get_task(connect(str(db_path)), task["task_id"])["status"] == (
        "completed"
    )
    rows = sales_by_serial(client)
    assert all(row["region_code"].endswith("2") for row in rows.values())


def test_double_publish_race_yields_single_task(client: TestClient) -> None:
    """Two publish requests racing on the same store: exactly one wins."""
    import_sales(client, sale("A", "2024-01-05"))
    results: list[int] = []
    lock = threading.Lock()

    def publish() -> None:
        response = client.put(
            "/api/v1/stores/S1/region-table",
            json={"store_id": "S1", "periods": PERIODS_V1},
        )
        with lock:
            results.append(response.status_code)

    threads = [threading.Thread(target=publish) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert sorted(results) == [202, 409]
    tasks = client.get("/api/v1/stores/S1/backfill-tasks").json()
    assert len(tasks) == 1
