# DBCheck 后端

基于 FastAPI 的轻量数据库运维平台后端。当前版本使用 SQLite 保存实例、备份任务和检测日志，适合本地演示、小团队内网原型和继续二次开发。

## 已实现功能

- 实例资产管理：查询、新增、编辑、删除
- 实例字段：类型、地址、端口、用户名、数据库名/服务名、环境、负责人、备注
- 连通性检测：单实例检测、批量检测、快速检测
- 检测日志：保存每次检测结果、耗时和错误信息
- 备份任务：列表、创建、删除、状态统计
- SQL 查询：MySQL / PostgreSQL 只读查询，自动限制返回行数
- 慢 SQL：腾讯云 OpenAPI 同步；自建 MySQL / PostgreSQL 可直接连库采集
- 工作台汇总：实例健康度、环境分布、备份状态、最近检测

## 支持的数据库

| 数据库 | 连通检测 | SQL 查询 |
| --- | --- | --- |
| MySQL | 支持 | 支持 |
| PostgreSQL | 支持 | 支持 |
| Redis | 支持 | 暂不支持 |
| MongoDB | 支持 | 暂不支持 |
| Oracle | 支持 | 暂不支持 |
| SQL Server | 支持 | 暂不支持 |

## 快速启动

### 1. 安装依赖

```bash
cd /Users/apus/Desktop/vs_code/dbcheck/backend
pip install -r requirements.txt
```

### 2. 启动后端

```bash
python3 -m uvicorn main:app --host 127.0.0.1 --port 8000
```

后端启动后会自动创建：

```text
backend/dbcheck.db
```

这是 SQLite 数据库文件，里面保存实例、备份和检测日志。

腾讯云主动调用开关：

- `DBCHECK_TENCENT_API_ENABLED=true`：腾讯云 API 总开关，关闭后不会主动调用腾讯云。
- `DBCHECK_CLOUD_BACKUP_ENABLED=true`：允许创建真实腾讯云备份任务。
- `DBCHECK_SCHEDULER_ENABLED=true`：启用慢 SQL 自动轮询。
- 自动轮询间隔默认 `3600` 秒；可用 `DBCHECK_POLL_INTERVAL=3600` 调整。

生产环境 `/opt/dbcheck/.env` 中推荐不要在值后面加行尾注释；后端会容错忽略
`#` 后面的内容，但纯值最容易排查。推荐写成：

```env
# 腾讯云 API 总开关
DBCHECK_TENCENT_API_ENABLED=true

# 真实创建腾讯云备份任务
DBCHECK_CLOUD_BACKUP_ENABLED=true

# 慢 SQL 自动轮询
DBCHECK_SCHEDULER_ENABLED=true
```

自建库慢 SQL 采集默认值：

- `DBCHECK_SELF_HOSTED_SLOW_MIN_MS=1000`：只采集最大执行耗时不低于该阈值的记录/摘要。
- `DBCHECK_SELF_HOSTED_SLOW_LIMIT=200`：单实例单次最多采集条数。
- `DBCHECK_SELF_HOSTED_CONNECT_TIMEOUT=5`：自建库采集连接超时秒数。
- `DBCHECK_SELF_HOSTED_SLOW_LOG_FILE_MAX_BYTES=5242880`：读取 MySQL 慢日志文件尾部最大字节数。

自建 MySQL 按顺序读取 `mysql.slow_log`、`LOAD_FILE(@@global.slow_query_log_file)`、
`performance_schema.events_statements_summary_by_digest`。文件读取需要 MySQL 账号具备
`FILE` 权限，且 `mysqld` 能读取 `slow_query_log_file` 指向的文件。自建 PostgreSQL 读取
`pg_stat_statements`。如果数据库侧没有开启这些统计/日志，接口会正常返回 0 条。

示例：

```bash
DBCHECK_SCHEDULER_ENABLED=true DBCHECK_POLL_INTERVAL=3600 \
python3 -m uvicorn main:app --host 127.0.0.1 --port 8000
```

### 3. 启动前端

在项目根目录启动静态服务：

```bash
cd /Users/apus/Desktop/vs_code/dbcheck
python3 -m http.server 8080 --bind 127.0.0.1
```

浏览器访问：

```text
http://127.0.0.1:8080
```

API 文档：

```text
http://127.0.0.1:8000/docs
```

## 主要 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/dashboard` | 工作台汇总 |
| GET | `/api/instances` | 获取实例列表 |
| GET | `/api/instances/{id}` | 获取单个实例 |
| POST | `/api/instances` | 新增实例 |
| PUT | `/api/instances/{id}` | 更新实例 |
| DELETE | `/api/instances/{id}` | 删除实例 |
| POST | `/api/instances/{id}/check` | 检测单个实例 |
| POST | `/api/instances/batch-check` | 批量检测 |
| POST | `/api/connectivity-check` | 快速检测 |
| GET | `/api/check-logs` | 检测日志 |
| GET | `/api/backups` | 获取备份列表 |
| POST | `/api/backups` | 创建备份任务 |
| DELETE | `/api/backups/{id}` | 删除备份记录 |
| POST | `/api/sql/execute` | 执行只读 SQL |
| POST | `/api/slow-queries/refresh` | 立即同步腾讯云慢 SQL 并采集自建库慢 SQL |

## 示例

新增实例：

```bash
curl -X POST http://127.0.0.1:8000/api/instances \
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
curl -X POST http://127.0.0.1:8000/api/instances/1/check
```

执行只读 SQL：

```bash
curl -X POST http://127.0.0.1:8000/api/sql/execute \
  -H "Content-Type: application/json" \
  -d '{
    "instance_id": 1,
    "sql": "SELECT 1 AS health_check",
    "limit": 1000
  }'
```

## 当前限制

- 还没有登录、权限、审计审批和多用户能力。
- 实例密码以明文保存在本地 SQLite 中，只适合本地演示或内网原型。
- 备份任务目前是记录层面的模拟任务，没有真正调用 `mysqldump`、`pg_dump` 等工具。
- SQL 查询只做基础只读前缀限制，生产环境还需要更严格的 SQL 解析、权限控制和脱敏。
