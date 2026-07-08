# -*- coding: utf-8 -*-
from __future__ import annotations

"""
慢 SQL 业务逻辑：拉取 → 归一化 → 去重入库 → 更新同步状态

供 scheduler 与手动刷新端点调用。函数本身是同步阻塞；由调度器用
asyncio.to_thread 包装以避免阻塞事件循环。
"""

import time
import hashlib
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import storage
from sql_fingerprint import normalize
from tc_client import TCClient, TencentCloudSDKException


# 腾讯云单次窗口硬上限 30 天；本地保留期更短时，回溯拉取窗口跟随保留期。
TC_MAX_WINDOW_SECONDS = 30 * storage.SECONDS_PER_DAY - 300
RETENTION_WINDOW_SECONDS = storage.SLOW_QUERY_RETENTION_DAYS * storage.SECONDS_PER_DAY
MAX_WINDOW_SECONDS = min(TC_MAX_WINDOW_SECONDS, RETENTION_WINDOW_SECONDS)
# 慢日志在 TC 端可能有最多 ~60s 延迟，所以 end_ts 比当前时间提前 60s
END_LAG_SECONDS = 60
SELF_HOSTED_MIN_QUERY_TIME_MS = max(0, int(os.getenv("DBCHECK_SELF_HOSTED_SLOW_MIN_MS", "1000")))
SELF_HOSTED_FETCH_LIMIT = max(1, int(os.getenv("DBCHECK_SELF_HOSTED_SLOW_LIMIT", "200")))
SELF_HOSTED_CONNECT_TIMEOUT = max(1, int(os.getenv("DBCHECK_SELF_HOSTED_CONNECT_TIMEOUT", "5")))
SELF_HOSTED_SLOW_LOG_FILE_MAX_BYTES = max(
    64 * 1024,
    int(os.getenv("DBCHECK_SELF_HOSTED_SLOW_LOG_FILE_MAX_BYTES", str(5 * 1024 * 1024))),
)
SELF_HOSTED_PG_BUCKET_SECONDS = max(
    60,
    int(os.getenv("DBCHECK_SELF_HOSTED_PG_BUCKET_SECONDS", "3600")),
)
SELF_HOSTED_PRODUCT_BY_DB_TYPE = {
    "MySQL": "self_mysql",
    "PostgreSQL": "self_postgresql",
}


def _utc_now_ts() -> int:
    return int(time.time())


def _compute_window(last_ts: int) -> tuple[int, int]:
    """根据 last_ts 与当前时间算出下次拉取窗口 (start_ts, end_ts)。

    规则：
        - start_ts = max(last_ts + 1, now - retention_window)
        - end_ts   = now - 60s
        - 若 start_ts >= end_ts，说明窗口无效，跳过
    """
    now = _utc_now_ts()
    end_ts = now - END_LAG_SECONDS
    start_ts = max(last_ts + 1, now - MAX_WINDOW_SECONDS)
    return start_ts, end_ts


def _build_client(binding: Dict[str, Any]) -> Optional[TCClient]:
    cred_id = binding.get("credential_id")
    if not cred_id:
        return None
    secret_key = storage.get_decrypted_secret_key(cred_id)
    if not secret_key:
        return None
    secret_id = storage.get_credential(cred_id, include_secret=False)["secret_id"]
    suffix = (
        storage.get_credential(cred_id, include_secret=False).get("endpoint_suffix")
        or "tencentcloudapi.com"
    )
    return TCClient(
        secret_id=secret_id,
        secret_key=secret_key,
        region=binding["tc_region"],
        endpoint_suffix=suffix,
    )


def _fetch_for_binding(client: TCClient, binding: Dict[str, Any],
                       start_ts: int, end_ts: int) -> List[Dict[str, Any]]:
    product = binding["tc_product"]
    instance = binding["tc_instance_id"]
    if product == "cdb":
        return client.describe_cdb_slow_logs(instance, start_ts, end_ts)
    if product == "cynosdb":
        return client.describe_cynosdb_slow_logs(instance, start_ts, end_ts)
    if product == "postgres":
        return client.describe_postgres_slow_logs(instance, start_ts, end_ts)
    raise ValueError(f"unsupported tc_product: {product}")


