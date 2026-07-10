# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent))

import main  # noqa: E402


class RuntimeStoragePathTests(unittest.TestCase):
    def test_validate_runtime_storage_paths_allows_non_production_layout(self) -> None:
        with patch("main.Path.cwd", return_value=Path("/tmp/dbcheck-dev")):
            main.validate_runtime_storage_paths()

    def test_validate_runtime_storage_paths_rejects_app_directory_storage_in_production(self) -> None:
        with (
            patch("main.Path.cwd", return_value=Path("/opt/dbcheck/app")),
            patch("main.SQLITE_DB_PATH", Path("/opt/dbcheck/app/dbcheck.db")),
            patch("main.FERNET_KEY_PATH", Path("/opt/dbcheck/app/.fernet_key")),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                main.validate_runtime_storage_paths()

        self.assertIn("DBCHECK_SQLITE_PATH", str(ctx.exception))
        self.assertIn("DBCHECK_FERNET_KEY_FILE", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
