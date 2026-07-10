# -*- coding: utf-8 -*-
"""
SQLite-backed repository for DBCheck.

The project is still intentionally lightweight, but data now survives service
restarts and API handlers no longer mutate module-level lists.
"""

import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from auth import (
    BOOTSTRAP_ADMIN_DISPLAY_NAME,
    BOOTSTRAP_ADMIN_PASSWORD,
    BOOTSTRAP_ADMIN_USERNAME,
    USER_ROLE_DBA,
    hash_password,
)


DB_PATH = Path(os.getenv("DBCHECK_SQLITE_PATH", Path(__file__).with_name("dbcheck.db")))
SECONDS_PER_DAY = 86400
SLOW_QUERY_RETENTION_DAYS = max(1, int(os.getenv("DBCHECK_SLOW_QUERY_RETENTION_DAYS", "3")))
SELF_HOSTED_CREDENTIAL_NAME = "__dbcheck_self_hosted__"
SELF_HOSTED_REGION = "self-hosted"
SELF_HOSTED_PRODUCTS = {"self_mysql", "self_postgresql"}


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS instances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                host TEXT NOT NULL,
                port INTEGER NOT NULL,
                db_type TEXT NOT NULL,
                username TEXT,
                password TEXT,
                database_name TEXT,
                version TEXT,
                environment TEXT NOT NULL DEFAULT 'prod',
                owner TEXT,
                status TEXT NOT NULL DEFAULT 'offline',
                last_check TEXT,
                remark TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS backups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                instance_id INTEGER NOT NULL,
                instance_name TEXT NOT NULL,
                backup_type TEXT NOT NULL,
                size TEXT,
                start_time TEXT,
                end_time TEXT,
                status TEXT NOT NULL,
                operator TEXT NOT NULL DEFAULT '系统',
                FOREIGN KEY(instance_id) REFERENCES instances(id)
            );

            CREATE TABLE IF NOT EXISTS check_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id INTEGER,
                instance_name TEXT,
                db_type TEXT,
                host TEXT NOT NULL,
                port INTEGER NOT NULL,
                success INTEGER NOT NULL,
                message TEXT NOT NULL,
                version TEXT,
                response_time REAL,
                checked_at TEXT NOT NULL
            );
            """
        )
        instance_count = conn.execute("SELECT COUNT(*) FROM instances").fetchone()[0]
        if instance_count == 0:
            seed_data(conn)


def seed_data(conn: sqlite3.Connection) -> None:
    created_at = "2024-01-15 10:30:00"
    instances = [
        (
            "生产-MySQL-主库",
            "192.168.1.10",
            3306,
            "MySQL",
            "root",
            "",
            "app",
            "8.0.32",
            "prod",
            "DBA",
            "online",
            "2024-01-15 10:30:00",
            "核心业务主库",
        ),
        (
            "生产-MySQL-从库1",
            "192.168.1.11",
            3306,
            "MySQL",
            "root",
            "",
            "app",
            "8.0.32",
            "prod",
            "DBA",
            "online",
            "2024-01-15 10:30:00",
            "报表只读",
        ),
        (
            "测试-PostgreSQL",
            "192.168.2.10",
            5432,
            "PostgreSQL",
            "postgres",
            "",
            "postgres",
            "15.2",
            "test",
            "研发",
            "online",
            "2024-01-15 10:28:00",
            "测试环境",
        ),
        (
            "生产-Redis-主",
            "192.168.1.20",
            6379,
            "Redis",
            "",
            "",
            "",
            "7.0.11",
            "prod",
            "SRE",
            "online",
            "2024-01-15 10:30:00",
            "缓存集群",
        ),
        (
            "测试-Redis",
            "192.168.2.20",
            6379,
            "Redis",
            "",
            "",
            "",
            "7.0.11",
            "test",
            "研发",
            "offline",
            "2024-01-15 09:00:00",
            "演示异常实例",
        ),
    ]
    conn.executemany(
        """
        INSERT INTO instances (
            name, host, port, db_type, username, password, database_name, version,
            environment, owner, status, last_check, remark, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [item + (created_at, created_at) for item in instances],
    )

    backups = [
        ("daily_mysql_full_20240115", 1, "生产-MySQL-主库", "full", "25.6 GB", "2024-01-15 02:00:00", "2024-01-15 03:45:23", "success", "系统"),
        ("weekly_mysql_full_20240114", 1, "生产-MySQL-主库", "full", "25.8 GB", "2024-01-14 02:00:00", "2024-01-14 04:12:45", "success", "系统"),
        ("daily_mysql_inc_20240115", 1, "生产-MySQL-主库", "incremental", "3.2 GB", "2024-01-15 14:00:00", "2024-01-15 14:15:32", "success", "系统"),
        ("manual_pg_backup_20240115", 3, "测试-PostgreSQL", "manual", "8.5 GB", "2024-01-15 10:00:00", "2024-01-15 10:35:18", "success", "admin"),
        ("daily_redis_rdb_20240115", 4, "生产-Redis-主", "rdb", "1.2 GB", "2024-01-15 03:00:00", "2024-01-15 03:02:45", "success", "系统"),
        ("test_pg_full_20240115", 3, "测试-PostgreSQL", "full", "15.3 GB", "2024-01-15 12:00:00", "-", "running", "admin"),
        ("failed_redis_rdb_20240115", 5, "测试-Redis", "rdb", "-", "2024-01-15 03:00:00", "-", "failed", "系统"),
    ]
    conn.executemany(
        """
        INSERT INTO backups (
            name, instance_id, instance_name, backup_type, size, start_time,
            end_time, status, operator
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        backups,
    )


def sanitize_instance(row: sqlite3.Row) -> Dict[str, Any]:
    data = dict(row)
    data["database"] = data.pop("database_name", None)
    data.pop("password", None)
    return data


def internal_instance(row: sqlite3.Row) -> Dict[str, Any]:
    data = dict(row)
    data["database"] = data.pop("database_name", None)
    return data


def list_instances(
    status: Optional[str] = None,
    db_type: Optional[str] = None,
    keyword: Optional[str] = None,
) -> List[Dict[str, Any]]:
    clauses: List[str] = []
    params: List[Any] = []

    if status:
        clauses.append("status = ?")
        params.append(status)
    if db_type:
        clauses.append("db_type = ?")
        params.append(db_type)
    if keyword:
        clauses.append("(LOWER(name) LIKE ? OR LOWER(host) LIKE ? OR LOWER(owner) LIKE ?)")
        like = f"%{keyword.lower()}%"
        params.extend([like, like, like])

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM instances {where} ORDER BY environment, id",
            params,
        ).fetchall()
    return [sanitize_instance(row) for row in rows]


def get_instance(instance_id: int, include_secret: bool = False) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM instances WHERE id = ?", (instance_id,)).fetchone()
    if not row:
        return None
    return internal_instance(row) if include_secret else sanitize_instance(row)


def create_instance(payload: Dict[str, Any]) -> Dict[str, Any]:
    ts = now()
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO instances (
                name, host, port, db_type, username, password, database_name, version,
                environment, owner, status, last_check, remark, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'offline', NULL, ?, ?, ?)
            """,
            (
                payload["name"],
                payload["host"],
                payload["port"],
                payload["db_type"],
                payload.get("username"),
                payload.get("password"),
                payload.get("database"),
                payload.get("version"),
                payload.get("environment") or "prod",
                payload.get("owner"),
                payload.get("remark"),
                ts,
                ts,
            ),
        )
        instance_id = cursor.lastrowid
    instance = get_instance(instance_id)
    assert instance is not None
    return instance


