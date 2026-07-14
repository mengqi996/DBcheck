# DBCheck 后端

DBCheck 是一个基于 FastAPI 的轻量数据库运维平台后端，使用 SQLite 做本地持久化。它覆盖实例资产、连通性检测、只读 SQL 查询、备份记录/云备份同步、慢 SQL 采集、归档日志和腾讯云监控查询，适合本地演示、小团队内网工具和继续二次开发。

当前 API 版本：`1.2.0`

## 功能概览

- 认证与权限：默认 DBA 启动引导、登录/退出、会话 token、DBA/RD 两类角色、用户管理。
- 实例资产：数据库实例列表、新增、编辑、删除，支持环境、负责人、备注、版本等字段。
- 连通性检测：单实例检测、批量检测、快速检测，检测结果会写入日志。
- 工作台汇总：实例健康度、数据库类型分布、环境分布、备份状态和最近检测记录。
- SQL 查询：MySQL / PostgreSQL 只读查询，自动补 `LIMIT`，支持 `SELECT`、`SHOW`、`DESCRIBE`、`EXPLAIN`。
- 备份管理：本地备份记录创建/删除；已绑定腾讯云实例时可发起云端手动备份并同步备份元数据。
- 腾讯云接入：凭证加密保存、凭证测试、CDB / TDSQL-C / PostgreSQL 实例发现、导入和绑定。
- 慢 SQL：腾讯云 OpenAPI 轮询；自建 MySQL / PostgreSQL 直接连库采集；支持列表、统计、趋势、Top 指纹和 EXPLAIN。
- 归档日志：腾讯云 CDB / TDSQL-C binlog、腾讯云 PostgreSQL xlog；自建 MySQL binlog 列表和直接下载。
- 监控指标：查询腾讯云 Monitor 支持的数据库指标和时间序列数据。

## 支持范围

| 数据库 | 连通检测 | SQL 查询 | 自建慢 SQL | 自建归档日志 |
| --- | --- | --- | --- | --- |
| MySQL | 支持 | 支持 | 支持 | 支持 binlog |
| PostgreSQL | 支持 | 支持 | 支持 | 暂不支持 |
| Redis | 支持 | 暂不支持 | 暂不支持 | 暂不支持 |
| MongoDB | 支持 | 暂不支持 | 暂不支持 | 暂不支持 |
| Oracle | 支持 | 暂不支持 | 暂不支持 | 暂不支持 |
| SQL Server | 支持 | 暂不支持 | 暂不支持 | 暂不支持 |

腾讯云侧当前支持 `cdb`、`cynosdb`、`postgres` 三类产品，用于实例发现、绑定、慢 SQL、备份、归档日志和监控查询。

## 项目结构

```text
backend/
├── main.py                 # FastAPI 入口、路由、生命周期和静态前端兜底
├── models.py               # Pydantic 请求/响应模型
├── storage.py              # SQLite repository、建表、迁移式补字段、种子数据
├── auth.py                 # 密码哈希、会话 token、默认管理员配置
├── crypto.py               # Tencent SecretKey 的 Fernet 加密
├── connectors.py           # 数据库连通性检测和只读 SQL 执行
├── tc_client.py            # 腾讯云 OpenAPI 客户端封装
├── slow_query_service.py   # 腾讯云/自建库慢 SQL 采集
├── scheduler.py            # 慢 SQL 与腾讯云备份同步后台任务
├── backup_service.py       # 腾讯云备份创建和同步
├── binlog_service.py       # 归档日志查询与下载
├── monitor_service.py      # 腾讯云 Monitor 指标查询
├── sql_fingerprint.py      # SQL 模板化和指纹
├── async_compat.py         # asyncio.to_thread 兼容封装
└── test_*.py               # 单元测试
```

## 快速启动

### 1. 安装依赖

```bash
cd /Users/apus/Desktop/vs_code/dbcheck/backend
python3 -m pip install -r requirements.txt
```

部分数据库驱动依赖本机客户端库。例如 Oracle 需要 Oracle Instant Client，SQL Server 需要 ODBC Driver 17 for SQL Server。只使用 MySQL / PostgreSQL / Redis / MongoDB 时，可以先按实际需要安装对应系统依赖。

### 2. 启动后端

```bash
python3 -m uvicorn main:app --host 127.0.0.1 --port 8000
```

启动时会自动创建 SQLite 数据库和 Fernet 密钥文件：

```text
backend/dbcheck.db
backend/.fernet_key
```

首次启动会创建默认 DBA：

```text
用户名：admin
密码：admin
```

生产或共享环境请首次登录后立即修改密码，或通过环境变量指定引导账号。

### 3. 访问服务

API 文档：

```text
http://127.0.0.1:8000/docs
```

如果前端 `index.html` 位于项目根目录，后端也会在 `/` 返回这个单页应用。也可以在项目根目录单独启动静态服务：

