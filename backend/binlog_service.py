# -*- coding: utf-8 -*-
from __future__ import annotations

"""腾讯云 binlog 查询与下载地址获取。"""

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import storage
from tc_client import TCClient


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


def list_binlogs(
    binding_id: int,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    binding = storage.get_binding(binding_id)
    if not binding:
        raise ValueError("绑定不存在")

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
    raise ValueError(f"不支持的腾讯云产品: {product}")