def update_instance(instance_id: int, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    field_map = {
        "name": "name",
        "host": "host",
        "port": "port",
        "db_type": "db_type",
        "username": "username",
        "password": "password",
        "database": "database_name",
        "version": "version",
        "environment": "environment",
        "owner": "owner",
        "remark": "remark",
    }
    nullable_fields = {"username", "password", "database", "version", "owner", "remark"}
    updates = []
    params: List[Any] = []
    for source, column in field_map.items():
        if source in payload and (payload[source] is not None or source in nullable_fields):
            updates.append(f"{column} = ?")
            params.append(payload[source])

    if not updates:
        return get_instance(instance_id)

    updates.append("updated_at = ?")
    params.append(now())
    params.append(instance_id)

    with get_connection() as conn:
        cursor = conn.execute(
            f"UPDATE instances SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        if cursor.rowcount == 0:
            return None
    return get_instance(instance_id)


def delete_instance(instance_id: int) -> bool:
    with get_connection() as conn:
        exists = conn.execute(
            "SELECT 1 FROM instances WHERE id = ?", (instance_id,)
        ).fetchone()
        if not exists:
            return False

        binding_rows = conn.execute(
            "SELECT id FROM instance_tc_bindings WHERE instance_id = ?",
            (instance_id,),
        ).fetchall()
        binding_ids = [row["id"] for row in binding_rows]
        if binding_ids:
            placeholders = ",".join(["?"] * len(binding_ids))
            conn.execute(
                f"DELETE FROM slow_query_sync_state WHERE binding_id IN ({placeholders})",
                binding_ids,
            )
            conn.execute(
                f"DELETE FROM slow_queries WHERE binding_id IN ({placeholders})",
                binding_ids,
            )
            conn.execute(
                f"DELETE FROM instance_tc_bindings WHERE id IN ({placeholders})",
                binding_ids,
            )

        conn.execute("DELETE FROM slow_queries WHERE instance_id = ?", (instance_id,))
        conn.execute("DELETE FROM backups WHERE instance_id = ?", (instance_id,))
        conn.execute("DELETE FROM instances WHERE id = ?", (instance_id,))
    return True


def update_instance_check(
    instance_id: int,
    success: bool,
    message: str,
    version: Optional[str],
    response_time: Optional[float],
) -> None:
    status = "online" if success else "offline"
    ts = now()
    with get_connection() as conn:
        instance = conn.execute("SELECT * FROM instances WHERE id = ?", (instance_id,)).fetchone()
        if not instance:
            return
        conn.execute(
            """
            UPDATE instances
            SET status = ?, last_check = ?, version = COALESCE(?, version), updated_at = ?
            WHERE id = ?
            """,
            (status, ts, version, ts, instance_id),
        )
        conn.execute(
            """
            INSERT INTO check_logs (
                instance_id, instance_name, db_type, host, port, success, message,
                version, response_time, checked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                instance_id,
                instance["name"],
                instance["db_type"],
                instance["host"],
                instance["port"],
                1 if success else 0,
                message,
                version,
                response_time,
                ts,
            ),
        )


def create_quick_check_log(
    host: str,
    port: int,
    db_type: str,
    success: bool,
    message: str,
    version: Optional[str],
    response_time: Optional[float],
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO check_logs (
                instance_id, instance_name, db_type, host, port, success, message,
                version, response_time, checked_at
            ) VALUES (NULL, '快速检测', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (db_type, host, port, 1 if success else 0, message, version, response_time, now()),
        )


def list_backups(
    status: Optional[str] = None,
    instance_id: Optional[int] = None,
    keyword: Optional[str] = None,
) -> List[Dict[str, Any]]:
    clauses: List[str] = []
    params: List[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if instance_id:
        clauses.append("instance_id = ?")
        params.append(instance_id)
    if keyword:
        clauses.append("(LOWER(name) LIKE ? OR LOWER(instance_name) LIKE ?)")
        like = f"%{keyword.lower()}%"
        params.extend([like, like])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM backups {where} ORDER BY COALESCE(start_time, '') DESC, id DESC",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def get_backup(backup_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM backups WHERE id = ?", (backup_id,)).fetchone()
    return dict(row) if row else None


def create_backup(instance_id: int, backup_type: str, operator: str = "系统") -> Dict[str, Any]:
    instance = get_instance(instance_id)
    if not instance:
        raise ValueError("实例不存在")

    backup_name = f"{instance['name']}_{backup_type}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO backups (
                name, instance_id, instance_name, backup_type, size, start_time,
                end_time, status, operator
            ) VALUES (?, ?, ?, ?, '-', ?, '-', 'running', ?)
            """,
            (backup_name, instance_id, instance["name"], backup_type, now(), operator),
        )
        backup_id = cursor.lastrowid
    backup = get_backup(backup_id)
    assert backup is not None
    return backup


def upsert_cloud_backups(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    """按云厂商备份 ID 写入/更新备份记录，返回插入和更新数量。"""
    if not rows:
        return {"inserted": 0, "updated": 0}

    inserted = 0
    updated = 0
    with get_connection() as conn:
        _ensure_backup_cloud_columns(conn)
        for row in rows:
            key = (
                row.get("cloud_provider") or "tencent",
                row["tc_product"],
                row["tc_region"],
                row["tc_instance_id"],
                str(row["tc_backup_id"]),
            )
            existing = conn.execute(
                """
                SELECT id FROM backups
                WHERE cloud_provider = ?
                  AND tc_product = ?
                  AND tc_region = ?
                  AND tc_instance_id = ?
                  AND tc_backup_id = ?
                """,
                key,
            ).fetchone()
            values = {
                "name": row["name"],
                "instance_id": row["instance_id"],
                "instance_name": row["instance_name"],
                "backup_type": row["backup_type"],
                "size": row.get("size"),
                "start_time": row.get("start_time"),
                "end_time": row.get("end_time"),
                "status": row["status"],
                "operator": row.get("operator") or "腾讯云",
                "cloud_provider": key[0],
                "tc_product": key[1],
                "tc_region": key[2],
                "tc_instance_id": key[3],
                "tc_backup_id": key[4],
                "raw_status": row.get("raw_status"),
                "synced_at": now(),
            }
            if existing:
                conn.execute(
                    """
                    UPDATE backups
                    SET name = ?, instance_id = ?, instance_name = ?, backup_type = ?,
                        size = ?, start_time = ?, end_time = ?, status = ?,
                        operator = ?, raw_status = ?, synced_at = ?
                    WHERE id = ?
                    """,
                    (
                        values["name"],
                        values["instance_id"],
                        values["instance_name"],
                        values["backup_type"],
                        values["size"],
                        values["start_time"],
                        values["end_time"],
                        values["status"],
                        values["operator"],
                        values["raw_status"],
                        values["synced_at"],
                        existing["id"],
                    ),
                )
                updated += 1
            else:
                conn.execute(
                    """
                    INSERT INTO backups (
                        name, instance_id, instance_name, backup_type, size,
                        start_time, end_time, status, operator, cloud_provider,
                        tc_product, tc_region, tc_instance_id, tc_backup_id,
                        raw_status, synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    tuple(values[k] for k in (
                        "name",
                        "instance_id",
                        "instance_name",
                        "backup_type",
                        "size",
                        "start_time",
                        "end_time",
                        "status",
                        "operator",
                        "cloud_provider",
                        "tc_product",
                        "tc_region",
                        "tc_instance_id",
                        "tc_backup_id",
                        "raw_status",
                        "synced_at",
                    )),
                )
                inserted += 1

    return {"inserted": inserted, "updated": updated}


def delete_backup(backup_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM backups WHERE id = ?", (backup_id,))
    return cursor.rowcount > 0


def list_check_logs(limit: int = 20) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM check_logs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def dashboard_summary() -> Dict[str, Any]:
    with get_connection() as conn:
        instances = conn.execute(
            "SELECT status, db_type, environment FROM instances"
        ).fetchall()
        backups = conn.execute("SELECT status FROM backups").fetchall()
        recent_logs = conn.execute(
            """
            SELECT * FROM check_logs
            ORDER BY id DESC
            LIMIT 8
            """
        ).fetchall()

    status_counts: Dict[str, int] = {"online": 0, "warning": 0, "offline": 0}
    type_counts: Dict[str, int] = {}
    env_counts: Dict[str, int] = {}
    for item in instances:
        status_counts[item["status"]] = status_counts.get(item["status"], 0) + 1
        type_counts[item["db_type"]] = type_counts.get(item["db_type"], 0) + 1
        env_counts[item["environment"]] = env_counts.get(item["environment"], 0) + 1

    backup_counts: Dict[str, int] = {"success": 0, "running": 0, "pending": 0, "failed": 0}
    for item in backups:
        backup_counts[item["status"]] = backup_counts.get(item["status"], 0) + 1

    total = len(instances)
    healthy = status_counts.get("online", 0)
    health_score = round((healthy / total) * 100, 1) if total else 100

    return {
        "instances": {
            "total": total,
            "status": status_counts,
            "types": type_counts,
            "environments": env_counts,
            "health_score": health_score,
        },
        "backups": {
            "total": len(backups),
            "status": backup_counts,
        },
        "recent_checks": [dict(row) for row in recent_logs],
    }


# =====================================================================
# 慢 SQL 模块：腾讯云凭证 / 实例绑定 / 慢查询 / 同步状态
# =====================================================================

SQL_MAX_LEN = 65535  # SQL 文本最大存储字节
_UNSET = object()


def _append_slow_sql_schema(script: str) -> str:
    """在 init_db() 末尾追加慢 SQL 模块的 4 张表。"""
    extra = """
            CREATE TABLE IF NOT EXISTS tencent_credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                secret_id TEXT NOT NULL,
                secret_key_enc TEXT NOT NULL,
                endpoint_suffix TEXT NOT NULL DEFAULT 'tencentcloudapi.com',
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS instance_tc_bindings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id INTEGER NOT NULL,
                tc_product TEXT NOT NULL,
                tc_instance_id TEXT NOT NULL,
                tc_region TEXT NOT NULL,
                credential_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(instance_id, tc_product, tc_instance_id),
                FOREIGN KEY(instance_id) REFERENCES instances(id) ON DELETE CASCADE,
                FOREIGN KEY(credential_id) REFERENCES tencent_credentials(id) ON DELETE RESTRICT
            );
            CREATE INDEX IF NOT EXISTS idx_bindings_enabled ON instance_tc_bindings(enabled);
            CREATE INDEX IF NOT EXISTS idx_bindings_instance ON instance_tc_bindings(instance_id);

            CREATE TABLE IF NOT EXISTS slow_queries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                binding_id INTEGER NOT NULL,
                instance_id INTEGER NOT NULL,
                tc_product TEXT NOT NULL,
                tc_instance_id TEXT NOT NULL,
                tc_region TEXT NOT NULL,
                database TEXT,
                user_name TEXT,
                user_host TEXT,
                sql_text TEXT NOT NULL,
                sql_template TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                tc_md5 TEXT,
                query_time_ms INTEGER NOT NULL,
                lock_time_ms INTEGER NOT NULL DEFAULT 0,
                rows_examined INTEGER NOT NULL DEFAULT 0,
                rows_sent INTEGER NOT NULL DEFAULT 0,
                ts INTEGER NOT NULL,
                ts_iso TEXT NOT NULL,
                ingested_at TEXT NOT NULL,
                UNIQUE(binding_id, ts, fingerprint, user_host, database),
                FOREIGN KEY(binding_id) REFERENCES instance_tc_bindings(id) ON DELETE CASCADE,
                FOREIGN KEY(instance_id) REFERENCES instances(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_slow_ts ON slow_queries(ts);
            CREATE INDEX IF NOT EXISTS idx_slow_fingerprint ON slow_queries(fingerprint);
            CREATE INDEX IF NOT EXISTS idx_slow_instance_ts ON slow_queries(instance_id, ts);
            CREATE INDEX IF NOT EXISTS idx_slow_tc ON slow_queries(tc_product, tc_instance_id);
            CREATE INDEX IF NOT EXISTS idx_slow_database ON slow_queries(database);

            CREATE TABLE IF NOT EXISTS slow_query_sync_state (
                binding_id INTEGER PRIMARY KEY,
                last_poll_at TEXT,
                last_success_at TEXT,
                last_ts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(binding_id) REFERENCES instance_tc_bindings(id) ON DELETE CASCADE
            );
    """
    return script + extra


def _append_auth_schema(script: str) -> str:
    extra = """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);

            CREATE TABLE IF NOT EXISTS user_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_user_sessions_expires ON user_sessions(expires_at);
    """
    return script + extra


def init_db() -> None:  # type: ignore[no-redef]
    with get_connection() as conn:
        base_script = """
            CREATE TABLE IF NOT EXISTS instances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                host TEXT NOT NULL,
                port INTEGER NOT NULL,
                db_type TEXT NOT NULL,
                username TEXT,
                password TEXT,
                database_name TEXT,
                version TEXT,
                environment TEXT NOT NULL DEFAULT 'prod',
                owner TEXT,
                status TEXT NOT NULL DEFAULT 'offline',
                last_check TEXT,
                remark TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS backups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                instance_id INTEGER NOT NULL,
                instance_name TEXT NOT NULL,
                backup_type TEXT NOT NULL,
                size TEXT,
                start_time TEXT,
                end_time TEXT,
                status TEXT NOT NULL,
                operator TEXT NOT NULL DEFAULT '系统',
                FOREIGN KEY(instance_id) REFERENCES instances(id)
            );

            CREATE TABLE IF NOT EXISTS check_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id INTEGER,
                instance_name TEXT,
                db_type TEXT,
                host TEXT NOT NULL,
                port INTEGER NOT NULL,
                success INTEGER NOT NULL,
                message TEXT NOT NULL,
                version TEXT,
                response_time REAL,
                checked_at TEXT NOT NULL
            );
        """
        conn.executescript(_append_auth_schema(_append_slow_sql_schema(base_script)))
        _ensure_backup_cloud_columns(conn)
        cleanup_orphan_slow_sql_state(conn)
        purge_old_slow_queries_with_conn(conn)
        purge_expired_user_sessions_with_conn(conn)
        instance_count = conn.execute("SELECT COUNT(*) FROM instances").fetchone()[0]
        if instance_count == 0:
            seed_data(conn)
        _ensure_default_admin(conn)


def cleanup_orphan_slow_sql_state(conn: sqlite3.Connection) -> None:
    """清理外键开启前残留的绑定同步状态和慢 SQL 记录。"""
    conn.execute(
        """
        DELETE FROM slow_query_sync_state
        WHERE binding_id NOT IN (SELECT id FROM instance_tc_bindings)
        """
    )
    conn.execute(
        """
        DELETE FROM slow_queries
        WHERE binding_id NOT IN (SELECT id FROM instance_tc_bindings)
        """
    )


def _ensure_backup_cloud_columns(conn: sqlite3.Connection) -> None:
    """为旧 SQLite 库补齐腾讯云备份同步字段。"""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(backups)").fetchall()}
    additions = {
        "cloud_provider": "cloud_provider TEXT",
        "tc_product": "tc_product TEXT",
        "tc_region": "tc_region TEXT",
        "tc_instance_id": "tc_instance_id TEXT",
        "tc_backup_id": "tc_backup_id TEXT",
        "raw_status": "raw_status TEXT",
        "synced_at": "synced_at TEXT",
    }
    for name, ddl in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE backups ADD COLUMN {ddl}")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_backups_cloud_key
        ON backups(cloud_provider, tc_product, tc_region, tc_instance_id, tc_backup_id)
        """
    )


