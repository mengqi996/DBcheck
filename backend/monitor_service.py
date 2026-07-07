# -*- coding: utf-8 -*-
"""Tencent Cloud Monitor integration for database metrics."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import storage

try:
    from tencentcloud.common import credential as tc_credential
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile
    from tencentcloud.common.exception.tencent_cloud_sdk_exception import (
        TencentCloudSDKException,
    )
    from tencentcloud.monitor.v20180724 import monitor_client as monitor_client_mod
    from tencentcloud.monitor.v20180724 import models as monitor_models

    _SDK_AVAILABLE = True
except ImportError:  # pragma: no cover
    _SDK_AVAILABLE = False
    tc_credential = None
    ClientProfile = None
    HttpProfile = None
    TencentCloudSDKException = Exception
    monitor_client_mod = None
    monitor_models = None


PRODUCT_CONFIG = {
    "cdb": {
        "namespace": "QCE/CDB",
        "label": "CDB MySQL",
        "default_dimension": "InstanceId",
    },
    "cynosdb": {
        "namespace": "QCE/CYNOSDB_MYSQL",
        "label": "TDSQL-C MySQL",
        "default_dimension": "InstanceId",
    },
    "postgres": {
        "namespace": "QCE/POSTGRES",
        "label": "PostgreSQL",
        "default_dimension": "resourceId",
    },
}


def _client_for_binding(binding: Dict[str, Any]):
    if not _SDK_AVAILABLE:
        raise RuntimeError("tencentcloud-sdk-python-monitor 未安装")

    cred = storage.get_credential(binding["credential_id"], include_secret=False)
    secret_key = storage.get_decrypted_secret_key(binding["credential_id"])
    if not cred or not secret_key:
        raise RuntimeError("腾讯云凭证缺失或解密失败")

    http_profile = HttpProfile()
    http_profile.endpoint = f"monitor.{cred.get('endpoint_suffix') or 'tencentcloudapi.com'}"
    client_profile = ClientProfile()
    client_profile.httpProfile = http_profile
    return monitor_client_mod.MonitorClient(
        tc_credential.Credential(cred["secret_id"], secret_key),
        binding["tc_region"],
        client_profile,
    )


def _to_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _default_dimensions(binding: Dict[str, Any]) -> Dict[str, str]:
    cfg = PRODUCT_CONFIG[binding["tc_product"]]
    return {cfg["default_dimension"]: binding["tc_instance_id"]}


def _flatten_metric_dimensions(metric: Any) -> List[str]:
    out: List[str] = []
    for item in getattr(metric, "Dimensions", None) or []:
        for dim in getattr(item, "Dimensions", None) or []:
            if dim not in out:
                out.append(dim)
    return out


def _metric_query_dimensions(
    binding: Dict[str, Any],
    dimension_names: List[str],
) -> Dict[str, str]:
    defaults = _default_dimensions(binding)
    product = binding["tc_product"]
    if product == "cdb" and set(dimension_names) == {"instanceid", "insttype"}:
        return defaults
    if not dimension_names:
        return defaults
    if all(name in defaults for name in dimension_names):
        return {name: defaults[name] for name in dimension_names}
    return {name: defaults.get(name, "") for name in dimension_names}


def _normalize_metric(metric: Any, binding: Dict[str, Any]) -> Dict[str, Any]:
    dimension_names = _flatten_metric_dimensions(metric)
    query_dimensions = _metric_query_dimensions(binding, dimension_names)
    return {
        "namespace": _to_str(getattr(metric, "Namespace", None)),
        "metric_name": _to_str(getattr(metric, "MetricName", None)) or "",
        "metric_cname": _to_str(getattr(metric, "MetricCName", None)),
        "metric_ename": _to_str(getattr(metric, "MetricEName", None)),
        "unit": _to_str(getattr(metric, "Unit", None)),
        "unit_cname": _to_str(getattr(metric, "UnitCname", None)),
        "periods": getattr(metric, "Period", None) or [],
        "dimension_names": dimension_names,
        "query_dimensions": query_dimensions,
        "query_ready": all(_to_str(v) for v in query_dimensions.values()),
        "meaning": {
            "zh": _to_str(getattr(getattr(metric, "Meaning", None), "Zh", None)),
            "en": _to_str(getattr(getattr(metric, "Meaning", None), "En", None)),
        },
    }


def _metric_by_name(binding: Dict[str, Any], metric_name: str) -> Optional[Dict[str, Any]]:
    for metric in list_metrics(binding["id"])["items"]:
        if metric["metric_name"] == metric_name:
            return metric
    return None


def _parse_time(value: Optional[str], default_dt: datetime) -> str:
    if not value:
        dt = default_dt
    else:
        s = value.strip()
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                dt = None  # type: ignore[assignment]
        if dt is None:
            try:
                parsed = datetime.fromisoformat(s)
                dt = parsed
            except ValueError:
                dt = default_dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
    return dt.astimezone(timezone(timedelta(hours=8))).replace(microsecond=0).isoformat()


def _to_iso_from_ts(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone(timedelta(hours=8))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except (TypeError, ValueError, OSError):
        return ""


def list_monitor_bindings() -> Dict[str, Any]:
    rows = []
    for binding in storage.list_bindings(enabled_only=True):
        cfg = PRODUCT_CONFIG.get(binding["tc_product"])
        if not cfg:
            continue
        row = dict(binding)
        row["namespace"] = cfg["namespace"]
        row["product_label"] = cfg["label"]
        row["default_dimensions"] = _default_dimensions(binding)
        rows.append(row)
    return {"total": len(rows), "items": rows}


def list_metrics(binding_id: int) -> Dict[str, Any]:
    binding = storage.get_binding(binding_id)
    if not binding:
        raise ValueError("绑定不存在")
    cfg = PRODUCT_CONFIG.get(binding["tc_product"])
    if not cfg:
        raise ValueError(f"不支持的腾讯云产品: {binding['tc_product']}")

    client = _client_for_binding(binding)
    req = monitor_models.DescribeBaseMetricsRequest()
    req.Namespace = cfg["namespace"]
    resp = client.DescribeBaseMetrics(req)
    items = [
        _normalize_metric(metric, binding)
        for metric in (getattr(resp, "MetricSet", None) or [])
        if _to_str(getattr(metric, "MetricName", None))
    ]
    items.sort(key=lambda m: (not m["query_ready"], m["metric_cname"] or m["metric_name"]))
    return {
        "binding": binding,
        "namespace": cfg["namespace"],
        "total": len(items),
        "items": items,
    }


def get_metric_data(
    binding_id: int,
    metric_name: str,
    period: int = 60,
    range_hours: int = 1,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    dimensions: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    binding = storage.get_binding(binding_id)
    if not binding:
        raise ValueError("绑定不存在")
    cfg = PRODUCT_CONFIG.get(binding["tc_product"])
    if not cfg:
        raise ValueError(f"不支持的腾讯云产品: {binding['tc_product']}")

    metric = _metric_by_name(binding, metric_name)
    query_dimensions = dict(metric["query_dimensions"] if metric else _default_dimensions(binding))
    for key, value in (dimensions or {}).items():
        if _to_str(value):
            query_dimensions[key] = str(value).strip()
    if not query_dimensions or any(not _to_str(v) for v in query_dimensions.values()):
        raise ValueError("监控维度不完整，请补充维度值")

    now_dt = datetime.now(timezone(timedelta(hours=8)))
    end_str = _parse_time(end_time, now_dt)
    start_str = _parse_time(start_time, now_dt - timedelta(hours=max(1, range_hours)))

    client = _client_for_binding(binding)
    req = monitor_models.GetMonitorDataRequest()
    req.Namespace = cfg["namespace"]
    req.MetricName = metric_name
    req.Period = int(period)
    req.StartTime = start_str
    req.EndTime = end_str

    instance = monitor_models.Instance()
    dims = []
    for name, value in query_dimensions.items():
        dim = monitor_models.Dimension()
        dim.Name = name
        dim.Value = value
        dims.append(dim)
    instance.Dimensions = dims
    req.Instances = [instance]

    resp = client.GetMonitorData(req)
    series = []
    for point in getattr(resp, "DataPoints", None) or []:
        values = getattr(point, "Values", None) or []
        timestamps = getattr(point, "Timestamps", None) or []
        rows = [
            {"ts": ts, "ts_iso": _to_iso_from_ts(ts), "value": val}
            for ts, val in zip(timestamps, values)
        ]
        series.append(
            {
                "dimensions": {
                    _to_str(getattr(dim, "Name", None)) or "": _to_str(getattr(dim, "Value", None))
                    for dim in (getattr(point, "Dimensions", None) or [])
                },
                "points": rows,
                "latest": values[-1] if values else None,
                "min": min(values) if values else None,
                "max": max(values) if values else None,
                "avg": round(sum(values) / len(values), 4) if values else None,
            }
        )

    return {
        "binding": binding,
        "namespace": cfg["namespace"],
        "metric": metric or {"metric_name": metric_name},
        "period": getattr(resp, "Period", None) or period,
        "start_time": getattr(resp, "StartTime", None) or start_str,
        "end_time": getattr(resp, "EndTime", None) or end_str,
        "message": _to_str(getattr(resp, "Msg", None)),
        "query_dimensions": query_dimensions,
        "series": series,
    }


__all__ = [
    "TencentCloudSDKException",
    "list_monitor_bindings",
    "list_metrics",
    "get_metric_data",
]
