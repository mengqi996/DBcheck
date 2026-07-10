# -*- coding: utf-8 -*-
from __future__ import annotations

"""
DBCheck backend service.
"""

import os
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response

from auth import (
    USER_ROLE_DBA,
    USER_ROLE_RD,
    hash_password,
    hash_session_token,
    new_session_token,
    session_expiry_iso,
    verify_password,
)
from connectors import check_connectivity, execute_sql
from models import (
    APIResponse,
    AuthLoginRequest,
    BackupCreate,
    BackupStatus,
    BindingCreate,
    BindingResponse,
    BindingUpdate,
    ConnectivityCheckRequest,
    ConnectivityCheckResponse,
    DBType,
    ExplainResponse,
    InstanceCreate,
    InstanceStatus,
    InstanceUpdate,
    MonitorDataRequest,
    SchedulerStatus,
    SQLExecuteRequest,
    SQLExecuteResponse,
    SlowQueryResponse,
    SlowQueryStats,
    TCDiscoveryRequest,
    TCCredentialCreate,
    TCCredentialResponse,
    TCCredentialUpdate,
    TCProduct,
    UserCreate,
    UserResponse,
    UserUpdate,
)
import scheduler as scheduler_mod
import slow_query_service
import backup_service
import binlog_service
import monitor_service
from async_compat import to_thread
from storage import (
    create_backup as repo_create_backup,
    create_binding as repo_create_binding,
    create_credential as repo_create_credential,
    create_instance as repo_create_instance,
    create_user as repo_create_user,
    create_user_session as repo_create_user_session,
    create_quick_check_log,
    count_enabled_dba_users,
    dashboard_summary,
    delete_backup as repo_delete_backup,
    delete_binding as repo_delete_binding,
    delete_credential as repo_delete_credential,
    delete_instance as repo_delete_instance,
    delete_user as repo_delete_user,
    delete_user_session as repo_delete_user_session,
    delete_user_sessions_for_user,
    get_backup as repo_get_backup,
    get_binding as repo_get_binding,
    get_credential as repo_get_credential,
    get_instance as repo_get_instance,
    get_slow_query as repo_get_slow_query,
    get_sync_state as repo_get_sync_state,
    get_user as repo_get_user,
    get_user_by_session_token_hash,
    get_user_by_username,
    init_db,
    list_backups,
    list_bindings,
    list_check_logs,
    list_credentials,
    list_instances,
    list_slow_queries,
    list_slow_queries_by_fingerprint,
    list_users,
    slow_query_stats,
    slow_query_timeseries,
    touch_user_session,
    top_slow_fingerprints,
    update_binding as repo_update_binding,
    update_credential as repo_update_credential,
    update_instance as repo_update_instance,
    update_user as repo_update_user,
    update_instance_check,
)
from tc_client import TCClient, TencentCloudSDKException
from crypto import encrypt as encrypt_secret
from crypto import decrypt as decrypt_secret
from crypto import KEY_PATH as FERNET_KEY_PATH
from storage import DB_PATH as SQLITE_DB_PATH


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.split("#", 1)[0].strip().lower()
    return normalized in {"1", "true", "yes", "on"}


# Tencent Cloud feature switches.
# Defaults are ON for production use. Set any value to false/0/no/off in
# /opt/dbcheck/.env to disable the corresponding capability.
TENCENT_API_ENABLED = env_flag("DBCHECK_TENCENT_API_ENABLED", True)
CLOUD_BACKUP_ENABLED = env_flag("DBCHECK_CLOUD_BACKUP_ENABLED", True)
SCHEDULER_ENABLED = env_flag("DBCHECK_SCHEDULER_ENABLED", True)

POLL_INTERVAL_SECONDS = int(os.getenv("DBCHECK_POLL_INTERVAL", "3600"))
SCHEDULER_MAX_CONCURRENCY = int(os.getenv("DBCHECK_SCHEDULER_CONCURRENCY", "4"))
SLOW_QUERY_PRODUCTS = {"cdb", "cynosdb", "postgres", "self_mysql", "self_postgresql"}

APP_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = APP_ROOT / "dbcheck.db"
DEFAULT_KEY_PATH = APP_ROOT / ".fernet_key"
PRODUCTION_APP_ROOT = Path("/opt/dbcheck/app")
PRODUCTION_DATA_ROOT = Path("/opt/dbcheck/data")


