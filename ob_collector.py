import re
from datetime import date, datetime
from decimal import Decimal


READONLY_ALLOWED_PREFIX = "select"
FORBIDDEN_SQL_PATTERN = re.compile(
    r"\b(insert|update|delete|merge|alter|drop|create|truncate|grant|revoke|set|call|replace)\b",
    re.IGNORECASE,
)


def collect_ob_cluster(cluster):
    pymysql = load_pymysql()
    conn = open_ob_connection(pymysql, cluster)
    try:
        return {
            "readonly": True,
            "observers": query_first(conn, OBSERVER_QUERIES),
            "tenants": query_first(conn, TENANT_QUERIES),
            "tenant_backups": query_first(conn, TENANT_BACKUP_QUERIES),
            "tenant_disk_usage": query_first(conn, TENANT_DISK_USAGE_QUERIES),
            "tenant_resources": query_first(conn, TENANT_RESOURCE_QUERIES),
            "tenant_merges": query_first(conn, TENANT_MERGE_QUERIES),
            "parameters": query_first(conn, PARAMETER_QUERIES),
        }
    finally:
        conn.close()


def collect_ob_tenant_detail(config):
    pymysql = load_pymysql()
    conn = open_tenant_connection(pymysql, config)
    try:
        return {
            "readonly": True,
            "top_objects": query_first(conn, TENANT_TOP_OBJECT_QUERIES),
            "runtime_metrics": query_first(conn, TENANT_RUNTIME_QUERIES),
        }
    finally:
        conn.close()


def probe_ob_tenant_connection(config):
    pymysql = load_pymysql()
    conn = open_tenant_connection(pymysql, config)
    try:
        with conn.cursor() as cur:
            cur.execute("select 1 as ok")
            row = cur.fetchone()
        return {"ok": True, "message": f"租户 {config.get('tenant_name') or ''} 连接成功，SELECT 1 通过", "result": row}
    finally:
        conn.close()


def probe_ob_cluster(cluster):
    pymysql = load_pymysql()
    conn = open_ob_connection(pymysql, cluster)
    try:
        with conn.cursor() as cur:
            cur.execute("select 1 as ok")
            row = cur.fetchone()
        return {"ok": True, "message": f"目标 OB {target_desc(cluster)} 连接成功，SELECT 1 通过", "result": row}
    finally:
        conn.close()


def load_pymysql():
    try:
        import pymysql
    except ImportError as exc:
        raise RuntimeError("缺少 PyMySQL 依赖，请先安装 PyMySQL==1.1.1 后再执行 OB 只读采集") from exc
    return pymysql


def open_tenant_connection(pymysql, config):
    host, port = split_endpoint(config.get("endpoint") or "", config.get("port") or 2881)
    user = config.get("tenant_user") or ""
    password = config.get("tenant_password") or ""
    database = config.get("database") or ""
    if not user or not password:
        raise RuntimeError(f"租户 {config.get('tenant_name') or ''} 未配置采集账号或密码")
    try:
        return pymysql.connect(
            host=host,
            port=int(port),
            user=user,
            password=password,
            database=database,
            charset="utf8mb4",
            connect_timeout=6,
            read_timeout=30,
            cursorclass=pymysql.cursors.DictCursor,
        )
    except Exception as exc:
        raise RuntimeError(format_ob_connect_error(host, port, user, exc)) from exc


def open_ob_connection(pymysql, cluster):
    host, port = split_endpoint(cluster.get("endpoint") or "", cluster.get("port") or 2881)
    password = cluster.get("sys_password") or ""
    if not password:
        raise RuntimeError(f"目标 OB {host}:{port} 未配置 {cluster.get('sys_user') or 'root@sys'} 密码，无法执行只读采集")
    try:
        return pymysql.connect(
            host=host,
            port=int(port),
            user=cluster.get("sys_user") or "root@sys",
            password=password,
            database="oceanbase",
            charset="utf8mb4",
            connect_timeout=6,
            read_timeout=30,
            cursorclass=pymysql.cursors.DictCursor,
        )
    except Exception as exc:
        raise RuntimeError(format_ob_connect_error(host, port, cluster.get("sys_user") or "root@sys", exc)) from exc


def target_desc(cluster):
    host, port = split_endpoint(cluster.get("endpoint") or "", cluster.get("port") or 2881)
    return f"{host}:{port}"


def format_ob_connect_error(host, port, user, exc):
    code = exc.args[0] if getattr(exc, "args", None) else None
    raw = str(exc)
    if code == 1045:
        return (
            f"目标 OB SQL 入口 {host}:{port} 已响应，但账号认证失败。"
            f"请检查用户格式和密码：当前用户为 {user}。"
            "OceanBase 常见格式为 root@sys，也可能需要 root@sys#集群名；"
            f"原始错误：{raw}"
        )
    if code == 2003:
        return (
            f"无法连接目标 OB SQL 入口 {host}:{port}。"
            "请检查地址、端口、防火墙、安全组、OBProxy/RootService 是否可达；"
            f"原始错误：{raw}"
        )
    return f"目标 OB SQL 入口 {host}:{port} 连接失败，当前用户 {user}；原始错误：{raw}"


