"""Shared fixtures."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api import create_app  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import connect, init_db  # noqa: E402


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "retail.db"


@pytest.fixture()
def settings(db_path: Path) -> Settings:
    return Settings(
        database_path=str(db_path),
        backfill_batch_size=2,
        worker_enabled=False,
    )


@pytest.fixture()
def client(settings: Settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture()
def conn(db_path: Path):
    connection = connect(str(db_path))
    init_db(connection)
    yield connection
    connection.close()


def import_sales(client: TestClient, *sales: dict) -> dict:
    response = client.post("/api/v1/sales", json={"sales": list(sales)})
    assert response.status_code == 201, response.text
    return response.json()


def sale(serial_no: str, business_date: str, amount: str = "10.00", store_id: str = "S1") -> dict:
    return {
        "serial_no": serial_no,
        "store_id": store_id,
        "business_date": business_date,
        "amount": amount,
    }


def replace_table(client: TestClient, store_id: str, periods: list[dict]) -> dict:
    response = client.put(
        f"/api/v1/stores/{store_id}/region-table",
        json={"store_id": store_id, "periods": periods},
    )
    assert response.status_code == 202, response.text
    return response.json()


def advance(client: TestClient, task_id: int, max_batches: int = 1000) -> dict:
    response = client.post(
        f"/api/v1/backfill-tasks/{task_id}/advance",
        params={"max_batches": max_batches},
    )
    assert response.status_code == 200, response.text
    return response.json()


def get_sales(client: TestClient, **params) -> dict:
    response = client.get("/api/v1/sales", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def sales_by_serial(client: TestClient) -> dict[str, dict]:
    return {item["serial_no"]: item for item in get_sales(client, limit=500)["items"]}


__all__ = [
    "connect",
    "init_db",
    "sqlite3",
    "import_sales",
    "sale",
    "replace_table",
    "advance",
    "get_sales",
    "sales_by_serial",
]
