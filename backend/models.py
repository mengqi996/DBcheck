# -*- coding: utf-8 -*-
"""
DBCheck 数据模型
"""

from typing import Optional, List, Dict
from pydantic import BaseModel, Field
from datetime import datetime
from enum import Enum


class DBType(str, Enum):
    """支持的数据库类型"""
    MYSQL = "MySQL"
    POSTGRESQL = "PostgreSQL"
    REDIS = "Redis"
    MONGODB = "MongoDB"
    ORACLE = "Oracle"
    SQLSERVER = "SQL Server"


class InstanceStatus(str, Enum):
    """实例状态"""
    ONLINE = "online"
    OFFLINE = "offline"
    WARNING = "warning"


class InstanceBase(BaseModel):
    """实例基础模型"""
    name: str = Field(..., description="实例名称", examples=["生产-MySQL-主库"])
    host: str = Field(..., description="主机地址", examples=["192.168.1.10"])
    port: int = Field(..., description="端口", examples=[3306])
    db_type: DBType = Field(..., description="数据库类型")
    username: Optional[str] = Field(None, description="数据库用户名", examples=["root"])
    database: Optional[str] = Field(None, description="数据库名/服务名", examples=["app"])
    version: Optional[str] = Field(None, description="数据库版本", examples=["8.0.32"])
    environment: Optional[str] = Field("prod", description="环境", examples=["prod"])
    owner: Optional[str] = Field(None, description="负责人", examples=["DBA"])


class InstanceCreate(InstanceBase):
    """创建实例请求"""
    password: Optional[str] = Field(None, description="数据库密码")
    remark: Optional[str] = Field(None, description="备注")


class InstanceUpdate(BaseModel):
    """更新实例请求"""
    name: Optional[str] = Field(None, description="实例名称")
    host: Optional[str] = Field(None, description="主机地址")
    port: Optional[int] = Field(None, description="端口")
    db_type: Optional[DBType] = Field(None, description="数据库类型")
    username: Optional[str] = Field(None, description="数据库用户名")
    password: Optional[str] = Field(None, description="数据库密码")
    database: Optional[str] = Field(None, description="数据库名/服务名")
    version: Optional[str] = Field(None, description="数据库版本")
    environment: Optional[str] = Field(None, description="环境")
    owner: Optional[str] = Field(None, description="负责人")
    remark: Optional[str] = Field(None, description="备注")


class InstanceResponse(InstanceBase):
    """实例响应模型"""
    id: int
    status: InstanceStatus
    last_check: Optional[str] = None
    remark: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    class Config:
        from_attributes = True


class ConnectivityCheckRequest(BaseModel):
    """连通性检测请求"""
    host: str = Field(..., description="主机地址")
    port: int = Field(..., description="端口")
    db_type: DBType = Field(..., description="数据库类型")
    username: Optional[str] = Field(None, description="用户名")
    password: Optional[str] = Field(None, description="密码")
    database: Optional[str] = Field(None, description="数据库名/服务名")


