# -*- coding: utf-8 -*-
from __future__ import annotations

"""腾讯云备份：发起云端备份任务，并同步备份元数据到本地。"""

from datetime import datetime
from typing import Any, Dict, List, Optional

import storage
from tc_client import TCClient, TencentCloudSDKException


def _client_for_binding(
    binding: Dict[str, Any],
    cache: Dict[tuple[int, str], TCClient],
) -> TCClient:
    key = (int(binding["credential_id"]), binding["tc_region"])
    if key in cache:
        return cache[key]

    cred = storage.get_credential(binding["credential_id"], include_secret=False)
    secret_key = storage.get_decrypted_secret_key(binding["credential_id"])
    if not cred or not secret_key:
        raise RuntimeError("腾讯云凭证缺失或解密失败")

    client = TCClient(
        secret_id=cred["secret_id"],
        secret_key=secret_key,
        region=binding["tc_region"],
        endpoint_suffix=cred.get("endpoint_suffix") or "tencentcloudapi.com",
    )
    cache[key] = client
    return client


def _attach_local_instance(
    backups: List[Dict[str, Any]],
    binding: Dict[str, Any],
) -> List[Dict[str, Any]]:
    out = []
    for item in backups:
        row = dict(item)
        row["instance_id"] = binding["instance_id"]
        row["instance_name"] = binding.get("instance_name") or binding["tc_instance_id"]
        row["cloud_provider"] = "tencent"
        out.append(row)
    return out


def _manual_backup_name(binding: Dict[str, Any]) -> str:
    base = binding.get("instance_name") or binding["tc_instance_id"]
    return f"DBCheck_{base}_{datetime.now().strftime('%Y%m%d%H%M%S')}"[:60]


def _cynos_cluster_id(client: TCClient, binding: Dict[str, Any]) -> str:
    instance = client.describe_cynosdb_instance(binding["tc_instance_id"])
    cluster_id = instance.get("cluster_id") if instance else None
    if not cluster_id:
        raise RuntimeError("未能获取 TDSQL-C ClusterId")
    return cluster_id


def _insert_pending_cloud_backup(
    binding: Dict[str, Any],
    tc_backup_id: str,
    backup_name: str,
) -> Dict[str, Any]:
    row = {
        "cloud_provider": "tencent",
        "tc_product": binding["tc_product"],
        "tc_region": binding["tc_region"],
        "tc_instance_id": binding["tc_instance_id"],
        "tc_backup_id": tc_backup_id,
        "name": backup_name,
        "instance_id": binding["instance_id"],
        "instance_name": binding.get("instance_name") or binding["tc_instance_id"],
        "backup_type": "full",
        "size": "-",
        "start_time": storage.now(),
        "end_time": "-",
        "status": "running",
        "operator": "腾讯云手动",
        "raw_status": "creating",
    }
    result = storage.upsert_cloud_backups([row])
    return {"row": row, **result}


