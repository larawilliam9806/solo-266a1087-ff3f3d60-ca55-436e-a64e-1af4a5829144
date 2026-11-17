# 零售销售区域归属 API

FastAPI + SQLite 服务：导入销售明细时按**营业日**匹配门店的**区域有效期表**并保存区域；
支持以“完整有效期表替换”的方式更正历史区域归属，替换通过**可恢复的后台回填**完成，
全部受影响销售（含期间新增销售与待归属销售）回填完成后才**原子切换**有效期表与销售归属。

## 关键规则

| 规则 | 实现 |
| --- | --- |
| 区域有效期左闭右开、不可重叠 | 请求校验 + `start_date < end_date` 约束；区间重叠返回 422 |
| 销售落库保存按营业日匹配的区域 | 导入时在写事务内按 `start_date <= 营业日 < end_date` 匹配 |
| 无匹配则“待归属” | `attribution_status = pending`, `region_code = NULL` |
| 重复流水号内容相同不重复入账 | 按 `serial_no` 幂等，内容哈希一致 → `duplicate`，不新增 |
| 同一流水号内容不同则拒绝 | 内容哈希不同 → 该行 `rejected`，不影响同批其它行 |
| 同店同时只允许一个未完成任务 | 部分唯一索引 `ux_backfill_one_unfinished_per_store`，冲突返回 409 |
| 回填期间仍可导入销售、查询沿用旧归属 | 新结果暂存 `backfill_items`；`sales` 行在提交前不改动 |
| 全部回填后才一起切换 | 单条 `BEGIN IMMEDIATE` 事务：重写销售归属 + 切换 active 有效期表 + 完成任务 |
| 待归属销售也须重新匹配 | 回填扫描该店**全部**销售，不只原有已归属的 |
| 失败保留旧结果 | 事务回滚；`sales` 与 active 有效期表均不变 |
| 进程重启可继续 | 游标、暂存行、状态全部在 SQLite；worker 启动即继续 |
| 金额精确 | 以整数“分”存储，输入不接受超过 2 位小数 |

## 运行

### Docker Compose（推荐）

```bash
docker compose up --build
# API:      http://localhost:8000
# OpenAPI:  http://localhost:8000/docs
# 数据卷:   retail-data -> /data/retail.db
```

后台 worker 默认随 API 进程启动（线程），自动回填并提交任务。

### 本地运行

```bash
python3 -m pip install -r requirements.txt
python3 -m uvicorn app.api:app --port 8000
# 或：python3 -m app
```

环境变量见 `.env.example`：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | `data/retail.db` | SQLite 文件路径 |
| `BACKFILL_BATCH_SIZE` | `500` | 单批暂存销售数 |
| `WORKER_ENABLED` | `true` | 是否启用后台回填线程 |
| `WORKER_POLL_INTERVAL` | `0.25` | worker 轮询间隔（秒） |

### 端到端演示

```bash
./examples/demo.sh http://localhost:8000
```

脚本覆盖：无有效期表时导入→待归属；首次发布有效期表；回填提交后归属；重复导入幂等；
更正有效期表期间查询仍为旧归属；回填期间新增销售；任务完成后全部按新表重新归属。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/sales` | 导入销售明细（1–1000 条），逐条返回结果 |
| `GET` | `/api/v1/sales` | 查询销售归属，可按门店/区域/营业日区间/归属状态过滤，分页 |
| `GET` | `/api/v1/sales/{serial_no}` | 按流水号查询单条 |
| `PUT` | `/api/v1/stores/{store_id}/region-table` | 提交完整的新区域有效期表并发起回填（202 / 409） |
| `GET` | `/api/v1/stores/{store_id}/region-table` | 查询当前**生效**有效期表 |
| `GET` | `/api/v1/backfill-tasks/{task_id}` | 任务进度（状态、总数、已处理、剩余、失败次数） |
| `GET` | `/api/v1/stores/{store_id}/backfill-tasks` | 门店任务历史 |
| `POST` | `/api/v1/backfill-tasks/{task_id}/retry` | 重试失败任务（从游标继续） |
| `POST` | `/api/v1/backfill-tasks/{task_id}/advance` | 手动推进（演示或无 worker 场景，与 worker 并发安全） |
| `GET` | `/health` | 健康检查 |

错误统一为 `{"error": {"code", "message", "detail"}}`。

### 示例请求

导入销售（首次、尚无有效期表，均为待归属）：

```bash
curl -s -X POST http://localhost:8000/api/v1/sales \
  -H 'Content-Type: application/json' -d '{
  "sales": [
    {"serial_no": "TX-1001", "store_id": "S1", "business_date": "2024-01-10", "amount": "120.00"},
    {"serial_no": "TX-1002", "store_id": "S1", "business_date": "2024-02-10", "amount": "80.50"}
  ]}'
