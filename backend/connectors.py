# -*- coding: utf-8 -*-
"""
数据库连接器
支持：MySQL, PostgreSQL, Redis, MongoDB, Oracle, SQL Server
"""

import socket
import time
from typing import Optional, Tuple
from models import DBType, ConnectivityCheckResponse, SQLExecuteResponse


def check_tcp_connectivity(host: str, port: int, timeout: int = 5) -> Tuple[bool, float]:
    """
    检查 TCP 端口是否可达
    """
    start_time = time.time()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        elapsed = time.time() - start_time
        return result == 0, elapsed
    except Exception:
        elapsed = time.time() - start_time
        return False, elapsed


def check_mysql(
    host: str,
    port: int,
    username: Optional[str] = None,
    password: Optional[str] = None,
    database: Optional[str] = None,
) -> ConnectivityCheckResponse:
    """检测 MySQL 连通性"""
    start_time = time.time()
    try:
        import pymysql
        conn = pymysql.connect(
            host=host,
            port=port,
            user=username or "root",
            password=password or "",
            database=database or None,
            connect_timeout=10,
            charset="utf8mb4"
        )
        version = conn.get_server_info()
        conn.close()
        elapsed = time.time() - start_time
        return ConnectivityCheckResponse(
            success=True,
            message="连接成功",
            version=version,
            response_time=round(elapsed, 3)
        )
    except ImportError:
        return ConnectivityCheckResponse(
            success=False,
            message="pymysql 模块未安装"
        )
    except Exception as e:
        elapsed = time.time() - start_time
        return ConnectivityCheckResponse(
            success=False,
            message=f"连接失败: {str(e)}",
            response_time=round(elapsed, 3)
        )


