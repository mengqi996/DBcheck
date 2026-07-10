# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

try:
    from fastapi.testclient import TestClient
except RuntimeError as exc:
    TestClient = None  # type: ignore[assignment]
    TESTCLIENT_IMPORT_ERROR = exc
else:
    TESTCLIENT_IMPORT_ERROR = None


sys.path.insert(0, str(Path(__file__).resolve().parent))

import auth  # noqa: E402
import main  # noqa: E402
import storage  # noqa: E402


class AuthApiTests(unittest.TestCase):
    @unittest.skipIf(TestClient is None, f"fastapi TestClient unavailable: {TESTCLIENT_IMPORT_ERROR}")
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        storage.DB_PATH = Path(self.tempdir.name) / "dbcheck-auth-test.db"
        main.SCHEDULER_ENABLED = False
        main.TENCENT_API_ENABLED = False

    def _client(self) -> "TestClient":
        assert TestClient is not None
        return TestClient(main.app)

    def _login(self, client: "TestClient", username: str = auth.BOOTSTRAP_ADMIN_USERNAME, password: str = auth.BOOTSTRAP_ADMIN_PASSWORD) -> str:
        response = client.post(
            "/api/auth/login",
            json={"username": username, "password": password},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertNotIn("password_hash", body["data"]["user"])
        return body["data"]["token"]

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def test_protected_endpoint_requires_login(self) -> None:
        with self._client() as client:
            response = client.get("/api/dashboard")

        self.assertEqual(response.status_code, 401)
        self.assertIn("请先登录", response.json()["detail"])

    def test_default_admin_can_login_and_read_me(self) -> None:
        with self._client() as client:
            token = self._login(client)
            response = client.get("/api/auth/me", headers=self._headers(token))

        self.assertEqual(response.status_code, 200, response.text)
        user = response.json()["data"]["user"]
        self.assertEqual(user["username"], auth.BOOTSTRAP_ADMIN_USERNAME)
        self.assertEqual(user["role"], auth.USER_ROLE_DBA)

    def test_rd_user_is_read_only(self) -> None:
        with self._client() as client:
            admin_token = self._login(client)
            created = client.post(
                "/api/users",
                headers=self._headers(admin_token),
                json={
                    "username": "rd_user",
                    "display_name": "研发账号",
                    "password": "Password@123",
                    "role": auth.USER_ROLE_RD,
                    "enabled": True,
                },
            )
            self.assertEqual(created.status_code, 200, created.text)

            rd_token = self._login(client, "rd_user", "Password@123")
            dashboard = client.get("/api/dashboard", headers=self._headers(rd_token))
            create_instance = client.post(
                "/api/instances",
                headers=self._headers(rd_token),
                json={
                    "name": "RD 不应创建",
                    "host": "127.0.0.1",
                    "port": 3306,
                    "db_type": "MySQL",
                },
            )
            list_users = client.get("/api/users", headers=self._headers(rd_token))

        self.assertEqual(dashboard.status_code, 200, dashboard.text)
        self.assertEqual(create_instance.status_code, 403, create_instance.text)
        self.assertEqual(list_users.status_code, 403, list_users.text)

    def test_dba_can_manage_users(self) -> None:
        with self._client() as client:
            token = self._login(client)
            created = client.post(
                "/api/users",
                headers=self._headers(token),
                json={
                    "username": "ops",
                    "display_name": "运维",
                    "password": "Password@123",
                    "role": auth.USER_ROLE_RD,
                    "enabled": True,
                },
            )
            self.assertEqual(created.status_code, 200, created.text)
            user_id = created.json()["data"]["user"]["id"]

            updated = client.put(
                f"/api/users/{user_id}",
                headers=self._headers(token),
                json={"display_name": "运维同学", "enabled": False},
            )
            self.assertEqual(updated.status_code, 200, updated.text)
            self.assertFalse(updated.json()["data"]["user"]["enabled"])

            deleted = client.delete(f"/api/users/{user_id}", headers=self._headers(token))

        self.assertEqual(deleted.status_code, 200, deleted.text)


if __name__ == "__main__":
    unittest.main()
