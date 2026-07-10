# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))

import auth  # noqa: E402
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

    def test_init_db_bootstraps_default_admin_user(self) -> None:
        users = storage.list_users()
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["username"], auth.BOOTSTRAP_ADMIN_USERNAME)
        self.assertEqual(users[0]["role"], auth.USER_ROLE_DBA)
        self.assertTrue(users[0]["enabled"])
        internal = storage.get_user(users[0]["id"], include_secret=True)
        self.assertIsNotNone(internal)
        assert internal is not None
        self.assertTrue(
            auth.verify_password(
                auth.BOOTSTRAP_ADMIN_PASSWORD,
                internal["password_salt"],
                internal["password_hash"],
            )
        )

    def test_user_session_round_trip(self) -> None:
        salt_hex, password_hash = auth.hash_password("Password@123")
        created = storage.create_user(
            {
                "username": "rd_user",
                "display_name": "研发账号",
                "password_salt": salt_hex,
                "password_hash": password_hash,
                "role": auth.USER_ROLE_RD,
                "enabled": True,
            }
        )

        token = auth.hash_session_token("token-123")
        storage.create_user_session(created["id"], token, "2099-01-01 00:00:00")
        session_user = storage.get_user_by_session_token_hash(token)

        self.assertIsNotNone(session_user)
        assert session_user is not None
        self.assertEqual(session_user["username"], "rd_user")
        self.assertEqual(session_user["role"], auth.USER_ROLE_RD)

        deleted = storage.delete_user_sessions_for_user(created["id"])
        self.assertEqual(deleted, 1)
        self.assertIsNone(storage.get_user_by_session_token_hash(token))


if __name__ == "__main__":
    unittest.main()