def create_tencent_backup(instance_id: int, backup_type: str) -> Optional[Dict[str, Any]]:
    """如果实例已绑定腾讯云，则发起云端手动备份；未绑定时返回 None。"""
    bindings = storage.list_bindings(instance_id=instance_id, enabled_only=True)
    binding = next(
        (item for item in bindings if item["tc_product"] in {"cdb", "cynosdb", "postgres"}),
        None,
    )
    if not binding:
        return None

    if backup_type not in {"full", "manual"}:
        raise ValueError("腾讯云手动备份目前只支持全量/手动备份")

    client = _client_for_binding(binding, {})
    product = binding["tc_product"]
    backup_name = _manual_backup_name(binding)
    placeholder: Optional[Dict[str, Any]] = None

    if product == "cdb":
        result = client.create_cdb_backup(
            binding["tc_instance_id"],
            backup_method="physical",
            manual_name=backup_name,
        )
        tc_task_id = result.get("backup_id")
        if tc_task_id:
            placeholder = _insert_pending_cloud_backup(binding, tc_task_id, backup_name)
    elif product == "postgres":
        result = client.create_postgres_backup(
            binding["tc_instance_id"],
            backup_method="physical",
        )
        tc_task_id = result.get("backup_id")
        if tc_task_id:
            placeholder = _insert_pending_cloud_backup(binding, tc_task_id, backup_name)
    elif product == "cynosdb":
        cluster_id = _cynos_cluster_id(client, binding)
        result = client.create_cynosdb_backup(
            cluster_id,
            backup_type="snapshot",
            backup_name=backup_name,
        )
        tc_task_id = result.get("flow_id")
        result["query_target"] = cluster_id
    else:
        raise ValueError(f"不支持的腾讯云产品: {product}")

    return {
        "cloud_provider": "tencent",
        "tc_product": product,
        "tc_region": binding["tc_region"],
        "tc_instance_id": binding["tc_instance_id"],
        "instance_id": binding["instance_id"],
        "instance_name": binding.get("instance_name"),
        "backup_name": backup_name,
        "tc_task_id": tc_task_id,
        "request_id": result.get("request_id"),
        "backup_method": result.get("backup_method"),
        "placeholder": placeholder,
    }


def sync_tencent_backups(instance_id: Optional[int] = None) -> Dict[str, Any]:
    """同步所有已接入腾讯云实例的备份信息。"""
    bindings = storage.list_bindings(instance_id=instance_id)
    client_cache: Dict[tuple[int, str], TCClient] = {}
    cynos_cluster_cache: Dict[tuple[int, str, str], Optional[str]] = {}
    synced_cynos_clusters: set[tuple[int, str, str]] = set()
    errors: List[Dict[str, Any]] = []
    total_fetched = 0
    inserted = 0
    updated = 0
    skipped = 0

    for binding in bindings:
        product = binding["tc_product"]
        try:
            client = _client_for_binding(binding, client_cache)
            if product == "cdb":
                cloud_rows = client.describe_cdb_backups(binding["tc_instance_id"])
            elif product == "postgres":
                cloud_rows = client.describe_postgres_backups(binding["tc_instance_id"])
            elif product == "cynosdb":
                cluster_key = (
                    int(binding["credential_id"]),
                    binding["tc_region"],
                    binding["tc_instance_id"],
                )
                cluster_id = cynos_cluster_cache.get(cluster_key)
                if cluster_key not in cynos_cluster_cache:
                    instance = client.describe_cynosdb_instance(binding["tc_instance_id"])
                    cluster_id = instance.get("cluster_id") if instance else None
                    cynos_cluster_cache[cluster_key] = cluster_id
                if not cluster_id:
                    raise RuntimeError("未能获取 TDSQL-C ClusterId")
                dedupe_key = (int(binding["credential_id"]), binding["tc_region"], cluster_id)
                if dedupe_key in synced_cynos_clusters:
                    skipped += 1
                    continue
                synced_cynos_clusters.add(dedupe_key)
                cloud_rows = client.describe_cynosdb_backups(cluster_id)
            else:
                skipped += 1
                continue

            total_fetched += len(cloud_rows)
            result = storage.upsert_cloud_backups(_attach_local_instance(cloud_rows, binding))
            inserted += result["inserted"]
            updated += result["updated"]
        except TencentCloudSDKException as exc:
            errors.append(
                {
                    "instance_id": binding["instance_id"],
                    "instance_name": binding.get("instance_name"),
                    "tc_product": product,
                    "tc_instance_id": binding["tc_instance_id"],
                    "message": str(exc),
                }
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(
                {
                    "instance_id": binding["instance_id"],
                    "instance_name": binding.get("instance_name"),
                    "tc_product": product,
                    "tc_instance_id": binding["tc_instance_id"],
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )

    return {
        "bindings": len(bindings),
        "fetched": total_fetched,
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
    }