def check_postgresql(
    host: str,
    port: int,
    username: Optional[str] = None,
    password: Optional[str] = None,
    database: Optional[str] = None,
) -> ConnectivityCheckResponse:
    """检测 PostgreSQL 连通性"""
    start_time = time.time()
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=host,
            port=port,
            user=username or "postgres",
            password=password or "",
            dbname=database or "postgres",
            connect_timeout=10
        )
        version = conn.server_version
        conn.close()
        elapsed = time.time() - start_time
        # 转换版本号为可读字符串
        major = version // 10000
        minor = (version // 100) % 100
        return ConnectivityCheckResponse(
            success=True,
            message="连接成功",
            version=f"PostgreSQL {major}.{minor}",
            response_time=round(elapsed, 3)
        )
    except ImportError:
        return ConnectivityCheckResponse(
            success=False,
            message="psycopg2 模块未安装"
        )
    except Exception as e:
        elapsed = time.time() - start_time
        return ConnectivityCheckResponse(
            success=False,
            message=f"连接失败: {str(e)}",
            response_time=round(elapsed, 3)
        )


def check_redis(
    host: str,
    port: int,
    username: Optional[str] = None,
    password: Optional[str] = None,
    database: Optional[str] = None,
) -> ConnectivityCheckResponse:
    """检测 Redis 连通性"""
    start_time = time.time()
    try:
        import redis
        r = redis.Redis(
            host=host,
            port=port,
            username=username or None,
            password=password,
            db=int(database) if database and database.isdigit() else 0,
            socket_timeout=10,
            socket_connect_timeout=10
        )
        version = r.info()["redis_version"]
        r.close()
        elapsed = time.time() - start_time
        return ConnectivityCheckResponse(
            success=True,
            message="连接成功",
            version=version,
            response_time=round(elapsed, 3)
        )
    except ImportError:
        return ConnectivityCheckResponse(
            success=False,
            message="redis 模块未安装"
        )
    except Exception as e:
        elapsed = time.time() - start_time
        return ConnectivityCheckResponse(
            success=False,
            message=f"连接失败: {str(e)}",
            response_time=round(elapsed, 3)
        )


def check_mongodb(
    host: str,
    port: int,
    username: Optional[str] = None,
    password: Optional[str] = None,
    database: Optional[str] = None,
) -> ConnectivityCheckResponse:
    """检测 MongoDB 连通性"""
    start_time = time.time()
    try:
        from pymongo import MongoClient
        auth_db = database or "admin"
        if username and password:
            uri = f"mongodb://{username}:{password}@{host}:{port}/?authSource={auth_db}"
        else:
            uri = f"mongodb://{host}:{port}/"
        client = MongoClient(uri, serverSelectionTimeoutMS=10000)
        version = client.server_info()["version"]
        client.close()
        elapsed = time.time() - start_time
        return ConnectivityCheckResponse(
            success=True,
            message="连接成功",
            version=version,
            response_time=round(elapsed, 3)
        )
    except ImportError:
        return ConnectivityCheckResponse(
            success=False,
            message="pymongo 模块未安装"
        )
    except Exception as e:
        elapsed = time.time() - start_time
        return ConnectivityCheckResponse(
            success=False,
            message=f"连接失败: {str(e)}",
            response_time=round(elapsed, 3)
        )


def check_oracle(
    host: str,
    port: int,
    username: Optional[str] = None,
    password: Optional[str] = None,
    database: Optional[str] = None,
) -> ConnectivityCheckResponse:
    """检测 Oracle 连通性"""
    start_time = time.time()
    try:
        import cx_Oracle
        dsn = cx_Oracle.makedsn(host, port, service_name=database or "ORCL")
        conn = cx_Oracle.connect(username or "system", password or "", dsn, timeout=10)
        version = conn.version
        conn.close()
        elapsed = time.time() - start_time
        return ConnectivityCheckResponse(
            success=True,
            message="连接成功",
            version=version,
            response_time=round(elapsed, 3)
        )
    except ImportError:
        return ConnectivityCheckResponse(
            success=False,
            message="cx_Oracle 模块未安装"
        )
    except Exception as e:
        elapsed = time.time() - start_time
        return ConnectivityCheckResponse(
            success=False,
            message=f"连接失败: {str(e)}",
            response_time=round(elapsed, 3)
        )


def check_sqlserver(
    host: str,
    port: int,
    username: Optional[str] = None,
    password: Optional[str] = None,
    database: Optional[str] = None,
) -> ConnectivityCheckResponse:
    """检测 SQL Server 连通性"""
    start_time = time.time()
    try:
        import pyodbc
        conn_str = (
            "DRIVER={ODBC Driver 17 for SQL Server};"
            f"SERVER={host},{port};DATABASE={database or 'master'};"
            f"UID={username or 'sa'};PWD={password or ''}"
        )
        conn = pyodbc.connect(conn_str, timeout=10)
        version = conn.getinfo(pyodbc.SQL_SERVER_VERSION)
        conn.close()
        elapsed = time.time() - start_time
        return ConnectivityCheckResponse(
            success=True,
            message="连接成功",
            version=version,
            response_time=round(elapsed, 3)
        )
    except ImportError:
        return ConnectivityCheckResponse(
            success=False,
            message="pyodbc 模块未安装"
        )
    except Exception as e:
        elapsed = time.time() - start_time
        return ConnectivityCheckResponse(
            success=False,
            message=f"连接失败: {str(e)}",
            response_time=round(elapsed, 3)
        )


def check_connectivity(
    db_type: DBType,
    host: str,
    port: int,
    username: Optional[str] = None,
    password: Optional[str] = None,
    database: Optional[str] = None,
) -> ConnectivityCheckResponse:
    """
    根据数据库类型检测连通性
    """
    check_functions = {
        DBType.MYSQL: check_mysql,
        DBType.POSTGRESQL: check_postgresql,
        DBType.REDIS: check_redis,
        DBType.MONGODB: check_mongodb,
        DBType.ORACLE: check_oracle,
        DBType.SQLSERVER: check_sqlserver,
    }

    check_func = check_functions.get(db_type)
    if check_func:
        return check_func(host, port, username, password, database)

    return ConnectivityCheckResponse(
        success=False,
        message=f"不支持的数据库类型: {db_type}"
    )


# ========== SQL 执行函数 ==========

def is_read_only_sql(sql: str, allowed_prefixes: Tuple[str, ...]) -> bool:
    normalized = sql.strip().strip(";").upper()
    return normalized.startswith(allowed_prefixes)


def apply_limit(sql: str, limit: int) -> str:
    sql_without_semicolon = sql.strip().rstrip(";")
    if " LIMIT " in f" {sql_without_semicolon.upper()} ":
        return sql_without_semicolon
    return f"{sql_without_semicolon} LIMIT {limit}"


def execute_mysql(
    sql: str,
    host: str,
    port: int,
    username: Optional[str] = None,
    password: Optional[str] = None,
    database: Optional[str] = None,
    limit: int = 1000,
) -> SQLExecuteResponse:
    """执行 MySQL SQL"""
    start_time = time.time()
    try:
        import pymysql

        # 限制 SQL 类型（只允许 SELECT 等查询语句）
        if not is_read_only_sql(sql, ("SELECT", "SHOW", "DESCRIBE", "EXPLAIN")):
            return SQLExecuteResponse(
                success=False,
                message="只允许执行 SELECT/SHOW/DESCRIBE/EXPLAIN 查询语句",
                execution_time=round(time.time() - start_time, 3)
            )

        conn = pymysql.connect(
            host=host,
            port=port,
            user=username or "root",
            password=password or "",
            database=database or None,
            connect_timeout=10,
            charset="utf8mb4"
        )

        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            # 添加 LIMIT
            if sql.strip().upper().startswith("SELECT"):
                sql = apply_limit(sql, limit)

            cursor.execute(sql)
            rows = cursor.fetchall()

            # 转换 datetime 等特殊类型
            for row in rows:
                for key, value in row.items():
                    if hasattr(value, 'isoformat'):
                        row[key] = value.isoformat()

            columns = list(rows[0].keys()) if rows else []

            elapsed = time.time() - start_time
            return SQLExecuteResponse(
                success=True,
                message="查询成功",
                columns=columns,
                rows=rows,
                row_count=len(rows),
                execution_time=round(elapsed, 3)
            )

    except ImportError:
        return SQLExecuteResponse(
            success=False,
            message="pymysql 模块未安装"
        )
    except Exception as e:
        elapsed = time.time() - start_time
        return SQLExecuteResponse(
            success=False,
            message=f"SQL 执行失败: {str(e)}",
            execution_time=round(elapsed, 3)
        )


def execute_postgresql(
    sql: str,
    host: str,
    port: int,
    username: Optional[str] = None,
    password: Optional[str] = None,
    database: Optional[str] = None,
    limit: int = 1000,
) -> SQLExecuteResponse:
    """执行 PostgreSQL SQL"""
    start_time = time.time()
    try:
        import psycopg2

        if not is_read_only_sql(sql, ("SELECT", "SHOW", "DESCRIBE", "EXPLAIN")):
            return SQLExecuteResponse(
                success=False,
                message="只允许执行 SELECT/SHOW/DESCRIBE/EXPLAIN 查询语句",
                execution_time=round(time.time() - start_time, 3)
            )

        conn = psycopg2.connect(
            host=host,
            port=port,
            user=username or "postgres",
            password=password or "",
            dbname=database or "postgres",
            connect_timeout=10
        )

        with conn.cursor() as cursor:
            if sql.strip().upper().startswith("SELECT"):
                sql = apply_limit(sql, limit)

            cursor.execute(sql)
            rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description] if cursor.description else []

            # 转换为字典列表
            result_rows = [dict(zip(columns, row)) for row in rows]

            # 转换特殊类型
            for row in result_rows:
                for key, value in row.items():
                    if hasattr(value, 'isoformat'):
                        row[key] = value.isoformat()

            elapsed = time.time() - start_time
            return SQLExecuteResponse(
                success=True,
                message="查询成功",
                columns=columns,
                rows=result_rows,
                row_count=len(result_rows),
                execution_time=round(elapsed, 3)
            )

    except ImportError:
        return SQLExecuteResponse(
            success=False,
            message="psycopg2 模块未安装"
        )
    except Exception as e:
        elapsed = time.time() - start_time
        return SQLExecuteResponse(
            success=False,
            message=f"SQL 执行失败: {str(e)}",
            execution_time=round(elapsed, 3)
        )


def execute_sql(
    db_type: DBType,
    sql: str,
    host: str,
    port: int,
    username: Optional[str] = None,
    password: Optional[str] = None,
    database: Optional[str] = None,
    limit: int = 1000,
) -> SQLExecuteResponse:
    """
    根据数据库类型执行 SQL
    """
    execute_functions = {
        DBType.MYSQL: execute_mysql,
        DBType.POSTGRESQL: execute_postgresql,
    }

    # 对于暂不支持的数据库类型，返回友好的提示
    if db_type not in execute_functions:
        return SQLExecuteResponse(
            success=False,
            message=f"SQL 执行功能暂不支持 {db_type.value} 数据库类型，当前仅支持 MySQL 和 PostgreSQL"
        )

    return execute_functions[db_type](sql, host, port, username, password, database, limit)