def slow_query_retention_cutoff_ts(retention_days: Optional[int] = None) -> int:
    days = SLOW_QUERY_RETENTION_DAYS if retention_days is None else max(1, int(retention_days))
    return int(time.time()) - days * SECONDS_PER_DAY


def purge_old_slow_queries_with_conn(
    conn: sqlite3.Connection,
    retention_days: Optional[int] = None,
) -> int:
    """删除保留期以前的慢 SQL，返回删除行数。"""
    cutoff_ts = slow_query_retention_cutoff_ts(retention_days)
    cur = conn.execute("DELETE FROM slow_queries WHERE ts < ?", (cutoff_ts,))
    return cur.rowcount if cur.rowcount > 0 else 0


def purge_old_slow_queries(retention_days: Optional[int] = None) -> int:
    with get_connection() as conn:
        return purge_old_slow_queries_with_conn(conn, retention_days)


def sanitize_user(row: sqlite3.Row) -> Dict[str, Any]:
    data = dict(row)
    data["enabled"] = bool(data.get("enabled"))
    data.pop("password_salt", None)
    data.pop("password_hash", None)
    return data


def internal_user(row: sqlite3.Row) -> Dict[str, Any]:
    data = dict(row)
    data["enabled"] = bool(data.get("enabled"))
    return data


