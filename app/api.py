"""FastAPI application and HTTP endpoints."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import asynccontextmanager
from typing import Iterator, Literal

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import backfill
from .config import Settings
from .dates import parse_business_date
from .db import connect, init_db
from .errors import NotFoundError, ServiceError, ValidationError
from .models import (
    BackfillTaskOut,
    HealthResponse,
    RegionPeriodOut,
    RegionTableOut,
    RegionTableReplaceRequest,
    RegionTableReplaceResponse,
    SaleOut,
    SaleQueryResponse,
    SalesImportRequest,
    SalesImportResult,
)
from .sales import import_sales
from .worker import Worker

logger = logging.getLogger("retail.api")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = connect(settings.database_path)
        try:
            init_db(conn)
        finally:
            conn.close()
        worker: Worker | None = None
        if settings.worker_enabled:
            worker = Worker(settings)
            worker.start()
        app.state.worker = worker
        try:
            yield
        finally:
            if worker is not None:
                worker.stop()
                worker.join(timeout=10)

    app = FastAPI(
        title="零售销售区域归属 API",
        version="1.0.0",
        description=(
            "销售明细导入、按营业日匹配区域、区域有效期表版本切换与可恢复回填。\n\n"
            "金额以分为单位精确存储。区域有效期左闭右开。"
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings

    def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
        conn = connect(request.app.state.settings.database_path)
        try:
            yield conn
        finally:
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover - teardown best effort
                logger.exception("failed to close request connection")

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "detail": exc.detail,
                }
            },
        )

    @app.exception_handler(sqlite3.Error)
    async def sqlite_error_handler(request: Request, exc: sqlite3.Error):
        logger.exception("database error")
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "database_error",
                    "message": "database operation failed",
                    "detail": {},
                }
            },
        )

    @app.get("/health", response_model=HealthResponse, tags=["ops"])
    def health(conn: sqlite3.Connection = Depends(get_conn)):
        conn.execute("SELECT 1").fetchone()
        return {"status": "ok", "database": settings.database_path}

    @app.post(
        "/api/v1/sales",
        response_model=SalesImportResult,
        status_code=201,
        tags=["sales"],
        summary="导入销售明细",
        description=(
            "按流水号幂等导入。内容相同视为重复且不重复入账；同一流水号内容不同则拒绝该条。"
            "逐条结果在 results 中返回，混批中其它行照常入账。"
            "入库时按门店当前生效的区域有效期表匹配营业日，无匹配则为待归属。"
        ),
    )
    def import_sales_endpoint(
        payload: SalesImportRequest,
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        sales = [sale.normalised() for sale in payload.sales]
        outcome = import_sales(conn, sales)
        return {
            "inserted": outcome.inserted,
            "attributed": outcome.attributed,
            "pending": outcome.pending,
            "duplicates": outcome.duplicates,
            "rejected": outcome.rejected,
            "results": outcome.results,
        }

    @app.get(
        "/api/v1/sales",
        response_model=SaleQueryResponse,
        tags=["sales"],
        summary="查询销售归属",
        description=(
            "返回销售当前已发布的区域归属。回填任务进行中时仍返回旧归属；"
            "任务提交后统一返回新归属。"
        ),
    )
    def query_sales(
        store_id: str | None = Query(default=None),
        region_code: str | None = Query(default=None),
        business_date_from: str | None = Query(default=None),
        business_date_to: str | None = Query(default=None),
        attribution_status: Literal["attributed", "pending"] | None = Query(
            default=None
        ),
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        if business_date_from is not None:
            parse_business_date(business_date_from)
        if business_date_to is not None:
            parse_business_date(business_date_to)
        clauses: list[str] = []
        params: list[object] = []
        if store_id is not None:
            clauses.append("store_id = ?")
            params.append(store_id)
        if region_code is not None:
            clauses.append("region_code = ?")
            params.append(region_code)
        if business_date_from is not None:
            clauses.append("business_date >= ?")
            params.append(business_date_from)
        if business_date_to is not None:
            clauses.append("business_date <= ?")
            params.append(business_date_to)
        if attribution_status is not None:
            clauses.append("attribution_status = ?")
            params.append(attribution_status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM sales {where}", params
        ).fetchone()["n"]
        rows = conn.execute(
            f"SELECT * FROM sales {where} ORDER BY id LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return {
            "items": [SaleOut.from_row(row).model_dump() for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @app.get(
        "/api/v1/sales/{serial_no}",
        response_model=SaleOut,
        tags=["sales"],
        summary="按流水号查询单条销售",
    )
    def get_sale(serial_no: str, conn: sqlite3.Connection = Depends(get_conn)):
        row = conn.execute(
            "SELECT * FROM sales WHERE serial_no = ?", (serial_no,)
        ).fetchone()
        if row is None:
            raise NotFoundError(
                "sale not found", detail={"serial_no": serial_no}
            )
        return SaleOut.from_row(row)

    @app.put(
        "/api/v1/stores/{store_id}/region-table",
        response_model=RegionTableReplaceResponse,
        status_code=202,
        tags=["region-tables"],
        summary="替换单店完整的区域有效期表",
        description=(
            "提交该门店完整的新有效期表（左闭右开、不可重叠）。新表先保存为未生效版本，"
            "并启动后台回填；同一门店只允许一个未完成任务（否则 409）。"
            "任务成功提交前，导入和查询继续使用旧有效期表及旧归属。"
        ),
    )
    def replace_region_table(
        store_id: str,
        payload: RegionTableReplaceRequest,
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        if payload.store_id != store_id:
            raise ValidationError(
                "store_id in path and body must match",
                detail={"path": store_id, "body": payload.store_id},
            )
        return backfill.create_region_table(conn, store_id, payload.normalised())

    @app.get(
        "/api/v1/stores/{store_id}/region-table",
        response_model=RegionTableOut,
        tags=["region-tables"],
        summary="查询当前生效的区域有效期表",
    )
    def get_active_region_table(
        store_id: str, conn: sqlite3.Connection = Depends(get_conn)
    ):
        table = conn.execute(
            "SELECT * FROM region_tables WHERE store_id = ? AND is_active = 1",
            (store_id,),
        ).fetchone()
        if table is None:
            raise NotFoundError(
                "no active region table for store", detail={"store_id": store_id}
            )
        periods = conn.execute(
            "SELECT region_code, start_date, end_date FROM region_periods "
            "WHERE table_id = ? ORDER BY start_date",
            (table["id"],),
        ).fetchall()
        return {
            "table_id": table["id"],
            "store_id": table["store_id"],
            "is_active": True,
            "created_at": table["created_at"],
            "active_since": table["active_since"],
            "periods": [RegionPeriodOut(**dict(p)).model_dump() for p in periods],
        }

    @app.get(
        "/api/v1/backfill-tasks/{task_id}",
        response_model=BackfillTaskOut,
        tags=["backfill"],
        summary="查询回填任务进度",
    )
    def get_backfill_task(task_id: int, conn: sqlite3.Connection = Depends(get_conn)):
        return backfill.task_to_dict(backfill.get_task(conn, task_id))

    @app.get(
        "/api/v1/stores/{store_id}/backfill-tasks",
        response_model=list[BackfillTaskOut],
        tags=["backfill"],
        summary="查询单店回填任务历史",
    )
    def list_backfill_tasks(
        store_id: str,
        limit: int = Query(default=20, ge=1, le=200),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        rows = conn.execute(
            "SELECT * FROM backfill_tasks WHERE store_id = ? ORDER BY id DESC LIMIT ?",
            (store_id, limit),
        ).fetchall()
        return [backfill.task_to_dict(row) for row in rows]

    @app.post(
        "/api/v1/backfill-tasks/{task_id}/retry",
        response_model=BackfillTaskOut,
        tags=["backfill"],
        summary="重试失败的回填任务",
        description="仅失败任务可重试；从已持久化的游标继续，不重复处理已暂存的行。",
    )
    def retry_backfill_task(
        task_id: int, conn: sqlite3.Connection = Depends(get_conn)
    ):
        return backfill.retry_task(conn, task_id)

    @app.post(
        "/api/v1/backfill-tasks/{task_id}/advance",
        response_model=BackfillTaskOut,
        tags=["backfill"],
        summary="手动推进回填任务（演示/无 worker 场景）",
        description=(
            "同步推进最多 max_batches 个批次。与后台 worker 并发安全；"
            "任务 ready 时会尝试原子提交。"
        ),
    )
    def advance_backfill_task(
        task_id: int,
        max_batches: int = Query(default=100, ge=1, le=1000),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        for _ in range(max_batches):
            task = backfill.get_task(conn, task_id)
            if task["status"] in {"completed", "failed"}:
                break
            result = backfill.scan_batch(
                conn, task_id, settings.backfill_batch_size
            )
            if result.status == "ready":
                backfill.commit_task(conn, task_id)
            elif not result.did_work:
                break
        return backfill.task_to_dict(backfill.get_task(conn, task_id))

    return app


app = create_app()