def ensure_tencent_api_enabled(operation: str = "腾讯云 API 调用") -> None:
    if not TENCENT_API_ENABLED:
        raise HTTPException(
            status_code=403,
            detail=(
                f"{operation} 已关闭。需要主动调用腾讯云时，设置 "
                "DBCHECK_TENCENT_API_ENABLED=true 后重启后端。"
            ),
        )


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_runtime_storage_paths() -> None:
    cwd = Path.cwd().resolve()
    db_path = SQLITE_DB_PATH.resolve()
    key_path = FERNET_KEY_PATH.resolve()

    likely_production = (
        cwd == PRODUCTION_APP_ROOT
        or _is_relative_to(db_path, PRODUCTION_APP_ROOT)
        or _is_relative_to(key_path, PRODUCTION_APP_ROOT)
    )
    if not likely_production:
        return

    errors = []
    if db_path == DEFAULT_DB_PATH:
        errors.append(
            "DBCHECK_SQLITE_PATH 仍指向代码目录 /opt/dbcheck/app/dbcheck.db。"
            " 请改到 /opt/dbcheck/data/dbcheck.db。"
        )
    if key_path == DEFAULT_KEY_PATH:
        errors.append(
            "DBCHECK_FERNET_KEY_FILE 仍指向代码目录 /opt/dbcheck/app/.fernet_key。"
            " 请改到 /opt/dbcheck/data/.fernet_key。"
        )
    if _is_relative_to(db_path, PRODUCTION_APP_ROOT):
        errors.append(
            f"当前 SQLite 路径在代码目录内：{db_path}。"
            " 请设置 DBCHECK_SQLITE_PATH=/opt/dbcheck/data/dbcheck.db"
        )
    if _is_relative_to(key_path, PRODUCTION_APP_ROOT):
        errors.append(
            f"当前 Fernet 密钥路径在代码目录内：{key_path}。"
            " 请设置 DBCHECK_FERNET_KEY_FILE=/opt/dbcheck/data/.fernet_key"
        )
    if errors:
        raise RuntimeError(
            "生产环境存储路径配置错误。继续启动会导致代码更新后读到新空库或新密钥。\n"
            + "\n".join(errors)
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_runtime_storage_paths()
    init_db()
    sched = scheduler_mod.SlowQueryScheduler(
        interval_seconds=POLL_INTERVAL_SECONDS,
        max_concurrency=SCHEDULER_MAX_CONCURRENCY,
    )
    app.state.scheduler = sched
    app.state.scheduler_auto_enabled = SCHEDULER_ENABLED and TENCENT_API_ENABLED
    if SCHEDULER_ENABLED and TENCENT_API_ENABLED:
        await sched.start()
    try:
        yield
    finally:
        await sched.stop()


app = FastAPI(
    title="DBCheck API",
    description="数据库运维平台后端 API",
    version="1.2.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/api/auth/login", response_model=APIResponse)
def login(payload: AuthLoginRequest):
    user = get_user_by_username(payload.username, include_secret=True)
    if not user or not bool(user.get("enabled")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    if not verify_password(payload.password, user["password_salt"], user["password_hash"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")

    token = new_session_token()
    repo_create_user_session(user["id"], hash_session_token(token), session_expiry_iso())
    return success(
        message="登录成功",
        data={
            "token": token,
            "user": _user_response(user).model_dump(),
        },
    )


@app.get("/api/auth/me", response_model=APIResponse)
def auth_me(request: Request):
    user = require_authenticated_user(request)
    return success(data={"user": _user_response(user).model_dump()})


@app.post("/api/auth/logout", response_model=APIResponse)
def logout(request: Request):
    token = _extract_bearer_token(request)
    if token:
        repo_delete_user_session(hash_session_token(token))
    return success(message="已退出登录")


@app.get("/api/users", response_model=APIResponse)
def list_user_accounts(request: Request):
    require_dba_user(request)
    rows = [_user_response(row).model_dump() for row in list_users()]
    return success(data={"total": len(rows), "items": rows})


@app.post("/api/users", response_model=APIResponse)
def create_user_account(request: Request, payload: UserCreate):
    require_dba_user(request)
    data = payload.model_dump(mode="json")
    salt_hex, password_hash = hash_password(data.pop("password"))
    data["password_salt"] = salt_hex
    data["password_hash"] = password_hash
    try:
        created = repo_create_user(data)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="用户名已存在")
    return success(message="用户已创建", data={"user": _user_response(created).model_dump()}, code=201)


@app.put("/api/users/{user_id}", response_model=APIResponse)
def update_user_account(request: Request, user_id: int, payload: UserUpdate):
    current = require_dba_user(request)
    data = payload.model_dump(mode="json", exclude_unset=True)
    password = data.pop("password", None)
    if password:
        salt_hex, password_hash = hash_password(password)
        data["password_salt"] = salt_hex
        data["password_hash"] = password_hash
    if user_id == current["id"] and data.get("enabled") is False:
        raise HTTPException(status_code=400, detail="不能停用当前登录账号")
    if user_id == current["id"] and data.get("role") == USER_ROLE_RD and count_enabled_dba_users(exclude_user_id=user_id) == 0:
        raise HTTPException(status_code=400, detail="至少需要保留一个启用中的 DBA 账号")
    updated = repo_update_user(user_id, data)
    if not updated:
        raise HTTPException(status_code=404, detail="用户不存在")
    if password or "enabled" in data or "role" in data:
        delete_user_sessions_for_user(user_id)
    return success(message="用户已更新", data={"user": _user_response(updated).model_dump()})


@app.delete("/api/users/{user_id}", response_model=APIResponse)
def delete_user_account(request: Request, user_id: int):
    current = require_dba_user(request)
    user = repo_get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user_id == current["id"]:
        raise HTTPException(status_code=400, detail="不能删除当前登录账号")
    if user["role"] == USER_ROLE_DBA and user["enabled"] and count_enabled_dba_users(exclude_user_id=user_id) == 0:
        raise HTTPException(status_code=400, detail="至少需要保留一个启用中的 DBA 账号")
    delete_user_sessions_for_user(user_id)
    if not repo_delete_user(user_id):
        raise HTTPException(status_code=404, detail="用户不存在")
    return success(message="用户已删除")


def success(message: str = "success", data: Optional[dict] = None, code: int = 200) -> APIResponse:
    return APIResponse(code=code, message=message, data=data)


def _user_response(row: dict) -> UserResponse:
    return UserResponse(
        id=row["id"],
        username=row["username"],
        display_name=row["display_name"],
        role=row["role"],
        enabled=bool(row["enabled"]),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _extract_bearer_token(request: Request) -> Optional[str]:
    header = request.headers.get("Authorization") or ""
    if not header.startswith("Bearer "):
        return None
    token = header[7:].strip()
    return token or None


def require_authenticated_user(request: Request) -> dict:
    token = _extract_bearer_token(request)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    user = get_user_by_session_token_hash(hash_session_token(token))
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已过期，请重新登录")
    touch_user_session(hash_session_token(token))
    return user


def require_dba_user(request: Request) -> dict:
    user = require_authenticated_user(request)
    if user["role"] != USER_ROLE_DBA:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="当前账号无管理权限")
    return user


@app.get("/api/dashboard", response_model=APIResponse)
def get_dashboard(request: Request):
    """获取运维工作台汇总数据。"""
    require_authenticated_user(request)
    return success(data=dashboard_summary())


@app.get("/api/instances", response_model=APIResponse)
def get_instances(
    request: Request,
    status: InstanceStatus = Query(None, description="按状态筛选"),
    db_type: DBType = Query(None, description="按数据库类型筛选"),
    keyword: str = Query(None, description="搜索关键词(名称/主机/负责人)"),
):
    require_authenticated_user(request)
    items = list_instances(
        status=status.value if status else None,
        db_type=db_type.value if db_type else None,
        keyword=keyword,
    )
    return success(data={"total": len(items), "items": items})


@app.get("/api/instances/{instance_id}", response_model=APIResponse)
def get_instance(request: Request, instance_id: int):
    require_authenticated_user(request)
    instance = repo_get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="实例不存在")
    return success(data=instance)


@app.post("/api/instances", response_model=APIResponse)
def create_instance(request: Request, instance: InstanceCreate):
    require_dba_user(request)
    try:
        created = repo_create_instance(instance.model_dump(mode="json"))
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="实例名称已存在")
    return success(message="实例创建成功", data=created, code=201)


@app.put("/api/instances/{instance_id}", response_model=APIResponse)
def update_instance(request: Request, instance_id: int, instance: InstanceUpdate):
    require_dba_user(request)
    try:
        updated = repo_update_instance(
            instance_id,
            instance.model_dump(mode="json", exclude_unset=True),
        )
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="实例名称已存在")
    if not updated:
        raise HTTPException(status_code=404, detail="实例不存在")
    return success(message="实例更新成功", data=updated)


