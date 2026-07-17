# -*- coding: utf-8 -*-
"""
慢 SQL 异步调度器

FastAPI lifespan 中创建，启动后台 asyncio 任务，每隔 interval_seconds
对所有 enabled binding 拉取一次慢日志。手动触发由 trigger_now() 提供。
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import backup_service
import slow_query_service
import storage
from async_compat import to_thread


logger = logging.getLogger("dbcheck.scheduler")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class SlowQueryScheduler:
    def __init__(self, interval_seconds: int = 3600, max_concurrency: int = 4):
        self.interval_seconds = max(5, int(interval_seconds))
        self._sem = asyncio.Semaphore(max_concurrency)
        self._tick_lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._trigger_event: Optional[asyncio.Event] = None
        self._last_tick_at: Optional[str] = None
        self._active_polls = 0
        self._manual_lock = asyncio.Lock()
        self._last_manual_at: float = 0.0

    # ----- 生命周期 -----

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._trigger_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="slow-query-scheduler")
        logger.info("SlowQueryScheduler started (interval=%ss)", self.interval_seconds)

    async def stop(self) -> None:
        if not self._task:
            return
        if self._stop_event:
            self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        self._task = None
        self._stop_event = None
        self._trigger_event = None
        logger.info("SlowQueryScheduler stopped")

    # ----- 状态 -----

    def status(self) -> dict:
        snap = storage.binding_poll_status_snapshot()
        return {
            "running": bool(self._task and not self._task.done()),
            "interval_seconds": self.interval_seconds,
            "last_tick_at": self._last_tick_at,
            "bindings_count": snap["total"],
            "enabled_bindings": snap["enabled"],
            "failing_bindings": snap["failing"],
            "active_polls": self._active_polls,
        }

    # ----- 手动触发 -----

    async def trigger_now(self) -> bool:
        """手动触发一次同步；5s 内重复触发被忽略。返回是否真正触发。"""
        import time
        now = time.time()
        if now - self._last_manual_at < 5.0:
            return False
        async with self._manual_lock:
            now = time.time()
            if now - self._last_manual_at < 5.0:
                return False
            self._last_manual_at = now
        if self._task and not self._task.done() and self._trigger_event:
            self._trigger_event.set()
        else:
            await self._tick()
        return True

    # ----- 主循环 -----

    async def _run(self) -> None:
        assert self._stop_event is not None
        assert self._trigger_event is not None
        stop_event: asyncio.Event = self._stop_event
        trigger_event: asyncio.Event = self._trigger_event

        while not stop_event.is_set():
            try:
                # 同时等待 interval 超时或 stop / trigger 信号，并显式回收
                # 未完成 task，避免在事件循环里留下未取回的取消任务。
                wait_tasks = {
                    asyncio.create_task(stop_event.wait()),
                    asyncio.create_task(trigger_event.wait()),
                    asyncio.create_task(asyncio.sleep(self.interval_seconds)),
                }
                done, _pending = await asyncio.wait(
                    wait_tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in _pending:
                    task.cancel()
                if _pending:
                    await asyncio.gather(*_pending, return_exceptions=True)
                for task in done:
                    task.result()
                if trigger_event.is_set():
                    trigger_event.clear()
                if stop_event.is_set():
                    break
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.exception("scheduler tick failed: %s", e)
                # 退避 5s 后继续
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass

    async def _tick(self) -> None:
        async with self._tick_lock:
            self._last_tick_at = _utc_now_iso()
            bindings = storage.list_bindings(enabled_only=True)
            if bindings:
                await asyncio.gather(*(self._poll_with_sem(b) for b in bindings))
            await to_thread(storage.purge_old_slow_queries)

    async def _poll_with_sem(self, binding: dict) -> None:
        async with self._sem:
            self._active_polls += 1
            try:
                await to_thread(slow_query_service.poll_one_binding, binding)
            finally:
                self._active_polls -= 1


class BackupSyncScheduler:
    def __init__(self, interval_seconds: int = 3600):
        self.interval_seconds = max(60, int(interval_seconds))
        self._tick_lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._last_tick_at: Optional[str] = None
        self._last_result: Optional[dict] = None
        self._last_error: Optional[str] = None
        self._active = False

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="backup-sync-scheduler")
        logger.info("BackupSyncScheduler started (interval=%ss)", self.interval_seconds)

    async def stop(self) -> None:
        if not self._task:
            return
        if self._stop_event:
            self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        self._task = None
        self._stop_event = None
        logger.info("BackupSyncScheduler stopped")

    def status(self) -> dict:
        return {
            "running": bool(self._task and not self._task.done()),
            "interval_seconds": self.interval_seconds,
            "last_tick_at": self._last_tick_at,
            "last_result": self._last_result,
            "last_error": self._last_error,
            "active": self._active,
        }

    async def _run(self) -> None:
        assert self._stop_event is not None
        stop_event = self._stop_event
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.interval_seconds)
                if stop_event.is_set():
                    break
            except asyncio.TimeoutError:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.exception("backup sync scheduler tick failed: %s", e)
                self._last_error = f"{type(e).__name__}: {e}"

    async def _tick(self) -> None:
        async with self._tick_lock:
            self._active = True
            self._last_tick_at = _utc_now_iso()
            try:
                result = await to_thread(backup_service.sync_tencent_backups)
                self._last_result = result
                self._last_error = None
                if result.get("errors"):
                    logger.warning(
                        "Backup sync completed with %s errors: %s",
                        len(result["errors"]),
                        result["errors"],
                    )
                else:
                    logger.info(
                        "Backup sync completed (fetched=%s inserted=%s updated=%s skipped=%s)",
                        result.get("fetched"),
                        result.get("inserted"),
                        result.get("updated"),
                        result.get("skipped"),
                    )
            except Exception as e:  # noqa: BLE001
                self._last_error = f"{type(e).__name__}: {e}"
                logger.exception("backup sync failed: %s", e)
            finally:
                self._active = False