def split_endpoint(endpoint, default_port):
    value = endpoint.strip()
    if value.startswith("[") and "]" in value:
        host, _, port = value[1:].partition("]")
        return host, int(port[1:] or default_port)
    if ":" in value and not re.match(r"^\d+\.\d+\.\d+\.\d+$", value):
        host, port = value.rsplit(":", 1)
        if port.isdigit():
            return host, int(port)
    return value, int(default_port)


def query_first(conn, queries):
    errors = []
    with conn.cursor() as cur:
        for sql in queries:
            try:
                ensure_readonly_select(sql)
                cur.execute(sql)
                return normalize_rows(cur.fetchall())
            except Exception as exc:
                errors.append(str(exc))
    return [{"_error": "; ".join(errors[:3])}]


def ensure_readonly_select(sql):
    normalized = sql.strip().rstrip(";").strip()
    if ";" in normalized:
        raise RuntimeError("拒绝执行多语句 SQL，OB 采集仅允许单条 SELECT")
    if not normalized.lower().startswith(READONLY_ALLOWED_PREFIX):
        raise RuntimeError("拒绝执行非 SELECT SQL，OB 采集必须只读")
    if FORBIDDEN_SQL_PATTERN.search(normalized):
        raise RuntimeError("拒绝执行包含变更关键字的 SQL，OB 采集必须只读")


def normalize_rows(rows):
    normalized = []
    for row in rows:
        normalized.append({str(key).lower(): normalize_value(value) for key, value in row.items()})
    return normalized


def normalize_value(value):
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, Decimal):
        return float(value)
    return value


OBSERVER_QUERIES = [
    """
    select zone, svr_ip, sql_port, rpc_port, status,
           cast(null as signed) as disk_total_gb,
           cast(null as signed) as disk_used_gb
    from oceanbase.DBA_OB_SERVERS
    """,
    """
    select zone, svr_ip, inner_port as sql_port, svr_port as rpc_port, status,
           cast(null as signed) as disk_total_gb,
           cast(null as signed) as disk_used_gb
    from oceanbase.__all_server
    """,
]


TENANT_QUERIES = [
    """
    select tenant_id, tenant_name as name, compatibility_mode as tenant_mode,
           primary_zone, locality, tenant_role, status,
           cast(null as signed) as unit_num
    from oceanbase.DBA_OB_TENANTS
    where tenant_name <> 'sys'
    """,
    """
    select tenant_id, tenant_name as name, compatibility_mode as tenant_mode,
           primary_zone, locality, tenant_role, status,
           cast(null as signed) as unit_num
    from oceanbase.CDB_OB_TENANTS
    where tenant_name <> 'sys'
    """,
    """
    select tenant_id, tenant_name as name, 'UNKNOWN' as tenant_mode,
           primary_zone, locality, tenant_role, status,
           cast(null as signed) as unit_num
    from oceanbase.__all_tenant
    where tenant_name <> 'sys'
    """,
]


TENANT_BACKUP_QUERIES = [
    """
    select tenant_id,
           max(end_timestamp) as last_full_backup_time
    from oceanbase.CDB_OB_BACKUP_SET_FILES
    where upper(backup_type) = 'FULL'
      and result = 0
      and end_timestamp is not null
    group by tenant_id
    """,
    """
    select tenant_id,
           max(end_time) as last_full_backup_time
    from oceanbase.CDB_OB_BACKUP_SET_FILES
    where upper(backup_type) = 'FULL'
      and upper(status) in ('SUCCESS', 'COMPLETED')
    group by tenant_id
    """,
    """
    select tenant_id,
           max(completion_time) as last_full_backup_time
    from oceanbase.CDB_OB_BACKUP_JOBS
    where upper(backup_type) = 'FULL'
      and upper(status) in ('SUCCESS', 'COMPLETED')
    group by tenant_id
    """,
    """
    select tenant_id,
           max(end_time) as last_full_backup_time
    from oceanbase.DBA_OB_BACKUP_SET_FILES
    where upper(backup_type) = 'FULL'
      and upper(status) in ('SUCCESS', 'COMPLETED')
    group by tenant_id
    """,
]