```bash
cd /Users/apus/Desktop/vs_code/dbcheck
python3 -m http.server 8080 --bind 127.0.0.1
```

浏览器访问：

```text
http://127.0.0.1:8080
```

## 配置项

### 认证与本地存储

```env
DBCHECK_BOOTSTRAP_ADMIN_USERNAME=admin
DBCHECK_BOOTSTRAP_ADMIN_DISPLAY_NAME=DBA Admin
DBCHECK_BOOTSTRAP_ADMIN_PASSWORD=admin
DBCHECK_SESSION_TTL_SECONDS=43200
DBCHECK_PASSWORD_ITERATIONS=200000
DBCHECK_SQLITE_PATH=/opt/dbcheck/data/dbcheck.db
DBCHECK_FERNET_KEY_FILE=/opt/dbcheck/data/.fernet_key
# 或使用固定材料派生 Fernet key
DBCHECK_FERNET_MATERIAL=change-me-to-a-long-random-string
```

生产路径建议固定到 `/opt/dbcheck/data`。代码中会拒绝典型生产目录 `/opt/dbcheck/app` 下的 `dbcheck.db` 和 `.fernet_key`，避免代码更新后读到新空库或新密钥。

### 腾讯云与调度

```env
DBCHECK_TENCENT_API_ENABLED=true
DBCHECK_CLOUD_BACKUP_ENABLED=true
DBCHECK_BACKUP_SYNC_ENABLED=true
DBCHECK_BACKUP_SYNC_INTERVAL=3600
DBCHECK_SCHEDULER_ENABLED=true
DBCHECK_POLL_INTERVAL=3600
DBCHECK_SCHEDULER_CONCURRENCY=4
DBCHECK_SLOW_QUERY_RETENTION_DAYS=3
```

- `DBCHECK_TENCENT_API_ENABLED` 是腾讯云主动调用总开关，关闭后不会测试凭证、发现实例、同步慢 SQL/备份、查询监控或获取云归档日志。
- `DBCHECK_CLOUD_BACKUP_ENABLED` 控制是否真实发起腾讯云手动备份；关闭时只创建本地备份记录。
- `DBCHECK_BACKUP_SYNC_ENABLED` 控制腾讯云备份元数据的后台同步。
- `DBCHECK_SCHEDULER_ENABLED` 控制腾讯云慢 SQL 后台轮询。
- 慢 SQL 本地保留期默认 3 天，启动和调度时会清理过期记录。

### 自建库慢 SQL 与 binlog

```env
DBCHECK_SELF_HOSTED_SLOW_MIN_MS=1000
DBCHECK_SELF_HOSTED_SLOW_LIMIT=200
DBCHECK_SELF_HOSTED_CONNECT_TIMEOUT=5
DBCHECK_SELF_HOSTED_SLOW_LOG_FILE_MAX_BYTES=5242880
DBCHECK_SELF_HOSTED_PG_BUCKET_SECONDS=3600
DBCHECK_SELF_HOSTED_BINLOG_DOWNLOAD_MAX_BYTES=268435456
```

自建 MySQL 慢 SQL 按顺序尝试：

1. `mysql.slow_log`
2. `LOAD_FILE(@@global.slow_query_log_file)`
3. `performance_schema.events_statements_summary_by_digest`

自建 PostgreSQL 读取 `pg_stat_statements`。如果数据库侧没有开启相关统计或日志，接口会正常返回 0 条或带错误说明。

自建 MySQL binlog 下载会先通过 `SHOW BINARY LOGS` 校验文件名，再用 `LOAD_FILE()` 读取文件。数据库账号需要 `FILE` 权限，`secure_file_priv` 需要允许读取 binlog 目录，`mysqld` 进程也需要有文件读取权限。

## 主要 API