def _normalize_rows(raw_rows: List[Dict[str, Any]], binding: Dict[str, Any]) -> List[Dict[str, Any]]:
    """对每条 TC 行：归一化指纹、附加 binding 信息。"""
    out: List[Dict[str, Any]] = []
    for r in raw_rows:
        sql_text = r.get("sql_text") or ""
        if not sql_text:
            continue
        template, fp = normalize(sql_text)
        out.append(
            {
                "binding_id": binding["id"],
                "instance_id": binding["instance_id"],
                "tc_product": binding["tc_product"],
                "tc_instance_id": binding["tc_instance_id"],
                "tc_region": binding["tc_region"],
                "database": r.get("database"),
                "user_name": r.get("user_name"),
                "user_host": r.get("user_host"),
                "sql_text": sql_text,
                "sql_template": template,
                "fingerprint": fp,
                "tc_md5": r.get("tc_md5"),
                "query_time_ms": int(r.get("query_time_ms", 0)),
                "lock_time_ms": int(r.get("lock_time_ms", 0)),
                "rows_examined": int(r.get("rows_examined", 0)),
                "rows_sent": int(r.get("rows_sent", 0)),
                "ts": int(r.get("ts", 0)),
                "ts_iso": r.get("ts_iso") or "",
            }
        )
    return out


def poll_one_binding(binding: Dict[str, Any]) -> Dict[str, Any]:
    """拉取一个 binding 的慢日志。结果：

    {
        "binding_id": int,
        "fetched": int,        # TC 返回条数
        "inserted": int,       # 实际入库条数（去重后）
        "skipped": int,        # 因重复被忽略的条数
        "window": [start_ts, end_ts],
        "new_high_water": int, # 本次窗口内最大 ts
        "error": str | None,
    }
    """
    binding_id = binding["id"]
    sync_state = storage.get_sync_state(binding_id)
    last_ts = int(sync_state.get("last_ts") or 0)

    start_ts, end_ts = _compute_window(last_ts)
    result: Dict[str, Any] = {
        "binding_id": binding_id,
        "fetched": 0,
        "inserted": 0,
        "skipped": 0,
        "window": [start_ts, end_ts],
        "new_high_water": last_ts,
        "error": None,
    }

    if start_ts >= end_ts:
        # 还没过 60s 窗口；仅更新 last_poll_at
        storage.upsert_sync_state(binding_id, last_poll_at=_now_iso())
        return result

    storage.upsert_sync_state(binding_id, last_poll_at=_now_iso())

    try:
        client = _build_client(binding)
        if client is None:
            raise RuntimeError("无法构造 TC 客户端（凭证缺失或解密失败）")

        raw_rows = _fetch_for_binding(client, binding, start_ts, end_ts)
        normalized = _normalize_rows(raw_rows, binding)
        result["fetched"] = len(raw_rows)
        inserted = storage.insert_slow_queries(normalized)
        result["inserted"] = inserted
        result["skipped"] = len(normalized) - inserted

        new_ts = last_ts
        for r in normalized:
            if r["ts"] > new_ts:
                new_ts = r["ts"]
        result["new_high_water"] = new_ts

        storage.upsert_sync_state(
            binding_id,
            last_success_at=_now_iso(),
            last_ts=new_ts if new_ts > last_ts else last_ts,
            last_error=None,
            consecutive_failures=0,
        )
        return result

    except TencentCloudSDKException as e:
        msg = f"TC SDK error: {e}"
        result["error"] = msg
        storage.upsert_sync_state(
            binding_id,
            last_error=msg,
            consecutive_failures=int(sync_state.get("consecutive_failures") or 0) + 1,
        )
        return result
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"
        result["error"] = msg
        storage.upsert_sync_state(
            binding_id,
            last_error=msg,
            consecutive_failures=int(sync_state.get("consecutive_failures") or 0) + 1,
        )
        return result


