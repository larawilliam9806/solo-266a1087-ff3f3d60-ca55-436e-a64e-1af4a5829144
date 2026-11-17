"""Pydantic request/response models.

Amounts are accepted as decimal strings or numbers and echoed back in both
decimal and integer-cent form so downstream revenue consumers can reconcile
without floating-point ambiguity.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

from .dates import format_cents, parse_amount_cents, parse_business_date
from .errors import ValidationError


NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
RegionCode = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class SaleIn(BaseModel):
    serial_no: NonEmptyStr = Field(description="Globally unique transaction serial number")
    store_id: NonEmptyStr
    business_date: str = Field(description="ISO business date YYYY-MM-DD")
    amount: str | int | float = Field(
        description="Sale amount as decimal (e.g. \"12.34\") or number; max 2 decimal places"
    )

    def normalised(self) -> "NormalisedSale":
        business_date = parse_business_date(self.business_date)
        cents = parse_amount_cents(self.amount)
        return NormalisedSale(
            serial_no=self.serial_no,
            store_id=self.store_id,
            business_date=business_date.isoformat(),
            amount_cents=cents,
        )


class NormalisedSale(BaseModel):
    serial_no: str
    store_id: str
    business_date: str
    amount_cents: int


class RegionPeriodIn(BaseModel):
    region_code: RegionCode
    start_date: str = Field(description="Inclusive start date YYYY-MM-DD")
    end_date: str = Field(description="Exclusive end date YYYY-MM-DD")

    def normalised(self) -> "NormalisedPeriod":
        start = parse_business_date(self.start_date)
        end = parse_business_date(self.end_date)
        if start >= end:
            raise ValidationError(
                "region period start_date must be strictly before end_date",
                detail={"start_date": self.start_date, "end_date": self.end_date},
            )
        return NormalisedPeriod(
            region_code=self.region_code,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
        )


class NormalisedPeriod(BaseModel):
    region_code: str
    start_date: str
    end_date: str


class SalesImportRequest(BaseModel):
    sales: list[SaleIn] = Field(min_length=1, max_length=1000)


class SalesImportResult(BaseModel):
    inserted: int
    attributed: int
    pending: int
    duplicates: int
    rejected: int
    results: list["SaleImportItem"]


class SaleImportItem(BaseModel):
    serial_no: str
    status: Literal["inserted_attributed", "inserted_pending", "duplicate", "rejected"]
    region_code: str | None = None
    message: str | None = None


class RegionTableReplaceRequest(BaseModel):
    store_id: NonEmptyStr
    periods: list[RegionPeriodIn] = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def _check_overlaps(self) -> "RegionTableReplaceRequest":
        periods = sorted(
            (p.normalised() for p in self.periods),
            key=lambda p: (p.start_date, p.end_date),
        )
        for previous, current in zip(periods, periods[1:]):
            if current.start_date < previous.end_date:
                raise ValueError(
                    "region periods must not overlap: "
                    f"{previous.region_code} [{previous.start_date}, {previous.end_date}) "
                    f"overlaps {current.region_code} [{current.start_date}, {current.end_date})"
                )
        return self

    def normalised(self) -> list[NormalisedPeriod]:
        return sorted(
            (p.normalised() for p in self.periods),
            key=lambda p: p.start_date,
        )


class RegionPeriodOut(BaseModel):
    region_code: str
    start_date: str
    end_date: str


class RegionTableOut(BaseModel):
    table_id: int
    store_id: str
    is_active: bool
    created_at: str
    active_since: str | None
    periods: list[RegionPeriodOut]


class BackfillTaskOut(BaseModel):
    task_id: int
    store_id: str
    source_table_id: int | None
    target_table_id: int
    status: Literal["pending", "running", "ready", "completed", "failed"]
    cursor_sales_id: int
    total_sales: int
    processed_sales: int
    remaining_sales: int
    failure_count: int
    last_error: str | None
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None


class RegionTableReplaceResponse(BaseModel):
    store_id: str
    task_id: int
    status: Literal["pending", "running", "ready", "completed"]
    source_table_id: int | None
    target_table_id: int
    message: str


class SaleOut(BaseModel):
    serial_no: str
    store_id: str
    business_date: str
    amount: str
    amount_cents: int
    region_code: str | None
    attribution_status: Literal["attributed", "pending"]
    attribution_table_id: int | None
    imported_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row) -> "SaleOut":
        return cls(
            serial_no=row["serial_no"],
            store_id=row["store_id"],
            business_date=row["business_date"],
            amount=format_cents(row["amount_cents"]),
            amount_cents=row["amount_cents"],
            region_code=row["region_code"],
            attribution_status=row["attribution_status"],
            attribution_table_id=row["attribution_table_id"],
            imported_at=row["imported_at"],
            updated_at=row["updated_at"],
        )


class SaleQueryResponse(BaseModel):
    items: list[SaleOut]
    total: int
    limit: int
    offset: int


class HealthResponse(BaseModel):
    status: Literal["ok"]
    database: str


SalesImportResult.model_rebuild()
