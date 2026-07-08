# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent))

import binlog_service  # noqa: E402


class FakeCursor:
    def __init__(self) -> None:
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, _params=None) -> None:
        if sql.startswith("SHOW VARIABLES"):
            self._rows = [
                {"Variable_name": "log_bin", "Value": "ON"},
                {"Variable_name": "binlog_format", "Value": "ROW"},
            ]
        elif sql == "SHOW BINARY LOGS":
            self._rows = [
                {"Log_name": "mysql-bin.000001", "File_size": 1024},
                {"Log_name": "mysql-bin.000002", "File_size": 2048},
            ]
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchall(self):
        return self._rows


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def cursor(self):
        return FakeCursor()

    def close(self) -> None:
        self.closed = True


class BinlogServiceTests(unittest.TestCase):
    def test_self_hosted_mysql_lists_binary_logs(self) -> None:
        binding = {
            "id": 7,
            "instance_id": 42,
            "instance_name": "自建 MySQL",
            "tc_product": "self_mysql",
            "tc_instance_id": "self:42",
            "tc_region": "self-hosted",
        }
        instance = {
            "id": 42,
            "name": "自建 MySQL",
            "db_type": "MySQL",
            "host": "127.0.0.1",
            "port": 3306,
            "username": "root",
            "password": "",
            "database": None,
        }

        with (
            patch("binlog_service.storage.get_binding", return_value=binding),
            patch("binlog_service.storage.get_instance", return_value=instance),
            patch("binlog_service._mysql_connect", return_value=FakeConnection()),
        ):
            result = binlog_service.list_binlogs(
                binding_id=7,
                start_time="2026-07-08 00:00:00",
                end_time="2026-07-08 23:59:59",
                limit=10,
            )

        self.assertEqual(result["total"], 2)
        self.assertEqual(result["meta"]["kind"], "self_mysql_binlog")
        self.assertFalse(result["meta"]["supports_download_url"])
        self.assertIn("暂不支持按时间过滤", result["meta"]["notice"])
        self.assertEqual(result["items"][0]["file_name"], "mysql-bin.000002")
        self.assertEqual(result["items"][0]["size"], "2.0 KB")
        self.assertEqual(result["items"][0]["tc_product"], "self_mysql")


if __name__ == "__main__":
    unittest.main()