class ConnectivityCheckResponse(BaseModel):
    """连通性检测响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(..., description="结果消息")
    version: Optional[str] = Field(None, description="数据库版本")
    response_time: Optional[float] = Field(None, description="响应时间(秒)")


class APIResponse(BaseModel):
    """通用 API 响应"""
    code: int = Field(200, description="状态码")
    message: str = Field("success", description="消息")
    data: Optional[dict] = Field(None, description="数据")


# ========== 备份相关模型 ==========

class BackupType(str, Enum):
    """备份类型"""
    FULL = "full"           # 全量备份
    INCREMENTAL = "incremental"  # 增量备份
    DIFFERENTIAL = "differential"  # 差异备份
    RDB = "rdb"             # Redis RDB
    MANUAL = "manual"        # 手动备份
    EXPDP = "expdp"          # Oracle 导出


class BackupStatus(str, Enum):
    """备份状态"""
    SUCCESS = "success"      # 成功
    FAILED = "failed"       # 失败
    RUNNING = "running"      # 执行中
    PENDING = "pending"     # 待执行


class BackupBase(BaseModel):
    """备份基础模型"""
    name: str = Field(..., description="备份名称")
    instance_id: int = Field(..., description="所属实例ID")
    instance_name: str = Field(..., description="所属实例名称")
    backup_type: BackupType = Field(..., description="备份类型")
    size: Optional[str] = Field(None, description="备份大小")
    start_time: Optional[str] = Field(None, description="开始时间")
    end_time: Optional[str] = Field(None, description="结束时间")
    status: BackupStatus = Field(..., description="状态")
    operator: str = Field("系统", description="操作人")


class BackupResponse(BackupBase):
    """备份响应模型"""
    id: int

    class Config:
        from_attributes = True


class BackupCreate(BaseModel):
    """创建备份请求"""
    instance_id: int = Field(..., description="实例ID")
    backup_type: BackupType = Field(BackupType.FULL, description="备份类型")


# ========== SQL 查询相关模型 ==========

class SQLExecuteRequest(BaseModel):
    """SQL 执行请求"""
    instance_id: int = Field(..., description="实例ID")
    sql: str = Field(..., description="SQL 语句")
    limit: int = Field(1000, description="返回结果条数限制")


class MonitorDataRequest(BaseModel):
    """腾讯云监控指标查询请求"""
    binding_id: int = Field(..., description="腾讯云绑定 ID")
    metric_name: str = Field(..., description="指标英文名")
    period: int = Field(60, ge=5, description="统计周期，单位秒")
    range_hours: int = Field(1, ge=1, le=168, description="默认查询最近 N 小时")
    start_time: Optional[str] = Field(None, description="开始时间")
    end_time: Optional[str] = Field(None, description="结束时间")
    dimensions: Dict[str, str] = Field(default_factory=dict, description="监控维度")


class SQLExecuteResponse(BaseModel):
    """SQL 执行响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(..., description="结果消息")
    columns: List[str] = Field(default_factory=list, description="列名列表")
    rows: List[dict] = Field(default_factory=list, description="数据行")
    row_count: int = Field(0, description="返回行数")
    execution_time: float = Field(0, description="执行时间(秒)")


# ========== 慢 SQL 模块：腾讯云凭证 ==========

class TCProduct(str, Enum):
    """腾讯云数据库产品"""
    CDB = "cdb"           # 云数据库 MySQL
    CYNOSDB = "cynosdb"   # TDSQL-C (MySQL 兼容)
    POSTGRES = "postgres" # 云数据库 PostgreSQL


class TCCredentialBase(BaseModel):
    """腾讯云凭证基础模型"""
    name: str = Field(..., max_length=64, description="凭证名称", examples=["prod-account"])
    secret_id: str = Field(..., min_length=8, description="SecretId")
    secret_key: str = Field(..., min_length=8, description="SecretKey（入库前加密）")
    endpoint_suffix: str = Field(
        "tencentcloudapi.com",
        description="API 域名后缀（境外用 tencentcloudapi.com，主机用 tencentcloudapi.com）",
    )
    is_default: bool = Field(False, description="是否默认凭证")


class TCCredentialCreate(TCCredentialBase):
    """创建凭证请求"""


class TCCredentialUpdate(BaseModel):
    """更新凭证请求"""
    name: Optional[str] = Field(None, max_length=64)
    secret_id: Optional[str] = Field(None, min_length=8)
    secret_key: Optional[str] = Field(None, min_length=8, description="为空则保留原值")
    endpoint_suffix: Optional[str] = None
    is_default: Optional[bool] = None


class TCCredentialResponse(BaseModel):
    """凭证响应（不包含 secret_key）"""
    id: int
    name: str
    secret_id: str
    endpoint_suffix: str
    is_default: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# ========== 慢 SQL 模块：实例 ↔ 腾讯云绑定 ==========

class BindingCreate(BaseModel):
    """创建绑定请求"""
    instance_id: int = Field(..., description="dbcheck 实例 ID（必须已存在）")
    tc_product: TCProduct = Field(..., description="腾讯云产品")
    tc_instance_id: str = Field(..., min_length=1, description="腾讯云实例 ID")
    tc_region: str = Field(..., min_length=1, description="腾讯云地域，如 ap-guangzhou")
    credential_id: int = Field(..., description="使用的凭证 ID")
    enabled: bool = Field(True, description="是否启用调度")


