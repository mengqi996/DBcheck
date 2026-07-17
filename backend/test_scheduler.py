# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent))

import scheduler  # noqa: E402


class SlowQuerySchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_trigger_now_runs_tick_and_stop_cleans_up(self) -> None:
        sched = scheduler.SlowQueryScheduler(interval_seconds=3600, max_concurrency=1)

        with (
            patch("scheduler.storage.list_bindings", return_value=[]),
            patch("scheduler.storage.purge_old_slow_queries") as purge,
        ):
            await sched.start()
            self.addAsyncCleanup(sched.stop)

            triggered = await sched.trigger_now()
            for _ in range(20):
                if sched.status()["last_tick_at"]:
                    break
                await asyncio.sleep(0.01)
            await sched.stop()

        self.assertTrue(triggered)
        self.assertFalse(sched.status()["running"])
        self.assertIsNotNone(sched.status()["last_tick_at"])
        purge.assert_called_once_with()


class BackupSyncSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_tick_syncs_tencent_backups_and_records_status(self) -> None:
        expected = {
            "bindings": 1,
            "fetched": 2,
            "inserted": 1,
            "updated": 1,
            "skipped": 0,
            "errors": [],
        }
        sched = scheduler.BackupSyncScheduler(interval_seconds=3600)

        with patch("scheduler.backup_service.sync_tencent_backups", return_value=expected) as sync:
            await sched._tick()

        sync.assert_called_once_with()
        status = sched.status()
        self.assertFalse(status["active"])
        self.assertEqual(status["last_result"], expected)
        self.assertIsNone(status["last_error"])
        self.assertIsNotNone(status["last_tick_at"])

    async def test_tick_keeps_scheduler_alive_when_sync_fails(self) -> None:
        sched = scheduler.BackupSyncScheduler(interval_seconds=3600)

        with (
            patch(
                "scheduler.backup_service.sync_tencent_backups",
                side_effect=RuntimeError("api failed"),
            ),
            patch("scheduler.logger.exception"),
        ):
            await sched._tick()

        status = sched.status()
        self.assertFalse(status["active"])
        self.assertIn("RuntimeError: api failed", status["last_error"])


if __name__ == "__main__":
    unittest.main()
