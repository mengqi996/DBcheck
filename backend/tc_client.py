# -*- coding: utf-8 -*-
from __future__ import annotations

"""
腾讯云 OpenAPI 慢日志客户端

封装：
    * CDB (云数据库 MySQL)        — DescribeSlowLogData (Unix 秒时窗)
    * CynosDB (TDSQL-C MySQL)     — DescribeInstanceSlowQueries (字符串时窗)

两种产品的返回结构、时窗格式、字段名都不同，本模块将其归一为统一的 slow_log dict：

    {
        "ts": int,                # Unix 秒（UTC）
        "ts_iso": str,            # 'YYYY-MM-DD HH:MM:SS' UTC
        "query_time_ms": int,
        "lock_time_ms": int,
        "rows_examined": int,
        "rows_sent": int,
        "database": str | None,
        "user_name": str | None,
        "user_host": str | None,
        "sql_text": str,
        "tc_template": str | None,
        "tc_md5": str | None,
    }

SDK 异常向上抛出，调用方负责重试/退避。
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


# ---------- SDK 可选导入（允许在没装 SDK 的环境也能 import 本模块） ----------

try:
    from tencentcloud.common import credential as tc_credential
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile
    from tencentcloud.cdb.v20170320 import cdb_client as cdb_client_mod
    from tencentcloud.cdb.v20170320 import models as cdb_models
    from tencentcloud.cynosdb.v20190107 import cynosdb_client as cynosdb_client_mod
    from tencentcloud.cynosdb.v20190107 import models as cynosdb_models
    from tencentcloud.postgres.v20170312 import postgres_client as postgres_client_mod
    from tencentcloud.postgres.v20170312 import models as postgres_models
    from tencentcloud.common.exception.tencent_cloud_sdk_exception import (
        TencentCloudSDKException,
    )
    _SDK_AVAILABLE = True
except ImportError:  # pragma: no cover - 仅在 SDK 缺失时触发
    _SDK_AVAILABLE = False
    tc_credential = None
    ClientProfile = None
    HttpProfile = None
    cdb_client_mod = None
    cdb_models = None
    cynosdb_client_mod = None
    cynosdb_models = None
    postgres_client_mod = None
    postgres_models = None
    TencentCloudSDKException = Exception


# ---------- 工具 ----------

def _utc_iso(ts: int) -> str:
    """Unix 秒 → 本地时区 'YYYY-MM-DD HH:MM:SS'，用于页面展示。"""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _tc_api_time(ts: int) -> str:
    """Unix 秒 → 腾讯云 CynosDB API 查询时间字符串（UTC+8）。"""
    return datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def _tc_time_to_ts(value: Any) -> int:
    """腾讯云返回的本地时间字符串（通常 UTC+8）→ Unix 秒。"""
    s = _to_str(value)
    if not s:
        return 0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
            return int(dt.timestamp())
        except ValueError:
            continue
    return 0


def _utc_iso_from_string(s: str) -> int:
    """CDB 返回的时间戳本身就是 int；此函数为占位兼容。"""
    return 0


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _sec_to_ms(value: Any) -> int:
    """TC 返回的 QueryTime/LockTime 单位是秒（浮点），转毫秒整数。"""
    try:
        return round(float(value) * 1000)
    except (TypeError, ValueError):
        return 0


def _duration_to_ms(value: Any) -> int:
    """PostgreSQL 慢查询 Duration 字段转毫秒。"""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return 0
    if amount <= 0:
        return 0
    # Tencent PostgreSQL SDK exposes Duration as float. In practice it is a
    # duration value suitable for millisecond display; very small fractions are
    # treated as seconds to avoid showing zero for sub-second slow logs.
    if amount < 1:
        return round(amount * 1000)
    return round(amount)


def _postgres_filter(name: str, values: List[str]):
    flt = postgres_models.Filter()
    flt.Name = name
    flt.Values = values
    return flt


# ---------- 客户端 ----------

class TCClient:
    """单租户单 product 客户端（每次 poll 临时构造即可，无须复用）。"""

    def __init__(
        self,
        secret_id: str,
        secret_key: str,
        region: str,
        endpoint_suffix: str = "tencentcloudapi.com",
    ):
        if not _SDK_AVAILABLE:
            raise RuntimeError(
                "tencentcloud-sdk-python 未安装，请 pip install tencentcloud-sdk-python-cdb "
                "tencentcloud-sdk-python-cynosdb tencentcloud-sdk-python-postgres"
            )
        self._cred = tc_credential.Credential(secret_id, secret_key)
        self._region = region
        self._suffix = endpoint_suffix or "tencentcloudapi.com"

    def _client_profile(self, service: str) -> ClientProfile:
        http_profile = HttpProfile()
        http_profile.endpoint = f"{service}.{self._suffix}"
        client_profile = ClientProfile()
        client_profile.httpProfile = http_profile
        return client_profile

    def _cdb_client(self):
        return cdb_client_mod.CdbClient(
            self._cred,
            self._region,
            self._client_profile("cdb"),
        )

    def _cynosdb_client(self):
        return cynosdb_client_mod.CynosdbClient(
            self._cred,
            self._region,
            self._client_profile("cynosdb"),
        )

    def _postgres_client(self):
        return postgres_client_mod.PostgresClient(
            self._cred,
            self._region,
            self._client_profile("postgres"),
        )

    # ---------- CDB ----------

    def describe_cdb_slow_logs(
        self,
        instance_id: str,
        start_ts: int,
        end_ts: int,
        page_size: int = 800,
    ) -> List[Dict[str, Any]]:
        """CDB DescribeSlowLogData：start_ts/end_ts 为 Unix 秒。自动分页。"""
        client = self._cdb_client()
        out: List[Dict[str, Any]] = []
        offset = 0
        while True:
            req = cdb_models.DescribeSlowLogDataRequest()
            req.InstanceId = instance_id
            req.StartTime = int(start_ts)
            req.EndTime = int(end_ts)
            req.Offset = offset
            req.Limit = page_size
            resp = client.DescribeSlowLogData(req)
            items = getattr(resp, "Items", None) or []
            for item in items:
                out.append(_normalize_cdb_item(item))
            total = getattr(resp, "TotalCount", 0) or 0
            offset += len(items)
            if not items or offset >= total:
                break
        return out

    # ---------- CynosDB (TDSQL-C) ----------

    def describe_cynosdb_slow_logs(
        self,
        instance_id: str,
        start_ts: int,
        end_ts: int,
        page_size: int = 800,
    ) -> List[Dict[str, Any]]:
        """CynosDB DescribeInstanceSlowQueries：时窗为字符串。自动分页。"""
        client = self._cynosdb_client()
        out: List[Dict[str, Any]] = []
        offset = 0
        start_str = _tc_api_time(int(start_ts))
        end_str = _tc_api_time(int(end_ts))
        while True:
            req = cynosdb_models.DescribeInstanceSlowQueriesRequest()
            req.InstanceId = instance_id
            req.StartTime = start_str
            req.EndTime = end_str
            req.Offset = offset
            req.Limit = page_size
            resp = client.DescribeInstanceSlowQueries(req)
            items = getattr(resp, "SlowQueries", None) or []
            for item in items:
                out.append(_normalize_cynosdb_item(item))
            total = getattr(resp, "TotalCount", 0) or 0
            offset += len(items)
            if not items or offset >= total:
                break
        return out

    # ---------- PostgreSQL ----------

    def describe_postgres_slow_logs(
        self,
        instance_id: str,
        start_ts: int,
        end_ts: int,
        page_size: int = 100,
    ) -> List[Dict[str, Any]]:
        """PostgreSQL DescribeSlowQueryList：时窗为字符串。自动分页。"""
        client = self._postgres_client()
        out: List[Dict[str, Any]] = []
        offset = 0
        start_str = _tc_api_time(int(start_ts))
        end_str = _tc_api_time(int(end_ts))
        while True:
            req = postgres_models.DescribeSlowQueryListRequest()
            req.DBInstanceId = instance_id
            req.StartTime = start_str
            req.EndTime = end_str
            req.OrderBy = "SessionStartTime"
            req.OrderByType = "asc"
            req.Offset = offset
            req.Limit = min(page_size, 100)
            resp = client.DescribeSlowQueryList(req)
            items = getattr(resp, "RawSlowQueryList", None) or []
            for item in items:
                out.append(_normalize_postgres_item(item))
            total = getattr(resp, "TotalCount", 0) or 0
            offset += len(items)
            if not items or offset >= total:
                break
        return out

    # ---------- Binlog ----------

    def describe_cdb_binlogs(
        self,
        instance_id: str,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        page_size: int = 1000,
        max_items: int = 500,
    ) -> List[Dict[str, Any]]:
        """CDB DescribeBinlogs：按实例查询 binlog 列表。"""
        client = self._cdb_client()
        out: List[Dict[str, Any]] = []
        offset = 0
        while len(out) < max_items:
            req = cdb_models.DescribeBinlogsRequest()
            req.InstanceId = instance_id
            req.Offset = offset
            req.Limit = min(page_size, max_items - len(out))
            if start_time:
                req.MinStartTime = start_time
                req.ContainsMinStartTime = True
            if end_time:
                req.MaxStartTime = end_time
            resp = client.DescribeBinlogs(req)
            items = getattr(resp, "Items", None) or []
            for item in items:
                out.append(_normalize_cdb_binlog(item, instance_id, self._region))
            total = getattr(resp, "TotalCount", 0) or 0
            offset += len(items)
            if not items or offset >= total:
                break
        return out

    def describe_cdb_local_binlog_config(self, instance_id: str) -> Dict[str, Any]:
        """查询 CDB 本地 binlog 保留策略。"""
        client = self._cdb_client()
        req = cdb_models.DescribeLocalBinlogConfigRequest()
        req.InstanceId = instance_id
        resp = client.DescribeLocalBinlogConfig(req)
        cfg = getattr(resp, "LocalBinlogConfig", None)
        default_cfg = getattr(resp, "LocalBinlogConfigDefault", None)
        return {
            "save_hours": _to_int(getattr(cfg, "SaveHours", None), 0) if cfg else None,
            "max_usage": _to_int(getattr(cfg, "MaxUsage", None), 0) if cfg else None,
            "default_save_hours": _to_int(getattr(default_cfg, "SaveHours", None), 0) if default_cfg else None,
            "default_max_usage": _to_int(getattr(default_cfg, "MaxUsage", None), 0) if default_cfg else None,
        }

    def describe_cynosdb_binlogs(
        self,
        cluster_id: str,
        start_time: str,
        end_time: str,
        page_size: int = 100,
        max_items: int = 500,
    ) -> List[Dict[str, Any]]:
        """CynosDB DescribeBinlogs：按集群查询 binlog 列表。"""
        client = self._cynosdb_client()
        out: List[Dict[str, Any]] = []
        offset = 0
        while len(out) < max_items:
            req = cynosdb_models.DescribeBinlogsRequest()
            req.ClusterId = cluster_id
            req.StartTime = start_time
            req.EndTime = end_time
            req.Offset = offset
            req.Limit = min(page_size, max_items - len(out))
            resp = client.DescribeBinlogs(req)
            items = getattr(resp, "Binlogs", None) or []
            for item in items:
                out.append(_normalize_cynosdb_binlog(item, cluster_id, self._region))
            total = getattr(resp, "TotalCount", 0) or 0
            offset += len(items)
            if not items or offset >= total:
                break
        return out

    def describe_cynosdb_binlog_download_url(
        self,
        cluster_id: str,
        binlog_id: int,
    ) -> str:
        """CynosDB binlog 下载链接。"""
        client = self._cynosdb_client()
        req = cynosdb_models.DescribeBinlogDownloadUrlRequest()
        req.ClusterId = cluster_id
        req.BinlogId = int(binlog_id)
        resp = client.DescribeBinlogDownloadUrl(req)
        return _to_str(getattr(resp, "DownloadUrl", None)) or ""

    def describe_postgres_xlogs(
        self,
        instance_id: str,
        start_time: str,
        end_time: str,
        page_size: int = 100,
        max_items: int = 500,
    ) -> List[Dict[str, Any]]:
        """PostgreSQL DescribeDBXlogs：按实例查询 XLog/WAL 列表。"""
        client = self._postgres_client()
        out: List[Dict[str, Any]] = []
        offset = 0
        while len(out) < max_items:
            req = postgres_models.DescribeDBXlogsRequest()
            req.DBInstanceId = instance_id
            req.StartTime = start_time
            req.EndTime = end_time
            req.Offset = offset
            req.Limit = min(page_size, max_items - len(out), 100)
            resp = client.DescribeDBXlogs(req)
            items = getattr(resp, "XlogList", None) or []
            for item in items:
                out.append(_normalize_postgres_xlog(item, instance_id, self._region))
            total = getattr(resp, "TotalCount", 0) or 0
            offset += len(items)
            if not items or offset >= total:
                break
        return out

    def describe_postgres_backup_download_url(
        self,
        instance_id: str,
        backup_type: str,
        backup_id: str,
    ) -> str:
        """PostgreSQL 备份/XLog 下载链接。"""
        client = self._postgres_client()
        req = postgres_models.DescribeBackupDownloadURLRequest()
        req.DBInstanceId = instance_id
        req.BackupType = backup_type
        req.BackupId = backup_id
        req.URLExpireTime = 12
        resp = client.DescribeBackupDownloadURL(req)
        return _to_str(getattr(resp, "BackupDownloadURL", None)) or ""

    def create_cdb_backup(
        self,
        instance_id: str,
        backup_method: str = "physical",
        manual_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """CDB 手动创建备份。"""
        client = self._cdb_client()
        req = cdb_models.CreateBackupRequest()
        req.InstanceId = instance_id
        req.BackupMethod = backup_method
        if manual_name:
            req.ManualBackupName = manual_name[:60]
        resp = client.CreateBackup(req)
        return {
            "backup_id": _to_str(getattr(resp, "BackupId", None)),
            "request_id": _to_str(getattr(resp, "RequestId", None)),
            "backup_method": backup_method,
        }

    def create_cynosdb_backup(
        self,
        cluster_id: str,
        backup_type: str = "snapshot",
        backup_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """TDSQL-C 手动创建备份。"""
        client = self._cynosdb_client()
        req = cynosdb_models.CreateBackupRequest()
        req.ClusterId = cluster_id
        req.BackupType = backup_type
        if backup_name:
            req.BackupName = backup_name[:60]
        resp = client.CreateBackup(req)
        return {
            "flow_id": _to_str(getattr(resp, "FlowId", None)),
            "request_id": _to_str(getattr(resp, "RequestId", None)),
            "backup_method": backup_type,
        }

    def create_postgres_backup(
        self,
        instance_id: str,
        backup_method: str = "physical",
    ) -> Dict[str, Any]:
        """PostgreSQL 手动创建基础备份。"""
        client = self._postgres_client()
        req = postgres_models.CreateBaseBackupRequest()
        req.DBInstanceId = instance_id
        req.BackupMethod = backup_method
        resp = client.CreateBaseBackup(req)
        return {
            "backup_id": _to_str(getattr(resp, "BaseBackupId", None)),
            "request_id": _to_str(getattr(resp, "RequestId", None)),
            "backup_method": backup_method,
        }

    # ---------- 备份 ----------

    def describe_cdb_backups(
        self,
        instance_id: str,
        page_size: int = 1000,
    ) -> List[Dict[str, Any]]:
        """CDB DescribeBackups：按实例查询备份列表。"""
        client = self._cdb_client()
        out: List[Dict[str, Any]] = []
        offset = 0
        while True:
            req = cdb_models.DescribeBackupsRequest()
            req.InstanceId = instance_id
            req.Offset = offset
            req.Limit = page_size
            resp = client.DescribeBackups(req)
            items = getattr(resp, "Items", None) or []
            for item in items:
                out.append(_normalize_cdb_backup(item, instance_id, self._region))
            total = getattr(resp, "TotalCount", 0) or 0
            offset += len(items)
            if not items or offset >= total:
                break
        return out

    def describe_cynosdb_backups(
        self,
        cluster_id: str,
        page_size: int = 100,
    ) -> List[Dict[str, Any]]:
        """CynosDB DescribeBackupList：按集群查询备份列表。"""
        client = self._cynosdb_client()
        out: List[Dict[str, Any]] = []
        offset = 0
        while True:
            req = cynosdb_models.DescribeBackupListRequest()
            req.ClusterId = cluster_id
            req.DbType = "MYSQL"
            req.Offset = offset
            req.Limit = page_size
            resp = client.DescribeBackupList(req)
            items = getattr(resp, "BackupList", None) or []
            for item in items:
                out.append(_normalize_cynosdb_backup(item, cluster_id, self._region))
            total = getattr(resp, "TotalCount", 0) or 0
            offset += len(items)
            if not items or offset >= total:
                break
        return out

    def describe_postgres_backups(
        self,
        instance_id: str,
        days: int = 30,
        page_size: int = 100,
        max_items: int = 1000,
    ) -> List[Dict[str, Any]]:
        """PostgreSQL DescribeBaseBackups + DescribeLogBackups。"""
        end = datetime.now(tz=timezone(timedelta(hours=8)))
        start = end - timedelta(days=days)
        start_str = start.strftime("%Y-%m-%d %H:%M:%S")
        end_str = end.strftime("%Y-%m-%d %H:%M:%S")
        filters = [_postgres_filter("db-instance-id", [instance_id])]
        out: List[Dict[str, Any]] = []

        client = self._postgres_client()
        offset = 0
        while len(out) < max_items:
            req = postgres_models.DescribeBaseBackupsRequest()
            req.Filters = filters
            req.MinFinishTime = start_str
            req.MaxFinishTime = end_str
            req.Offset = offset
            req.Limit = min(page_size, max_items - len(out), 100)
            req.OrderBy = "FinishTime"
            req.OrderByType = "desc"
            resp = client.DescribeBaseBackups(req)
            items = getattr(resp, "BaseBackupSet", None) or []
            for item in items:
                out.append(_normalize_postgres_backup(item, instance_id, self._region, "BaseBackup"))
            total = getattr(resp, "TotalCount", 0) or 0
            offset += len(items)
            if not items or offset >= total or len(out) >= max_items:
                break

        offset = 0
        while len(out) < max_items:
            req = postgres_models.DescribeLogBackupsRequest()
            req.Filters = filters
            req.MinFinishTime = start_str
            req.MaxFinishTime = end_str
            req.Offset = offset
            req.Limit = min(page_size, max_items - len(out), 100)
            req.OrderBy = "FinishTime"
            req.OrderByType = "desc"
            resp = client.DescribeLogBackups(req)
            items = getattr(resp, "LogBackupSet", None) or []
            for item in items:
                out.append(_normalize_postgres_backup(item, instance_id, self._region, "LogBackup"))
            total = getattr(resp, "TotalCount", 0) or 0
            offset += len(items)
            if not items or offset >= total or len(out) >= max_items:
                break

        return out

    def describe_cynosdb_instance(self, instance_id: str) -> Optional[Dict[str, Any]]:
        """查询单个 CynosDB 实例，用于拿到备份接口需要的 ClusterId。"""
        client = self._cynosdb_client()
        req = cynosdb_models.DescribeInstancesRequest()
        req.InstanceIds = [instance_id]
        req.DbType = "MYSQL"
        req.ClusterType = "CYNOSDB"
        req.Offset = 0
        req.Limit = 1
        resp = client.DescribeInstances(req)
        items = getattr(resp, "InstanceSet", None) or []
        if not items:
            return None
        return _normalize_cynosdb_instance(items[0], self._region)

    # ---------- 凭证验证 ----------

    def test_credentials(self) -> bool:
        """通过 CDB 查询接口验证 SecretId/Key 签名是否正确。"""
        client = self._cdb_client()
        req = cdb_models.DescribeDBInstancesRequest()
        req.Offset = 0
        req.Limit = 1
        client.DescribeDBInstances(req)
        return True

    # ---------- 实例发现 ----------

    def describe_cdb_instances(self, page_size: int = 200) -> List[Dict[str, Any]]:
        """枚举当前 region 下的 CDB MySQL 实例。"""
        client = self._cdb_client()
        out: List[Dict[str, Any]] = []
        offset = 0
        while True:
            req = cdb_models.DescribeDBInstancesRequest()
            req.Offset = offset
            req.Limit = page_size
            resp = client.DescribeDBInstances(req)
            items = getattr(resp, "Items", None) or []
            for item in items:
                out.append(_normalize_cdb_instance(item, self._region))
            total = getattr(resp, "TotalCount", 0) or 0
            offset += len(items)
            if not items or offset >= total:
                break
        return out

    def describe_cynosdb_instances(self, page_size: int = 100) -> List[Dict[str, Any]]:
        """枚举当前 region 下的 TDSQL-C MySQL 实例。"""
        client = self._cynosdb_client()
        out: List[Dict[str, Any]] = []
        offset = 0
        while True:
            req = cynosdb_models.DescribeInstancesRequest()
            req.Offset = offset
            req.Limit = page_size
            req.DbType = "MYSQL"
            req.ClusterType = "CYNOSDB"
            resp = client.DescribeInstances(req)
            items = getattr(resp, "InstanceSet", None) or []
            for item in items:
                out.append(_normalize_cynosdb_instance(item, self._region))
            total = getattr(resp, "TotalCount", 0) or 0
            offset += len(items)
            if not items or offset >= total:
                break
        return out

    def describe_postgres_instances(self, page_size: int = 100) -> List[Dict[str, Any]]:
        """枚举当前 region 下的 PostgreSQL 实例。"""
        client = self._postgres_client()
        out: List[Dict[str, Any]] = []
        offset = 0
        while True:
            req = postgres_models.DescribeDBInstancesRequest()
            req.Offset = offset
            req.Limit = min(page_size, 100)
            resp = client.DescribeDBInstances(req)
            items = getattr(resp, "DBInstanceSet", None) or []
            for item in items:
                out.append(_normalize_postgres_instance(item, self._region))
            total = getattr(resp, "TotalCount", 0) or 0
            offset += len(items)
            if not items or offset >= total:
                break
        return out


# ---------- 字段归一化 ----------

def _normalize_cdb_item(item: Any) -> Dict[str, Any]:
    ts = _to_int(getattr(item, "Timestamp", None))
    return {
        "ts": ts,
        "ts_iso": _utc_iso(ts) if ts else "",
        "query_time_ms": _sec_to_ms(getattr(item, "QueryTime", None)),
        "lock_time_ms": _sec_to_ms(getattr(item, "LockTime", None)),
        "rows_examined": _to_int(getattr(item, "RowsExamined", None)),
        "rows_sent": _to_int(getattr(item, "RowsSent", None)),
        "database": _to_str(getattr(item, "Database", None)),
        "user_name": _to_str(getattr(item, "UserName", None)),
        "user_host": _to_str(getattr(item, "UserHost", None)),
        "sql_text": _to_str(getattr(item, "SqlText", None)) or "",
        "tc_template": _to_str(getattr(item, "SqlTemplate", None)),
        "tc_md5": _to_str(getattr(item, "Md5", None)),
    }


def _normalize_cynosdb_item(item: Any) -> Dict[str, Any]:
    ts = _to_int(getattr(item, "Timestamp", None))
    return {
        "ts": ts,
        "ts_iso": _utc_iso(ts) if ts else "",
        "query_time_ms": _sec_to_ms(getattr(item, "QueryTime", None)),
        "lock_time_ms": _sec_to_ms(getattr(item, "LockTime", None)),
        "rows_examined": _to_int(getattr(item, "RowsExamined", None)),
        "rows_sent": _to_int(getattr(item, "RowsSent", None)),
        "database": _to_str(getattr(item, "Database", None)),
        "user_name": _to_str(getattr(item, "UserName", None)),
        "user_host": _to_str(getattr(item, "UserHost", None)),
        "sql_text": _to_str(getattr(item, "SqlText", None)) or "",
        "tc_template": _to_str(getattr(item, "SqlTemplate", None)),
        "tc_md5": _to_str(getattr(item, "SqlMd5", None)),
    }


def _normalize_postgres_item(item: Any) -> Dict[str, Any]:
    ts = _tc_time_to_ts(getattr(item, "SessionStartTime", None))
    return {
        "ts": ts,
        "ts_iso": _utc_iso(ts) if ts else _to_str(getattr(item, "SessionStartTime", None)) or "",
        "query_time_ms": _duration_to_ms(getattr(item, "Duration", None)),
        "lock_time_ms": 0,
        "rows_examined": 0,
        "rows_sent": 0,
        "database": _to_str(getattr(item, "DatabaseName", None)),
        "user_name": _to_str(getattr(item, "UserName", None)),
        "user_host": _to_str(getattr(item, "ClientAddr", None)),
        "sql_text": _to_str(getattr(item, "RawQuery", None)) or "",
        "tc_template": None,
        "tc_md5": _to_str(getattr(item, "SessionId", None)),
    }


def _normalize_cdb_binlog(item: Any, instance_id: str, region: str) -> Dict[str, Any]:
    status = _to_str(getattr(item, "Status", None))
    name = _to_str(getattr(item, "Name", None)) or ""
    return {
        "binlog_id": name,
        "file_name": name,
        "file_size": _to_int(getattr(item, "Size", None)),
        "size": _format_bytes(getattr(item, "Size", None)),
        "start_time": _to_str(getattr(item, "BinlogStartTime", None))
        or _to_str(getattr(item, "Date", None)),
        "end_time": _to_str(getattr(item, "BinlogFinishTime", None)),
        "storage_time": _to_str(getattr(item, "Date", None)),
        "status": _backup_status(status),
        "raw_status": status,
        "download_url": _to_str(getattr(item, "InternetUrl", None))
        or _to_str(getattr(item, "IntranetUrl", None)),
        "tc_product": "cdb",
        "tc_region": _to_str(getattr(item, "Region", None)) or region,
        "tc_instance_id": _to_str(getattr(item, "InstanceId", None)) or instance_id,
    }


def _normalize_cynosdb_binlog(item: Any, cluster_id: str, region: str) -> Dict[str, Any]:
    binlog_id = _to_str(getattr(item, "BinlogId", None)) or ""
    return {
        "binlog_id": binlog_id,
        "file_name": _to_str(getattr(item, "FileName", None)) or binlog_id,
        "file_size": _to_int(getattr(item, "FileSize", None)),
        "size": _format_bytes(getattr(item, "FileSize", None)),
        "start_time": _to_str(getattr(item, "StartTime", None)),
        "end_time": _to_str(getattr(item, "FinishTime", None)),
        "storage_time": None,
        "status": _backup_status(getattr(item, "CopyStatus", None)) if getattr(item, "CopyStatus", None) else "success",
        "raw_status": _to_str(getattr(item, "CopyStatus", None)),
        "download_url": None,
        "tc_product": "cynosdb",
        "tc_region": region,
        "tc_instance_id": cluster_id,
    }


def _normalize_postgres_xlog(item: Any, instance_id: str, region: str) -> Dict[str, Any]:
    xlog_id = _to_str(getattr(item, "Id", None)) or ""
    size_kb = _to_int(getattr(item, "Size", None))
    return {
        "binlog_id": xlog_id,
        "file_name": xlog_id,
        "file_size": size_kb * 1024,
        "size": _format_kb_as_mb(size_kb),
        "start_time": _to_str(getattr(item, "StartTime", None)),
        "end_time": _to_str(getattr(item, "EndTime", None)),
        "storage_time": None,
        "status": "success",
        "raw_status": "available",
        "download_url": _to_str(getattr(item, "ExternalAddr", None))
        or _to_str(getattr(item, "InternalAddr", None)),
        "tc_product": "postgres",
        "tc_region": region,
        "tc_instance_id": instance_id,
    }


def _normalize_cdb_backup(item: Any, instance_id: str, region: str) -> Dict[str, Any]:
    status = _to_str(getattr(item, "Status", None))
    method = _to_str(getattr(item, "Method", None))
    backup_type = _backup_type(method, getattr(item, "Type", None))
    start_time = _to_str(getattr(item, "StartTime", None)) or _to_str(getattr(item, "Date", None))
    finish_time = _to_str(getattr(item, "FinishTime", None))
    name = (
        _to_str(getattr(item, "ManualBackupName", None))
        or _to_str(getattr(item, "Name", None))
        or f"{instance_id}_{_to_str(getattr(item, 'BackupId', None)) or 'backup'}"
    )
    return {
        "tc_backup_id": _to_str(getattr(item, "BackupId", None)) or f"{name}:{start_time}",
        "name": name,
        "backup_type": backup_type,
        "size": _format_bytes(getattr(item, "Size", None)),
        "start_time": start_time,
        "end_time": finish_time,
        "status": _backup_status(status),
        "raw_status": status,
        "operator": _backup_operator(getattr(item, "Way", None), getattr(item, "Creator", None)),
        "tc_product": "cdb",
        "tc_region": _to_str(getattr(item, "Region", None)) or region,
        "tc_instance_id": _to_str(getattr(item, "InstanceId", None)) or instance_id,
    }


def _normalize_cynosdb_backup(item: Any, cluster_id: str, region: str) -> Dict[str, Any]:
    status = _to_str(getattr(item, "BackupStatus", None))
    snap_type = _to_str(getattr(item, "SnapShotType", None))
    backup_type = _backup_type(snap_type, getattr(item, "BackupType", None))
    start_time = _to_str(getattr(item, "StartTime", None)) or _to_str(getattr(item, "SnapshotTime", None))
    finish_time = _to_str(getattr(item, "FinishTime", None))
    name = (
        _to_str(getattr(item, "BackupName", None))
        or _to_str(getattr(item, "FileName", None))
        or f"{cluster_id}_{_to_str(getattr(item, 'BackupId', None)) or 'backup'}"
    )
    return {
        "tc_backup_id": (
            _to_str(getattr(item, "BackupId", None))
            or _to_str(getattr(item, "SnapshotId", None))
            or f"{name}:{start_time}"
        ),
        "name": name,
        "backup_type": backup_type,
        "size": _format_bytes(getattr(item, "FileSize", None)),
        "start_time": start_time,
        "end_time": finish_time,
        "status": _backup_status(status),
        "raw_status": status,
        "operator": _backup_operator(getattr(item, "BackupMethod", None), None),
        "tc_product": "cynosdb",
        "tc_region": region,
        "tc_instance_id": cluster_id,
    }


def _normalize_postgres_backup(
    item: Any,
    instance_id: str,
    region: str,
    backup_kind: str,
) -> Dict[str, Any]:
    backup_id = _to_str(getattr(item, "Id", None)) or ""
    start_time = _to_str(getattr(item, "StartTime", None))
    finish_time = _to_str(getattr(item, "FinishTime", None)) or _to_str(getattr(item, "EndTime", None))
    name = _to_str(getattr(item, "Name", None)) or f"{instance_id}_{backup_kind}_{backup_id or start_time}"
    return {
        "tc_backup_id": f"{backup_kind}:{backup_id or name}:{start_time}",
        "name": name,
        "backup_type": "incremental" if backup_kind == "LogBackup" else "full",
        "size": _format_bytes(getattr(item, "Size", None)),
        "start_time": start_time,
        "end_time": finish_time,
        "status": _backup_status(getattr(item, "State", None)),
        "raw_status": _to_str(getattr(item, "State", None)),
        "operator": _backup_operator(getattr(item, "BackupMode", None), None),
        "tc_product": "postgres",
        "tc_region": region,
        "tc_instance_id": _to_str(getattr(item, "DBInstanceId", None)) or instance_id,
    }


def _normalize_cdb_instance(item: Any, region: str) -> Dict[str, Any]:
    status = _cdb_status_label(getattr(item, "Status", None))
    return {
        "tc_product": "cdb",
        "tc_region": _to_str(getattr(item, "Region", None)) or region,
        "tc_instance_id": _to_str(getattr(item, "InstanceId", None)) or "",
        "name": _to_str(getattr(item, "InstanceName", None))
        or _to_str(getattr(item, "InstanceId", None))
        or "",
        "host": _to_str(getattr(item, "Vip", None)),
        "port": _to_int(getattr(item, "Vport", None), 3306),
        "db_type": "MySQL",
        "version": _to_str(getattr(item, "EngineVersion", None)),
        "status": status,
        "zone": _to_str(getattr(item, "Zone", None)),
        "cluster_id": None,
        "cluster_name": None,
        "role": str(getattr(item, "InstanceType", "")),
    }


def _normalize_cynosdb_instance(item: Any, region: str) -> Dict[str, Any]:
    instance_id = _to_str(getattr(item, "InstanceId", None)) or ""
    instance_name = _to_str(getattr(item, "InstanceName", None))
    cluster_name = _to_str(getattr(item, "ClusterName", None))
    name = instance_name
    if not name or name == instance_id:
        name = cluster_name or instance_id
    return {
        "tc_product": "cynosdb",
        "tc_region": _to_str(getattr(item, "Region", None)) or region,
        "tc_instance_id": instance_id,
        "name": name or "",
        "host": _to_str(getattr(item, "Vip", None)),
        "port": _to_int(getattr(item, "Vport", None), 3306),
        "db_type": "MySQL",
        "version": _to_str(getattr(item, "DbVersion", None)),
        "status": _to_str(getattr(item, "Status", None)),
        "zone": _to_str(getattr(item, "Zone", None)),
        "cluster_id": _to_str(getattr(item, "ClusterId", None)),
        "cluster_name": cluster_name,
        "role": _to_str(getattr(item, "InstanceType", None)),
    }


def _normalize_postgres_instance(item: Any, region: str) -> Dict[str, Any]:
    host, port = _postgres_net_address(item)
    instance_id = _to_str(getattr(item, "DBInstanceId", None)) or ""
    return {
        "tc_product": "postgres",
        "tc_region": _to_str(getattr(item, "Region", None)) or region,
        "tc_instance_id": instance_id,
        "name": _to_str(getattr(item, "DBInstanceName", None)) or instance_id,
        "host": host,
        "port": port or 5432,
        "db_type": "PostgreSQL",
        "version": _to_str(getattr(item, "DBVersion", None))
        or _to_str(getattr(item, "DBMajorVersion", None)),
        "status": _to_str(getattr(item, "DBInstanceStatus", None)),
        "zone": _to_str(getattr(item, "Zone", None)),
        "cluster_id": None,
        "cluster_name": None,
        "role": _to_str(getattr(item, "DBInstanceType", None)),
    }


def _postgres_net_address(item: Any) -> tuple[Optional[str], int]:
    net_infos = getattr(item, "DBInstanceNetInfo", None) or []
    preferred = []
    fallback = []
    for net in net_infos:
        host = _to_str(getattr(net, "Ip", None)) or _to_str(getattr(net, "Address", None))
        if not host:
            continue
        port = _to_int(getattr(net, "Port", None), 5432)
        net_type = (_to_str(getattr(net, "NetType", None)) or "").lower()
        status = (_to_str(getattr(net, "Status", None)) or "").lower()
        row = (host, port)
        if status == "opened" and net_type in {"private", "inner"}:
            preferred.append(row)
        elif status == "opened":
            fallback.append(row)
        else:
            fallback.append(row)
    if preferred:
        return preferred[0]
    if fallback:
        return fallback[0]
    return None, 5432


def _cdb_status_label(status: Any) -> str:
    labels = {
        0: "creating",
        1: "running",
        4: "isolating",
        5: "isolated",
    }
    value = _to_int(status, -1)
    return labels.get(value, str(status) if status is not None else "")


def _format_bytes(value: Any) -> str:
    size = _to_int(value, 0)
    if size <= 0:
        return "-"
    amount = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if amount < 1024 or unit == "PB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.2f} {unit}"
        amount /= 1024
    return f"{size} B"


def _format_kb_as_mb(value: Any) -> str:
    size_kb = _to_int(value, 0)
    if size_kb <= 0:
        return "-"
    return f"{size_kb / 1024:.2f} MB"


def _backup_status(value: Any) -> str:
    status = (_to_str(value) or "").lower()
    if status in {"2", "success", "successful", "finished", "completed"}:
        return "success"
    if status in {"3", "failed", "fail", "failure", "canceled", "cancelled"}:
        return "failed"
    if status in {"1", "init", "running", "creating", "deleting", "processing"}:
        return "running"
    return "pending"


def _backup_type(*values: Any) -> str:
    for value in values:
        item = (_to_str(value) or "").lower()
        if not item:
            continue
        if item in {"full", "snapshot"}:
            return "full" if item == "full" else "snapshot"
        if item in {"increment", "incremental"}:
            return "incremental"
        if item == "partial":
            return "differential"
        if item in {"logic", "logical", "physical", "manual"}:
            return "logical" if item == "logic" else item
    return "manual"


def _backup_operator(method: Any, creator: Any) -> str:
    value = (_to_str(method) or _to_str(creator) or "").lower()
    if value in {"automatic", "auto", "system"}:
        return "腾讯云自动"
    if value == "manual":
        return "腾讯云手动"
    return "腾讯云"


__all__ = ["TCClient", "TencentCloudSDKException"]