def _ensure_default_admin(conn: sqlite3.Connection) -> None:
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if count:
        return
    ts = now()
    salt_hex, hash_hex = hash_password(BOOTSTRAP_ADMIN_PASSWORD)
    conn.execute(
        """
        INSERT INTO users (
            username, display_name, password_salt, password_hash, role, enabled, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            BOOTSTRAP_ADMIN_USERNAME,
            BOOTSTRAP_ADMIN_DISPLAY_NAME,
            salt_hex,
            hash_hex,
            USER_ROLE_DBA,
            ts,
            ts,
        ),
    )


def list_users() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY role, id").fetchall()
    return [sanitize_user(row) for row in rows]


def get_user(user_id: int, include_secret: bool = False) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        return None
    return internal_user(row) if include_secret else sanitize_user(row)


def get_user_by_username(username: str, include_secret: bool = False) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not row:
        return None
    return internal_user(row) if include_secret else sanitize_user(row)


def create_user(payload: Dict[str, Any]) -> Dict[str, Any]:
    ts = now()
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO users (
                username, display_name, password_salt, password_hash, role, enabled, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["username"],
                payload["display_name"],
                payload["password_salt"],
                payload["password_hash"],
                payload["role"],
                1 if payload.get("enabled", True) else 0,
                ts,
                ts,
            ),
        )
        user_id = cursor.lastrowid
    created = get_user(user_id)
    assert created is not None
    return created


def update_user(user_id: int, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    field_map = {
        "display_name": "display_name",
        "role": "role",
        "enabled": "enabled",
        "password_salt": "password_salt",
        "password_hash": "password_hash",
    }
    updates = []
    params: List[Any] = []
    for source, column in field_map.items():
        if source not in payload or payload[source] is None:
            continue
        value = payload[source]
        if source == "enabled":
            value = 1 if value else 0
        updates.append(f"{column} = ?")
        params.append(value)
    if not updates:
        return get_user(user_id)
    updates.append("updated_at = ?")
    params.append(now())
    params.append(user_id)
    with get_connection() as conn:
        cursor = conn.execute(
            f"UPDATE users SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        if cursor.rowcount == 0:
            return None
    return get_user(user_id)


def delete_user(user_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return cursor.rowcount > 0


def count_enabled_dba_users(exclude_user_id: Optional[int] = None) -> int:
    clauses = ["role = ?", "enabled = 1"]
    params: List[Any] = [USER_ROLE_DBA]
    if exclude_user_id is not None:
        clauses.append("id != ?")
        params.append(exclude_user_id)
    with get_connection() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS c FROM users WHERE {' AND '.join(clauses)}",
            params,
        ).fetchone()
    return int(row["c"] if row else 0)


def create_user_session(user_id: int, token_hash: str, expires_at: str) -> None:
    ts = now()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO user_sessions (token_hash, user_id, created_at, expires_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (token_hash, user_id, ts, expires_at, ts),
        )


def get_user_by_session_token_hash(token_hash: str) -> Optional[Dict[str, Any]]:
    purge_expired_user_sessions()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT u.*, s.expires_at, s.last_seen_at, s.created_at AS session_created_at
            FROM user_sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ?
              AND s.expires_at > ?
              AND u.enabled = 1
            """,
            (token_hash, now()),
        ).fetchone()
    if not row:
        return None
    return internal_user(row)