TENANT_DISK_USAGE_QUERIES = [
    """
    select tenant_id,
           round(sum(data_disk_in_use) / 1024 / 1024 / 1024, 2) as data_disk_used_gb,
           round(sum(data_disk_allocated) / 1024 / 1024 / 1024, 2) as data_disk_total_gb,
           round(case when sum(data_disk_allocated) > 0
                      then sum(data_disk_in_use) * 100 / sum(data_disk_allocated)
                 end, 2) as data_disk_usage_pct,
           round(sum(log_disk_in_use) / 1024 / 1024 / 1024, 2) as log_disk_used_gb,
           round(sum(log_disk_size) / 1024 / 1024 / 1024, 2) as log_disk_total_gb,
           round(case when sum(log_disk_size) > 0
                      then sum(log_disk_in_use) * 100 / sum(log_disk_size)
                 end, 2) as log_disk_usage_pct
    from oceanbase.GV$OB_UNITS
    group by tenant_id
    """,
    """
    select tenant_id,
           round(sum(data_disk_in_use) / 1024 / 1024 / 1024, 2) as data_disk_used_gb,
           cast(null as signed) as data_disk_total_gb,
           cast(null as signed) as data_disk_usage_pct,
           round(sum(log_disk_in_use) / 1024 / 1024 / 1024, 2) as log_disk_used_gb,
           round(sum(log_disk_size) / 1024 / 1024 / 1024, 2) as log_disk_total_gb,
           round(case when sum(log_disk_size) > 0
                      then sum(log_disk_in_use) * 100 / sum(log_disk_size)
                 end, 2) as log_disk_usage_pct
    from oceanbase.GV$OB_UNITS
    group by tenant_id
    """,
]


TENANT_RESOURCE_QUERIES = [
    """
    select tenant_id,
           round(sum(max_cpu), 2) as cpu_cores,
           round(sum(memory_size) / 1024 / 1024 / 1024, 2) as memory_gb
    from oceanbase.GV$OB_UNITS
    group by tenant_id
    """,
    """
    select tenant_id,
           round(sum(cpu_capacity), 2) as cpu_cores,
           round(sum(memory_size) / 1024 / 1024 / 1024, 2) as memory_gb
    from oceanbase.GV$OB_UNITS
    group by tenant_id
    """,
]


TENANT_MERGE_QUERIES = [
    """
    select tenant_id,
           max(last_finish_time) as last_success_merge_time,
           max(status) as last_merge_status
    from oceanbase.CDB_OB_MAJOR_COMPACTION
    where is_error = 'NO'
      and last_finish_time is not null
    group by tenant_id
    """,
    """
    select tenant_id,
           max(end_time) as last_success_merge_time,
           max(status) as last_merge_status
    from oceanbase.CDB_OB_MAJOR_COMPACTION
    where upper(status) in ('SUCCESS', 'COMPLETED', 'IDLE')
    group by tenant_id
    """,
    """
    select tenant_id,
           max(finish_time) as last_success_merge_time,
           max(status) as last_merge_status
    from oceanbase.CDB_OB_MAJOR_COMPACTION
    where upper(status) in ('SUCCESS', 'COMPLETED', 'IDLE')
    group by tenant_id
    """,
    """
    select tenant_id,
           max(end_time) as last_success_merge_time,
           max(status) as last_merge_status
    from oceanbase.DBA_OB_MAJOR_COMPACTION
    where upper(status) in ('SUCCESS', 'COMPLETED', 'IDLE')
    group by tenant_id
    """,
]


TENANT_TOP_OBJECT_QUERIES = [
    """
    select table_schema as database_name,
           table_name,
           case when table_type = 'BASE TABLE' then 'TABLE' else table_type end as object_type,
           round(coalesce(data_length, 0) / 1024 / 1024 / 1024, 4) as data_gb,
           round(coalesce(index_length, 0) / 1024 / 1024 / 1024, 4) as index_gb,
           round((coalesce(data_length, 0) + coalesce(index_length, 0)) / 1024 / 1024 / 1024, 4) as total_gb,
           table_rows
    from information_schema.tables
    where table_schema not in ('oceanbase', 'information_schema', 'mysql', 'performance_schema')
    order by coalesce(data_length, 0) + coalesce(index_length, 0) desc
    """,
]


TENANT_RUNTIME_QUERIES = [
    """
    select
      (select count(*) from information_schema.processlist) as current_processes,
      @@max_connections as max_processes
    """,
]


PARAMETER_QUERIES = [
    """
    select tenant_id, name, value, info, section, scope
    from oceanbase.GV$OB_PARAMETERS
    where name in ('enable_rebalance', 'enable_transfer', 'enable_syslog_recycle',
                   'datafile_size', 'memory_limit', 'cpu_count')
    """,
    """
    select tenant_id, name, value, info, section, scope
    from oceanbase.__all_virtual_sys_parameter_stat
    where name in ('enable_rebalance', 'enable_transfer', 'enable_syslog_recycle',
                   'datafile_size', 'memory_limit', 'cpu_count')
    """,
]