class BindingUpdate(BaseModel):
    """更新绑定请求"""
    tc_product: Optional[TCProduct] = None
    tc_instance_id: Optional[str] = None
    tc_region: Optional[str] = None
    credential_id: Optional[int] = None
    enabled: Optional[bool] = None


class BindingResponse(BaseModel):
    """绑定响应"""
    id: int
    instance_id: int
    instance_name: Optional[str] = None
    tc_product: str
    tc_instance_id: str
    tc_region: str
    credential_id: int
    credential_name: Optional[str] = None
    enabled: bool
    last_poll_at: Optional[str] = None
    last_success_at: Optional[str] = None
    last_error: Optional[str] = None
    consecutive_failures: int = 0


# ========== 慢 SQL 模块：腾讯云实例自动发现 ==========

class TCDiscoveryRequest(BaseModel):
    """腾讯云实例发现 / 导入请求"""
    credential_id: int = Field(..., description="使用的腾讯云凭证 ID")
    regions: List[str] = Field(
        default_factory=lambda: ["ap-guangzhou", "ap-shanghai", "ap-beijing", "ap-singapore"],
        description="要扫描的腾讯云地域列表",
    )
    products: List[TCProduct] = Field(
        default_factory=lambda: [TCProduct.CDB, TCProduct.CYNOSDB, TCProduct.POSTGRES],
        description="要扫描的腾讯云产品",
    )
    create_missing_instances: bool = Field(True, description="导入时是否自动创建本地 dbcheck 实例")
    enabled: bool = Field(True, description="导入绑定后是否启用调度")


class TCDiscoveredInstance(BaseModel):
    """从腾讯云发现的数据库实例"""
    tc_product: str
    tc_region: str
    tc_instance_id: str
    name: str
    host: Optional[str] = None
    port: int = 3306
    db_type: str = "MySQL"
    version: Optional[str] = None
    status: Optional[str] = None
    zone: Optional[str] = None
    cluster_id: Optional[str] = None
    cluster_name: Optional[str] = None
    role: Optional[str] = None


class TCImportResult(BaseModel):
    """单个腾讯云实例导入结果"""
    tc_product: str
    tc_region: str
    tc_instance_id: str
    name: str
    action: str
    instance_id: Optional[int] = None
    binding_id: Optional[int] = None
    message: str


# ========== 慢 SQL 模块：慢查询 ==========

class SlowQueryResponse(BaseModel):
    """慢 SQL 列表 / 详情响应"""
    id: int
    binding_id: int
    instance_id: int
    instance_name: Optional[str] = None
    tc_product: str
    tc_instance_id: str
    tc_region: str
    database: Optional[str] = None
    user_name: Optional[str] = None
    user_host: Optional[str] = None
    sql_text: str
    sql_template: str
    fingerprint: str
    query_time_ms: int
    lock_time_ms: int
    rows_examined: int
    rows_sent: int
    ts: int                  # Unix 秒（UTC）
    ts_iso: str              # YYYY-MM-DD HH:MM:SS（UTC，便于直接展示）
    ingested_at: str


class SlowQueryStats(BaseModel):
    """慢 SQL 聚合统计"""
    total: int
    by_database: dict = Field(default_factory=dict)
    by_product: dict = Field(default_factory=dict)
    by_region: dict = Field(default_factory=dict)
    avg_query_time_ms: float
    p50_query_time_ms: float
    p95_query_time_ms: float
    max_query_time_ms: int
    top_fingerprints: List[dict] = Field(default_factory=list)


class SchedulerStatus(BaseModel):
    """调度器状态"""
    running: bool
    interval_seconds: int
    last_tick_at: Optional[str] = None
    bindings_count: int
    active_polls: int


class ExplainResponse(BaseModel):
    """EXPLAIN 执行结果（复用 SQLExecuteResponse 形态）"""
    success: bool
    message: str
    columns: List[str] = Field(default_factory=list)
    rows: List[dict] = Field(default_factory=list)
    execution_time: float = 0.0