@app.delete("/api/instances/{instance_id}", response_model=APIResponse)
def delete_instance(request: Request, instance_id: int):
    require_dba_user(request)
    if not repo_delete_instance(instance_id):
        raise HTTPException(status_code=404, detail="实例不存在")
    return success(message="实例删除成功")


def _preferred_binding_for_instance(instance_id: int) -> Optional[dict]:
    bindings = list_bindings(instance_id=instance_id)
    if not bindings:
        return None
    enabled = [b for b in bindings if b.get("enabled")]
    return (enabled or bindings)[0]


def _cloud_instances_for_binding(binding: dict, cache: Optional[dict] = None) -> list[dict]:
    cache = cache if cache is not None else {}
    key = (binding["credential_id"], binding["tc_region"], binding["tc_product"])
    if key in cache:
        cached = cache[key]
        if isinstance(cached, Exception):
            raise cached
        return cached

    cred = repo_get_credential(binding["credential_id"], include_secret=True)
    if not cred:
        error = RuntimeError("腾讯云凭证不存在")
        cache[key] = error
        raise error

    try:
        secret_key = decrypt_secret(cred["secret_key_enc"])
        client = TCClient(
            cred["secret_id"],
            secret_key,
            binding["tc_region"],
            cred.get("endpoint_suffix") or "tencentcloudapi.com",
        )
        if binding["tc_product"] == "cdb":
            items = client.describe_cdb_instances()
        elif binding["tc_product"] == "cynosdb":
            items = client.describe_cynosdb_instances()
        elif binding["tc_product"] == "postgres":
            items = client.describe_postgres_instances()
        else:
            raise RuntimeError(f"不支持的腾讯云产品: {binding['tc_product']}")
    except Exception as e:  # noqa: BLE001
        cache[key] = e
        raise

    cache[key] = items
    return items


def _cloud_instance_is_healthy(item: dict) -> bool:
    status = str(item.get("status") or "").lower()
    return status in {"running", "online", "1"}


def _check_cloud_bound_instance(
    instance: dict,
    cache: Optional[dict] = None,
) -> Optional[ConnectivityCheckResponse]:
    """有腾讯云 binding 的实例走 OpenAPI 检测；无 binding 返回 None。"""
    binding = _preferred_binding_for_instance(instance["id"])
    if not binding:
        return None
    if not TENCENT_API_ENABLED:
        return ConnectivityCheckResponse(
            success=False,
            message=(
                "腾讯云 API 调用已关闭，未执行云端实例检测；"
                "需要启用时设置 DBCHECK_TENCENT_API_ENABLED=true 后重启后端"
            ),
            version=instance.get("version"),
            response_time=0,
        )

    started_at = time.time()
    product = binding["tc_product"]
    region = binding["tc_region"]
    tc_instance_id = binding["tc_instance_id"]

    try:
        items = _cloud_instances_for_binding(binding, cache=cache)
        cloud_instance = next(
            (item for item in items if item.get("tc_instance_id") == tc_instance_id),
            None,
        )
        elapsed = round(time.time() - started_at, 3)
        if not cloud_instance:
            return ConnectivityCheckResponse(
                success=False,
                message=f"腾讯云 API 检测失败：未找到绑定实例 {product} {region} {tc_instance_id}",
                version=instance.get("version"),
                response_time=elapsed,
            )

        cloud_status = cloud_instance.get("status") or "unknown"
        is_healthy = _cloud_instance_is_healthy(cloud_instance)
        return ConnectivityCheckResponse(
            success=is_healthy,
            message=(
                f"腾讯云 API 检测{'成功' if is_healthy else '异常'}："
                f"{product} {region} {tc_instance_id} 状态 {cloud_status}"
            ),
            version=cloud_instance.get("version") or instance.get("version"),
            response_time=elapsed,
        )
    except TencentCloudSDKException as e:
        return ConnectivityCheckResponse(
            success=False,
            message=f"腾讯云 API 检测失败：{e}",
            version=instance.get("version"),
            response_time=round(time.time() - started_at, 3),
        )
    except Exception as e:  # noqa: BLE001
        return ConnectivityCheckResponse(
            success=False,
            message=f"腾讯云 API 检测失败：{type(e).__name__}: {e}",
            version=instance.get("version"),
            response_time=round(time.time() - started_at, 3),
        )


def _check_instance(
    instance: dict,
    password: Optional[str] = None,
    cloud_cache: Optional[dict] = None,
) -> ConnectivityCheckResponse:
    cloud_result = _check_cloud_bound_instance(instance, cache=cloud_cache)
    if cloud_result is not None:
        return cloud_result

    return check_connectivity(
        db_type=DBType(instance["db_type"]),
        host=instance["host"],
        port=instance["port"],
        username=instance.get("username"),
        password=password if password is not None else instance.get("password"),
        database=instance.get("database"),
    )


@app.post("/api/instances/{instance_id}/check", response_model=APIResponse)
def check_instance_connection(
    request: Request,
    instance_id: int,
    password: str = Query(None, description="临时覆盖数据库密码"),
):
    require_dba_user(request)
    instance = repo_get_instance(instance_id, include_secret=True)
    if not instance:
        raise HTTPException(status_code=404, detail="实例不存在")

    result = _check_instance(instance, password=password)
    update_instance_check(
        instance_id=instance_id,
        success=result.success,
        message=result.message,
        version=result.version,
        response_time=result.response_time,
    )

    return success(
        message="检测完成",
        data={
            "instance_id": instance_id,
            "success": result.success,
            "message": result.message,
            "version": result.version,
            "response_time": result.response_time,
        },
    )