def touch_user_session(token_hash: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE user_sessions SET last_seen_at = ? WHERE token_hash = ?",
            (now(), token_hash),
        )


def delete_user_session(token_hash: str) -> bool:
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM user_sessions WHERE token_hash = ?", (token_hash,))
    return cursor.rowcount > 0


def delete_user_sessions_for_user(user_id: int) -> int:
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))
    return cursor.rowcount if cursor.rowcount > 0 else 0


def purge_expired_user_sessions_with_conn(conn: sqlite3.Connection) -> int:
    cursor = conn.execute("DELETE FROM user_sessions WHERE expires_at <= ?", (now(),))
    return cursor.rowcount if cursor.rowcount > 0 else 0


def purge_expired_user_sessions() -> int:
    with get_connection() as conn:
        return purge_expired_user_sessions_with_conn(conn)


# ========== 凭证 CRUD ==========

def list_credentials() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM tencent_credentials
            WHERE name != ?
            ORDER BY is_default DESC, id
            """,
            (SELF_HOSTED_CREDENTIAL_NAME,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_credential(credential_id: int, include_secret: bool = False) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM tencent_credentials WHERE id = ?", (credential_id,)
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    if not include_secret:
        data.pop("secret_key_enc", None)
    return data


def create_credential(payload: Dict[str, Any], secret_key_enc: str) -> Dict[str, Any]:
    ts = now()
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO tencent_credentials (
                name, secret_id, secret_key_enc, endpoint_suffix,
                is_default, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["name"],
                payload["secret_id"],
                secret_key_enc,
                payload.get("endpoint_suffix") or "tencentcloudapi.com",
                1 if payload.get("is_default") else 0,
                ts,
                ts,
            ),
        )
        credential_id = cursor.lastrowid
    created = get_credential(credential_id)
    assert created is not None
    return created


def update_credential(
    credential_id: int, payload: Dict[str, Any], secret_key_enc: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    field_map = {
        "name": "name",
        "secret_id": "secret_id",
        "endpoint_suffix": "endpoint_suffix",
        "is_default": "is_default",
    }
    updates = []
    params: List[Any] = []
    for source, column in field_map.items():
        if source in payload and payload[source] is not None:
            value = payload[source]
            if source == "is_default":
                value = 1 if value else 0
            updates.append(f"{column} = ?")
            params.append(value)
    if secret_key_enc is not None:
        updates.append("secret_key_enc = ?")
        params.append(secret_key_enc)

    if not updates:
        return get_credential(credential_id)

    updates.append("updated_at = ?")
    params.append(now())
    params.append(credential_id)

    with get_connection() as conn:
        cursor = conn.execute(
            f"UPDATE tencent_credentials SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        if cursor.rowcount == 0:
            return None
    return get_credential(credential_id)


def delete_credential(credential_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM tencent_credentials WHERE id = ?", (credential_id,)
        )
    return cursor.rowcount > 0


def get_decrypted_secret_key(credential_id: int) -> Optional[str]:
    """返回解密后的 SecretKey，仅供内部调用（TC API 拉取时）。"""
    from crypto import decrypt

    cred = get_credential(credential_id, include_secret=True)
    if not cred or not cred.get("secret_key_enc"):
        return None
    return decrypt(cred["secret_key_enc"])


# ========== 绑定 CRUD ==========

def list_bindings(
    instance_id: Optional[int] = None,
    enabled_only: bool = False,
    include_self_hosted: bool = False,
) -> List[Dict[str, Any]]:
    clauses: List[str] = []
    params: List[Any] = []
    if instance_id is not None:
        clauses.append("b.instance_id = ?")
        params.append(instance_id)
    if enabled_only:
        clauses.append("b.enabled = 1")
    if not include_self_hosted:
        clauses.append("b.tc_product NOT LIKE 'self_%'")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT b.*,
               i.name AS instance_name,
               c.name AS credential_name
        FROM instance_tc_bindings b
        LEFT JOIN instances i ON i.id = b.instance_id
        LEFT JOIN tencent_credentials c ON c.id = b.credential_id
        {where}
        ORDER BY b.instance_id, b.id
    """
    with get_connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def list_self_hosted_slow_instances(
    instance_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """返回适合直接连库采集慢 SQL 的自建 MySQL/PostgreSQL 实例。"""
    clauses = ["i.db_type IN ('MySQL', 'PostgreSQL')"]
    params: List[Any] = []
    if instance_id is not None:
        clauses.append("i.id = ?")
        params.append(instance_id)
    clauses.append(
        """
        NOT EXISTS (
            SELECT 1
            FROM instance_tc_bindings b
            WHERE b.instance_id = i.id
              AND b.tc_product NOT LIKE 'self_%'
        )
        """
    )
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT i.*
            FROM instances i
            WHERE {' AND '.join(clauses)}
            ORDER BY i.id
            """,
            params,
        ).fetchall()
    return [internal_instance(row) for row in rows]


def list_self_hosted_binlog_instances() -> List[Dict[str, Any]]:
    """返回归档日志页可直接连库查询 binlog 的自建 MySQL 实例。"""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT i.*
            FROM instances i
            WHERE i.db_type = 'MySQL'
              AND NOT EXISTS (
                  SELECT 1
                  FROM instance_tc_bindings b
                  WHERE b.instance_id = i.id
                    AND b.tc_product IN ('cdb', 'cynosdb')
              )
            ORDER BY i.id
            """
        ).fetchall()
    return [internal_instance(row) for row in rows]