所有业务接口除登录外都需要 `Authorization: Bearer <token>`。用户、凭证、绑定、备份创建/删除、腾讯云发现/导入、慢 SQL 刷新等写操作需要 DBA。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/auth/login` | 登录并获取 token |
| GET | `/api/auth/me` | 当前用户 |
| POST | `/api/auth/logout` | 退出登录 |
| GET/POST | `/api/users` | 用户列表/创建用户 |
| PUT/DELETE | `/api/users/{id}` | 更新/删除用户 |
| GET | `/api/dashboard` | 工作台汇总 |
| GET/POST | `/api/instances` | 实例列表/创建实例 |
| GET/PUT/DELETE | `/api/instances/{id}` | 实例详情/更新/删除 |
| POST | `/api/instances/{id}/check` | 检测单个实例 |
| POST | `/api/instances/batch-check` | 批量检测 |
| POST | `/api/connectivity-check` | 快速检测 |
| GET | `/api/check-logs` | 检测日志 |
| GET/POST | `/api/backups` | 备份列表/创建备份 |
| POST | `/api/backups/sync-tencent` | 同步腾讯云备份 |
| GET/DELETE | `/api/backups/{id}` | 备份详情/删除 |
| GET | `/api/binlogs/bindings` | 可查询归档日志的绑定 |
| GET | `/api/binlogs` | 归档日志列表 |
| GET | `/api/binlogs/download` | 自建 MySQL binlog 直接下载 |
| GET | `/api/binlogs/download-url` | 云归档日志下载地址 |
| GET | `/api/monitor/bindings` | 可查询监控的腾讯云绑定 |
| GET | `/api/monitor/metrics` | 监控指标列表 |
| POST | `/api/monitor/data` | 监控时间序列数据 |
| POST | `/api/sql/execute` | 执行只读 SQL |
| GET/POST | `/api/tc/credentials` | 腾讯云凭证列表/创建 |
| PUT/DELETE | `/api/tc/credentials/{id}` | 更新/删除腾讯云凭证 |
| POST | `/api/tc/credentials/{id}/test` | 测试腾讯云凭证 |
| GET/POST | `/api/bindings` | 腾讯云绑定列表/创建 |
| PUT/DELETE | `/api/bindings/{id}` | 更新/删除腾讯云绑定 |
| POST | `/api/tc/discovery/instances` | 扫描腾讯云实例 |
| POST | `/api/tc/discovery/import` | 导入并绑定腾讯云实例 |
| GET | `/api/slow-queries` | 慢 SQL 列表 |
| GET | `/api/slow-queries/stats` | 慢 SQL 聚合统计 |
| GET | `/api/slow-queries/timeseries` | 慢 SQL 趋势 |
| GET | `/api/slow-queries/top` | Top SQL 指纹 |
| POST | `/api/slow-queries/refresh` | 手动采集自建慢 SQL 并触发腾讯云同步 |
| GET | `/api/slow-queries/{id}` | 慢 SQL 详情和同指纹历史 |
| POST | `/api/slow-queries/{id}/explain` | 对慢 SQL 执行 EXPLAIN |
| GET | `/api/scheduler/status` | 慢 SQL/备份同步调度状态 |

## 调用示例

登录：

```bash
TOKEN=$(
  curl -s -X POST http://127.0.0.1:8000/api/auth/login \
    -H "Content-Type: application/json" \
    -d '{"username":"admin","password":"admin"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["token"])'
)
```

新增实例：

```bash
curl -X POST http://127.0.0.1:8000/api/instances \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "测试-MySQL",
    "host": "127.0.0.1",
    "port": 3306,
    "db_type": "MySQL",
    "username": "root",
    "password": "",
    "database": "mysql",
    "environment": "test",
    "owner": "DBA"
  }'
```

检测实例：

```bash
curl -X POST http://127.0.0.1:8000/api/instances/1/check \
  -H "Authorization: Bearer $TOKEN"
```

执行只读 SQL：

```bash
curl -X POST http://127.0.0.1:8000/api/sql/execute \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "instance_id": 1,
    "sql": "SELECT 1 AS health_check",
    "limit": 1000
  }'
```

创建腾讯云凭证：

```bash
curl -X POST http://127.0.0.1:8000/api/tc/credentials \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "prod-account",
    "secret_id": "AKIDxxxxxxxx",
    "secret_key": "xxxxxxxx",
    "endpoint_suffix": "tencentcloudapi.com",
    "is_default": true
  }'
```

手动刷新慢 SQL：

```bash
curl -X POST http://127.0.0.1:8000/api/slow-queries/refresh \
  -H "Authorization: Bearer $TOKEN"
```

## 测试

```bash
cd /Users/apus/Desktop/vs_code/dbcheck/backend
python3 -m unittest
```

测试会覆盖存储路径校验、认证、调度窗口、慢 SQL/归档日志相关逻辑等。部分测试会通过 mock 隔离外部数据库和腾讯云调用。

## 安全与限制

- SQLite 适合轻量部署，不适合高并发多实例写入；需要更强审计和并发能力时建议迁移到独立数据库。
- 用户密码使用 PBKDF2-HMAC-SHA256 加盐哈希保存；腾讯云 SecretKey 使用 Fernet 加密保存。
- 数据库实例密码目前仍以明文保存在本地 SQLite 中，请限制 `dbcheck.db` 文件权限，生产使用前建议改为加密存储或外部密钥管理。
- SQL 查询只做基础只读前缀限制，生产环境还需要更严格的 SQL 解析、权限控制、行列级脱敏和审计审批。
- 自建慢 SQL 和 binlog 能力依赖数据库侧日志/扩展/权限配置；账号权限不足时接口会返回错误或 0 条结果。
- 后端只在 `/` 和非 API 路径返回项目根目录的 `index.html`，不会把项目根目录整体挂成静态目录，避免暴露 SQLite 数据库和密钥文件。
