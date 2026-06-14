import os
import re
import subprocess
import tempfile
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
            "tenant_zone_resources": query_first(conn, TENANT_ZONE_RESOURCE_QUERIES),
            "tenant_merges": query_first(conn, TENANT_MERGE_QUERIES),
            "parameters": query_first(conn, PARAMETER_QUERIES),
        }
    finally:
        conn.close()


def collect_ob_tenant_detail(config):
    if is_oracle_tenant(config):
        if oracle_tenant_driver(config) == "obclient":
            return {
                "readonly": True,
                "top_objects": obclient_query_first(config, ORACLE_TENANT_TOP_OBJECT_QUERIES),
                "runtime_metrics": obclient_query_first(config, ORACLE_TENANT_RUNTIME_QUERIES),
            }
        conn = open_oracle_tenant_connection(config)
        try:
            return {
                "readonly": True,
                "top_objects": query_first(conn, ORACLE_TENANT_TOP_OBJECT_QUERIES),
                "runtime_metrics": query_first(conn, ORACLE_TENANT_RUNTIME_QUERIES),
            }
        finally:
            conn.close()
    else:
        pymysql = load_pymysql()
        conn = open_tenant_connection(pymysql, config)
        queries = TENANT_TOP_OBJECT_QUERIES
        runtime_queries = TENANT_RUNTIME_QUERIES
        try:
            return {
                "readonly": True,
                "top_objects": query_first(conn, queries),
                "runtime_metrics": query_first(conn, runtime_queries),
            }
        finally:
            conn.close()


def probe_ob_tenant_connection(config):
    if is_oracle_tenant(config):
        if oracle_tenant_driver(config) == "obclient":
            rows = obclient_query(config, "select 1 as ok from dual")
            return {"ok": True, "message": f"Oracle租户 {config.get('tenant_name') or ''} obclient 连接成功，SELECT 1 通过", "result": rows}
        conn = open_oracle_tenant_connection(config)
        try:
            with conn.cursor() as cur:
                cur.execute("select 1 as ok from dual")
                row = cur.fetchone()
            return {"ok": True, "message": f"Oracle租户 {config.get('tenant_name') or ''} 连接成功，SELECT 1 通过", "result": normalize_value(row)}
        finally:
            conn.close()
    else:
        pymysql = load_pymysql()
        conn = open_tenant_connection(pymysql, config)
        try:
            with conn.cursor() as cur:
                cur.execute("select 1 as ok")
                row = cur.fetchone()
            return {"ok": True, "message": f"租户 {config.get('tenant_name') or ''} 连接成功，SELECT 1 通过", "result": normalize_value(row)}
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


def load_oracledb():
    try:
        import oracledb
    except ImportError as exc:
        raise RuntimeError("缺少 python-oracledb 依赖，请先安装 oracledb==2.4.1 后再执行 Oracle 租户采集") from exc
    return oracledb


def is_oracle_tenant(config):
    return str(config.get("tenant_mode") or "").upper() == "ORACLE"


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


def open_oracle_tenant_connection(config):
    host, port = split_endpoint(config.get("endpoint") or "", config.get("port") or 2881)
    user = config.get("tenant_user") or ""
    password = config.get("tenant_password") or ""
    if not user or not password:
        raise RuntimeError(f"Oracle 租户 {config.get('tenant_name') or ''} 未配置采集账号或密码")

    driver = oracle_tenant_driver(config)
    if driver == "pymysql":
        mysql_config = dict(config)
        # Only use this compatibility path if the site explicitly enables it.
        mysql_config["database"] = ""
        try:
            return open_tenant_connection(load_pymysql(), mysql_config)
        except Exception as exc:
            raise RuntimeError(
                "Oracle 模式租户已配置为 PyMySQL 兼容连接，但当前 OB 返回该客户端不支持 Oracle 租户。"
                f"目标 {host}:{port}，用户 {user}；原始错误：{exc}"
            ) from exc

    oracledb = load_oracledb()
    service_name = config.get("database") or config.get("tenant_name") or ""
    if not service_name:
        raise RuntimeError("python-oracledb 模式需要配置服务名，默认可填写租户名")
    try:
        return oracledb.connect(
            user=user,
            password=password,
            dsn=oracledb.makedsn(host, int(port), service_name=service_name),
        )
    except Exception as exc:
        raise RuntimeError(format_oracle_connect_error(host, port, service_name, user, exc)) from exc


def oracle_tenant_driver(config):
    driver = str(config.get("oracle_driver") or os.environ.get("OB_ORACLE_TENANT_DRIVER") or "obclient").lower()
    if driver in ("pymysql", "mysql"):
        return "pymysql"
    if driver in ("oracledb", "oracle", "python-oracledb"):
        return "oracledb"
    return "obclient"


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