@app.post("/api/connectivity-check", response_model=ConnectivityCheckResponse)
def connectivity_check(http_request: Request, request: ConnectivityCheckRequest):
    require_dba_user(http_request)
    result = check_connectivity(
        db_type=request.db_type,
        host=request.host,
        port=request.port,
        username=request.username,
        password=request.password,
        database=request.database,
    )
    create_quick_check_log(
        host=request.host,
        port=request.port,
        db_type=request.db_type.value,
        success=result.success,
        message=result.message,
        version=result.version,
        response_time=result.response_time,
    )
    return result


@app.post("/api/instances/batch-check", response_model=APIResponse)
def batch_check_instances(
    request: Request,
    password: str = Query(None, description="临时覆盖数据库密码"),
):
    require_dba_user(request)
    results = []
    cloud_cache: dict[tuple[Any, ...], Any] = {}
    for instance in list_instances():
        internal = repo_get_instance(instance["id"], include_secret=True)
        if not internal:
            continue
        result = _check_instance(internal, password=password, cloud_cache=cloud_cache)
        update_instance_check(
            instance_id=internal["id"],
            success=result.success,
            message=result.message,
            version=result.version,
            response_time=result.response_time,
        )
        results.append(
            {
                "id": internal["id"],
                "name": internal["name"],
                "success": result.success,
                "message": result.message,
                "response_time": result.response_time,
            }
        )

    return success(message="批量检测完成", data={"total": len(results), "results": results})


@app.get("/api/check-logs", response_model=APIResponse)
def get_check_logs(request: Request, limit: int = Query(20, ge=1, le=100)):
    require_authenticated_user(request)
    logs = list_check_logs(limit)
    return success(data={"total": len(logs), "items": logs})


@app.get("/api/backups", response_model=APIResponse)
def get_backups(
    request: Request,
    status: BackupStatus = Query(None, description="按状态筛选"),
    instance_id: int = Query(None, description="按实例ID筛选"),
    keyword: str = Query(None, description="搜索关键词(名称/实例)"),
):
    require_authenticated_user(request)
    items = list_backups(
        status=status.value if status else None,
        instance_id=instance_id,
        keyword=keyword,
    )
    return success(data={"total": len(items), "items": items})


@app.post("/api/backups/sync-tencent", response_model=APIResponse)
def sync_tencent_backups_endpoint(
    request: Request,
    instance_id: int = Query(None, description="仅同步指定本地实例 ID"),
):
    require_dba_user(request)
    ensure_tencent_api_enabled("腾讯云备份同步")
    result = backup_service.sync_tencent_backups(instance_id=instance_id)
    message = (
        f"腾讯云备份同步完成：新增 {result['inserted']}，更新 {result['updated']}"
        if not result["errors"]
        else f"腾讯云备份同步完成，{len(result['errors'])} 个实例失败"
    )
    return success(message=message, data=result)


@app.get("/api/backups/{backup_id}", response_model=APIResponse)
def get_backup(request: Request, backup_id: int):
    require_authenticated_user(request)
    backup = repo_get_backup(backup_id)
    if not backup:
        raise HTTPException(status_code=404, detail="备份记录不存在")
    return success(data=backup)