def get_binding(binding_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT b.*,
                   i.name AS instance_name,
                   c.name AS credential_name
            FROM instance_tc_bindings b
            LEFT JOIN instances i ON i.id = b.instance_id
            LEFT JOIN tencent_credentials c ON c.id = b.credential_id
            WHERE b.id = ?
            """,
            (binding_id,),
        ).fetchone()
    return dict(row) if row else None


def create_binding(payload: Dict[str, Any]) -> Dict[str, Any]:
    ts = now()
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO instance_tc_bindings (
                instance_id, tc_product, tc_instance_id, tc_region,
                credential_id, enabled, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["instance_id"],
                payload["tc_product"],
                payload["tc_instance_id"],
                payload["tc_region"],
                payload["credential_id"],
                1 if payload.get("enabled", True) else 0,
                ts,
                ts,
            ),
        )
        binding_id = cursor.lastrowid
    binding = get_binding(binding_id)
    assert binding is not None
    return binding


def _ensure_self_hosted_credential(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT id FROM tencent_credentials WHERE name = ?",
        (SELF_HOSTED_CREDENTIAL_NAME,),
    ).fetchone()
    if row:
        return int(row["id"])

    ts = now()
    cursor = conn.execute(
        """
        INSERT INTO tencent_credentials (
            name, secret_id, secret_key_enc, endpoint_suffix,
            is_default, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 0, ?, ?)
        """,
        (
            SELF_HOSTED_CREDENTIAL_NAME,
            "self-hosted",
            "not-used",
            "local",
            ts,
            ts,
        ),
    )
    return int(cursor.lastrowid)


def get_or_create_self_hosted_binding(instance_id: int, product: str) -> Dict[str, Any]:
    if product not in SELF_HOSTED_PRODUCTS:
        raise ValueError(f"unsupported self-hosted product: {product}")

    ts = now()
    tc_instance_id = f"self:{instance_id}"
    with get_connection() as conn:
        instance = conn.execute(
            "SELECT id FROM instances WHERE id = ?", (instance_id,)
        ).fetchone()
        if not instance:
            raise ValueError("实例不存在")

        existing = conn.execute(
            """
            SELECT id
            FROM instance_tc_bindings
            WHERE instance_id = ?
              AND tc_product = ?
              AND tc_instance_id = ?
            """,
            (instance_id, product, tc_instance_id),
        ).fetchone()
        if existing:
            binding_id = int(existing["id"])
        else:
            credential_id = _ensure_self_hosted_credential(conn)
            cursor = conn.execute(
                """
                INSERT INTO instance_tc_bindings (
                    instance_id, tc_product, tc_instance_id, tc_region,
                    credential_id, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    instance_id,
                    product,
                    tc_instance_id,
                    SELF_HOSTED_REGION,
                    credential_id,
                    ts,
                    ts,
                ),
            )
            binding_id = int(cursor.lastrowid)

    binding = get_binding(binding_id)
    assert binding is not None
    return binding


