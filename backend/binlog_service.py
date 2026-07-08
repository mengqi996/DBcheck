# -*- coding: utf-8 -*-
from __future__ import annotations

"""Binlog / WAL 查询与下载地址获取。"""

import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import storage
from tc_client import TCClient


SELF_HOSTED_CONNECT_TIMEOUT = max(1, int(os.getenv("DBCHECK_SELF_HOSTED_CONNECT_TIMEOUT", "5")))
SELF_HOSTED_MYSQL_PRODUCT = "self_mysql"


def _default_time_window() -> tuple[str, str]:
    end = datetime.now()
    start = end - timedelta(days=1)
    return start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")


def _client_for_binding(binding: Dict[str, Any]) -> TCClient:
    cred = storage.get_credential(binding["credential_id"], include_secret=False)
    secret_key = storage.get_decrypted_secret_key(binding["credential_id"])
    if not cred or not secret_key:
        raise RuntimeError("腾讯云凭证缺失或解密失败")
    return TCClient(
        secret_id=cred["secret_id"],
        secret_key=secret_key,
        region=binding["tc_region"],
        endpoint_suffix=cred.get("endpoint_suffix") or "tencentcloudapi.com",
    )


def _cynos_cluster_id(client: TCClient, binding: Dict[str, Any]) -> str:
    instance = client.describe_cynosdb_instance(binding["tc_instance_id"])
    cluster_id: Optional[str] = instance.get("cluster_id") if instance else None
    if not cluster_id:
        raise RuntimeError("未能获取 TDSQL-C ClusterId")
    return cluster_id


def _format_bytes(value: Any) -> str:
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    return f"{size:.1f} {units[idx]}" if idx else f"{int(size)} {units[idx]}"


def _mysql_connect(instance: Dict[str, Any]):
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


def _fetch_mysql_binlog_variables(conn: Any) -> Dict[str, Any]:
    names = (
        "log_bin",
        "log_bin_basename",
        "log_bin_index",
        "binlog_format",
        "binlog_expire_logs_seconds",
        "expire_logs_days",
    )
    placeholders = ", ".join(["%s"] * len(names))
    with conn.cursor() as cur:
        cur.execute(f"SHOW VARIABLES WHERE Variable_name IN ({placeholders})", names)
        rows = cur.fetchall()
    return {row.get("Variable_name"): row.get("Value") for row in rows}


def _show_mysql_binary_logs(conn: Any) -> list[Dict[str, Any]]:
    with conn.cursor() as cur:
        try:
            cur.execute("SHOW BINARY LOGS")
        except Exception:
            cur.execute("SHOW MASTER LOGS")
        return list(cur.fetchall())


def _list_self_hosted_mysql_binlogs(
    binding: Dict[str, Any],
    start_time: Optional[str],
    end_time: Optional[str],
    limit: int,
) -> Dict[str, Any]:
    instance = storage.get_instance(binding["instance_id"], include_secret=True)
    if not instance:
        raise ValueError("实例不存在")
    if instance.get("db_type") != "MySQL":
        raise ValueError("自建 binlog 查询仅支持 MySQL 实例")

    conn = _mysql_connect(instance)
    try:
        variables = _fetch_mysql_binlog_variables(conn)
        raw_rows = _show_mysql_binary_logs(conn)
    finally:
        conn.close()

    selected = list(reversed(raw_rows))[:limit]
    rows = []
    for row in selected:
        file_name = row.get("Log_name") or row.get("Log_name".lower()) or row.get("File_name")
        file_size = row.get("File_size") or row.get("File_size".lower())
        if not file_name:
            continue
        rows.append(
            {
                "binding_id": binding["id"],
                "instance_id": binding["instance_id"],
                "instance_name": binding.get("instance_name"),
                "tc_product": SELF_HOSTED_MYSQL_PRODUCT,
                "tc_region": "self-hosted",
                "tc_instance_id": binding["tc_instance_id"],
                "binlog_id": file_name,
                "file_name": file_name,
                "size_bytes": int(file_size or 0),
                "size": _format_bytes(file_size),
                "start_time": None,
                "end_time": None,
                "status": "success",
                "download_url": None,
            }
        )

    notices = []
    if str(variables.get("log_bin", "")).upper() not in {"ON", "1", "TRUE"}:
        notices.append("当前 MySQL 未开启 log_bin，无法产生 binlog 文件")
    if start_time or end_time:
        notices.append("自建 MySQL 只能通过 SHOW BINARY LOGS 列出可用文件，暂不支持按时间过滤")

    return {
        "binding": binding,
        "start_time": start_time,
        "end_time": end_time,
        "meta": {
            "kind": "self_mysql_binlog",
            "variables": variables,
            "notice": "；".join(notices) if notices else None,
            "supports_download_url": False,
        },
        "total": len(rows),
        "items": rows,
    }


