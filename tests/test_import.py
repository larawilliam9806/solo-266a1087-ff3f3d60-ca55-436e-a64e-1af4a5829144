"""Import, idempotency and attribution-matching tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import get_sales, import_sales, replace_table, sale, sales_by_serial


def test_import_without_region_table_is_pending(client: TestClient) -> None:
    result = import_sales(client, sale("S-1", "2024-01-15", "12.34"))
    assert result["inserted"] == 1
    assert result["pending"] == 1
    assert result["attributed"] == 0

    rows = sales_by_serial(client)
    assert rows["S-1"]["attribution_status"] == "pending"
    assert rows["S-1"]["region_code"] is None
    assert rows["S-1"]["amount"] == "12.34"
    assert rows["S-1"]["amount_cents"] == 1234


def test_import_matches_half_open_period(client: TestClient) -> None:
    # First table publication is itself a backfill task; complete it.
    task = replace_table(
        client,
        "S1",
        [
            {"region_code": "NORTH", "start_date": "2024-01-01", "end_date": "2024-02-01"},
            {"region_code": "SOUTH", "start_date": "2024-02-01", "end_date": "2024-03-01"},
        ],
    )
    from .conftest import advance

    assert advance(client, task["task_id"])["status"] == "completed"

    result = import_sales(
        client,
        sale("A", "2024-01-01"),   # start boundary inclusive
        sale("B", "2024-01-31"),   # last day before end
        sale("C", "2024-02-01"),   # end boundary exclusive -> SOUTH
        sale("D", "2024-02-29"),
        sale("E", "2024-03-01"),   # outside every window
        sale("F", "2023-12-31"),
    )
    assert result["attributed"] == 4
    assert result["pending"] == 2

    rows = sales_by_serial(client)
    assert rows["A"]["region_code"] == "NORTH"
    assert rows["B"]["region_code"] == "NORTH"
    assert rows["C"]["region_code"] == "SOUTH"
    assert rows["D"]["region_code"] == "SOUTH"
    assert rows["E"]["attribution_status"] == "pending"
    assert rows["F"]["attribution_status"] == "pending"


def test_duplicate_identical_is_noop(client: TestClient) -> None:
    first = import_sales(client, sale("DUP", "2024-01-10", "5.50"))
    assert first["inserted"] == 1

    second = import_sales(client, sale("DUP", "2024-01-10", "5.50"))
    assert second["inserted"] == 0
    assert second["duplicates"] == 1
    assert second["results"][0]["status"] == "duplicate"

    assert get_sales(client)["total"] == 1


def test_duplicate_conflicting_content_is_rejected(client: TestClient) -> None:
    import_sales(client, sale("CONFLICT", "2024-01-10", "5.50"))

    for changed in (
        sale("CONFLICT", "2024-01-11", "5.50"),
        sale("CONFLICT", "2024-01-10", "5.51"),
        sale("CONFLICT", "2024-01-10", "5.50", store_id="S2"),
    ):
        result = import_sales(client, changed)
        assert result["inserted"] == 0
        assert result["rejected"] == 1
        assert result["results"][0]["status"] == "rejected"

    rows = get_sales(client)["items"]
    assert len(rows) == 1
    assert rows[0]["store_id"] == "S1"
    assert rows[0]["amount"] == "5.50"


def test_mixed_batch_reports_per_row_outcome(client: TestClient) -> None:
    import_sales(client, sale("EXIST", "2024-01-10", "1.00"))
    result = import_sales(
        client,
        sale("EXIST", "2024-01-10", "1.00"),   # duplicate
        sale("EXIST", "2024-01-10", "2.00"),   # rejected
        sale("NEW", "2024-01-10", "3.00"),     # inserted
    )
    assert (result["duplicates"], result["rejected"], result["inserted"]) == (1, 1, 1)
    assert get_sales(client)["total"] == 2


def test_same_serial_in_another_store_is_rejected(client: TestClient) -> None:
    import_sales(client, sale("SHARED", "2024-01-10", "1.00", store_id="S1"))
    result = import_sales(client, sale("SHARED", "2024-01-10", "1.00", store_id="S2"))
    assert result["rejected"] == 1
    assert result["results"][0]["status"] == "rejected"

    row = client.get("/api/v1/sales/SHARED").json()
    assert row["store_id"] == "S1"


def test_amount_validation(client: TestClient) -> None:
    for bad_amount in ("1.001", "abc", "NaN", "Infinity"):
        response = client.post(
            "/api/v1/sales",
            json={"sales": [sale("BAD", "2024-01-10", bad_amount)]},
        )
        assert response.status_code == 422, bad_amount

    response = client.post(
        "/api/v1/sales",
        json={"sales": [sale("BAD", "2024/01/10", "1.00")]},
    )
    assert response.status_code == 422

    response = client.post("/api/v1/sales", json={"sales": []})
    assert response.status_code == 422


def test_region_table_rejects_overlap_and_bad_windows(client: TestClient) -> None:
    response = client.put(
        "/api/v1/stores/S1/region-table",
        json={
            "store_id": "S1",
            "periods": [
                {"region_code": "A", "start_date": "2024-01-01", "end_date": "2024-02-01"},
                {"region_code": "B", "start_date": "2024-01-31", "end_date": "2024-03-01"},
            ],
        },
    )
    assert response.status_code == 422
    assert "overlap" in response.text

    for periods in (
        [{"region_code": "A", "start_date": "2024-02-01", "end_date": "2024-02-01"}],
        [{"region_code": "A", "start_date": "2024-03-01", "end_date": "2024-02-01"}],
        [{"region_code": "A", "start_date": "2024-02-30", "end_date": "2024-03-01"}],
    ):
        response = client.put(
            "/api/v1/stores/S1/region-table",
            json={"store_id": "S1", "periods": periods},
        )
        assert response.status_code == 422, periods


def test_store_id_path_and_body_must_match(client: TestClient) -> None:
    response = client.put(
        "/api/v1/stores/S1/region-table",
        json={
            "store_id": "S2",
            "periods": [
                {"region_code": "A", "start_date": "2024-01-01", "end_date": "2024-02-01"}
            ],
        },
    )
    assert response.status_code == 422


def test_query_filters(client: TestClient) -> None:
    from .conftest import advance

    task = replace_table(
        client,
        "S1",
        [{"region_code": "A", "start_date": "2024-01-01", "end_date": "2024-02-01"}],
    )
    advance(client, task["task_id"])
    import_sales(
        client,
        sale("Q1", "2024-01-10", "1.00"),
        sale("Q2", "2024-03-10", "2.00"),
        sale("Q3", "2024-01-20", "3.00"),
    )

    assert get_sales(client, region_code="A")["total"] == 2
    assert get_sales(client, attribution_status="pending")["total"] == 1
    page = get_sales(client, limit=1, offset=1)
    assert page["total"] == 3
    assert len(page["items"]) == 1
    assert page["limit"] == 1 and page["offset"] == 1
    assert get_sales(client, business_date_from="2024-01-15")["total"] == 2
    assert get_sales(client, business_date_to="2024-01-15")["total"] == 1
