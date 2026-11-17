"""Environment-driven configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _env_float(name: str, default: float, minimum: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


@dataclass(frozen=True)
class Settings:
    database_path: str = "data/retail.db"
    backfill_batch_size: int = 500
    worker_enabled: bool = True
    worker_poll_interval: float = 0.25
    worker_max_steps_per_cycle: int = 200

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_path=os.getenv("DATABASE_PATH", "data/retail.db"),
            backfill_batch_size=_env_int("BACKFILL_BATCH_SIZE", 500, 1),
            worker_enabled=_env_bool("WORKER_ENABLED", True),
            worker_poll_interval=_env_float("WORKER_POLL_INTERVAL", 0.25, 0.01),
            worker_max_steps_per_cycle=_env_int("WORKER_MAX_STEPS_PER_CYCLE", 200, 1),
        )