def update_binding(binding_id: int, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    field_map = {
        "tc_product": "tc_product",
        "tc_instance_id": "tc_instance_id",
        "tc_region": "tc_region",
        "credential_id": "credential_id",
        "enabled": "enabled",
    }
    updates = []
    params: List[Any] = []
    for source, column in field_map.items():
        if source in payload and payload[source] is not None:
            value = payload[source]
            if source == "enabled":
                value = 1 if value else 0
            updates.append(f"{column} = ?")
            params.append(value)

    if not updates:
        return get_binding(binding_id)

    updates.append("updated_at = ?")
    params.append(now())
    params.append(binding_id)

    with get_connection() as conn:
        cursor = conn.execute(
            f"UPDATE instance_tc_bindings SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        if cursor.rowcount == 0:
            return None
    return get_binding(binding_id)


def delete_binding(binding_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM instance_tc_bindings WHERE id = ?", (binding_id,)
        )
    return cursor.rowcount > 0


# ========== 同步状态 ==========

def get_sync_state(binding_id: int) -> Dict[str, Any]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM slow_query_sync_state WHERE binding_id = ?", (binding_id,)
        ).fetchone()
    if not row:
        return {
            "binding_id": binding_id,
            "last_poll_at": None,
            "last_success_at": None,
            "last_ts": 0,
            "last_error": None,
            "consecutive_failures": 0,
        }
    return dict(row)


def upsert_sync_state(
    binding_id: int,
    last_poll_at: Optional[str] = None,
    last_success_at: Optional[str] = None,
    last_ts: Optional[int] = None,
    last_error: Any = _UNSET,
    consecutive_failures: Optional[int] = None,
) -> None:
    """合并式更新：未传入的字段保持不变；last_error 可显式传 None 清空。"""
    fields: Dict[str, Any] = {}
    if last_poll_at is not None:
        fields["last_poll_at"] = last_poll_at
    if last_success_at is not None:
        fields["last_success_at"] = last_success_at
    if last_ts is not None:
        fields["last_ts"] = last_ts
    if last_error is not _UNSET:
        fields["last_error"] = last_error
    if consecutive_failures is not None:
        fields["consecutive_failures"] = consecutive_failures

    if not fields:
        return

    with get_connection() as conn:
        existing = conn.execute(
            "SELECT binding_id FROM slow_query_sync_state WHERE binding_id = ?",
            (binding_id,),
        ).fetchone()
        if existing:
            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(
                f"UPDATE slow_query_sync_state SET {sets} WHERE binding_id = ?",
                [*fields.values(), binding_id],
            )
        else:
            cols = ["binding_id"] + list(fields.keys())
            placeholders = ",".join(["?"] * len(cols))
            conn.execute(
                f"INSERT INTO slow_query_sync_state ({','.join(cols)}) VALUES ({placeholders})",
                [binding_id, *fields.values()],
            )


# ========== 慢查询 写入 / 查询 ==========

def insert_slow_queries(rows: List[Dict[str, Any]]) -> int:
    """批量 INSERT OR IGNORE，返回实际插入的条数。"""
    if not rows:
        return 0
    payload = []
    for r in rows:
        sql_text = (r.get("sql_text") or "")[:SQL_MAX_LEN]
        payload.append(
            (
                r["binding_id"],
                r["instance_id"],
                r["tc_product"],
                r["tc_instance_id"],
                r["tc_region"],
                r.get("database"),
                r.get("user_name"),
                r.get("user_host"),
                sql_text,
                r["sql_template"],
                r["fingerprint"],
                r.get("tc_md5"),
                int(r["query_time_ms"]),
                int(r.get("lock_time_ms", 0)),
                int(r.get("rows_examined", 0)),
                int(r.get("rows_sent", 0)),
                int(r["ts"]),
                r["ts_iso"],
                now(),
            )
        )

    inserted = 0
    with get_connection() as conn:
        before = conn.execute("SELECT COUNT(*) FROM slow_queries").fetchone()[0]
        conn.executemany(
            """
            INSERT OR IGNORE INTO slow_queries (
                binding_id, instance_id, tc_product, tc_instance_id, tc_region,
                database, user_name, user_host, sql_text, sql_template, fingerprint, tc_md5,
                query_time_ms, lock_time_ms, rows_examined, rows_sent,
                ts, ts_iso, ingested_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            payload,
        )
        after = conn.execute("SELECT COUNT(*) FROM slow_queries").fetchone()[0]
        inserted = after - before
    return inserted


def list_slow_queries(
    instance_id: Optional[int] = None,
    tc_product: Optional[str] = None,
    tc_region: Optional[str] = None,
    database: Optional[str] = None,
    min_query_time_ms: Optional[int] = None,
    fingerprint: Optional[str] = None,
    keyword: Optional[str] = None,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    page: int = 1,
    page_size: int = 20,
) -> Dict[str, Any]:
    clauses: List[str] = []
    params: List[Any] = []
    if instance_id is not None:
        clauses.append("s.instance_id = ?")
        params.append(instance_id)
    if tc_product:
        clauses.append("s.tc_product = ?")
        params.append(tc_product)
    if tc_region:
        clauses.append("s.tc_region = ?")
        params.append(tc_region)
    if database:
        clauses.append("s.database = ?")
        params.append(database)
    if min_query_time_ms is not None:
        clauses.append("s.query_time_ms >= ?")
        params.append(min_query_time_ms)
    if fingerprint:
        clauses.append("s.fingerprint = ?")
        params.append(fingerprint)
    if keyword:
        clauses.append("s.sql_text LIKE ?")
        params.append(f"%{keyword}%")
    if start_ts is not None:
        clauses.append("s.ts >= ?")
        params.append(start_ts)
    if end_ts is not None:
        clauses.append("s.ts <= ?")
        params.append(end_ts)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    offset = max(0, (page - 1) * page_size)

    sql_count = f"SELECT COUNT(*) FROM slow_queries s {where}"
    sql_list = f"""
        SELECT s.*, i.name AS instance_name
        FROM slow_queries s
        LEFT JOIN instances i ON i.id = s.instance_id
        {where}
        ORDER BY s.ts DESC, s.id DESC
        LIMIT ? OFFSET ?
    """
    with get_connection() as conn:
        total = conn.execute(sql_count, params).fetchone()[0]
        rows = conn.execute(sql_list, [*params, page_size, offset]).fetchall()
    return {"total": total, "items": [dict(r) for r in rows]}


def get_slow_query(slow_query_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT s.*, i.name AS instance_name
            FROM slow_queries s
            LEFT JOIN instances i ON i.id = s.instance_id
            WHERE s.id = ?
            """,
            (slow_query_id,),
        ).fetchone()
    return dict(row) if row else None


def list_slow_queries_by_fingerprint(
    fingerprint: str, exclude_id: Optional[int] = None, limit: int = 50
) -> List[Dict[str, Any]]:
    clauses = ["fingerprint = ?"]
    params: List[Any] = [fingerprint]
    if exclude_id is not None:
        clauses.append("id != ?")
        params.append(exclude_id)
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT id, ts, ts_iso, database, user_name, user_host,
                   query_time_ms, rows_examined
            FROM slow_queries
            WHERE {' AND '.join(clauses)}
            ORDER BY ts DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
    return [dict(r) for r in rows]


def slow_query_stats(
    instance_id: Optional[int] = None,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    top_limit: int = 10,
) -> Dict[str, Any]:
    clauses: List[str] = []
    params: List[Any] = []
    if instance_id is not None:
        clauses.append("instance_id = ?")
        params.append(instance_id)
    if start_ts is not None:
        clauses.append("ts >= ?")
        params.append(start_ts)
    if end_ts is not None:
        clauses.append("ts <= ?")
        params.append(end_ts)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with get_connection() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM slow_queries {where}", params
        ).fetchone()[0]

        if total == 0:
            return {
                "total": 0,
                "by_database": {},
                "by_product": {},
                "by_region": {},
                "avg_query_time_ms": 0.0,
                "p50_query_time_ms": 0.0,
                "p95_query_time_ms": 0.0,
                "max_query_time_ms": 0,
                "top_fingerprints": [],
            }

        row = conn.execute(
            f"""
            SELECT
                AVG(query_time_ms) AS avg_qt,
                MAX(query_time_ms) AS max_qt
            FROM slow_queries {where}
            """,
            params,
        ).fetchone()
        avg_qt = float(row["avg_qt"] or 0.0)
        max_qt = int(row["max_qt"] or 0)

        qt_values = [
            int(r[0])
            for r in conn.execute(
                f"SELECT query_time_ms FROM slow_queries {where} ORDER BY query_time_ms",
                params,
            ).fetchall()
        ]
        p50 = qt_values[int(len(qt_values) * 0.5)] if qt_values else 0
        p95 = qt_values[int(len(qt_values) * 0.95)] if qt_values else 0

        by_db = {
            (r["database"] or "(none)"): r["c"]
            for r in conn.execute(
                f"""
                SELECT database, COUNT(*) AS c
                FROM slow_queries {where}
                GROUP BY database
                ORDER BY c DESC
                LIMIT 20
                """,
                params,
            ).fetchall()
        }
        by_product = {
            r["tc_product"]: r["c"]
            for r in conn.execute(
                f"""
                SELECT tc_product, COUNT(*) AS c
                FROM slow_queries {where}
                GROUP BY tc_product
                """,
                params,
            ).fetchall()
        }
        by_region = {
            r["tc_region"]: r["c"]
            for r in conn.execute(
                f"""
                SELECT tc_region, COUNT(*) AS c
                FROM slow_queries {where}
                GROUP BY tc_region
                """,
                params,
            ).fetchall()
        }
        top_rows = conn.execute(
            f"""
            SELECT fingerprint, sql_template,
                   COUNT(*) AS cnt,
                   AVG(query_time_ms) AS avg_qt,
                   MAX(query_time_ms) AS max_qt,
                   MAX(ts) AS last_ts
            FROM slow_queries {where}
            GROUP BY fingerprint
            ORDER BY avg_qt DESC
            LIMIT ?
            """,
            [*params, top_limit],
        ).fetchall()

    top_fps = [
        {
            "fingerprint": r["fingerprint"],
            "sql_template": r["sql_template"],
            "count": r["cnt"],
            "avg_query_time_ms": round(float(r["avg_qt"] or 0.0), 2),
            "max_query_time_ms": int(r["max_qt"] or 0),
            "last_ts": int(r["last_ts"] or 0),
        }
        for r in top_rows
    ]

    return {
        "total": total,
        "by_database": by_db,
        "by_product": by_product,
        "by_region": by_region,
        "avg_query_time_ms": round(avg_qt, 2),
        "p50_query_time_ms": int(p50),
        "p95_query_time_ms": int(p95),
        "max_query_time_ms": max_qt,
        "top_fingerprints": top_fps,
    }


