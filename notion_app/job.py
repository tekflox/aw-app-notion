"""A single background job slot for the sync.

Why this exists: a full ``notion-sync`` runs for minutes, and the BYOD tunnel
in front of this workspace drops a long-held request, answering the caller
``502 workspace offline`` while the work is still running fine on this side.
Holding the connection is therefore not an option regardless of how patient
the client is. Core hit exactly this with ``POST /api/apps/install`` and
answered it the same way — return 202 with a job the caller polls.

One slot, not a queue: two concurrent syncs would race on the same files and
the same state document, and there is no sensible reason to run two. A second
start while one is running is refused (409), not queued.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable

log = logging.getLogger("aw_apps.notion.sync")

IDLE = "idle"
RUNNING = "running"
DONE = "done"
FAILED = "failed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SyncJob:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status = IDLE
        self._started_at: str | None = None
        self._finished_at: str | None = None
        self._result: dict | None = None
        self._error: str | None = None

    def snapshot(self) -> dict:
        with self._lock:
            return {"status": self._status, "started_at": self._started_at,
                    "finished_at": self._finished_at, "result": self._result,
                    "error": self._error}

    def start(self, work: Callable[[], dict]) -> bool:
        """Claim the slot and run ``work`` off the event loop. Returns False
        if a sync is already running."""
        with self._lock:
            if self._status == RUNNING:
                return False
            self._status = RUNNING
            self._started_at = _now()
            self._finished_at = None
            self._result = None
            self._error = None

        asyncio.get_running_loop().create_task(self._run(work))
        return True

    async def _run(self, work: Callable[[], dict]) -> None:
        from fastapi.concurrency import run_in_threadpool

        try:
            result: Any = await run_in_threadpool(work)
            status, error = DONE, None
        except Exception as exc:  # noqa: BLE001 — the job's failure IS the result
            log.exception("notion-sync job failed")
            result, status, error = None, FAILED, str(exc)

        with self._lock:
            self._status = status
            self._result = result
            self._error = error
            self._finished_at = _now()
