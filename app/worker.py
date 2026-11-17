"""Background worker that advances pending backfill tasks.

The worker is deliberately DB-driven: all state lives in ``backfill_tasks``,
so a killed process resumes from the stored cursor and staged rows.  Nothing
is held in memory between batches.
"""

from __future__ import annotations

import logging
import threading
import time

from . import backfill
from .config import Settings
from .db import connect, init_db

logger = logging.getLogger("retail.worker")


def run_once(conn, batch_size: int) -> int:
    """Advance every runnable task by one batch.  Returns the number of tasks
    that did work."""
    now = int(time.time())
    tasks = conn.execute(
        """
        SELECT id FROM backfill_tasks
        WHERE status IN ('pending', 'running', 'ready')
          AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
        ORDER BY id
        """,
        (now,),
    ).fetchall()

    worked = 0
    for row in tasks:
        task_id = row["id"]
        try:
            result = backfill.scan_batch(conn, task_id, batch_size)
            if result.did_work:
                worked += 1
            if result.status == "ready":
                committed = backfill.commit_task(conn, task_id)
                if committed.did_work:
                    worked += 1
            elif result.status == "completed":
                worked += 1
        except Exception as exc:  # noqa: BLE001 - failure must not kill the worker
            logger.exception("backfill task %s failed", task_id)
            message = f"{type(exc).__name__}: {exc}"[:500]
            try:
                backfill.record_failure(conn, task_id, message)
            except Exception:  # noqa: BLE001
                logger.exception("could not record failure for task %s", task_id)
    return worked


class Worker(threading.Thread):
    """Polls for runnable backfill tasks until stopped."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(name="retail-backfill-worker", daemon=True)
        self.settings = settings
        self._stop_event = threading.Event()
        self._conn = None

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:  # pragma: no cover - exercised via worker tests/run_once
        self._conn = connect(self.settings.database_path)
        init_db(self._conn)
        logger.info("worker started (db=%s)", self.settings.database_path)
        try:
            while not self._stop_event.is_set():
                try:
                    steps = 0
                    while (
                        not self._stop_event.is_set()
                        and steps < self.settings.worker_max_steps_per_cycle
                    ):
                        worked = run_once(
                            self._conn, self.settings.backfill_batch_size
                        )
                        steps += 1
                        if worked == 0:
                            break
                except Exception:  # noqa: BLE001
                    logger.exception("worker cycle failed")
                self._stop_event.wait(self.settings.worker_poll_interval)
        finally:
            self._conn.close()
            logger.info("worker stopped")