```

```json
{"inserted":2,"attributed":0,"pending":2,"duplicates":0,"rejected":0,
 "results":[
  {"serial_no":"TX-1001","status":"inserted_pending","region_code":null,"message":null},
  {"serial_no":"TX-1002","status":"inserted_pending","region_code":null,"message":null}]}
```

提交完整区域有效期表（先保存为未生效版本，回填在后台进行）：

```bash
curl -s -X PUT http://localhost:8000/api/v1/stores/S1/region-table \
  -H 'Content-Type: application/json' -d '{
  "store_id": "S1",
  "periods": [
    {"region_code": "NORTH", "start_date": "2024-01-01", "end_date": "2024-03-01"},
    {"region_code": "SOUTH", "start_date": "2024-03-01", "end_date": "2024-06-01"}
  ]}'
```

```json
{"store_id":"S1","task_id":1,"status":"pending","source_table_id":null,
 "target_table_id":1,
 "message":"region table stored; backfill task queued. The table and attributions switch together when the task completes."}
```

查询归属（回填未提交时仍返回旧值）：

```bash
curl -s 'http://localhost:8000/api/v1/sales?store_id=S1&attribution_status=attributed'
```

```json
{"items":[{"serial_no":"TX-1001","store_id":"S1","business_date":"2024-01-10",
 "amount":"120.00","amount_cents":12000,"region_code":"NORTH",
 "attribution_status":"attributed","attribution_table_id":1, "...": "..."}],
 "total":1,"limit":50,"offset":0}
```

查询任务进度：

```bash
curl -s http://localhost:8000/api/v1/backfill-tasks/1
```

```json
{"task_id":1,"store_id":"S1","status":"completed","cursor_sales_id":2,
 "total_sales":2,"processed_sales":2,"remaining_sales":0,
 "failure_count":0,"last_error":null, "...": "..."}
```

同一门店再次提交有效期表（未完成任务未结束时）：

```json
{"error":{"code":"backfill_in_progress",
          "message":"store already has an unfinished backfill task",
          "detail":{"store_id":"S1","task_id":1}}}
```

## 回填与切换机制

1. `PUT region-table` 在**一个写事务**内校验并发控制：确认该店无未完成任务 → 保存新表（`is_active=0`）
   → 建立 `pending` 任务（`cursor_sales_id=0`, `total_sales=当前销售数`）。
2. worker（或 `/advance`）按 `sales.id` 升序分批扫描该店**所有**销售，按新表计算归属，
   写入 `backfill_items`（含扫描时的 `sales.updated_at`）。**不修改** `sales`。
   导入与查询在此期间完全正常，使用旧 active 表。
3. 扫描越过末尾后任务置 `ready`。提交时在 `BEGIN IMMEDIATE` 内再次检查：
   - 是否出现 `id > cursor` 的新销售 → 回到 `running` 继续扫描；
   - 暂存行对应的销售是否被改动（`updated_at` 不一致）→ 从该行回退重扫；
   - 暂存行数必须等于该店销售总数，否则继续扫描。
4. 校验通过后同一事务内：批量重写 `sales` 的 `region_code`/`attribution_status`/`attribution_table_id`
   → 旧表 `is_active=0`、新表 `is_active=1` 且写入 `active_since` → 任务 `completed`。
   任一步失败整体回滚，旧表与旧归属保持可用。
5. 失败任务（worker 异常达 5 次）记录 `last_error` 并可 `retry`；若另有更新任务已存在则拒绝重试，
   避免把归属回滚到过时版本。

## 自测

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q
```

覆盖内容：

- `tests/test_import.py`：左闭右开边界、无表待归属、幂等重复、内容冲突拒绝、混批、金额/日期校验、
  区间重叠校验、查询过滤与分页。
- `tests/test_backfill.py`：单店单任务约束、回填期间查询/导入沿用旧归属、待归属销售重新匹配、
  空店切换、任务进度接口、注入式提交失败后整体回滚且可继续提交、重启后从游标续跑、
  后台 worker 自动完成、失败任务保留旧结果并可重试。
- `tests/test_concurrency.py`：扫描中途导入的新销售仍被回填、扫描末尾之后才到的新销售阻止提交并重扫、
  双连接并发扫描 + 提交、并发发布有效期表只有一个成功。

## 说明与限制

- SQLite 没有原生的排他区间约束，重叠校验在应用层（事务内）完成；有效期表一经发布即不可变。
- 金额以整数分存储，导入时拒绝超过两位小数的金额。
- 单实例部署：后台 worker 是进程内线程。SQLite 写入由 `BEGIN IMMEDIATE` + 忙等超时串行化，
  多副本共享同一文件不建议（多副本请改用独立 worker 进程或 Postgres）。
- 暂存表在任务完成后保留用于审计；生产环境可按需清理 `backfill_items`。
- 回填提交前没有 active 有效期表的门店，查询该店 `/region-table` 返回 404（表尚未生效），
  销售仍可导入/查询为待归属。
