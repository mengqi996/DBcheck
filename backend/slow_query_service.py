# -*- coding: utf-8 -*-
"""
慢 SQL 业务逻辑：拉取 → 归一化 → 去重入库 → 更新同步状态

供 scheduler 与手动刷新端点调用。函数本身是同步阻塞；由调度器用
asyncio.to_thread 包装以避免阻塞事件循环。
"""

import time
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