def list_binlog_bindings() -> Dict[str, Any]:
    """Return bindings supported by the archive log page.

    Cloud bindings are managed by TC discovery. Self-hosted MySQL bindings are
    created lazily so a locally managed MySQL instance can appear in the same UI
    without requiring a slow-query refresh first.
    """
    for instance in storage.list_self_hosted_slow_instances():
        if instance.get("db_type") == "MySQL":
            storage.get_or_create_self_hosted_binding(int(instance["id"]), SELF_HOSTED_MYSQL_PRODUCT)

    supported = {"cdb", "cynosdb", "postgres", SELF_HOSTED_MYSQL_PRODUCT}
    all_bindings = storage.list_bindings(include_self_hosted=True)
    cloud_instance_ids = {
        item.get("instance_id")
        for item in all_bindings
        if item.get("tc_product") in {"cdb", "cynosdb", "postgres"}
    }
    rows = [
        item
        for item in all_bindings
        if item.get("tc_product") in supported
        and not (
            item.get("tc_product") == SELF_HOSTED_MYSQL_PRODUCT
            and item.get("instance_id") in cloud_instance_ids
        )
    ]
    return {"total": len(rows), "items": rows}


def list_binlogs(
    binding_id: int,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    binding = storage.get_binding(binding_id)
    if not binding:
        raise ValueError("绑定不存在")

    if binding["tc_product"] == SELF_HOSTED_MYSQL_PRODUCT:
        return _list_self_hosted_mysql_binlogs(binding, start_time, end_time, limit)

    if not start_time or not end_time:
        default_start, default_end = _default_time_window()
        start_time = start_time or default_start
        end_time = end_time or default_end

    client = _client_for_binding(binding)
    product = binding["tc_product"]
    meta: Dict[str, Any] = {}
    if product == "cdb":
        rows = client.describe_cdb_binlogs(
            binding["tc_instance_id"],
            start_time=start_time,
            end_time=end_time,
            max_items=limit,
        )
        meta["local_binlog_config"] = client.describe_cdb_local_binlog_config(binding["tc_instance_id"])
        if not rows and (start_time or end_time):
            fallback_rows = client.describe_cdb_binlogs(
                binding["tc_instance_id"],
                max_items=min(limit, 50),
            )
            meta["unfiltered_count"] = len(fallback_rows)
            if fallback_rows:
                meta["notice"] = "所选时间范围返回空，已展示腾讯云默认返回的最新 binlog。"
                rows = fallback_rows
        query_target = binding["tc_instance_id"]
    elif product == "cynosdb":
        query_target = _cynos_cluster_id(client, binding)
        rows = client.describe_cynosdb_binlogs(
            query_target,
            start_time=start_time,
            end_time=end_time,
            max_items=limit,
        )
    elif product == "postgres":
        query_target = binding["tc_instance_id"]
        rows = client.describe_postgres_xlogs(
            query_target,
            start_time=start_time,
            end_time=end_time,
            max_items=limit,
        )
        meta["kind"] = "postgres_xlog"
    else:
        raise ValueError(f"不支持的腾讯云产品: {product}")

    for row in rows:
        row["binding_id"] = binding["id"]
        row["instance_id"] = binding["instance_id"]
        row["instance_name"] = binding.get("instance_name")
        row["query_target"] = query_target

    return {
        "binding": binding,
        "start_time": start_time,
        "end_time": end_time,
        "meta": meta,
        "total": len(rows),
        "items": rows,
    }


def binlog_download_url(binding_id: int, binlog_id: str) -> Dict[str, Any]:
    binding = storage.get_binding(binding_id)
    if not binding:
        raise ValueError("绑定不存在")

    client = _client_for_binding(binding)
    product = binding["tc_product"]
    if product == "cynosdb":
        cluster_id = _cynos_cluster_id(client, binding)
        return {
            "download_url": client.describe_cynosdb_binlog_download_url(cluster_id, int(binlog_id)),
            "query_target": cluster_id,
        }
    if product == "cdb":
        raise ValueError("CDB binlog 列表已直接返回下载地址")
    if product == "postgres":
        return {
            "download_url": client.describe_postgres_backup_download_url(
                binding["tc_instance_id"],
                "LogBackup",
                binlog_id,
            ),
            "query_target": binding["tc_instance_id"],
        }
    if product == SELF_HOSTED_MYSQL_PRODUCT:
        raise ValueError("自建 MySQL binlog 暂不支持生成浏览器下载链接")
    raise ValueError(f"不支持的腾讯云产品: {product}")