def slow_query_timeseries(
    instance_id: Optional[int] = None,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    bucket: str = "hour",
) -> List[Dict[str, Any]]:
    """bucket: hour (3600s) | day (86400s)。返回 [{bucket_ts, count, avg_qt}]。"""
    bucket_seconds = 3600 if bucket == "hour" else 86400
    clauses: List[str] = []
    params: List[Any] = []
    if instance_id is not None:
        clauses.append("instance_id = ?")
        params.append(instance_id)
    if start_ts is not None:
        clauses.append("ts >= ?")
        params.append(start_ts)
    if end_ts is not None:
        clauses.append("ts <= ?")
        params.append(end_ts)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT (ts / {bucket_seconds}) * {bucket_seconds} AS bucket_ts,
                   COUNT(*) AS cnt,
                   AVG(query_time_ms) AS avg_qt
            FROM slow_queries {where}
            GROUP BY bucket_ts
            ORDER BY bucket_ts
            """,
            params,
        ).fetchall()
    return [
        {
            "bucket_ts": int(r["bucket_ts"]),
            "count": int(r["cnt"]),
            "avg_query_time_ms": round(float(r["avg_qt"] or 0.0), 2),
        }
        for r in rows
    ]


def top_slow_fingerprints(
    instance_id: Optional[int] = None,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """按 avg query_time 排序的 TOP 模板。"""
    clauses: List[str] = []
    params: List[Any] = []
    if instance_id is not None:
        clauses.append("instance_id = ?")
        params.append(instance_id)
    if start_ts is not None:
        clauses.append("ts >= ?")
        params.append(start_ts)
    if end_ts is not None:
        clauses.append("ts <= ?")
        params.append(end_ts)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT fingerprint, sql_template,
                   COUNT(*) AS cnt,
                   AVG(query_time_ms) AS avg_qt,
                   MAX(query_time_ms) AS max_qt
            FROM slow_queries {where}
            GROUP BY fingerprint
            ORDER BY avg_qt DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
    return [
        {
            "fingerprint": r["fingerprint"],
            "sql_template": r["sql_template"],
            "count": int(r["cnt"]),
            "avg_query_time_ms": round(float(r["avg_qt"] or 0.0), 2),
            "max_query_time_ms": int(r["max_qt"] or 0),
        }
        for r in rows
    ]


def binding_poll_status_snapshot() -> Dict[str, int]:
    """统计当前 binding 数 / 启用数 / 失败 binding 数。"""
    with get_connection() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM instance_tc_bindings WHERE tc_product NOT LIKE 'self_%'"
        ).fetchone()[0]
        enabled = conn.execute(
            """
            SELECT COUNT(*)
            FROM instance_tc_bindings
            WHERE enabled = 1
              AND tc_product NOT LIKE 'self_%'
            """
        ).fetchone()[0]
        failing = conn.execute(
            """
            SELECT COUNT(*)
            FROM slow_query_sync_state s
            JOIN instance_tc_bindings b ON b.id = s.binding_id
            WHERE s.consecutive_failures > 0
              AND b.tc_product NOT LIKE 'self_%'
            """
        ).fetchone()[0]
    return {"total": total, "enabled": enabled, "failing": failing}