@app.post("/api/backups", response_model=APIResponse)
def create_backup(request: Request, backup: BackupCreate):
    require_dba_user(request)
    cloud_backup_skipped = False
    bound_to_tencent = any(
        item.get("tc_product") in {"cdb", "cynosdb", "postgres"}
        for item in list_bindings(instance_id=backup.instance_id, enabled_only=True)
    )

    if CLOUD_BACKUP_ENABLED and TENCENT_API_ENABLED:
        try:
            cloud_result = backup_service.create_tencent_backup(
                backup.instance_id,
                backup.backup_type.value,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except TencentCloudSDKException as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")

        if cloud_result:
            sync_result = backup_service.sync_tencent_backups(instance_id=backup.instance_id)
            cloud_result["sync"] = sync_result
            return success(message="腾讯云备份任务已发起", data=cloud_result, code=201)
    elif bound_to_tencent:
        cloud_backup_skipped = True

    try:
        created = repo_create_backup(backup.instance_id, backup.backup_type.value)
    except ValueError:
        raise HTTPException(status_code=404, detail="实例不存在")
    if cloud_backup_skipped:
        created["cloud_backup_disabled"] = True
        return success(
            message="本地备份记录已创建；真实腾讯云备份默认禁用",
            data=created,
            code=201,
        )
    return success(message="备份任务已创建", data=created, code=201)


@app.delete("/api/backups/{backup_id}", response_model=APIResponse)
def delete_backup(request: Request, backup_id: int):
    require_dba_user(request)
    if not repo_delete_backup(backup_id):
        raise HTTPException(status_code=404, detail="备份记录不存在")
    return success(message="备份记录已删除")


@app.get("/api/binlogs", response_model=APIResponse)
def list_binlogs_endpoint(
    request: Request,
    binding_id: int = Query(..., description="绑定 ID"),
    start_time: str = Query(None, description="开始时间 YYYY-MM-DD HH:MM:SS"),
    end_time: str = Query(None, description="结束时间 YYYY-MM-DD HH:MM:SS"),
    limit: int = Query(200, ge=1, le=1000),
):
    require_authenticated_user(request)
    binding = repo_get_binding(binding_id)
    if not binding:
        raise HTTPException(status_code=404, detail="绑定不存在")
    if not str(binding.get("tc_product") or "").startswith("self_"):
        ensure_tencent_api_enabled("腾讯云日志文件查询")
    try:
        data = binlog_service.list_binlogs(
            binding_id=binding_id,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")
    return success(data=data)


@app.get("/api/binlogs/bindings", response_model=APIResponse)
def list_binlog_bindings_endpoint(request: Request):
    require_authenticated_user(request)
    data = binlog_service.list_binlog_bindings()
    enriched = []
    for row in data["items"]:
        state = repo_get_sync_state(row["id"])
        enriched.append(_to_binding_response({**row, **state}).model_dump())
    return success(data={"total": len(enriched), "items": enriched})


def _content_disposition(filename: str) -> str:
    ascii_name = "".join(
        ch if ord(ch) < 128 and (ch.isalnum() or ch in "._-") else "_"
        for ch in filename
    ).strip("._")
    ascii_name = ascii_name or "binlog"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


@app.get("/api/binlogs/download")
def download_binlog_endpoint(
    request: Request,
    binding_id: int = Query(..., description="绑定 ID"),
    binlog_id: str = Query(..., description="Binlog ID"),
):
    require_authenticated_user(request)
    binding = repo_get_binding(binding_id)
    if not binding:
        raise HTTPException(status_code=404, detail="绑定不存在")
    if binding.get("tc_product") != "self_mysql":
        raise HTTPException(status_code=400, detail="该接口仅支持自建 MySQL binlog 下载")
    try:
        data = binlog_service.download_self_hosted_mysql_binlog(binding_id, binlog_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")

    content = data["content"]
    filename = data["file_name"]
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": _content_disposition(filename),
            "Content-Length": str(len(content)),
        },
    )


@app.get("/api/binlogs/download-url", response_model=APIResponse)
def binlog_download_url_endpoint(
    request: Request,
    binding_id: int = Query(..., description="绑定 ID"),
    binlog_id: str = Query(..., description="Binlog ID"),
):
    require_authenticated_user(request)
    binding = repo_get_binding(binding_id)
    if not binding:
        raise HTTPException(status_code=404, detail="绑定不存在")
    if not str(binding.get("tc_product") or "").startswith("self_"):
        ensure_tencent_api_enabled("腾讯云日志下载链接查询")
    try:
        data = binlog_service.binlog_download_url(binding_id, binlog_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")
    return success(data=data)


@app.get("/api/monitor/bindings", response_model=APIResponse)
def list_monitor_bindings_endpoint(request: Request):
    require_authenticated_user(request)
    return success(data=monitor_service.list_monitor_bindings())


@app.get("/api/monitor/metrics", response_model=APIResponse)
def list_monitor_metrics_endpoint(
    request: Request,
    binding_id: int = Query(..., description="腾讯云绑定 ID"),
):
    require_authenticated_user(request)
    ensure_tencent_api_enabled("腾讯云监控指标查询")
    try:
        data = monitor_service.list_metrics(binding_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except TencentCloudSDKException as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")
    return success(data=data)


@app.post("/api/monitor/data", response_model=APIResponse)
def get_monitor_data_endpoint(http_request: Request, request: MonitorDataRequest):
    require_authenticated_user(http_request)
    ensure_tencent_api_enabled("腾讯云监控数据查询")
    try:
        data = monitor_service.get_metric_data(
            binding_id=request.binding_id,
            metric_name=request.metric_name,
            period=request.period,
            range_hours=request.range_hours,
            start_time=request.start_time,
            end_time=request.end_time,
            dimensions=request.dimensions,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except TencentCloudSDKException as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")
    return success(data=data)


@app.post("/api/sql/execute", response_model=SQLExecuteResponse)
def sql_execute(http_request: Request, request: SQLExecuteRequest):
    require_authenticated_user(http_request)
    instance = repo_get_instance(request.instance_id, include_secret=True)
    if not instance:
        return SQLExecuteResponse(success=False, message="实例不存在")

    return execute_sql(
        db_type=DBType(instance["db_type"]),
        sql=request.sql,
        host=instance["host"],
        port=instance["port"],
        username=instance.get("username"),
        password=instance.get("password"),
        database=instance.get("database"),
        limit=request.limit,
    )


# =====================================================================
# 慢 SQL 模块：腾讯云凭证 / 绑定 / 慢查询 / 调度器
# =====================================================================

def _to_credential_response(row: dict) -> TCCredentialResponse:
    return TCCredentialResponse(
        id=row["id"],
        name=row["name"],
        secret_id=row["secret_id"],
        endpoint_suffix=row.get("endpoint_suffix") or "tencentcloudapi.com",
        is_default=bool(row.get("is_default")),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


@app.get("/api/tc/credentials", response_model=APIResponse)
def list_tc_credentials(request: Request):
    require_dba_user(request)
    rows = list_credentials()
    return success(data={"total": len(rows), "items": [_to_credential_response(r).model_dump() for r in rows]})


@app.post("/api/tc/credentials", response_model=APIResponse)
def create_tc_credential(request: Request, payload: TCCredentialCreate):
    require_dba_user(request)
    try:
        encrypted = encrypt_secret(payload.secret_key)
        created = repo_create_credential(payload.model_dump(mode="json"), encrypted)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="凭证名称已存在")
    return success(message="凭证已创建", data=_to_credential_response(created).model_dump(), code=201)


@app.put("/api/tc/credentials/{credential_id}", response_model=APIResponse)
def update_tc_credential(request: Request, credential_id: int, payload: TCCredentialUpdate):
    require_dba_user(request)
    data = payload.model_dump(mode="json", exclude_unset=True)
    encrypted = encrypt_secret(data["secret_key"]) if data.get("secret_key") else None
    data.pop("secret_key", None)
    try:
        updated = repo_update_credential(credential_id, data, secret_key_enc=encrypted)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="凭证名称已存在")
    if not updated:
        raise HTTPException(status_code=404, detail="凭证不存在")
    return success(message="凭证已更新", data=_to_credential_response(updated).model_dump())


@app.delete("/api/tc/credentials/{credential_id}", response_model=APIResponse)
def delete_tc_credential(request: Request, credential_id: int):
    require_dba_user(request)
    if not repo_delete_credential(credential_id):
        raise HTTPException(status_code=404, detail="凭证不存在")
    return success(message="凭证已删除")


@app.post("/api/tc/credentials/{credential_id}/test", response_model=APIResponse)
def test_tc_credential(request: Request, credential_id: int):
    from crypto import decrypt as decrypt_secret

    require_dba_user(request)
    ensure_tencent_api_enabled("腾讯云凭证测试")
    cred = repo_get_credential(credential_id, include_secret=True)
    if not cred:
        raise HTTPException(status_code=404, detail="凭证不存在")
    try:
        sk = decrypt_secret(cred["secret_key_enc"])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"解密失败: {e}")

    # 选用第一个绑定此凭证的 binding 区域作为探测，否则 ap-guangzhou
    region = "ap-guangzhou"
    for b in list_bindings():
        if b.get("credential_id") == credential_id and b.get("tc_region"):
            region = b["tc_region"]
            break

    try:
        TCClient(
            cred["secret_id"],
            sk,
            region,
            cred.get("endpoint_suffix") or "tencentcloudapi.com",
        ).test_credentials()
    except TencentCloudSDKException as e:
        raise HTTPException(status_code=400, detail=f"签名验证失败: {e}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"测试失败: {e}")
    return success(message="凭证有效", data={"region_used": region})


def _to_binding_response(row: dict) -> BindingResponse:
    return BindingResponse(
        id=row["id"],
        instance_id=row["instance_id"],
        instance_name=row.get("instance_name"),
        tc_product=row["tc_product"],
        tc_instance_id=row["tc_instance_id"],
        tc_region=row["tc_region"],
        credential_id=row["credential_id"],
        credential_name=row.get("credential_name"),
        enabled=bool(row.get("enabled")),
        last_poll_at=row.get("last_poll_at"),
        last_success_at=row.get("last_success_at"),
        last_error=row.get("last_error"),
        consecutive_failures=int(row.get("consecutive_failures") or 0),
    )


def _clean_region_list(regions: list[str]) -> list[str]:
    cleaned = []
    seen = set()
    for region in regions or []:
        item = str(region).strip()
        if item and item not in seen:
            cleaned.append(item)
            seen.add(item)
    if not cleaned:
        raise HTTPException(status_code=400, detail="至少需要指定一个地域")
    return cleaned


def _discover_instances_from_tc(payload: TCDiscoveryRequest) -> tuple[list[dict], list[dict]]:
    ensure_tencent_api_enabled("腾讯云实例扫描")
    cred = repo_get_credential(payload.credential_id, include_secret=True)
    if not cred:
        raise HTTPException(status_code=404, detail="凭证不存在")
    try:
        secret_key = decrypt_secret(cred["secret_key_enc"])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"凭证解密失败: {e}")

    products = {p.value if isinstance(p, TCProduct) else str(p) for p in payload.products}
    regions = _clean_region_list(payload.regions)
    items: list[dict] = []
    errors: list[dict] = []

    for region in regions:
        try:
            client = TCClient(
                cred["secret_id"],
                secret_key,
                region,
                cred.get("endpoint_suffix") or "tencentcloudapi.com",
            )
        except Exception as e:  # noqa: BLE001
            errors.append({"region": region, "product": "*", "message": f"{type(e).__name__}: {e}"})
            continue

        if "cdb" in products:
            try:
                items.extend(client.describe_cdb_instances())
            except TencentCloudSDKException as e:
                errors.append({"region": region, "product": "cdb", "message": str(e)})
            except Exception as e:  # noqa: BLE001
                errors.append({"region": region, "product": "cdb", "message": f"{type(e).__name__}: {e}"})

        if "cynosdb" in products:
            try:
                items.extend(client.describe_cynosdb_instances())
            except TencentCloudSDKException as e:
                errors.append({"region": region, "product": "cynosdb", "message": str(e)})
            except Exception as e:  # noqa: BLE001
                errors.append({"region": region, "product": "cynosdb", "message": f"{type(e).__name__}: {e}"})

        if "postgres" in products:
            try:
                items.extend(client.describe_postgres_instances())
            except TencentCloudSDKException as e:
                errors.append({"region": region, "product": "postgres", "message": str(e)})
            except Exception as e:  # noqa: BLE001
                errors.append({"region": region, "product": "postgres", "message": f"{type(e).__name__}: {e}"})

    unique: dict[tuple[str, str, str], dict] = {}
    for item in items:
        if not item.get("tc_instance_id"):
            continue
        key = (item["tc_product"], item["tc_region"], item["tc_instance_id"])
        unique[key] = item
    return list(unique.values()), errors


def _existing_cloud_binding(item: dict) -> Optional[dict]:
    for binding in list_bindings():
        if (
            binding.get("tc_product") == item.get("tc_product")
            and binding.get("tc_region") == item.get("tc_region")
            and binding.get("tc_instance_id") == item.get("tc_instance_id")
        ):
            return binding
    return None


def _find_local_instance(item: dict) -> Optional[dict]:
    host = item.get("host")
    port = int(item.get("port") or 3306)
    name = item.get("name")
    instances = list_instances()
    if host:
        for instance in instances:
            if instance.get("host") == host and int(instance.get("port") or 0) == port:
                return instance
        return None
    if name:
        for instance in instances:
            if instance.get("name") == name:
                return instance
    return None


def _unique_instance_name(base_name: str, tc_instance_id: str) -> str:
    names = {i["name"] for i in list_instances()}
    base = (base_name or tc_instance_id or "腾讯云数据库").strip()
    if base not in names:
        return base
    candidate = f"{base}-{tc_instance_id}" if tc_instance_id else base
    if candidate not in names:
        return candidate
    seq = 2
    while f"{candidate}-{seq}" in names:
        seq += 1
    return f"{candidate}-{seq}"


def _import_discovered_instance(item: dict, payload: TCDiscoveryRequest) -> dict:
    existing_binding = _existing_cloud_binding(item)
    if existing_binding:
        return {
            "tc_product": item["tc_product"],
            "tc_region": item["tc_region"],
            "tc_instance_id": item["tc_instance_id"],
            "name": item.get("name") or item["tc_instance_id"],
            "action": "already_bound",
            "instance_id": existing_binding["instance_id"],
            "binding_id": existing_binding["id"],
            "message": "云实例已存在绑定，已复用",
        }

    instance = _find_local_instance(item)
    action = "bound"
    if not instance:
        if not payload.create_missing_instances:
            return {
                "tc_product": item["tc_product"],
                "tc_region": item["tc_region"],
                "tc_instance_id": item["tc_instance_id"],
                "name": item.get("name") or item["tc_instance_id"],
                "action": "skipped",
                "instance_id": None,
                "binding_id": None,
                "message": "未找到匹配的本地实例",
            }
        if not item.get("host"):
            return {
                "tc_product": item["tc_product"],
                "tc_region": item["tc_region"],
                "tc_instance_id": item["tc_instance_id"],
                "name": item.get("name") or item["tc_instance_id"],
                "action": "skipped",
                "instance_id": None,
                "binding_id": None,
                "message": "腾讯云未返回内网地址，无法自动创建本地实例",
            }
        instance = repo_create_instance(
            {
                "name": _unique_instance_name(item.get("name") or "", item["tc_instance_id"]),
                "host": item["host"],
                "port": int(item.get("port") or (5432 if item.get("db_type") == "PostgreSQL" else 3306)),
                "db_type": item.get("db_type") or "MySQL",
                "username": None,
                "password": None,
                "database": None,
                "version": item.get("version"),
                "environment": "prod",
                "owner": None,
                "remark": (
                    f"腾讯云自动导入：{item['tc_product']} "
                    f"{item['tc_region']} {item['tc_instance_id']}"
                ),
            }
        )
        action = "created"

    try:
        binding = repo_create_binding(
            {
                "instance_id": instance["id"],
                "tc_product": item["tc_product"],
                "tc_instance_id": item["tc_instance_id"],
                "tc_region": item["tc_region"],
                "credential_id": payload.credential_id,
                "enabled": payload.enabled,
            }
        )
    except sqlite3.IntegrityError as e:
        return {
            "tc_product": item["tc_product"],
            "tc_region": item["tc_region"],
            "tc_instance_id": item["tc_instance_id"],
            "name": item.get("name") or item["tc_instance_id"],
            "action": "error",
            "instance_id": instance["id"],
            "binding_id": None,
            "message": f"创建绑定失败: {e}",
        }

    return {
        "tc_product": item["tc_product"],
        "tc_region": item["tc_region"],
        "tc_instance_id": item["tc_instance_id"],
        "name": item.get("name") or item["tc_instance_id"],
        "action": action,
        "instance_id": instance["id"],
        "binding_id": binding["id"],
        "message": "已自动创建本地实例并绑定" if action == "created" else "已复用本地实例并绑定",
    }


@app.get("/api/bindings", response_model=APIResponse)
def list_tc_bindings(request: Request, instance_id: int = Query(None)):
    require_authenticated_user(request)
    rows = list_bindings(instance_id=instance_id)
    # 补同步状态
    enriched = []
    for r in rows:
        state = repo_get_sync_state(r["id"])
        merged = {**r, **state}
        enriched.append(_to_binding_response(merged).model_dump())
    return success(data={"total": len(enriched), "items": enriched})


@app.post("/api/bindings", response_model=APIResponse)
def create_tc_binding(request: Request, payload: BindingCreate):
    require_dba_user(request)
    # 校验 instance 与 credential 必须存在
    if not repo_get_instance(payload.instance_id):
        raise HTTPException(status_code=404, detail="实例不存在，请先在实例列表录入")
    if not repo_get_credential(payload.credential_id, include_secret=False):
        raise HTTPException(status_code=404, detail="凭证不存在")
    try:
        created = repo_create_binding(payload.model_dump(mode="json"))
    except sqlite3.IntegrityError as e:
        raise HTTPException(status_code=400, detail=f"绑定已存在: {e}")
    return success(message="绑定已创建", data=_to_binding_response(created).model_dump(), code=201)


@app.put("/api/bindings/{binding_id}", response_model=APIResponse)
def update_tc_binding(request: Request, binding_id: int, payload: BindingUpdate):
    require_dba_user(request)
    data = payload.model_dump(mode="json", exclude_unset=True)
    updated = repo_update_binding(binding_id, data)
    if not updated:
        raise HTTPException(status_code=404, detail="绑定不存在")
    return success(message="绑定已更新", data=_to_binding_response(updated).model_dump())


@app.delete("/api/bindings/{binding_id}", response_model=APIResponse)
def delete_tc_binding(request: Request, binding_id: int):
    require_dba_user(request)
    if not repo_delete_binding(binding_id):
        raise HTTPException(status_code=404, detail="绑定不存在")
    return success(message="绑定已删除")


@app.post("/api/tc/discovery/instances", response_model=APIResponse)
def discover_tc_instances(request: Request, payload: TCDiscoveryRequest):
    require_dba_user(request)
    items, errors = _discover_instances_from_tc(payload)
    return success(
        data={
            "total": len(items),
            "items": items,
            "errors": errors,
        }
    )


@app.post("/api/tc/discovery/import", response_model=APIResponse)
def import_tc_instances(request: Request, payload: TCDiscoveryRequest):
    require_dba_user(request)
    items, errors = _discover_instances_from_tc(payload)
    results = [_import_discovered_instance(item, payload) for item in items]
    summary: dict[str, int] = {}
    for item in results:
        summary[item["action"]] = summary.get(item["action"], 0) + 1
    return success(
        message="腾讯云实例导入完成",
        data={
            "total_discovered": len(items),
            "summary": summary,
            "results": results,
            "errors": errors,
        },
    )


@app.get("/api/slow-queries", response_model=APIResponse)
def list_slow_queries_endpoint(
    request: Request,
    instance_id: int = Query(None),
    tc_product: str = Query(None),
    tc_region: str = Query(None),
    database: str = Query(None),
    min_query_time_ms: int = Query(None, ge=0),
    fingerprint: str = Query(None),
    keyword: str = Query(None),
    start_ts: int = Query(None),
    end_ts: int = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
):
    require_authenticated_user(request)
    if tc_product and tc_product not in SLOW_QUERY_PRODUCTS:
        raise HTTPException(status_code=400, detail="不支持的慢查询来源类型")
    result = list_slow_queries(
        instance_id=instance_id,
        tc_product=tc_product,
        tc_region=tc_region,
        database=database,
        min_query_time_ms=min_query_time_ms,
        fingerprint=fingerprint,
        keyword=keyword,
        start_ts=start_ts,
        end_ts=end_ts,
        page=page,
        page_size=page_size,
    )
    return success(data=result)


# 注意：以下 fixed 子路径必须放在 /api/slow-queries/{slow_query_id} 之前，
# 否则 FastAPI 会把 stats/timeseries/top 当作 slow_query_id 拦截。
@app.get("/api/slow-queries/stats", response_model=APIResponse)
def slow_query_stats_endpoint(
    request: Request,
    instance_id: int = Query(None),
    start_ts: int = Query(None),
    end_ts: int = Query(None),
    top_limit: int = Query(10, ge=1, le=100),
):
    require_authenticated_user(request)
    stats = slow_query_stats(
        instance_id=instance_id,
        start_ts=start_ts,
        end_ts=end_ts,
        top_limit=top_limit,
    )
    return success(data=stats)


@app.get("/api/slow-queries/timeseries", response_model=APIResponse)
def slow_query_timeseries_endpoint(
    request: Request,
    instance_id: int = Query(None),
    start_ts: int = Query(None),
    end_ts: int = Query(None),
    bucket: str = Query("hour"),
):
    require_authenticated_user(request)
    if bucket not in ("hour", "day"):
        raise HTTPException(status_code=400, detail="bucket 必须是 hour 或 day")
    rows = slow_query_timeseries(
        instance_id=instance_id,
        start_ts=start_ts,
        end_ts=end_ts,
        bucket=bucket,
    )
    return success(data={"items": rows})


@app.get("/api/slow-queries/top", response_model=APIResponse)
def slow_query_top_endpoint(
    request: Request,
    instance_id: int = Query(None),
    start_ts: int = Query(None),
    end_ts: int = Query(None),
    limit: int = Query(10, ge=1, le=100),
):
    require_authenticated_user(request)
    return success(data={"items": top_slow_fingerprints(instance_id, start_ts, end_ts, limit)})


@app.post("/api/slow-queries/refresh", response_model=APIResponse)
async def refresh_slow_queries(
    request: Request,
    instance_id: int = Query(None, description="仅采集指定自建实例 ID"),
):
    require_dba_user(request)
    self_hosted = await to_thread(
        slow_query_service.poll_all_self_hosted,
        instance_id,
    )
    tencent_triggered = False
    tencent_skipped_reason = None
    if TENCENT_API_ENABLED:
        sched = app.state.scheduler
        tencent_triggered = await sched.trigger_now()
    else:
        tencent_skipped_reason = "腾讯云 API 调用已关闭，已跳过腾讯云慢查询同步"

    message = (
        f"自建库采集完成：新增 {self_hosted['inserted']} 条"
        if self_hosted["total"]
        else "未找到可直接采集的自建 MySQL/PostgreSQL 实例"
    )
    if tencent_triggered:
        message += "；腾讯云同步已触发"
    return success(
        message=message,
        data={
            "triggered": True,
            "tencent_triggered": tencent_triggered,
            "tencent_skipped_reason": tencent_skipped_reason,
            "self_hosted": self_hosted,
        },
    )


@app.get("/api/slow-queries/{slow_query_id}", response_model=APIResponse)
def get_slow_query_endpoint(request: Request, slow_query_id: int):
    require_authenticated_user(request)
    row = repo_get_slow_query(slow_query_id)
    if not row:
        raise HTTPException(status_code=404, detail="慢查询不存在")
    # 同指纹历史
    history = list_slow_queries_by_fingerprint(row["fingerprint"], exclude_id=slow_query_id, limit=50)
    return success(data={"detail": row, "history": history})


@app.post("/api/slow-queries/{slow_query_id}/explain", response_model=ExplainResponse)
def explain_slow_query(request: Request, slow_query_id: int):
    require_authenticated_user(request)
    row = repo_get_slow_query(slow_query_id)
    if not row:
        return ExplainResponse(success=False, message="慢查询不存在")
    instance = repo_get_instance(row["instance_id"], include_secret=True)
    if not instance or not instance.get("host"):
        return ExplainResponse(success=False, message="实例连接信息未配置，无法执行 EXPLAIN")
    db_type = instance.get("db_type")
    if db_type not in ("MySQL", "PostgreSQL"):
        return ExplainResponse(success=False, message="仅 MySQL / PostgreSQL 支持 EXPLAIN")

    sql = f"EXPLAIN {row['sql_text']}"
    resp = execute_sql(
        db_type=DBType(db_type),
        sql=sql,
        host=instance["host"],
        port=int(instance["port"]),
        username=instance.get("username"),
        password=instance.get("password"),
        database=instance.get("database"),
        limit=200,
    )
    return ExplainResponse(
        success=resp.success,
        message=resp.message,
        columns=resp.columns,
        rows=resp.rows,
        execution_time=resp.execution_time,
    )


@app.get("/api/scheduler/status", response_model=APIResponse)
def scheduler_status_endpoint(request: Request):
    require_authenticated_user(request)
    sched: scheduler_mod.SlowQueryScheduler = app.state.scheduler
    data = sched.status()
    data["tencent_api_enabled"] = TENCENT_API_ENABLED
    data["cloud_backup_enabled"] = CLOUD_BACKUP_ENABLED
    data["scheduler_enabled"] = SCHEDULER_ENABLED
    data["auto_enabled"] = bool(getattr(app.state, "scheduler_auto_enabled", False))
    return success(data=data)


# === Static frontend serving ===
# When the FastAPI app is the only listener, also serve the bundled
# single-file SPA from the project root. We deliberately do NOT mount the
# project root via StaticFiles — that would expose data/dbcheck.db and
# data/.fernet_key to the network. Only index.html is reachable.
_FRONTEND_DIR = Path(__file__).resolve().parent.parent
_SPA_API_PREFIXES = ("api", "docs", "redoc", "openapi.json")


@app.get("/", include_in_schema=False)
def _serve_root():
    return FileResponse(_FRONTEND_DIR / "index.html", media_type="text/html")


@app.get("/{full_path:path}", include_in_schema=False)
def _serve_spa(full_path: str):
    # Never serve anything that looks like an API/docs path. The 404 here is
    # only reached for paths that didn't match any earlier API route, so it's
    # a genuine "not found" rather than masking a real API.
    if full_path.split("/", 1)[0] in _SPA_API_PREFIXES:
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(_FRONTEND_DIR / "index.html", media_type="text/html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
