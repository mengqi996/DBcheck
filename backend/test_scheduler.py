# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent))

import scheduler  # noqa: E402


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