def format_oracle_connect_error(host, port, service_name, user, exc):
    raw = str(exc)
    return (
        f"Oracle租户连接失败：{host}:{port}/{service_name}，"
        f"当前用户 {user}；原始错误：{raw}"
    )


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
    empty_results = 0
    with conn.cursor() as cur:
        for sql in queries:
            try:
                ensure_readonly_select(sql)
                cur.execute(sql)
                rows = normalize_rows(cur.fetchall(), cur.description)
                if rows:
                    return rows
                empty_results += 1
            except Exception as exc:
                errors.append(str(exc))
    if empty_results and not errors:
        return []
    return [{"_error": "; ".join(errors[:3])}]


def obclient_query_first(config, queries):
    errors = []
    empty_results = 0
    for sql in queries:
        try:
            rows = obclient_query(config, sql)
            if rows:
                return rows
            empty_results += 1
        except Exception as exc:
            errors.append(str(exc))
    if empty_results and not errors:
        return []
    return [{"_error": "; ".join(errors[:3])}]


def obclient_query(config, sql):
    ensure_readonly_select(sql)
    host, port = split_endpoint(config.get("endpoint") or "", config.get("port") or 2881)
    user = config.get("tenant_user") or ""
    password = config.get("tenant_password") or ""
    if not user or not password:
        raise RuntimeError(f"Oracle 租户 {config.get('tenant_name') or ''} 未配置采集账号或密码")
    sql_text = sql.strip().rstrip(";") + ";"
    with tempfile.NamedTemporaryFile("w", delete=False) as defaults_file:
        defaults_file.write("[client]\n")
        defaults_file.write(f"password={password}\n")
        defaults_path = defaults_file.name
    try:
        cmd = [
            os.environ.get("OBCLIENT_BIN", "obclient"),
            f"--defaults-extra-file={defaults_path}",
            "-h",
            host,
            "-P",
            str(int(port)),
            "-u",
            user,
            "-A",
            "-B",
            "-e",
            sql_text,
        ]
        result = subprocess.run(cmd, text=True, capture_output=True, timeout=45)
    except FileNotFoundError as exc:
        raise RuntimeError("未找到 obclient，请先安装 OceanBase Client，并确认 obclient 在 PATH 中") from exc
    finally:
        try:
            os.unlink(defaults_path)
        except OSError:
            pass
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"obclient 执行失败，目标 {host}:{port}，用户 {user}；{detail[:800]}")
    return parse_tabular_output(result.stdout)


def parse_tabular_output(output):
    rows = []
    for line in output.splitlines():
        line = line.strip("\r\n")
        if not line:
            continue
        values = [normalize_obclient_value(value) for value in line.split("\t")]
        rows.append(values)
    return rows_to_dicts(rows)


def rows_to_dicts(rows):
    if not rows:
        return []
    columns = [str(column).lower() for column in rows[0]]
    return [
        {columns[index]: value for index, value in enumerate(row)}
        for row in rows[1:]
    ]


def normalize_obclient_value(value):
    if value in ("NULL", "\\N"):
        return None
    return normalize_value(value)


def ensure_readonly_select(sql):
    normalized = sql.strip().rstrip(";").strip()
    if ";" in normalized:
        raise RuntimeError("拒绝执行多语句 SQL，OB 采集仅允许单条 SELECT")
    if not normalized.lower().startswith(READONLY_ALLOWED_PREFIX):
        raise RuntimeError("拒绝执行非 SELECT SQL，OB 采集必须只读")
    if FORBIDDEN_SQL_PATTERN.search(normalized):
        raise RuntimeError("拒绝执行包含变更关键字的 SQL，OB 采集必须只读")


