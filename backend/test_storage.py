# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))

import storage  # noqa: E402


class InstanceStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        storage.DB_PATH = Path(self.tempdir.name) / "dbcheck-test.db"
        storage.init_db()

    def test_update_instance_can_clear_nullable_fields(self) -> None:
        created = storage.create_instance(
            {
                "name": "清空字段测试",
                "host": "127.0.0.1",
                "port": 3306,
                "db_type": "MySQL",
                "username": "app",
                "password": "secret",
                "database": "appdb",
                "version": "8.0",
                "environment": "test",
                "owner": "DBA",
                "remark": "temporary",
            }
        )

        updated = storage.update_instance(
            created["id"],
            {
                "username": None,
                "password": None,
                "database": None,
                "version": None,
                "owner": None,
                "remark": None,
            },
        )

        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertIsNone(updated["username"])
        self.assertIsNone(updated["database"])
        self.assertIsNone(updated["version"])
        self.assertIsNone(updated["owner"])
        self.assertIsNone(updated["remark"])

        internal = storage.get_instance(created["id"], include_secret=True)
        self.assertIsNotNone(internal)
        assert internal is not None
        self.assertIsNone(internal["password"])

    def test_update_instance_keeps_omitted_nullable_fields(self) -> None:
        created = storage.create_instance(
            {
                "name": "保留字段测试",
                "host": "127.0.0.2",
                "port": 5432,
                "db_type": "PostgreSQL",
                "username": "postgres",
                "password": "secret",
                "database": "postgres",
                "version": "15",
                "environment": "test",
                "owner": "研发",
                "remark": "keep me",
            }
        )

        updated = storage.update_instance(
            created["id"],
            {
                "host": "127.0.0.3",
            },
        )

        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated["host"], "127.0.0.3")
        self.assertEqual(updated["username"], "postgres")
        self.assertEqual(updated["database"], "postgres")
        self.assertEqual(updated["version"], "15")
        self.assertEqual(updated["owner"], "研发")
        self.assertEqual(updated["remark"], "keep me")

        internal = storage.get_instance(created["id"], include_secret=True)
        self.assertIsNotNone(internal)
        assert internal is not None
        self.assertEqual(internal["password"], "secret")


if __name__ == "__main__":
    unittest.main()