def poll_all_enabled() -> List[Dict[str, Any]]:
    """对所有启用 binding 执行一次同步。供调度器与手动刷新使用。"""
    bindings = storage.list_bindings(enabled_only=True)
    results = [poll_one_binding(b) for b in bindings]
    storage.purge_old_slow_queries()
    return results


def _ms(value: Any) -> int:
    try:
        return max(0, int(round(float(value or 0))))
    except (TypeError, ValueError):
        return 0


def _hash_signature(*parts: Any) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _utc_iso_from_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _normalize_self_hosted_rows(
    raw_rows: List[Dict[str, Any]],
    binding: Dict[str, Any],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for r in raw_rows:
        sql_text = (r.get("sql_text") or "").strip()
        if not sql_text:
            continue
        template, fp = normalize(sql_text)
        rows.append(
            {
                "binding_id": binding["id"],
                "instance_id": binding["instance_id"],
                "tc_product": binding["tc_product"],
                "tc_instance_id": binding["tc_instance_id"],
                "tc_region": binding["tc_region"],
                "database": r.get("database"),
                "user_name": r.get("user_name"),
                "user_host": r.get("user_host"),
                "sql_text": sql_text,
                "sql_template": template,
                "fingerprint": fp,
                "tc_md5": r.get("signature") or _hash_signature(fp, r.get("ts")),
                "query_time_ms": _ms(r.get("query_time_ms")),
                "lock_time_ms": _ms(r.get("lock_time_ms")),
                "rows_examined": int(r.get("rows_examined") or 0),
                "rows_sent": int(r.get("rows_sent") or 0),
                "ts": int(r.get("ts") or _utc_now_ts()),
                "ts_iso": r.get("ts_iso") or _utc_iso_from_ts(int(r.get("ts") or _utc_now_ts())),
            }
        )
    return rows


def _connect_mysql(instance: Dict[str, Any]):
    import pymysql

    return pymysql.connect(
        host=instance["host"],
        port=int(instance["port"]),
        user=instance.get("username") or "root",
        password=instance.get("password") or "",
        database=instance.get("database") or None,
        connect_timeout=SELF_HOSTED_CONNECT_TIMEOUT,
        read_timeout=max(SELF_HOSTED_CONNECT_TIMEOUT, 10),
        write_timeout=max(SELF_HOSTED_CONNECT_TIMEOUT, 10),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def _fetch_mysql_slow_log(conn: Any, start_ts: int) -> List[Dict[str, Any]]:
    sql = """
        SELECT
            UNIX_TIMESTAMP(start_time) AS ts,
            DATE_FORMAT(start_time, '%%Y-%%m-%%d %%H:%%i:%%s') AS ts_iso,
            db AS database_name,
            user_host,
            sql_text,
            TIME_TO_SEC(query_time) * 1000 AS query_time_ms,
            TIME_TO_SEC(lock_time) * 1000 AS lock_time_ms,
            rows_examined,
            rows_sent
        FROM mysql.slow_log
        WHERE start_time >= FROM_UNIXTIME(%s)
          AND sql_text IS NOT NULL
          AND sql_text NOT LIKE 'SET timestamp=%%'
          AND TIME_TO_SEC(query_time) * 1000 >= %s
        ORDER BY start_time DESC
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (start_ts, SELF_HOSTED_MIN_QUERY_TIME_MS, SELF_HOSTED_FETCH_LIMIT))
        rows = cur.fetchall()
    return [
        {
            "database": row.get("database_name"),
            "user_host": row.get("user_host"),
            "sql_text": row.get("sql_text"),
            "query_time_ms": row.get("query_time_ms"),
            "lock_time_ms": row.get("lock_time_ms"),
            "rows_examined": row.get("rows_examined"),
            "rows_sent": row.get("rows_sent"),
            "ts": int(row.get("ts") or 0),
            "ts_iso": row.get("ts_iso"),
            "signature": _hash_signature("mysql.slow_log", row.get("ts"), row.get("user_host"), row.get("sql_text")),
        }
        for row in rows
        if row.get("ts")
    ]


def _parse_mysql_time(value: str) -> Optional[int]:
    value = value.strip()
    if not value:
        return None

    try:
        if "T" in value:
            normalized = value.replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%y%m%d %H:%M:%S"):
        try:
            return int(datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    return None


def _parse_mysql_user_host(line: str) -> tuple[Optional[str], Optional[str]]:
    body = line.split(":", 1)[1].strip() if ":" in line else line.strip()
    user_name = body.split("[", 1)[0].strip() or None
    user_host = None
    if "@" in body:
        host_part = body.split("@", 1)[1].strip()
        host_match = re.search(r"\[([^\]]*)\]", host_part)
        if host_match and host_match.group(1).strip():
            user_host = host_match.group(1).strip()
        else:
            user_host = host_part.split("Id:", 1)[0].strip() or None
    return user_name, user_host


def _clean_mysql_use_database(line: str) -> Optional[str]:
    item = line.strip().rstrip(";").strip()
    if not item.lower().startswith("use "):
        return None
    database = item[4:].strip()
    if database.startswith("`") and database.endswith("`"):
        database = database[1:-1]
    return database or None


def _parse_mysql_slow_log_content(content: str, start_ts: int) -> List[Dict[str, Any]]:
    query_re = re.compile(
        r"Query_time:\s*([0-9.]+)\s+Lock_time:\s*([0-9.]+)\s+"
        r"Rows_sent:\s*(\d+)\s+Rows_examined:\s*(\d+)",
        re.IGNORECASE,
    )
    timestamp_re = re.compile(r"SET\s+timestamp\s*=\s*(\d+)", re.IGNORECASE)
    schema_re = re.compile(r"^#\s*Schema:\s*(\S+)", re.IGNORECASE)

    rows: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    def finish_current() -> None:
        nonlocal current
        if not current:
            return
        sql_text = "\n".join(current.get("sql_lines") or []).strip()
        if not sql_text:
            current = None
            return
        ts = int(current.get("ts") or 0)
        if not ts or ts < start_ts:
            current = None
            return
        query_time_ms = _ms(current.get("query_time_ms"))
        if query_time_ms < SELF_HOSTED_MIN_QUERY_TIME_MS:
            current = None
            return
        rows.append(
            {
                "database": current.get("database"),
                "user_name": current.get("user_name"),
                "user_host": current.get("user_host"),
                "sql_text": sql_text,
                "query_time_ms": query_time_ms,
                "lock_time_ms": _ms(current.get("lock_time_ms")),
                "rows_examined": int(current.get("rows_examined") or 0),
                "rows_sent": int(current.get("rows_sent") or 0),
                "ts": ts,
                "ts_iso": _utc_iso_from_ts(ts),
                "signature": _hash_signature(
                    "mysql.slow_log_file",
                    ts,
                    current.get("user_host"),
                    query_time_ms,
                    sql_text,
                ),
            }
        )
        current = None

    for raw_line in content.splitlines():
        line = raw_line.rstrip("\n")
        if line.startswith("# Time:"):
            finish_current()
            current = {"sql_lines": [], "ts": _parse_mysql_time(line.split(":", 1)[1].strip())}
            continue
        if current is None:
            continue

        if line.startswith("# User@Host:"):
            user_name, user_host = _parse_mysql_user_host(line)
            current["user_name"] = user_name
            current["user_host"] = user_host
            continue

        if line.startswith("# Query_time:"):
            match = query_re.search(line)
            if match:
                current["query_time_ms"] = float(match.group(1)) * 1000
                current["lock_time_ms"] = float(match.group(2)) * 1000
                current["rows_sent"] = int(match.group(3))
                current["rows_examined"] = int(match.group(4))
            continue

        schema_match = schema_re.match(line)
        if schema_match and schema_match.group(1) != "":
            current["database"] = schema_match.group(1)
            continue

        timestamp_match = timestamp_re.search(line)
        if timestamp_match:
            current["ts"] = int(timestamp_match.group(1))
            continue

        database = _clean_mysql_use_database(line)
        if database:
            current["database"] = database
            continue

        if line.startswith("#") or not line.strip():
            continue
        current.setdefault("sql_lines", []).append(line)

    finish_current()
    rows.sort(key=lambda item: int(item.get("ts") or 0), reverse=True)
    return rows[:SELF_HOSTED_FETCH_LIMIT]


def _fetch_mysql_slow_log_file(conn: Any, start_ts: int) -> tuple[List[Dict[str, Any]], str]:
    sql = """
        SELECT
            @@global.slow_query_log_file AS log_file,
            @@global.log_output AS log_output,
            @@global.slow_query_log AS slow_query_log,
            RIGHT(LOAD_FILE(@@global.slow_query_log_file), %s) AS content
    """
    with conn.cursor() as cur:
        cur.execute(sql, (SELF_HOSTED_SLOW_LOG_FILE_MAX_BYTES,))
        row = cur.fetchone() or {}

    log_file = row.get("log_file") or "unknown"
    content = row.get("content")
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    if not content:
        raise RuntimeError(
            "LOAD_FILE(@@global.slow_query_log_file) 返回空；"
            "需要 MySQL 账号具备 FILE 权限，且 slow_query_log_file 可被 mysqld 读取"
        )

    rows = _parse_mysql_slow_log_content(str(content), start_ts)
    return rows, f"mysql.slow_log_file:{log_file}"


def _fetch_mysql_performance_schema(conn: Any, start_ts: int) -> List[Dict[str, Any]]:
    sql = """
        SELECT
            SCHEMA_NAME AS database_name,
            DIGEST_TEXT AS sql_text,
            DIGEST AS digest,
            COUNT_STAR AS calls,
            ROUND(AVG_TIMER_WAIT / 1000000000) AS avg_ms,
            ROUND(MAX_TIMER_WAIT / 1000000000) AS max_ms,
            ROUND(SUM_LOCK_TIME / GREATEST(COUNT_STAR, 1) / 1000000000) AS lock_ms,
            ROUND(SUM_ROWS_EXAMINED / GREATEST(COUNT_STAR, 1)) AS rows_examined,
            ROUND(SUM_ROWS_SENT / GREATEST(COUNT_STAR, 1)) AS rows_sent,
            UNIX_TIMESTAMP(LAST_SEEN) AS ts,
            DATE_FORMAT(LAST_SEEN, '%%Y-%%m-%%d %%H:%%i:%%s') AS ts_iso
        FROM performance_schema.events_statements_summary_by_digest
        WHERE DIGEST_TEXT IS NOT NULL
          AND LAST_SEEN >= FROM_UNIXTIME(%s)
          AND (MAX_TIMER_WAIT / 1000000000) >= %s
          AND UPPER(DIGEST_TEXT) NOT REGEXP '^(EXPLAIN|SHOW|SET|COMMIT|ROLLBACK|BEGIN|USE)[[:space:]]'
        ORDER BY MAX_TIMER_WAIT DESC
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (start_ts, SELF_HOSTED_MIN_QUERY_TIME_MS, SELF_HOSTED_FETCH_LIMIT))
        rows = cur.fetchall()
    return [
        {
            "database": row.get("database_name"),
            "user_host": "performance_schema",
            "sql_text": row.get("sql_text"),
            "query_time_ms": row.get("max_ms") or row.get("avg_ms"),
            "lock_time_ms": row.get("lock_ms"),
            "rows_examined": row.get("rows_examined"),
            "rows_sent": row.get("rows_sent"),
            "ts": int(row.get("ts") or 0),
            "ts_iso": row.get("ts_iso"),
            "signature": _hash_signature(
                "mysql.performance_schema",
                row.get("digest"),
                row.get("calls"),
                row.get("max_ms"),
                row.get("ts"),
            ),
        }
        for row in rows
        if row.get("ts")
    ]


def _fetch_self_hosted_mysql(instance: Dict[str, Any], start_ts: int) -> tuple[List[Dict[str, Any]], str]:
    slow_log_error: Optional[str] = None
    slow_file_error: Optional[str] = None
    slow_file_source: Optional[str] = None
    conn = _connect_mysql(instance)
    try:
        try:
            rows = _fetch_mysql_slow_log(conn, start_ts)
            if rows:
                return rows, "mysql.slow_log"
        except Exception as exc:  # noqa: BLE001
            slow_log_error = f"{type(exc).__name__}: {exc}"

        try:
            rows, slow_file_source = _fetch_mysql_slow_log_file(conn, start_ts)
            if rows:
                return rows, slow_file_source
        except Exception as exc:  # noqa: BLE001
            slow_file_error = f"{type(exc).__name__}: {exc}"

        rows = _fetch_mysql_performance_schema(conn, start_ts)
        source = "performance_schema.events_statements_summary_by_digest"
        if not rows and slow_file_source:
            source = slow_file_source
        errors = []
        if slow_log_error:
            errors.append(f"slow_log 表不可用：{slow_log_error}")
        if slow_file_error:
            errors.append(f"slow_log 文件不可用：{slow_file_error}")
        if errors:
            source = f"{source}（{'；'.join(errors)}）"
        return rows, source
    finally:
        conn.close()


def _connect_postgresql(instance: Dict[str, Any]):
    import psycopg2
    import psycopg2.extras

    return psycopg2.connect(
        host=instance["host"],
        port=int(instance["port"]),
        user=instance.get("username") or "postgres",
        password=instance.get("password") or "",
        dbname=instance.get("database") or "postgres",
        connect_timeout=SELF_HOSTED_CONNECT_TIMEOUT,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def _pg_stat_statement_columns(conn: Any) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM pg_stat_statements LIMIT 0")
        return {desc.name for desc in cur.description}


def _fetch_self_hosted_postgresql(instance: Dict[str, Any], _start_ts: int) -> tuple[List[Dict[str, Any]], str]:
    bucket_ts = (_utc_now_ts() // SELF_HOSTED_PG_BUCKET_SECONDS) * SELF_HOSTED_PG_BUCKET_SECONDS
    conn = _connect_postgresql(instance)
    try:
        columns = _pg_stat_statement_columns(conn)
        if "mean_exec_time" in columns:
            mean_col = "mean_exec_time"
            max_col = "max_exec_time"
        elif "mean_time" in columns:
            mean_col = "mean_time"
            max_col = "max_time"
        else:
            raise RuntimeError("pg_stat_statements 缺少执行时间字段")

        sql = f"""
            SELECT
                d.datname AS database_name,
                u.usename AS user_name,
                s.query AS sql_text,
                s.calls AS calls,
                COALESCE(s.{mean_col}, 0) AS mean_ms,
                COALESCE(s.{max_col}, 0) AS max_ms,
                COALESCE(s.rows, 0) AS rows_sent
            FROM pg_stat_statements s
            JOIN pg_database d ON d.oid = s.dbid
            JOIN pg_user u ON u.usesysid = s.userid
            WHERE s.query IS NOT NULL
              AND COALESCE(s.{max_col}, 0) >= %s
              AND s.query !~* '^\\s*(EXPLAIN|SET|SHOW|BEGIN|COMMIT|ROLLBACK)'
            ORDER BY COALESCE(s.{max_col}, 0) DESC
            LIMIT %s
        """
        with conn.cursor() as cur:
            cur.execute(sql, (SELF_HOSTED_MIN_QUERY_TIME_MS, SELF_HOSTED_FETCH_LIMIT))
            rows = cur.fetchall()
        return [
            {
                "database": row.get("database_name"),
                "user_name": row.get("user_name"),
                "user_host": "pg_stat_statements",
                "sql_text": row.get("sql_text"),
                "query_time_ms": row.get("max_ms") or row.get("mean_ms"),
                "lock_time_ms": 0,
                "rows_examined": 0,
                "rows_sent": int(row.get("rows_sent") or 0),
                "ts": bucket_ts,
                "ts_iso": _utc_iso_from_ts(bucket_ts),
                "signature": _hash_signature(
                    "pg_stat_statements",
                    row.get("database_name"),
                    row.get("user_name"),
                    row.get("calls"),
                    row.get("max_ms"),
                    row.get("rows_sent"),
                    row.get("sql_text"),
                    bucket_ts,
                ),
            }
            for row in rows
        ], "pg_stat_statements"
    finally:
        conn.close()


def poll_self_hosted_instance(instance: Dict[str, Any]) -> Dict[str, Any]:
    db_type = instance.get("db_type")
    product = SELF_HOSTED_PRODUCT_BY_DB_TYPE.get(db_type)
    result: Dict[str, Any] = {
        "instance_id": instance.get("id"),
        "instance_name": instance.get("name"),
        "db_type": db_type,
        "fetched": 0,
        "inserted": 0,
        "skipped": 0,
        "source": None,
        "error": None,
    }
    if not product:
        result["error"] = f"不支持的自建慢 SQL 类型: {db_type}"
        return result

    try:
        binding = storage.get_or_create_self_hosted_binding(int(instance["id"]), product)
        sync_state = storage.get_sync_state(binding["id"])
        last_ts = int(sync_state.get("last_ts") or 0)
        start_ts = max(last_ts + 1, _utc_now_ts() - MAX_WINDOW_SECONDS)
        storage.upsert_sync_state(binding["id"], last_poll_at=_now_iso())

        if db_type == "MySQL":
            raw_rows, source = _fetch_self_hosted_mysql(instance, start_ts)
        elif db_type == "PostgreSQL":
            raw_rows, source = _fetch_self_hosted_postgresql(instance, start_ts)
        else:
            raise RuntimeError(f"不支持的自建慢 SQL 类型: {db_type}")

        normalized = _normalize_self_hosted_rows(raw_rows, binding)
        inserted = storage.insert_slow_queries(normalized)
        max_ts = last_ts
        for row in normalized:
            if row["ts"] > max_ts:
                max_ts = row["ts"]
        storage.upsert_sync_state(
            binding["id"],
            last_success_at=_now_iso(),
            last_ts=max_ts,
            last_error=None,
            consecutive_failures=0,
        )
        result.update(
            {
                "fetched": len(raw_rows),
                "inserted": inserted,
                "skipped": len(normalized) - inserted,
                "source": source,
            }
        )
        return result
    except Exception as exc:  # noqa: BLE001
        msg = f"{type(exc).__name__}: {exc}"
        result["error"] = msg
        try:
            if product and instance.get("id"):
                binding = storage.get_or_create_self_hosted_binding(int(instance["id"]), product)
                state = storage.get_sync_state(binding["id"])
                storage.upsert_sync_state(
                    binding["id"],
                    last_error=msg,
                    consecutive_failures=int(state.get("consecutive_failures") or 0) + 1,
                )
        except Exception:
            pass
        return result


def poll_all_self_hosted(instance_id: Optional[int] = None) -> Dict[str, Any]:
    instances = storage.list_self_hosted_slow_instances(instance_id=instance_id)
    results = [poll_self_hosted_instance(instance) for instance in instances]
    storage.purge_old_slow_queries()
    return {
        "total": len(results),
        "fetched": sum(int(r.get("fetched") or 0) for r in results),
        "inserted": sum(int(r.get("inserted") or 0) for r in results),
        "skipped": sum(int(r.get("skipped") or 0) for r in results),
        "errors": [r for r in results if r.get("error")],
        "results": results,
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# 供测试使用
def compute_window_for_test(last_ts: int, now_ts: Optional[int] = None) -> tuple[int, int]:
    """_compute_window 的可注入时间版本，便于单元测试。"""
    if now_ts is None:
        now_ts = _utc_now_ts()
    end_ts = now_ts - END_LAG_SECONDS
    start_ts = max(last_ts + 1, now_ts - MAX_WINDOW_SECONDS)
    return start_ts, end_ts