def normalize_rows(rows, description=None):
    normalized = []
    for row in rows:
        if hasattr(row, "items"):
            normalized.append({str(key).lower(): normalize_value(value) for key, value in row.items()})
            continue
        columns = [str(col[0]).lower() for col in (description or [])]
        normalized.append({columns[index]: normalize_value(value) for index, value in enumerate(row)})
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
           count(distinct unit_id) as unit_num,
           round(sum(max_cpu), 2) as cpu_cores,
           round(sum(memory_size) / 1024 / 1024 / 1024, 2) as memory_gb
    from oceanbase.GV$OB_UNITS
    group by tenant_id
    """,
    """
    select tenant_id,
           count(*) as unit_num,
           round(sum(max_cpu), 2) as cpu_cores,
           round(sum(memory_size) / 1024 / 1024 / 1024, 2) as memory_gb
    from oceanbase.GV$OB_UNITS
    group by tenant_id
    """,
    """
    select tenant_id,
           count(distinct unit_id) as unit_num,
           round(sum(cpu_capacity), 2) as cpu_cores,
           round(sum(memory_size) / 1024 / 1024 / 1024, 2) as memory_gb
    from oceanbase.GV$OB_UNITS
    group by tenant_id
    """,
    """
    select tenant_id,
           count(*) as unit_num,
           round(sum(cpu_capacity), 2) as cpu_cores,
           round(sum(memory_size) / 1024 / 1024 / 1024, 2) as memory_gb
    from oceanbase.GV$OB_UNITS
    group by tenant_id
    """,
]


TENANT_ZONE_RESOURCE_QUERIES = [
    """
    select tenant_id,
           zone,
           round(sum(max_cpu), 2) as cpu_cores,
           round(sum(memory_size) / 1024 / 1024 / 1024, 2) as memory_gb
    from oceanbase.GV$OB_UNITS
    group by tenant_id, zone
    order by tenant_id, zone
    """,
    """
    select tenant_id,
           zone,
           round(sum(cpu_capacity), 2) as cpu_cores,
           round(sum(memory_size) / 1024 / 1024 / 1024, 2) as memory_gb
    from oceanbase.GV$OB_UNITS
    group by tenant_id, zone
    order by tenant_id, zone
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


ORACLE_TENANT_TOP_OBJECT_QUERIES = [
    """
    select *
    from (
        select o.owner as database_name,
               o.object_name as table_name,
               o.object_type,
               round(max(t.data_size) / 1024 / 1024 / 1024, 4) as data_gb,
               cast(0 as number) as index_gb,
               round(max(t.data_size) / 1024 / 1024 / 1024, 4) as total_gb,
               max(dt.num_rows) as table_rows
        from SYS.DBA_OBJECTS o
        join SYS.DBA_OB_TABLE_LOCATIONS t
          on t.database_name = o.owner
         and t.table_name = o.object_name
        left join SYS.DBA_TABLES dt
          on dt.owner = o.owner
         and dt.table_name = o.object_name
        where o.owner not in ('SYS', 'SYSTEM', 'LBACSYS', 'ORAAUDITOR', 'OCEANBASE', 'PUBLIC')
          and o.object_type in ('TABLE', 'INDEX', 'LOB', 'LOBSEGMENT', 'TABLE PARTITION', 'INDEX PARTITION')
          and nvl(t.data_size, 0) > 0
        group by o.owner, o.object_name, o.object_type
        order by max(t.data_size) desc
    )
    where rownum <= 50
    """,
    """
    select *
    from (
        select s.owner as database_name,
               s.segment_name as table_name,
               s.segment_type as object_type,
               round(sum(s.bytes) / 1024 / 1024 / 1024, 4) as data_gb,
               cast(0 as number) as index_gb,
               round(sum(s.bytes) / 1024 / 1024 / 1024, 4) as total_gb,
               max(dt.num_rows) as table_rows
        from dba_segments s
        left join dba_tables dt
          on dt.owner = s.owner
         and dt.table_name = s.segment_name
        where s.owner not in ('SYS', 'SYSTEM', 'LBACSYS', 'ORAAUDITOR', 'OCEANBASE', 'PUBLIC')
        group by s.owner, s.segment_name, s.segment_type
        having sum(s.bytes) > 0
        order by sum(s.bytes) desc
    )
    where rownum <= 50
    """,
    """
    select *
    from (
        select user as database_name,
               s.segment_name as table_name,
               s.segment_type as object_type,
               round(sum(s.bytes) / 1024 / 1024 / 1024, 4) as data_gb,
               cast(0 as number) as index_gb,
               round(sum(s.bytes) / 1024 / 1024 / 1024, 4) as total_gb,
               max(ut.num_rows) as table_rows
        from user_segments s
        left join user_tables ut
          on ut.table_name = s.segment_name
        group by s.segment_name, s.segment_type
        order by sum(s.bytes) desc
    )
    where rownum <= 50
    """,
]


ORACLE_TENANT_RUNTIME_QUERIES = [
    """
    select
      (select count(*) from SYS.GV$OB_PROCESSLIST) as current_processes,
      (
        select case
                 when max(to_number(value)) > 0 then max(to_number(value))
                 else null
               end
        from SYS.V$OB_PARAMETERS
        where name = '_resource_limit_max_session_num'
          and scope = 'TENANT'
      ) as max_processes
    from dual
    """,
    """
    select count(*) as current_processes,
           cast(null as number) as max_processes
    from GV$OB_PROCESSLIST
    """,
    """
    select current_utilization as current_processes,
           limit_value as max_processes
    from v$resource_limit
    where resource_name = 'processes'
    """,
    """
    select count(*) as current_processes,
           cast(null as number) as max_processes
    from v$session
    """,
    """
    select cast(null as number) as current_processes,
           cast(null as number) as max_processes
    from dual
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
