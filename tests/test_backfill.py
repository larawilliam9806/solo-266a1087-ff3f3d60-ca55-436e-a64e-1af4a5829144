"""Region-table replacement, isolation and atomic-switch tests."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import backfill
from app.config import Settings
from app.db import connect

from .conftest import (
    advance,
    import_sales,
    replace_table,
    sale,
    sales_by_serial,
)

NORTH = {"region_code": "NORTH", "start_date": "2024-01-01", "end_date": "2024-03-01"}
SOUTH = {"region_code": "SOUTH", "start_date": "2024-03-01", "end_date": "2024-06-01"}
NEW_NORTH = {"region_code": "NORTH-B", "start_date": "2024-01-01", "end_date": "2024-03-01"}
NEW_SOUTH = {"region_code": "SOUTH-B", "start_date": "2024-03-01", "end_date": "2024-06-01"}


def seed(client: TestClient, count: int = 5) -> dict:
    task = replace_table(client, "S1", [NORTH, SOUTH])
    assert advance(client, task["task_id"])["status"] == "completed"
    import_sales(
        client,
        *[sale(f"S-{i:03d}", f"2024-01-{i + 1:02d}", "10.00") for i in range(count)],
        *[sale(f"S-1{i:02d}", f"2024-04-{i + 1:02d}", "20.00") for i in range(count)],
        sale("OUTSIDE", "2023-12-01", "5.00"),
    )
    return sales_by_serial(client)


def test_single_unfinished_task_per_store(client: TestClient) -> None:
    first = replace_table(client, "S1", [NORTH])
    second = client.put(
        "/api/v1/stores/S1/region-table",
        json={"store_id": "S1", "periods": [SOUTH]},
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "backfill_in_progress"
    assert second.json()["error"]["detail"]["task_id"] == first["task_id"]

    # A different store is unaffected by S1's unfinished task.
    other = client.put(
        "/api/v1/stores/S2/region-table",
        json={"store_id": "S2", "periods": [NORTH]},
    )
    assert other.status_code == 202


def test_replacement_is_isolated_until_commit(client: TestClient) -> None:
    seed(client)
    before = sales_by_serial(client)
    assert before["S-000"]["region_code"] == "NORTH"

    task = replace_table(client, "S1", [NEW_NORTH, NEW_SOUTH])
    assert task["status"] == "pending"

    # Task not yet run: old table is still active, attributions unchanged.
    active = client.get("/api/v1/stores/S1/region-table").json()
    assert active["table_id"] == task["source_table_id"]
    assert sales_by_serial(client) == before

    # Run one batch only (batch size 1) -> still old attribution visible.
    result = advance(client, task["task_id"], max_batches=1)
    assert result["status"] == "running"
    assert sales_by_serial(client) == before
    active = client.get("/api/v1/stores/S1/region-table").json()
    assert active["table_id"] == task["source_table_id"]

    # Imports during the backfill also use the old table.
    import_sales(client, sale("MID", "2024-01-15", "1.00"))
    mid = sales_by_serial(client)["MID"]
    assert mid["region_code"] == "NORTH"
    assert mid["attribution_table_id"] == task["source_table_id"]

    # New sales get attributed by the backfill before it can commit.
    final = advance(client, task["task_id"])
    assert final["status"] == "completed"
    assert final["processed_sales"] == final["total_sales"] == 12

    after = sales_by_serial(client)
    assert after["S-000"]["region_code"] == "NORTH-B"
    assert after["S-100"]["region_code"] == "SOUTH-B"
    assert after["MID"]["region_code"] == "NORTH-B"
    assert after["OUTSIDE"]["attribution_status"] == "pending"
    assert after["OUTSIDE"]["attribution_table_id"] == task["target_table_id"]

    active = client.get("/api/v1/stores/S1/region-table").json()
    assert active["table_id"] == task["target_table_id"]
    assert active["active_since"] is not None
    assert all(p["region_code"].endswith("-B") for p in active["periods"])


def test_pending_sales_are_rematched_by_new_table(client: TestClient) -> None:
    seed(client)
    assert sales_by_serial(client)["OUTSIDE"]["attribution_status"] == "pending"

    # Extend coverage backwards so the previously unmatched sale matches.
    task = replace_table(
        client,
        "S1",
        [
            {"region_code": "HISTORY", "start_date": "2023-01-01", "end_date": "2024-01-01"},
            NORTH,
            SOUTH,
        ],
    )
    advance(client, task["task_id"])
    assert sales_by_serial(client)["OUTSIDE"]["region_code"] == "HISTORY"

    # And a table that drops coverage puts an attributed sale back to pending.
    task2 = replace_table(client, "S1", [NORTH])
    advance(client, task2["task_id"])
    rows = sales_by_serial(client)
    assert rows["S-100"]["attribution_status"] == "pending"
    assert rows["S-100"]["region_code"] is None
    assert rows["S-000"]["region_code"] == "NORTH"


def test_empty_store_table_switch(client: TestClient) -> None:
    task = replace_table(client, "EMPTY", [NORTH])
    done = advance(client, task["task_id"])
    assert done["status"] == "completed"
    assert done["total_sales"] == 0
    assert client.get("/api/v1/stores/EMPTY/region-table").json()["table_id"] == task[
        "target_table_id"
    ]


def test_task_progress_and_history_endpoints(client: TestClient) -> None:
    task = replace_table(client, "S1", [NORTH])
    progress = client.get(f"/api/v1/backfill-tasks/{task['task_id']}").json()
    assert progress["store_id"] == "S1"
    assert progress["processed_sales"] == 0

    advance(client, task["task_id"], max_batches=1)
    history = client.get("/api/v1/stores/S1/backfill-tasks").json()
    assert [t["task_id"] for t in history] == [task["task_id"]]

    missing = client.get("/api/v1/backfill-tasks/999")
    assert missing.status_code == 404


def test_commit_is_atomic_under_simulated_failure(
    client: TestClient, db_path, monkeypatch
) -> None:
    seed(client)
    first = replace_table(client, "S1", [NEW_NORTH, NEW_SOUTH])
    assert advance(client, first["task_id"])["status"] == "completed"

    second = replace_table(client, "S1", [NORTH, SOUTH])
    assert advance(client, second["task_id"])["status"] == "completed"
    before_failure = sales_by_serial(client)
    assert before_failure["S-000"]["region_code"] == "NORTH"

    third = replace_table(client, "S1", [NEW_NORTH])
    conn = connect(str(db_path))
    try:
        assert backfill.scan_batch(conn, third["task_id"], 500).status == "ready"

        # Fail at the very end of the commit transaction, after sales rows and
        # the active-table flag have already been written.
        def boom(*args, **kwargs):
            raise RuntimeError("injected commit failure")

        monkeypatch.setattr(backfill, "_refresh_counts", boom)
        with pytest.raises(RuntimeError, match="injected commit failure"):
            backfill.commit_task(conn, third["task_id"])
        monkeypatch.undo()

        # Everything rolled back: old table active, old attributions intact.
        active = client.get("/api/v1/stores/S1/region-table").json()
        assert active["table_id"] == second["target_table_id"]
        assert sales_by_serial(client) == before_failure
        state = client.get(f"/api/v1/backfill-tasks/{third['task_id']}").json()
        assert state["status"] == "ready"

        # Staged work survived; the task can be committed after the failure.
        assert backfill.commit_task(conn, third["task_id"]).status == "completed"
    finally:
        conn.close()

    rows = sales_by_serial(client)
    assert rows["S-000"]["region_code"] == "NORTH-B"
    assert rows["S-100"]["attribution_status"] == "pending"
    assert client.get("/api/v1/stores/S1/region-table").json()["table_id"] == third[
        "target_table_id"
    ]


def test_backfill_resumes_after_restart(tmp_path, settings: Settings) -> None:
    from app.api import create_app

    with TestClient(create_app(settings)) as first_client:
        seed(first_client)
        original = sales_by_serial(first_client)
        task = replace_table(first_client, "S1", [NEW_NORTH, NEW_SOUTH])
        advance(first_client, task["task_id"], max_batches=1)
        progress = first_client.get(
            f"/api/v1/backfill-tasks/{task['task_id']}"
        ).json()
        assert progress["processed_sales"] > 0

    # A fresh process (new app + connections) must pick up the persisted cursor.
    with TestClient(create_app(settings)) as second_client:
        resumed = second_client.get(f"/api/v1/backfill-tasks/{task['task_id']}").json()
        assert resumed["processed_sales"] == progress["processed_sales"]

        # Old attribution is still visible before the resumed run finishes.
        assert sales_by_serial(second_client) == original
        done = advance(second_client, task["task_id"])
        assert done["status"] == "completed"
        assert sales_by_serial(second_client)["S-000"]["region_code"] == "NORTH-B"


def test_worker_completes_task_without_manual_advance(tmp_path) -> None:
    from app.api import create_app

    settings = Settings(
        database_path=str(tmp_path / "w.db"),
        backfill_batch_size=2,
        worker_enabled=True,
        worker_poll_interval=0.02,
    )
    with TestClient(create_app(settings)) as client:
        task = replace_table(client, "S1", [NORTH])
        import_sales(
            client,
            *[sale(f"W-{i}", f"2024-01-{i + 1:02d}") for i in range(1, 6)],
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = client.get(f"/api/v1/backfill-tasks/{task['task_id']}").json()
            if state["status"] == "completed":
                break
            time.sleep(0.05)
        assert state["status"] == "completed"
        assert sales_by_serial(client)["W-1"]["region_code"] == "NORTH"


def test_retry_endpoint_only_for_failed_tasks(client: TestClient) -> None:
    task = replace_table(client, "S1", [NORTH])
    response = client.post(f"/api/v1/backfill-tasks/{task['task_id']}/retry")
    assert response.status_code == 409


def test_failed_task_retains_old_results_and_can_retry(
    client: TestClient, db_path
) -> None:
    seed(client)
    before = sales_by_serial(client)
    task = replace_table(client, "S1", [NEW_NORTH, NEW_SOUTH])

    conn = connect(str(db_path))
    for _ in range(backfill.MAX_ATTEMPTS):
        backfill.record_failure(conn, task["task_id"], "simulated")
    conn.close()

    state = client.get(f"/api/v1/backfill-tasks/{task['task_id']}").json()
    assert state["status"] == "failed"
    assert state["last_error"] == "simulated"
    assert state["finished_at"] is not None
    assert sales_by_serial(client) == before

    retried = client.post(f"/api/v1/backfill-tasks/{task['task_id']}/retry")
    assert retried.status_code == 200
    assert retried.json()["status"] == "pending"
    assert retried.json()["finished_at"] is None
    assert retried.json()["last_error"] is None

    done = advance(client, task["task_id"])
    assert done["status"] == "completed"
    assert sales_by_serial(client)["S-000"]["region_code"] == "NORTH-B"


def test_failed_task_does_not_block_new_task_and_cannot_roll_back(
    client: TestClient, db_path
) -> None:
    seed(client)
    old = replace_table(client, "S1", [NEW_NORTH, NEW_SOUTH])
    conn = connect(str(db_path))
    for _ in range(backfill.MAX_ATTEMPTS):
        backfill.record_failure(conn, old["task_id"], "boom")
    conn.close()

    # A failed task is not "unfinished", so a new replacement may start.
    newer = replace_table(client, "S1", [NORTH])
    assert advance(client, newer["task_id"], max_batches=1)["status"] == "running"

    # Retrying the older task would roll attributions back; it must be refused.
    blocked = client.post(f"/api/v1/backfill-tasks/{old['task_id']}/retry")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["detail"]["newer_task_id"] == newer["task_id"]

    assert advance(client, newer["task_id"])["status"] == "completed"
    assert sales_by_serial(client)["S-000"]["region_code"] == "NORTH"
