import json
import logging
import os
import re
import shlex
import subprocess
import zipfile
from io import BytesIO
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from xml.sax.saxutils import escape

import oracledb
from flask import Flask, Response, g, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from ob_collector import collect_ob_cluster, collect_ob_tenant_detail, probe_ob_cluster, probe_ob_tenant_connection, check_ob_major_compaction
from cpu_history import run_cpu_sampling, cpu_history
from ocp_collector import (
    OcpClient,
    normalize_ocp_clusters,
    normalize_ocp_databases,
    normalize_ocp_observers,
    normalize_ocp_tenants,
)


BASE_DIR = Path(__file__).resolve().parent
CONFIG = {}


def load_config():
    config_path = Path(os.environ.get("APP_CONFIG", BASE_DIR / "config.json"))
    if not config_path.exists():
        return {}
    with config_path.open("r", encoding="utf-8") as config_file:
        return json.load(config_file)


CONFIG = load_config()
ORACLE_CONFIG = CONFIG.get("oracle", {})
APP_CONFIG = CONFIG.get("app", {})
POOL_CONFIG = ORACLE_CONFIG.get("pool", {})
LOG_DIR = Path(os.environ.get("APP_LOG_DIR", APP_CONFIG.get("log_dir", BASE_DIR / "logs")))
if not LOG_DIR.is_absolute():
    LOG_DIR = BASE_DIR / LOG_DIR
LOG_FILE = LOG_DIR / "ob-ops-monitor.log"


def config_value(config, key, env_name, default=""):
    value = config.get(key)
    if value not in (None, ""):
        return value
    return os.environ.get(env_name, default)


ORACLE_USER = config_value(ORACLE_CONFIG, "user", "ORACLE_USER", "")
ORACLE_PASSWORD = config_value(ORACLE_CONFIG, "password", "ORACLE_PASSWORD", "")
ORACLE_DSN = config_value(ORACLE_CONFIG, "dsn", "ORACLE_DSN", "")
DEFAULT_OCP_VERSION = os.environ.get(
    "DEFAULT_OCP_VERSION",
    APP_CONFIG.get("default_ocp_version", "4.3.5-20250610160438"),
)
DEFAULT_OB_VERSION = os.environ.get("DEFAULT_OB_VERSION", APP_CONFIG.get("default_ob_version", "4.2.1.8"))
POOL_MIN = int(os.environ.get("ORACLE_POOL_MIN", POOL_CONFIG.get("min", 1)))
POOL_MAX = int(os.environ.get("ORACLE_POOL_MAX", POOL_CONFIG.get("max", 5)))
POOL_INCREMENT = int(os.environ.get("ORACLE_POOL_INCREMENT", POOL_CONFIG.get("increment", 1)))
SYS_CHECK_INTERVAL_MINUTES = int(os.environ.get("SYS_CHECK_INTERVAL_MINUTES", APP_CONFIG.get("sys_check_interval_minutes", 60)))

pool = None
schema_initialized = False

XLSX_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>"""
XLSX_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""
XLSX_WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="租户信息" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
XLSX_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""


def setup_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("ob_ops_monitor")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


APP_LOGGER = setup_logging()


def sanitize_log_value(value):
    if isinstance(value, dict):
        return {
            key: "***" if any(secret in key.lower() for secret in ("password", "token", "secret")) else sanitize_log_value(val)
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_log_value(item) for item in value]
    return value


def log_event(level, event, **fields):
    exc_info = fields.pop("exc_info", False)
    APP_LOGGER.log(
        level,
        "%s %s",
        event,
        json.dumps(sanitize_log_value(fields), ensure_ascii=False, default=str),
        exc_info=exc_info,
    )


def generic_log_message(action):
    return f"{action}失败，详细原因请查看服务器日志 {LOG_FILE}"


def local_now():
    return datetime.now()


def create_app():
    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False

    @app.errorhandler(oracledb.DatabaseError)
    def database_error(exc):
        error = exc.args[0] if exc.args else exc
        message = getattr(error, "message", str(exc))
        log_event(
            logging.ERROR,
            "oracle_asset_db_error",
            method=request.method,
            path=request.path,
            oracle_user=ORACLE_USER,
            oracle_dsn=ORACLE_DSN,
            error=message,
            exc_info=True,
        )
        if request.path.startswith("/api/"):
            return jsonify({"error": "database unavailable", "message": generic_log_message("后台资产库访问")}), 503
        raise exc

    @app.errorhandler(Exception)
    def api_error(exc):
        if request.path.startswith("/api/"):
            if isinstance(exc, HTTPException):
                return jsonify({"error": exc.name, "message": exc.description}), exc.code
            log_event(
                logging.ERROR,
                "api_unhandled_error",
                method=request.method,
                path=request.path,
                error=str(exc),
                exc_info=True,
            )
            return jsonify({"error": "internal server error", "message": generic_log_message("接口处理")}), 500
        raise exc

    @app.before_request
    def open_db():
        if request.path.startswith("/api/"):
            g.db = get_pool().acquire()

    @app.teardown_request
    def close_db(_exc):
        db = getattr(g, "db", None)
        if db is not None:
            db.close()

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/cpu-history")
    def cpu_history_page():
        return render_template("cpu_history.html")

    @app.route("/api/cpu-history")
    def cpu_history_api():
        try:
            days = int(request.args.get("days", "7"))
            ids = list(dict.fromkeys(int(x) for x in request.args.get("tenants", "").split(",") if x.strip()))
        except ValueError:
            return jsonify({"error":"invalid tenant IDs or days"}),400
        if days not in (7,14,30,60) or len(ids)>100 or any(x==0 for x in ids):
            return jsonify({"error":"days must be 7,14,30,60; maximum 100 tenants"}),400
        return jsonify(cpu_history(g.db, all_rows, ids, days))

    @app.route("/api/cpu-history/tenants")
    def cpu_history_tenants():
        return jsonify(all_rows(g.db, """select q.* from (
                 select t.id,t.cluster_id,t.name,t.active,c.name as cluster_name
                 from tenants t join clusters c on c.id=t.cluster_id
                 where t.active=1 and lower(t.name) <> 'sys'
                 union all
                 select -c.id as id,c.id as cluster_id,'sys' as name,1 as active,c.name as cluster_name
                 from clusters c
               ) q order by q.active desc,q.cluster_name,case when q.id<0 then 0 else 1 end,q.name"""))

    @app.route("/api/cpu-history/status")
    def cpu_sampling_status():
        return jsonify(all_rows(g.db, """
          select c.id,c.name as cluster_name,j.status,j.message,j.checked_at,
                 x.last_sample_at,x.sample_count
          from clusters c
          left join cpu_collection_status j on j.cluster_id=c.id
          left join (
            select cluster_id,max(sampled_at) last_sample_at,count(*) sample_count from (
              select t.cluster_id,s.sampled_at from tenant_cpu_samples s join tenants t on t.id=s.tenant_id
              union all
              select cluster_id,sampled_at from cluster_sys_cpu_samples
            ) group by cluster_id
          ) x on x.cluster_id=c.id
          order by c.name
        """))

    @app.route("/api/health")
    def health():
        return jsonify(
            {
                "ok": True,
                "oracle_user": ORACLE_USER,
                "oracle_dsn": ORACLE_DSN,
                "server_time": local_now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

    @app.route("/api/summary")
    def summary():
        db = g.db
        tenant_risks = tenant_risk_summary(db)
        return jsonify(
            {
                "clusters": scalar(db, "select count(*) from clusters"),
                "tenants": scalar(db, "select count(*) from tenants where active=1"),
                "databases": scalar(db, "select count(*) from databases"),
                "servers": scalar(db, "select count(*) from servers"),
                "observers": scalar(db, "select count(*) from ob_servers"),
                "log_errors": scalar(
                    db,
                    """
                    select count(*) from ob_log_events
                    where severity in ('ERROR', 'FATAL')
                      and not regexp_like(message, '(^|[^[:alnum:]_])(INFO|TRACE|DEBUG|WDIAG|TDIAG|EDIAG|DIAG)([^[:alnum:]_]|$)', 'i')
                    """,
                ),
                "ocp_connections": safe_scalar(db, "select count(*) from ocp_connections"),
                **tenant_risks,
                "sys_unhealthy": safe_scalar(
                    db,
                    """
                    select count(*)
                    from (
                        select cluster_id, status,
                               row_number() over (partition by cluster_id order by checked_at desc, id desc) as rn
                        from sys_tenant_checks
                    )
                    where rn = 1 and status <> 'success'
                    """,
                ),
            }
        )

    @app.route("/api/clusters", methods=["GET", "POST"])
    def clusters():
        if request.method == "POST":
            payload = request.get_json(force=True)
            missing = [key for key in ["name", "endpoint", "sys_user"] if not payload.get(key)]
            if missing:
                return jsonify({"error": "missing fields", "fields": missing}), 400
            existing = one(g.db, "select id from clusters where name = :name", {"name": payload["name"]})
            if existing:
                return jsonify({"error": "cluster exists", "message": "集群名称已存在，请使用新名称，避免覆盖老集群"}), 409
            new_id = upsert_cluster(g.db, payload)
            g.db.commit()
            return jsonify({"id": new_id}), 201

        return jsonify(
            all_rows(
                g.db,
                """
                select c.id, c.name, c.environment, c.region, c.endpoint, c.port,
                       c.sys_user,
                       case when c.sys_password is null then 0 else 1 end as has_password,
                       c.version, c.status, c.owner, c.remark,
                       nvl(s.enabled, 0) as schedule_enabled,
                       s.run_time as schedule_run_time,
                       s.last_run_at as schedule_last_run_at,
                       c.created_at, c.updated_at,
                       count(distinct t.id) as tenant_count,
                       count(distinct o.id) as observer_count
                from clusters c
                left join tenants t on t.cluster_id = c.id and t.active = 1
                left join ob_servers o on o.cluster_id = c.id
                left join cluster_collect_schedules s on s.cluster_id = c.id
                group by c.id, c.name, c.environment, c.region, c.endpoint, c.port,
                         c.sys_user, c.sys_password, c.version, c.status, c.owner, c.remark,
                         s.enabled, s.run_time, s.last_run_at,
                         c.created_at, c.updated_at
                order by c.environment, c.name
                """,
            )
        )

    @app.route("/api/clusters/<int:cluster_id>", methods=["GET", "DELETE"])
    def cluster_detail(cluster_id):
        cluster = one(
            g.db,
            """
            select id, name, environment, region, endpoint, port, sys_user,
                   case when sys_password is null then 0 else 1 end as has_password,
                   version, status, owner, remark, created_at, updated_at
            from clusters
            where id = :id
            """,
            {"id": cluster_id},
        )
        if not cluster:
            return jsonify({"error": "cluster not found"}), 404
        if request.method == "DELETE":
            delete_cluster_local(g.db, cluster_id)
            g.db.commit()
            return jsonify({"deleted": True, "id": cluster_id})
        return jsonify(
            {
                "cluster": cluster,
                "tenants": all_rows(
                    g.db,
                    """
                    select id, name, tenant_mode, primary_zone, locality, tenant_role,
                           zone_resource_summary,
                           unit_num, cpu_cores, memory_gb, last_full_backup_time,
                           data_disk_used_gb, data_disk_total_gb, data_disk_usage_pct,
                           log_disk_used_gb, log_disk_total_gb, log_disk_usage_pct,
                           last_success_merge_time, last_merge_status,
                           status, created_at, updated_at
                    from tenants
                    where cluster_id = :id and active = 1
                    order by name
                    """,
                    {"id": cluster_id},
                ),
                "observers": all_rows(
                    g.db,
                    """
                    select id, zone, svr_ip, sql_port, rpc_port, status,
                           disk_total_gb, disk_used_gb, created_at, updated_at
                    from ob_servers
                    where cluster_id = :id
                    order by zone, svr_ip
                    """,
                    {"id": cluster_id},
                ),
                "parameters": all_rows(
                    g.db,
                    """
                    select p.id, t.name as tenant_name, p.name, p.param_value,
                           p.section, p.scope, p.updated_at
                    from ob_parameters p
                    left join tenants t on t.id = p.tenant_id
                    where p.cluster_id = :id
                    order by t.name, p.name
                    fetch first 200 rows only
                    """,
                    {"id": cluster_id},
                ),
                "logs": all_rows(
                    g.db,
                    """
                    select id, event_time, severity, server_ip, error_code, component, message
                    from ob_log_events
                    where cluster_id = :id
                    order by event_time desc, id desc
                    fetch first 50 rows only
                    """,
                    {"id": cluster_id},
                ),
            }
        )

    @app.route("/api/tenants")
    def tenants():
        return jsonify(list_tenants(g.db))

    @app.route("/api/tenants/export.xlsx")
    def export_tenants():
        rows = filter_tenant_export_rows(
            list_tenants(g.db),
            request.args.get("cluster_id", ""),
            request.args.get("hide_standby", "1") == "1",
            request.args.get("hide_meta", "1") == "1",
        )
        risk = request.args.get('risk', '')
        if risk in ('backup_overdue','merge_missing','collect_abnormal'):
            rows = [row for row in rows if row.get(risk)]
        workbook = build_tenants_xlsx(rows)
        filename = f"ob-tenants-{local_now().strftime('%Y%m%d-%H%M%S')}.xlsx"
        return Response(
            workbook,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @app.route("/api/tenants/<int:tenant_id>")
    def tenant_detail(tenant_id):
        tenant = get_tenant_with_cluster(g.db, tenant_id)
        if not tenant:
            return jsonify({"error": "tenant not found"}), 404
        connection = one(
            g.db,
            """
            select id, tenant_user, database_name,
                   case when tenant_password is null then 0 else 1 end as has_password,
                   updated_at
            from tenant_connections
            where tenant_id = :tenant_id
            """,
            {"tenant_id": tenant_id},
        )
        runtime = one(
            g.db,
            """
            select current_processes, max_processes, collected_at
            from tenant_runtime_metrics
            where tenant_id = :tenant_id
            order by collected_at desc
            fetch first 1 rows only
            """,
            {"tenant_id": tenant_id},
        )
        return jsonify(
            {
                "tenant": tenant,
                "connection": connection or {},
                "runtime": runtime or {},
                "runtime_history": all_rows(
                    g.db,
                    """
                    select current_processes, max_processes, collected_at
                    from tenant_runtime_metrics
                    where tenant_id = :tenant_id
                    order by collected_at desc
                    fetch first 10 rows only
                    """,
                    {"tenant_id": tenant_id},
                ),
                "top_objects": all_rows(
                    g.db,
                    """
                    select database_name, object_name, object_type, data_gb, index_gb,
                           total_gb, table_rows, collected_at
                    from tenant_top_objects
                    where tenant_id = :tenant_id
                      and collected_at = (
                        select max(collected_at)
                        from tenant_top_objects
                        where tenant_id = :tenant_id
                      )
                    order by total_gb desc, object_name
                    fetch first 10 rows only
                    """,
                    {"tenant_id": tenant_id},
                ),
                "schedule": one(
                    g.db,
                    """
                    select id, enabled, frequency, run_time, day_of_week, day_of_month, last_run_at, updated_at
                    from tenant_collect_schedules
                    where tenant_id = :tenant_id
                    """,
                    {"tenant_id": tenant_id},
                )
                or {},
                "errors": all_rows(
                    g.db,
                    """
                    select id, event_time, severity, server_ip, error_code, component, message
                    from ob_log_events
                    where cluster_id = :cluster_id
                      and event_time >= systimestamp - interval '1' day
                      and lower(message) like :tenant_pattern
                    order by event_time desc, id desc
                    fetch first 50 rows only
                    """,
                    {"cluster_id": tenant["cluster_id"], "tenant_pattern": f"%{tenant['name'].lower()}%"},
                ),
            }
        )

    @app.route("/api/tenants/<int:tenant_id>/connection", methods=["POST"])
    def save_tenant_connection(tenant_id):
        tenant = get_tenant_with_cluster(g.db, tenant_id)
        if not tenant:
            return jsonify({"error": "tenant not found"}), 404
        payload = request.get_json(force=True)
        if not payload.get("tenant_user"):
            return jsonify({"error": "tenant_user is required"}), 400
        payload["tenant_user"] = normalize_base_tenant_user(payload.get("tenant_user"))
        upsert_tenant_connection(g.db, tenant_id, payload)
        g.db.commit()
        return jsonify({"saved": True, "tenant_id": tenant_id})

    @app.route("/api/tenants/<int:tenant_id>/connection/test", methods=["POST"])
    def test_tenant_connection(tenant_id):
        tenant = get_tenant_with_cluster(g.db, tenant_id)
        if not tenant:
            return jsonify({"error": "tenant not found"}), 404
        payload = request.get_json(force=True) if request.data else {}
        connection = one(g.db, "select * from tenant_connections where tenant_id = :tenant_id", {"tenant_id": tenant_id}) or {}
        config = {
            "endpoint": tenant["endpoint"],
            "port": tenant["port"],
            "tenant_name": tenant["name"],
            "cluster_name": tenant["cluster_name"],
            "tenant_mode": tenant["tenant_mode"],
            "tenant_user": build_tenant_collect_user(payload.get("tenant_user") or connection.get("tenant_user"), tenant),
            "tenant_password": payload.get("tenant_password") or connection.get("tenant_password"),
            "database": build_tenant_service_name(payload.get("database_name") or connection.get("database_name"), tenant),
        }
        started = local_now()
        try:
            result = probe_ob_tenant_connection(config)
        except Exception as exc:
            message = str(exc)[:1000]
            log_event(
                logging.ERROR,
                "tenant_connection_test_failed",
                tenant_id=tenant_id,
                tenant_name=tenant["name"],
                target=f"{tenant['endpoint']}:{tenant['port']}",
                user=config.get("tenant_user"),
                service_name=config.get("database"),
                error=message,
                exc_info=True,
            )
            record_collection_job(g.db, tenant["cluster_id"], "tenant_probe", "failed", message, started)
            g.db.commit()
            return jsonify({"error": "tenant test failed", "message": message}), 400
        record_collection_job(g.db, tenant["cluster_id"], "tenant_probe", "success", result.get("message", ""), started)
        g.db.commit()
        return jsonify(result)

    @app.route("/api/tenants/<int:tenant_id>/schedule", methods=["POST"])
    def save_tenant_schedule(tenant_id):
        tenant = get_tenant_with_cluster(g.db, tenant_id)
        if not tenant:
            return jsonify({"error": "tenant not found"}), 404
        payload = request.get_json(force=True)
        upsert_tenant_schedule(g.db, tenant_id, payload)
        g.db.commit()
        return jsonify({"saved": True, "tenant_id": tenant_id})

    @app.route("/api/tenants/<int:tenant_id>/objects/history")
    def tenant_object_history(tenant_id):
        database_name = request.args.get("database_name", "")
        object_name = request.args.get("object_name", "")
        object_type = request.args.get("object_type", "")
        if not object_name:
            return jsonify({"error": "object_name is required"}), 400
        return jsonify(
            all_rows(
                g.db,
                """
                select database_name, object_name, object_type, data_gb, index_gb,
                       total_gb, table_rows, collected_at
                from tenant_top_objects
                where tenant_id = :tenant_id
                  and database_name = :database_name
                  and object_name = :object_name
                  and object_type = :object_type
                order by collected_at desc
                fetch first 10 rows only
                """,
                {
                    "tenant_id": tenant_id,
                    "database_name": database_name,
                    "object_name": object_name,
                    "object_type": object_type,
                },
            )
        )

    @app.route("/api/tenants/<int:tenant_id>/runtime/history")
    def tenant_runtime_history(tenant_id):
        return jsonify(
            all_rows(
                g.db,
                """
                select current_processes, max_processes, collected_at
                from tenant_runtime_metrics
                where tenant_id = :tenant_id
                order by collected_at desc
                fetch first 10 rows only
                """,
                {"tenant_id": tenant_id},
            )
        )

    @app.route("/api/tenants/<int:tenant_id>/collect-detail", methods=["POST"])
    def collect_tenant_detail(tenant_id):
        tenant = get_tenant_with_cluster(g.db, tenant_id)
        if not tenant:
            return jsonify({"error": "tenant not found"}), 404
        if tenant.get('active') != 1:
            return jsonify({'error':'tenant inactive','message':'租户已停采，请先核验资产状态'}),409
        connection = one(g.db, "select * from tenant_connections where tenant_id = :tenant_id", {"tenant_id": tenant_id})
        if not connection or not connection.get("tenant_password"):
            return jsonify({"error": "tenant connection missing", "message": "请先保存租户采集账号和密码"}), 400
        started = local_now()
        try:
            collected = collect_ob_tenant_detail(
                {
                    "endpoint": tenant["endpoint"],
                    "port": tenant["port"],
                    "tenant_name": tenant["name"],
                    "cluster_name": tenant["cluster_name"],
                    "tenant_mode": tenant["tenant_mode"],
                    "tenant_user": build_tenant_collect_user(connection["tenant_user"], tenant),
                    "tenant_password": connection["tenant_password"],
                    "database": build_tenant_service_name(connection.get("database_name"), tenant),
                }
            )
        except Exception as exc:
            log_event(
                logging.ERROR,
                "tenant_detail_collect_failed",
                tenant_id=tenant_id,
                tenant_name=tenant["name"],
                target=f"{tenant['endpoint']}:{tenant['port']}",
                user=build_tenant_collect_user(connection["tenant_user"], tenant),
                service_name=connection.get("database_name") or tenant["name"],
                error=str(exc)[:1000],
                exc_info=True,
            )
            message = str(exc)[:1000]
            record_collection_job(g.db, tenant["cluster_id"], "tenant_detail", "failed", message, started)
            g.db.commit()
            return jsonify({"error": "tenant collect failed", "message": message}), 502
        stats = store_tenant_detail(g.db, tenant_id, collected)
        status = "warning" if stats.get("warnings") else "success"
        record_collection_job(g.db, tenant["cluster_id"], "tenant_detail", status, json.dumps(stats, ensure_ascii=False), started)
        g.db.commit()
        return jsonify(stats)

    @app.route("/api/servers", methods=["GET", "POST"])
    def servers():
        if request.method == "POST":
            payload = request.get_json(force=True)
            missing = [key for key in ["hostname", "ip"] if not payload.get(key)]
            if missing:
                return jsonify({"error": "missing fields", "fields": missing}), 400
            new_id = insert_returning_id(
                g.db,
                """
                insert into servers
                (hostname, ip, idc, rack, os_version, cpu_cores, memory_gb,
                 disk_gb, ssh_port, owner, status, created_at, updated_at)
                values (:hostname, :ip, :idc, :rack, :os_version, :cpu_cores,
                        :memory_gb, :disk_gb, :ssh_port, :owner, :status,
                        systimestamp, systimestamp)
                returning id into :id
                """,
                {
                    "hostname": payload["hostname"],
                    "ip": payload["ip"],
                    "idc": payload.get("idc", ""),
                    "rack": payload.get("rack", ""),
                    "os_version": payload.get("os_version", "RHEL 7.9"),
                    "cpu_cores": int(payload.get("cpu_cores") or 0),
                    "memory_gb": int(payload.get("memory_gb") or 0),
                    "disk_gb": int(payload.get("disk_gb") or 0),
                    "ssh_port": int(payload.get("ssh_port") or 22),
                    "owner": payload.get("owner", ""),
                    "status": payload.get("status", "unknown"),
                },
            )
            g.db.commit()
            return jsonify({"id": new_id}), 201
        return jsonify(all_rows(g.db, "select * from servers order by hostname"))

    @app.route("/api/logs", methods=["GET", "POST"])
    def logs():
        if request.method == "POST":
            payload = request.get_json(force=True)
            if not payload.get("raw_log"):
                return jsonify({"error": "raw_log is required"}), 400
            events = parse_ob_log(
                payload["raw_log"],
                payload.get("cluster_id"),
                payload.get("server_ip", ""),
                payload.get("log_path", ""),
            )
            inserted = insert_ob_log_events(g.db, events)
            g.db.commit()
            return jsonify({"inserted": inserted, "parsed": len(events)}), 201

        return jsonify(
            all_rows(
                g.db,
                """
                select e.id, e.cluster_id, c.name as cluster_name, e.server_ip, e.log_path,
                       e.event_time, e.severity, e.error_code, e.component,
                       e.message, e.created_at
                from ob_log_events e
                left join clusters c on c.id = e.cluster_id
                where not regexp_like(e.message, '(^|[^[:alnum:]_])(INFO|TRACE|DEBUG|WDIAG|TDIAG|EDIAG|DIAG)([^[:alnum:]_]|$)', 'i')
                order by e.event_time desc, e.id desc
                fetch first 100 rows only
                """,
            )
        )

    @app.route("/api/log-alerts")
    def log_alerts():
        return jsonify(
            all_rows(
                g.db,
                """
                select e.id, e.cluster_id, c.name as cluster_name, e.server_ip, e.log_path,
                       e.event_time, e.severity, e.error_code, e.component,
                       e.message, e.created_at
                from ob_log_events e
                left join clusters c on c.id = e.cluster_id
                where e.severity in ('ERROR', 'FATAL', 'WARN')
                  and not regexp_like(e.message, '(^|[^[:alnum:]_])(INFO|TRACE|DEBUG|WDIAG|TDIAG|EDIAG|DIAG)([^[:alnum:]_]|$)', 'i')
                order by e.event_time desc, e.id desc
                fetch first 10 rows only
                """,
            )
        )

    @app.route("/api/observer-log-sources", methods=["GET", "POST"])
    def observer_log_sources():
        if request.method == "POST":
            payload = request.get_json(force=True)
            missing = [key for key in ("cluster_id", "observer_ip", "ssh_user", "log_path") if not payload.get(key)]
            if missing:
                return jsonify({"error": "missing fields", "fields": missing}), 400
            source_id = upsert_observer_log_source(g.db, payload)
            g.db.commit()
            return jsonify({"id": source_id}), 201
        return jsonify(list_observer_log_sources(g.db))

    @app.route("/api/observer-log-sources/<int:source_id>/collect", methods=["POST"])
    def collect_observer_log_source_now(source_id):
        source = get_observer_log_source(g.db, source_id)
        if not source:
            return jsonify({"error": "observer log source not found"}), 404
        result = collect_observer_log_source(g.db, source)
        g.db.commit()
        return jsonify(result)

    @app.route("/api/observer-log-sources/collect-all", methods=["POST"])
    def collect_observer_log_sources_now():
        result = collect_due_observer_log_sources(g.db, force=True)
        g.db.commit()
        return jsonify(result)

    @app.route("/api/collection-jobs")
    def collection_jobs():
        return jsonify(
            all_rows(
                g.db,
                """
                select j.id, j.cluster_id, c.name as cluster_name, j.target_type,
                       j.status, j.message, j.started_at, j.finished_at
                from collection_jobs j
                left join clusters c on c.id = j.cluster_id
                order by j.started_at desc, j.id desc
                fetch first 10 rows only
                """,
            )
        )

    @app.route("/api/sys-tenant-checks")
    def sys_tenant_checks():
        return jsonify(latest_sys_tenant_checks(g.db))

    @app.route("/api/sys-tenant-checks/run", methods=["POST"])
    def run_sys_tenant_checks_now():
        stats = run_sys_tenant_checks(g.db, force=True)
        g.db.commit()
        return jsonify(stats)

    @app.route("/api/ocp/connections", methods=["GET", "POST"])
    def ocp_connections():
        if request.method == "POST":
            payload = request.get_json(force=True)
            missing = [key for key in ["name", "base_url"] if not payload.get(key)]
            if missing:
                return jsonify({"error": "missing fields", "fields": missing}), 400
            new_id = upsert_ocp_connection(g.db, payload)
            g.db.commit()
            return jsonify({"id": new_id}), 201

        return jsonify(
            all_rows(
                g.db,
                """
                select id, name, base_url, ocp_version, auth_type, username, verify_ssl,
                       api_prefix, status, last_sync_at, created_at, updated_at
                from ocp_connections
                order by id desc
                """,
            )
        )

    @app.route("/api/ocp/connections/<int:connection_id>/test", methods=["POST"])
    def ocp_test(connection_id):
        config = get_ocp_config(g.db, connection_id)
        if not config:
            return jsonify({"error": "OCP connection not found"}), 404
        result = OcpClient(config).test()
        status = "online" if result["ok"] else "failed"
        execute(
            g.db,
            "update ocp_connections set status = :status, updated_at = systimestamp where id = :id",
            {"status": status, "id": connection_id},
        )
        g.db.commit()
        return jsonify(result)

    @app.route("/api/ocp/connections/<int:connection_id>", methods=["DELETE"])
    def delete_ocp_connection(connection_id):
        execute(g.db, "delete from ocp_sync_runs where connection_id = :id", {"id": connection_id})
        execute(g.db, "delete from ocp_connections where id = :id", {"id": connection_id})
        g.db.commit()
        return jsonify({"deleted": True, "id": connection_id})

    @app.route("/api/clusters/<int:cluster_id>/schedule", methods=["POST"])
    def save_cluster_schedule(cluster_id):
        cluster = one(g.db, "select id from clusters where id = :id", {"id": cluster_id})
        if not cluster:
            return jsonify({"error": "cluster not found"}), 404
        payload = request.get_json(force=True)
        upsert_cluster_schedule(g.db, cluster_id, payload)
        g.db.commit()
        return jsonify({"saved": True, "cluster_id": cluster_id})

    @app.route("/api/ocp/connections/<int:connection_id>/sync", methods=["POST"])
    def ocp_sync(connection_id):
        config = get_ocp_config(g.db, connection_id)
        if not config:
            return jsonify({"error": "OCP connection not found"}), 404
        payload = request.get_json(force=True) if request.data else {}
        confirm_duplicate_databases = bool(payload.get("confirm_duplicate_databases"))
        started = local_now()
        client = OcpClient(config)
        raw_clusters = client.fetch_clusters()
        clusters = normalize_ocp_clusters(raw_clusters)
        stats = {
            "clusters": 0,
            "observers": 0,
            "tenants": 0,
            "databases": 0,
            "duplicate_databases": 0,
            "duplicate_database_examples": [],
            "duplicate_confirmation_required": False,
            "warnings": [],
        }
        for cluster in clusters:
            cluster_id = upsert_cluster(g.db, cluster)
            stats["clusters"] += 1
            assets = client.fetch_cluster_assets(cluster)
            for key, path_info in assets["paths"].items():
                if isinstance(path_info, str) and ":" in path_info and not path_info.startswith("ob/"):
                    stats["warnings"].append(f"{cluster['name']} {key}: {path_info[:300]}")
            for observer in normalize_ocp_observers(assets["observers"], cluster_id):
                upsert_observer(g.db, observer)
                upsert_server_from_observer(g.db, observer)
                stats["observers"] += 1
            tenant_id_by_name = {}
            for tenant in normalize_ocp_tenants(assets["tenants"], cluster_id):
                tenant_id = upsert_tenant(g.db, tenant)
                tenant_id_by_name[tenant["name"]] = tenant_id
                stats["tenants"] += 1
            for database in normalize_ocp_databases(assets["databases"]):
                tenant_id = database.get("tenant_id")
                if not tenant_id and database.get("tenant_name"):
                    tenant_id = tenant_id_by_name.get(database["tenant_name"])
                if tenant_id:
                    database["tenant_id"] = tenant_id
                    existing_database = find_database(g.db, tenant_id, database["name"])
                    if existing_database and not confirm_duplicate_databases:
                        stats["duplicate_databases"] += 1
                        stats["duplicate_confirmation_required"] = True
                        if len(stats["duplicate_database_examples"]) < 20:
                            stats["duplicate_database_examples"].append(
                                {
                                    "cluster": cluster["name"],
                                    "tenant": database.get("tenant_name") or "",
                                    "database": database["name"],
                                }
                            )
                        continue
                    upsert_database(g.db, database)
                    stats["databases"] += 1
        run_status = "warning" if stats["duplicate_confirmation_required"] else "success"
        run_id = insert_returning_id(
            g.db,
            """
            insert into ocp_sync_runs
            (connection_id, status, cluster_count, message, started_at, finished_at)
            values (:connection_id, :status, :cluster_count, :message,
                    to_timestamp(:started_at, 'YYYY-MM-DD HH24:MI:SS'), systimestamp)
            returning id into :id
            """,
            {
                "connection_id": connection_id,
                "status": run_status,
                "cluster_count": len(clusters),
                "message": json.dumps(stats, ensure_ascii=False)[:1000],
                "started_at": started.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        execute(
            g.db,
            """
            update ocp_connections
            set status = 'online', last_sync_at = systimestamp, updated_at = systimestamp
            where id = :id
            """,
            {"id": connection_id},
        )
        g.db.commit()
        return jsonify({"id": run_id, **stats, "raw_cluster_count": len(raw_clusters)})

    @app.route("/api/clusters/<int:cluster_id>/collect", methods=["POST"])
    def collect_cluster(cluster_id):
        cluster = one(
            g.db,
            """
            select id, name, endpoint, port, sys_user, sys_password
            from clusters
            where id = :id
            """,
            {"id": cluster_id},
        )
        if not cluster:
            return jsonify({"error": "cluster not found"}), 404
        result = collect_one_cluster(g.db, cluster)
        g.db.commit()
        if result["status"] == "failed":
            return jsonify({"error": "collect failed", "message": result["message"]}), 400
        return jsonify(result["stats"])

    @app.route("/api/clusters/collect-all", methods=["POST"])
    def collect_all_clusters():
        clusters = all_rows(
            g.db,
            """
            select id, name, endpoint, port, sys_user, sys_password
            from clusters
            order by environment, name
            """,
        )
        summary = {
            "total": len(clusters),
            "success": 0,
            "warning": 0,
            "failed": 0,
            "skipped": 0,
            "results": [],
        }
        for cluster in clusters:
            if not cluster.get("sys_password"):
                started = local_now()
                message = "未配置系统用户密码，已跳过"
                record_collection_job(g.db, cluster["id"], "ob_cluster", "skipped", message, started)
                summary["skipped"] += 1
                summary["results"].append({"id": cluster["id"], "name": cluster["name"], "status": "skipped", "message": message})
                continue
            result = collect_one_cluster(g.db, cluster)
            summary[result["status"]] = summary.get(result["status"], 0) + 1
            summary["results"].append(
                {
                    "id": cluster["id"],
                    "name": cluster["name"],
                    "status": result["status"],
                    "message": result["message"],
                    "stats": result.get("stats", {}),
                }
            )
        g.db.commit()
        return jsonify(summary)

    @app.route("/api/clusters/<int:cluster_id>/probe", methods=["POST"])
    def probe_cluster(cluster_id):
        cluster = one(
            g.db,
            """
            select id, name, endpoint, port, sys_user, sys_password
            from clusters
            where id = :id
            """,
            {"id": cluster_id},
        )
        if not cluster:
            return jsonify({"error": "cluster not found"}), 404
        started = local_now()
        try:
            result = probe_ob_cluster(cluster)
        except RuntimeError as exc:
            log_event(
                logging.ERROR,
                "ob_probe_failed",
                cluster_id=cluster_id,
                cluster_name=cluster["name"],
                target=f"{cluster['endpoint']}:{cluster['port']}",
                user=cluster["sys_user"],
                error=str(exc),
            )
            message = generic_log_message("OB 连接测试")
            record_collection_job(g.db, cluster_id, "ob_probe", "failed", message, started)
            g.db.commit()
            return jsonify({"error": "probe failed", "message": message}), 400
        except Exception as exc:
            log_event(
                logging.ERROR,
                "ob_probe_unhandled_error",
                cluster_id=cluster_id,
                cluster_name=cluster["name"],
                target=f"{cluster['endpoint']}:{cluster['port']}",
                user=cluster["sys_user"],
                error=str(exc),
                exc_info=True,
            )
            message = generic_log_message("OB 连接测试")
            record_collection_job(g.db, cluster_id, "ob_probe", "failed", message, started)
            g.db.commit()
            return jsonify({"error": "probe failed", "message": message}), 502
        record_collection_job(g.db, cluster_id, "ob_probe", "success", result["message"], started)
        g.db.commit()
        return jsonify(result)

    @app.route("/api/clusters/<int:cluster_id>/collect-config", methods=["POST"])
    def update_cluster_collect_config(cluster_id):
        payload = request.get_json(force=True)
        cluster = one(g.db, "select id from clusters where id = :id", {"id": cluster_id})
        if not cluster:
            return jsonify({"error": "cluster not found"}), 404
        execute(
            g.db,
            """
            update clusters
            set endpoint = :endpoint,
                port = :port,
                sys_user = :sys_user,
                sys_password = case when :sys_password is null or :sys_password = ''
                                    then sys_password else :sys_password end,
                updated_at = systimestamp
            where id = :id
            """,
            {
                "id": cluster_id,
                "endpoint": payload.get("endpoint", ""),
                "port": int(payload.get("port") or 2881),
                "sys_user": payload.get("sys_user", "root@sys"),
                "sys_password": payload.get("sys_password", ""),
            },
        )
        g.db.commit()
        return jsonify({"id": cluster_id})

    @app.route("/api/collect", methods=["POST"])
    def collect():
        new_id = insert_returning_id(
            g.db,
            """
            insert into collection_jobs
            (target_type, status, message, started_at, finished_at)
            values ('all', 'success', 'Collection entry is ready. Use OCP sync for real data.',
                    systimestamp, systimestamp)
            returning id into :id
            """,
            {},
        )
        g.db.commit()
        return jsonify({"id": new_id, "status": "success"}), 201

    return app


def get_pool():
    global pool
    if pool is None:
        pool = oracledb.create_pool(
            user=ORACLE_USER,
            password=ORACLE_PASSWORD,
            dsn=ORACLE_DSN,
            min=POOL_MIN,
            max=POOL_MAX,
            increment=POOL_INCREMENT,
        )
    return pool


def execute(db, sql, params=None):
    with db.cursor() as cur:
        cur.execute(sql, bind_params(sql, params))


def scalar(db, sql, params=None):
    with db.cursor() as cur:
        cur.execute(sql, bind_params(sql, params))
        return cur.fetchone()[0]


def safe_scalar(db, sql, params=None):
    try:
        return scalar(db, sql, params)
    except oracledb.DatabaseError:
        return 0


def all_rows(db, sql, params=None):
    with db.cursor() as cur:
        cur.execute(sql, bind_params(sql, params))
        columns = [col[0].lower() for col in cur.description]
        return [normalize_row(dict(zip(columns, row))) for row in cur.fetchall()]


def one(db, sql, params=None):
    rows = all_rows(db, sql, params)
    return rows[0] if rows else None


def normalize_row(row):
    normalized = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            normalized[key] = value.strftime("%Y-%m-%d %H:%M:%S")
        elif hasattr(value, "read"):
            normalized[key] = value.read()
        else:
            normalized[key] = value
    return normalized


def insert_returning_id(db, sql, params):
    with db.cursor() as cur:
        out_id = cur.var(oracledb.NUMBER)
        cur.execute(sql, bind_params(sql, {**params, "id": out_id}))
        value = out_id.getvalue()
        if isinstance(value, list):
            value = value[0]
        return int(value)


def bind_params(sql, params=None):
    if not params:
        return {}
    placeholders = set(re.findall(r":([A-Za-z_][A-Za-z0-9_]*)", sql))
    return {key: value for key, value in params.items() if key in placeholders}


def upsert_cluster(db, payload):
    existing = one(db, "select id from clusters where name = :name", {"name": payload["name"]})
    data = {
        "name": payload["name"],
        "environment": payload.get("environment", "prod"),
        "region": payload.get("region", ""),
        "endpoint": payload.get("endpoint", ""),
        "port": int(payload.get("port") or 2881),
        "sys_user": payload.get("sys_user", "root@sys"),
        "sys_password": payload.get("sys_password", ""),
        "version": payload.get("version", DEFAULT_OB_VERSION),
        "status": payload.get("status", "unknown"),
        "owner": payload.get("owner", "DBA"),
        "remark": payload.get("remark", ""),
    }
    if existing:
        execute(
            db,
            """
            update clusters
            set environment = :environment, region = :region, endpoint = :endpoint,
                port = :port, sys_user = :sys_user, version = :version,
                sys_password = case when :sys_password is null or :sys_password = ''
                                    then sys_password else :sys_password end,
                status = :status, owner = :owner, remark = :remark,
                updated_at = systimestamp
            where name = :name
            """,
            data,
        )
        return existing["id"]
    return insert_returning_id(
        db,
        """
        insert into clusters
        (name, environment, region, endpoint, port, sys_user, version,
         sys_password, status, owner, remark, created_at, updated_at)
        values (:name, :environment, :region, :endpoint, :port, :sys_user,
                :version, :sys_password, :status, :owner, :remark, systimestamp, systimestamp)
        returning id into :id
        """,
        data,
    )


def delete_cluster_local(db, cluster_id):
    execute(db, "delete from cluster_sys_cpu_samples where cluster_id=:id", {"id":cluster_id})
    tenant_ids = [row["id"] for row in all_rows(db, "select id from tenants where cluster_id = :id", {"id": cluster_id})]
    for tenant_id in tenant_ids:
        delete_tenant_local(db, tenant_id)
    execute(db, "delete from ob_parameters where cluster_id = :id", {"id": cluster_id})
    execute(db, "delete from ob_log_events where cluster_id = :id", {"id": cluster_id})
    execute(db, "delete from observer_log_sources where cluster_id = :id", {"id": cluster_id})
    execute(db, "delete from sys_tenant_checks where cluster_id = :id", {"id": cluster_id})
    execute(db, "delete from cluster_merge_checks where cluster_id = :id", {"id": cluster_id})
    execute(db, "delete from tenant_cpu_counters where cluster_id = :id", {"id": cluster_id})
    execute(db, "delete from cpu_collection_status where cluster_id=:id", {"id":cluster_id})
    execute(db, "delete from collection_jobs where cluster_id = :id", {"id": cluster_id})
    execute(db, "delete from cluster_collect_schedules where cluster_id = :id", {"id": cluster_id})
    execute(db, "delete from ob_servers where cluster_id = :id", {"id": cluster_id})
    execute(db, "delete from tenants where cluster_id = :id", {"id": cluster_id})
    execute(db, "delete from clusters where id = :id", {"id": cluster_id})


def delete_tenant_local(db, tenant_id):
    execute(db, "delete from tenant_absence_checks where tenant_id=:id", {"id":tenant_id})
    execute(db, "delete from databases where tenant_id = :id", {"id": tenant_id})
    execute(db, "delete from tenant_collect_schedules where tenant_id = :id", {"id": tenant_id})
    execute(db, "delete from tenant_runtime_metrics where tenant_id = :id", {"id": tenant_id})
    execute(db, "delete from tenant_cpu_samples where tenant_id = :id", {"id": tenant_id})
    execute(db, "delete from tenant_top_objects where tenant_id = :id", {"id": tenant_id})
    execute(db, "delete from tenant_connections where tenant_id = :id", {"id": tenant_id})
    execute(db, "delete from ob_parameters where tenant_id = :id", {"id": tenant_id})
    execute(db, "delete from tenants where id = :id", {"id": tenant_id})


def collect_one_cluster(db, cluster):
    cluster_id = cluster["id"]
    started = local_now()
    try:
        collected = collect_ob_cluster(cluster)
    except RuntimeError as exc:
        log_event(
            logging.ERROR,
            "ob_collect_failed",
            cluster_id=cluster_id,
            cluster_name=cluster["name"],
            target=f"{cluster['endpoint']}:{cluster['port']}",
            user=cluster["sys_user"],
            error=str(exc),
        )
        message = generic_log_message("OB 只读采集")
        record_collection_job(db, cluster_id, "ob_cluster", "failed", message, started)
        return {"status": "failed", "message": message, "stats": {}}
    except Exception as exc:
        log_event(
            logging.ERROR,
            "ob_collect_unhandled_error",
            cluster_id=cluster_id,
            cluster_name=cluster["name"],
            target=f"{cluster['endpoint']}:{cluster['port']}",
            user=cluster["sys_user"],
            error=str(exc),
            exc_info=True,
        )
        message = generic_log_message("OB 只读采集")
        record_collection_job(db, cluster_id, "ob_cluster", "failed", message, started)
        return {"status": "failed", "message": message, "stats": {}}
    stats = collect_cluster_assets(db, cluster_id, collected)
    status = "warning" if stats.get("warnings") else "success"
    message = json.dumps(stats, ensure_ascii=False)
    record_collection_job(db, cluster_id, "ob_cluster", status, message, started)
    return {"status": status, "message": message, "stats": stats}


def record_collection_job(db, cluster_id, target_type, status, message, started):
    execute(
        db,
        """
        insert into collection_jobs
        (cluster_id, target_type, status, message, started_at, finished_at)
        values (:cluster_id, :target_type, :status, :message,
                to_timestamp(:started_at, 'YYYY-MM-DD HH24:MI:SS'), systimestamp)
        """,
        {
            "cluster_id": cluster_id,
            "target_type": target_type,
            "status": status,
            "message": str(message or "")[:1000],
            "started_at": started.strftime("%Y-%m-%d %H:%M:%S"),
        },
    )


def get_tenant_with_cluster(db, tenant_id):
    return one(
        db,
        """
        select t.id, t.cluster_id, t.active, c.name as cluster_name, c.endpoint, c.port,
               t.name, t.tenant_mode, t.primary_zone, t.locality, t.tenant_role,
               t.zone_resource_summary, t.unit_num, t.cpu_cores, t.memory_gb, t.last_full_backup_time,
               t.data_disk_used_gb, t.data_disk_total_gb, t.data_disk_usage_pct,
               t.log_disk_used_gb, t.log_disk_total_gb, t.log_disk_usage_pct,
               t.last_success_merge_time, t.last_merge_status, t.status
        from tenants t
        join clusters c on c.id = t.cluster_id
        where t.id = :id
        """,
        {"id": tenant_id},
    )


def upsert_tenant_connection(db, tenant_id, payload):
    existing = one(db, "select id from tenant_connections where tenant_id = :tenant_id", {"tenant_id": tenant_id})
    data = {
        "tenant_id": tenant_id,
        "tenant_user": payload.get("tenant_user", ""),
        "tenant_password": payload.get("tenant_password", ""),
        "database_name": payload.get("database_name", ""),
    }
    if existing:
        execute(
            db,
            """
            update tenant_connections
            set tenant_user = :tenant_user,
                tenant_password = case when :tenant_password is null or :tenant_password = ''
                                       then tenant_password else :tenant_password end,
                database_name = :database_name,
                updated_at = systimestamp
            where tenant_id = :tenant_id
            """,
            data,
        )
        return existing["id"]
    return insert_returning_id(
        db,
        """
        insert into tenant_connections
        (tenant_id, tenant_user, tenant_password, database_name, created_at, updated_at)
        values (:tenant_id, :tenant_user, :tenant_password, :database_name, systimestamp, systimestamp)
        returning id into :id
        """,
        data,
    )


def normalize_base_tenant_user(user):
    value = (user or "").strip()
    if "@" in value:
        value = value.split("@", 1)[0]
    if "#" in value:
        value = value.split("#", 1)[0]
    return value


def build_tenant_login_user(base_user, tenant):
    user = normalize_base_tenant_user(base_user)
    return f"{user}@{tenant['name']}#{tenant['cluster_name']}"


def is_oracle_mode_tenant(tenant):
    return str(tenant.get("tenant_mode") or "").upper() == "ORACLE"


def build_tenant_collect_user(base_user, tenant):
    user = normalize_base_tenant_user(base_user)
    if not user:
        return ""
    return build_tenant_login_user(user, tenant)


def build_tenant_service_name(value, tenant):
    if value:
        return value
    if is_oracle_mode_tenant(tenant):
        return tenant["name"]
    return ""


def upsert_tenant_schedule(db, tenant_id, payload):
    existing = one(db, "select id from tenant_collect_schedules where tenant_id = :tenant_id", {"tenant_id": tenant_id})
    data = {
        "tenant_id": tenant_id,
        "enabled": 1 if payload.get("enabled", True) else 0,
        "frequency": payload.get("frequency", "daily"),
        "run_time": payload.get("run_time", "07:00"),
        "day_of_week": int(payload.get("day_of_week") or 1),
        "day_of_month": int(payload.get("day_of_month") or 1),
    }
    if existing:
        execute(
            db,
            """
            update tenant_collect_schedules
            set enabled = :enabled, frequency = :frequency, run_time = :run_time,
                day_of_week = :day_of_week, day_of_month = :day_of_month,
                updated_at = systimestamp
            where tenant_id = :tenant_id
            """,
            data,
        )
        return existing["id"]
    return insert_returning_id(
        db,
        """
        insert into tenant_collect_schedules
        (tenant_id, enabled, frequency, run_time, day_of_week, day_of_month, created_at, updated_at)
        values (:tenant_id, :enabled, :frequency, :run_time, :day_of_week, :day_of_month,
                systimestamp, systimestamp)
        returning id into :id
        """,
        data,
    )


def list_tenants(db):
    rows = all_rows(
        db,
        """
        select t.id, t.cluster_id, c.name as cluster_name, t.name, t.tenant_mode,
               t.primary_zone, t.locality, t.tenant_role, t.zone_resource_summary, t.unit_num,
               t.cpu_cores, t.memory_gb,
               t.last_full_backup_time,
               t.data_disk_used_gb, t.data_disk_total_gb, t.data_disk_usage_pct,
               t.log_disk_used_gb, t.log_disk_total_gb, t.log_disk_usage_pct,
               t.last_success_merge_time, t.last_merge_status,
               t.status,
               t.created_at, t.updated_at
        from tenants t
        left join clusters c on c.id = t.cluster_id
        where t.active = 1
        order by c.name, t.name
        """,
    )
    now = local_now()
    for row in rows:
        row.update(tenant_risk_flags(row, now))
    return rows


def filter_tenant_export_rows(rows, cluster_id, hide_standby, hide_meta):
    filtered = []
    for row in rows:
        if cluster_id and str(row.get("cluster_id")) != str(cluster_id):
            continue
        if hide_standby and is_standby_tenant_row(row):
            continue
        if hide_meta and is_meta_tenant_row(row):
            continue
        filtered.append(row)
    return filtered


def is_standby_tenant_row(row):
    values = [row.get("tenant_role"), row.get("status"), row.get("name")]
    return any("standby" in str(value or "").lower() for value in values)


def is_meta_tenant_row(row):
    name = str(row.get("name") or "")
    return name.lower().startswith("meta$")


def build_tenants_xlsx(rows):
    headers = [
        ("cluster_name", "集群"),
        ("name", "租户"),
        ("tenant_mode", "模式"),
        ("primary_zone", "Primary Zone"),
        ("tenant_role", "主备角色"),
        ("unit_num", "Unit数"),
        ("cpu_cores", "CPU"),
        ("memory_gb", "内存GB"),
        ("last_full_backup_time", "上次全备份"),
        ("data_disk_used_gb", "数据盘已用GB"),
        ("data_disk_total_gb", "数据盘总GB"),
        ("data_disk_usage_pct", "数据盘使用率%"),
        ("log_disk_used_gb", "日志盘已用GB"),
        ("log_disk_total_gb", "日志盘总GB"),
        ("log_disk_usage_pct", "日志盘使用率%"),
        ("last_success_merge_time", "上次成功合并"),
        ("last_merge_status", "合并状态"),
        ("status", "状态"),
        ("locality", "Locality"),
        ("zone_resource_summary", "Zone资源"),
        ("updated_at", "更新时间"),
    ]
    data = [[label for _key, label in headers]]
    for row in rows:
        data.append([row.get(key, "") for key, _label in headers])
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", XLSX_CONTENT_TYPES)
        zf.writestr("_rels/.rels", XLSX_RELS)
        zf.writestr("xl/workbook.xml", XLSX_WORKBOOK)
        zf.writestr("xl/_rels/workbook.xml.rels", XLSX_WORKBOOK_RELS)
        zf.writestr("xl/worksheets/sheet1.xml", build_sheet_xml(data))
    return output.getvalue()


def build_sheet_xml(rows):
    sheet_rows = []
    for row_index, row in enumerate(rows, start=1):
        cells = []
        for col_index, value in enumerate(row, start=1):
            ref = f"{column_name(col_index)}{row_index}"
            text = escape("" if value is None else str(value))
            cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>')
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>'
        f'{"".join(sheet_rows)}'
        '</sheetData>'
        '</worksheet>'
    )


def column_name(index):
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def latest_sys_tenant_checks(db):
    # Optional extension must never hide the existing cluster health list.
    available = scalar(db, "select count(*) from user_tables where table_name='CLUSTER_MERGE_CHECKS'")
    if not available:
        rows = all_rows(db, """
          select c.id as cluster_id,c.name as cluster_name,c.endpoint,c.port,c.sys_user,
                 case when c.sys_password is null then 0 else 1 end as has_password,
                 x.status,x.message,x.checked_at
          from clusters c left join (
            select cluster_id,status,message,checked_at from (
              select cluster_id,status,message,checked_at,
                     row_number() over(partition by cluster_id order by checked_at desc,id desc) rn
              from sys_tenant_checks
            ) where rn=1
          ) x on x.cluster_id=c.id order by c.name
        """)
        for row in rows:
            row.update(merge_status='unknown', merge_message='合并检查表未迁移，请先完成测试环境迁移')
        return rows
    return all_rows(db, """
        select c.id as cluster_id, c.name as cluster_name, c.endpoint, c.port, c.sys_user,
               case when c.sys_password is null then 0 else 1 end as has_password,
               x.status, x.message, x.checked_at,
               case when trunc(m.checked_at)=trunc(sysdate) then m.status else 'unknown' end as merge_status,
               case when trunc(m.checked_at)=trunc(sysdate) then m.message else '今日尚未核验凌晨合并' end as merge_message,
               m.checked_at as merge_checked_at
        from clusters c
        left join (
            select cluster_id,status,message,checked_at from (
              select cluster_id,status,message,checked_at,
                     row_number() over(partition by cluster_id order by checked_at desc,id desc) rn
              from sys_tenant_checks
            ) where rn=1
        ) x on x.cluster_id=c.id
        left join (
            select cluster_id,status,message,checked_at from (
              select cluster_id,status,message,checked_at,
                     row_number() over(partition by cluster_id order by checked_at desc,id desc) rn
              from cluster_merge_checks
            ) where rn=1
        ) m on m.cluster_id=c.id
        order by case when x.status='success' and nvl(m.status,'unknown')!='warning' then 1 else 0 end,c.name
    """)


def record_merge_check(db, cluster_id, status, message):
    execute(db, """insert into cluster_merge_checks(cluster_id,status,message,checked_at)
                 values(:cid,:status,:message,systimestamp)""",
            {'cid':cluster_id,'status':status,'message':str(message).encode('utf-8')[:1000].decode('utf-8', errors='ignore')})


def run_sys_tenant_checks(db, force=False):
    stats = {"checked": 0, "success": 0, "failed": 0, "skipped": 0}
    clusters = all_rows(
        db,
        """
        select c.id, c.name, c.endpoint, c.port, c.sys_user, c.sys_password,
               x.checked_at as last_checked_at
        from clusters c
        left join (
            select cluster_id, checked_at
            from (
                select cluster_id, checked_at,
                       row_number() over (partition by cluster_id order by checked_at desc, id desc) as rn
                from sys_tenant_checks
            )
            where rn = 1
        ) x on x.cluster_id = c.id
        order by c.name
        """,
    )
    for cluster in clusters:
        if not force and not sys_check_due(cluster.get("last_checked_at")):
            stats["skipped"] += 1
            continue
        if not cluster.get("sys_password"):
            record_sys_tenant_check(db, cluster["id"], "failed", "未配置 sys 租户采集密码")
            stats["checked"] += 1
            stats["failed"] += 1
            continue
        try:
            result = probe_ob_cluster(cluster)
            record_sys_tenant_check(db, cluster["id"], "success", result.get("message", "sys 租户连接正常"))
        except Exception as exc:
            message = str(exc)[:1000]
            record_sys_tenant_check(db, cluster["id"], "failed", message)
            log_event(logging.ERROR, "sys_tenant_check_failed", cluster_id=cluster['id'], error=message, exc_info=True)
            stats['failed'] += 1
            stats['checked'] += 1
            continue
        stats['success'] += 1
        stats['checked'] += 1
        # Optional compaction must not turn a successful connectivity check into failure.
        try:
            merge = check_ob_major_compaction(cluster, deadline_hour=int(APP_CONFIG.get("major_compaction_deadline_hour", 12)))
            record_merge_check(db, cluster['id'], merge['status'], merge['message'])
        except Exception as merge_exc:
            log_event(logging.WARNING, 'major_compaction_check_failed', cluster_id=cluster['id'], error=str(merge_exc))
            try:
                record_merge_check(db, cluster['id'], 'unknown', '合并检查失败：'+str(merge_exc)[:250])
            except oracledb.DatabaseError as persist_exc:
                log_event(logging.WARNING, 'major_compaction_persist_failed', cluster_id=cluster['id'], error=str(persist_exc))
    return stats


def sys_check_due(last_checked_at):
    if not last_checked_at:
        return True
    if isinstance(last_checked_at, str):
        try:
            last_checked_at = datetime.strptime(last_checked_at[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return True
    elapsed = local_now() - last_checked_at
    return elapsed.total_seconds() >= SYS_CHECK_INTERVAL_MINUTES * 60


def record_sys_tenant_check(db, cluster_id, status, message):
    execute(
        db,
        """
        insert into sys_tenant_checks
        (cluster_id, status, message, checked_at)
        values (:cluster_id, :status, :message, systimestamp)
        """,
        {"cluster_id": cluster_id, "status": status, "message": str(message or "")[:1000]},
    )


def upsert_cluster_schedule(db, cluster_id, payload):
    existing = one(db, "select id from cluster_collect_schedules where cluster_id = :cluster_id", {"cluster_id": cluster_id})
    data = {
        "cluster_id": cluster_id,
        "enabled": 1 if payload.get("enabled", True) else 0,
        "run_time": payload.get("run_time", "07:00"),
    }
    if existing:
        execute(
            db,
            """
            update cluster_collect_schedules
            set enabled = :enabled, run_time = :run_time, updated_at = systimestamp
            where cluster_id = :cluster_id
            """,
            data,
        )
        return existing["id"]
    return insert_returning_id(
        db,
        """
        insert into cluster_collect_schedules
        (cluster_id, enabled, run_time, created_at, updated_at)
        values (:cluster_id, :enabled, :run_time, systimestamp, systimestamp)
        returning id into :id
        """,
        data,
    )


def store_tenant_detail(db, tenant_id, collected):
    stats = {"top_objects": 0, "runtime_metrics": 0, "warnings": []}
    collected_at = local_now().strftime("%Y-%m-%d %H:%M:%S")
    for item in collected.get("top_objects", []):
        if item.get("_error"):
            stats["warnings"].append(f"十大对象采集失败: {item['_error'][:300]}")
            continue
        insert_returning_id(
            db,
            """
            insert into tenant_top_objects
            (tenant_id, database_name, object_name, object_type, data_gb, index_gb,
             total_gb, table_rows, collected_at)
            values (:tenant_id, :database_name, :object_name, :object_type, :data_gb,
                    :index_gb, :total_gb, :table_rows,
                    to_timestamp(:collected_at, 'YYYY-MM-DD HH24:MI:SS'))
            returning id into :id
            """,
            {
                "tenant_id": tenant_id,
                "database_name": item.get("database_name") or "",
                "object_name": item.get("table_name") or item.get("object_name") or "",
                "object_type": item.get("object_type") or "TABLE",
                "data_gb": to_number(item.get("data_gb")),
                "index_gb": to_number(item.get("index_gb")),
                "total_gb": to_number(item.get("total_gb")),
                "table_rows": int(item.get("table_rows") or 0),
                "collected_at": collected_at,
            },
        )
        stats["top_objects"] += 1
    for metric in collected.get("runtime_metrics", []):
        if metric.get("_error"):
            stats["warnings"].append(f"运行指标采集失败: {metric['_error'][:300]}")
            continue
        current_processes = to_number(metric.get("current_processes"))
        max_processes = to_number(metric.get("max_processes"))
        insert_returning_id(
            db,
            """
            insert into tenant_runtime_metrics
            (tenant_id, current_processes, max_processes, collected_at)
            values (:tenant_id, :current_processes, :max_processes,
                    to_timestamp(:collected_at, 'YYYY-MM-DD HH24:MI:SS'))
            returning id into :id
            """,
            {
                "tenant_id": tenant_id,
                "current_processes": int(current_processes or 0),
                "max_processes": int(max_processes) if max_processes is not None else None,
                "collected_at": collected_at,
            },
        )
        stats["runtime_metrics"] += 1
    return stats


def collect_cluster_assets(db, cluster_id, collected):
    stats = {
        "observers": 0,
        "servers": 0,
        "tenants": 0,
        "tenant_backups": 0,
        "tenant_disk_usage": 0,
        "tenant_resources": 0,
        "tenant_zone_resources": 0,
        "tenant_merges": 0,
        "parameters": 0,
        "warnings": [],
    }
    for observer in collected.get("observers", []):
        if observer.get("_error"):
            stats["warnings"].append(f"OBServer采集失败: {observer['_error'][:300]}")
            continue
        payload = {
            "cluster_id": cluster_id,
            "zone": observer.get("zone") or "",
            "svr_ip": observer.get("svr_ip") or observer.get("server_ip") or observer.get("ip") or "",
            "sql_port": int(observer.get("sql_port") or observer.get("port") or 2881),
            "rpc_port": int(observer.get("rpc_port") or 2882),
            "status": str(observer.get("status") or "unknown").lower(),
            "disk_total_gb": int(observer.get("disk_total_gb") or 0),
            "disk_used_gb": int(observer.get("disk_used_gb") or 0),
        }
        if not payload["svr_ip"]:
            continue
        upsert_observer(db, payload)
        upsert_server_from_observer(db, payload)
        stats["observers"] += 1
        stats["servers"] += 1

    # An optional source query failure must not erase previously collected metrics.
    metric_groups = {
        'tenant_backups': ('last_full_backup_time',),
        'tenant_disk_usage': ('data_disk_used_gb','data_disk_total_gb','data_disk_usage_pct','log_disk_used_gb','log_disk_total_gb','log_disk_usage_pct'),
        'tenant_resources': ('unit_num','cpu_cores','memory_gb'),
        'tenant_zone_resources': ('zone_resource_summary',),
        'tenant_merges': ('last_success_merge_time','last_merge_status'),
    }
    preserve = [field for key,fields in metric_groups.items()
                if any(row.get('_error') for row in collected.get(key,[])) for field in fields]
    tenant_id_by_source = {}
    backup_by_tenant = index_collected_rows(collected.get("tenant_backups", []), "tenant_id", "租户全备份", stats)
    disk_by_tenant = index_collected_rows(collected.get("tenant_disk_usage", []), "tenant_id", "租户磁盘", stats)
    resource_by_tenant = index_collected_rows(collected.get("tenant_resources", []), "tenant_id", "租户资源规格", stats)
    zone_resources_by_tenant = group_zone_resources(collected.get("tenant_zone_resources", []), stats)
    merge_by_tenant = index_collected_rows(collected.get("tenant_merges", []), "tenant_id", "租户合并", stats)

    for tenant in collected.get("tenants", []):
        if tenant.get("_error"):
            stats["warnings"].append(f"租户采集失败: {tenant['_error'][:300]}")
            continue
        source_tenant_id = str(tenant.get("tenant_id") or "")
        backup = backup_by_tenant.get(source_tenant_id, {})
        disk = disk_by_tenant.get(source_tenant_id, {})
        resource = resource_by_tenant.get(source_tenant_id, {})
        merge = merge_by_tenant.get(source_tenant_id, {})
        payload = {
            "cluster_id": cluster_id,
            "name": tenant.get("name") or tenant.get("tenant_name") or "",
            "tenant_mode": str(tenant.get("tenant_mode") or tenant.get("compatibility_mode") or "UNKNOWN").upper(),
            "primary_zone": tenant.get("primary_zone") or "",
            "locality": tenant.get("locality") or "",
            "tenant_role": tenant.get("tenant_role") or tenant.get("role") or "",
            "zone_resource_summary": zone_resources_by_tenant.get(source_tenant_id) or "",
            "unit_num": int(to_number(tenant.get("unit_num")) or to_number(resource.get("unit_num")) or 0),
            "cpu_cores": to_number(resource.get("cpu_cores")),
            "memory_gb": to_number(resource.get("memory_gb")),
            "last_full_backup_time": backup.get("last_full_backup_time"),
            "data_disk_used_gb": to_number(disk.get("data_disk_used_gb")),
            "data_disk_total_gb": to_number(disk.get("data_disk_total_gb")),
            "data_disk_usage_pct": to_number(disk.get("data_disk_usage_pct")),
            "log_disk_used_gb": to_number(disk.get("log_disk_used_gb")),
            "log_disk_total_gb": to_number(disk.get("log_disk_total_gb")),
            "log_disk_usage_pct": to_number(disk.get("log_disk_usage_pct")),
            "last_success_merge_time": merge.get("last_success_merge_time"),
            "last_merge_status": merge.get("last_merge_status") or "",
            "status": str(tenant.get("status") or "unknown").lower(),
        }
        if not payload["name"]:
            continue
        if preserve:
            previous = one(db, 'select '+','.join(preserve)+' from tenants where cluster_id=:cid and name=:name',
                           {'cid':cluster_id,'name':payload['name']})
            if previous:
                for field in preserve:payload[field]=previous.get(field)
        tenant_id = upsert_tenant(db, payload)
        tenant_id_by_source[source_tenant_id or payload["name"]] = tenant_id
        stats["tenants"] += 1
        if backup:
            stats["tenant_backups"] += 1
        if disk:
            stats["tenant_disk_usage"] += 1
        if resource:
            stats["tenant_resources"] += 1
        if zone_resources_by_tenant.get(source_tenant_id):
            stats["tenant_zone_resources"] += 1
        if merge:
            stats["tenant_merges"] += 1
    from tenant_lifecycle import reconcile_tenants
    stats.update(reconcile_tenants(db, cluster_id, collected, all_rows, one, execute, local_now()))


    parameter_collect_failed = any(param.get("_error") for param in collected.get("parameters", []))
    if not parameter_collect_failed:
        execute(db, "delete from ob_parameters where cluster_id = :cluster_id", {"cluster_id": cluster_id})
    for param in collected.get("parameters", []):
        if param.get("_error"):
            stats["warnings"].append(f"参数采集失败: {param['_error'][:300]}")
            continue
        tenant_id = tenant_id_by_source.get(str(param.get("tenant_id") or ""))
        upsert_ob_parameter(
            db,
            {
                "cluster_id": cluster_id,
                "tenant_id": tenant_id,
                "name": param.get("name") or "",
                "param_value": str(param.get("value") or ""),
                "info": param.get("info") or "",
                "section": param.get("section") or "",
                "scope": param.get("scope") or "",
            },
        )
        stats["parameters"] += 1
    return stats


def index_collected_rows(rows, key, label, stats):
    indexed = {}
    for row in rows:
        if row.get("_error"):
            stats["warnings"].append(f"{label}采集失败: {row['_error'][:300]}")
            continue
        row_key = row.get(key)
        if row_key not in (None, ""):
            indexed[str(row_key)] = row
    return indexed


def group_zone_resources(rows, stats):
    grouped = {}
    for row in rows:
        if row.get("_error"):
            stats["warnings"].append(f"租户Zone资源采集失败: {row['_error'][:300]}")
            continue
        tenant_id = row.get("tenant_id")
        zone = row.get("zone")
        if tenant_id in (None, "") or not zone:
            continue
        cpu = to_number(row.get("cpu_cores")) or 0
        memory = to_number(row.get("memory_gb")) or 0
        grouped.setdefault(str(tenant_id), []).append(f"{zone}: {cpu:g}C/{memory:g}GB")
    return {tenant_id: "; ".join(values) for tenant_id, values in grouped.items()}


def to_number(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def upsert_ocp_connection(db, payload):
    data = {
        "name": payload["name"],
        "base_url": payload["base_url"].rstrip("/"),
        "ocp_version": payload.get("ocp_version", DEFAULT_OCP_VERSION),
        "auth_type": payload.get("auth_type", "basic"),
        "username": payload.get("username", ""),
        "password": payload.get("password", ""),
        "token": payload.get("token", ""),
        "verify_ssl": 1 if payload.get("verify_ssl", True) else 0,
        "api_prefix": payload.get("api_prefix", "/api/v2"),
    }
    existing = one(
        db,
        "select id from ocp_connections where name = :name and base_url = :base_url",
        {"name": data["name"], "base_url": data["base_url"]},
    )
    if existing:
        execute(
            db,
            """
            update ocp_connections
            set ocp_version = :ocp_version, auth_type = :auth_type, username = :username,
                password = :password, token = :token, verify_ssl = :verify_ssl,
                api_prefix = :api_prefix, status = 'unknown', updated_at = systimestamp
            where id = :id
            """,
            {**data, "id": existing["id"]},
        )
        return existing["id"]
    return insert_returning_id(
        db,
        """
        insert into ocp_connections
        (name, base_url, ocp_version, auth_type, username, password, token, verify_ssl,
         api_prefix, status, created_at, updated_at)
        values (:name, :base_url, :ocp_version, :auth_type, :username, :password, :token,
                :verify_ssl, :api_prefix, 'unknown', systimestamp, systimestamp)
        returning id into :id
        """,
        data,
    )


def upsert_server_from_observer(db, payload):
    if not payload.get("svr_ip"):
        return None
    existing = one(db, "select id from servers where ip = :ip", {"ip": payload["svr_ip"]})
    data = {
        "hostname": payload["svr_ip"],
        "ip": payload["svr_ip"],
        "idc": payload.get("zone", ""),
        "rack": "",
        "os_version": "",
        "cpu_cores": 0,
        "memory_gb": 0,
        "disk_gb": payload.get("disk_total_gb", 0),
        "ssh_port": 22,
        "owner": "OCP",
        "status": payload.get("status", "unknown"),
    }
    if existing:
        execute(
            db,
            """
            update servers
            set idc = :idc, disk_gb = :disk_gb, status = :status,
                owner = :owner, updated_at = systimestamp
            where ip = :ip
            """,
            data,
        )
        return existing["id"]
    return insert_returning_id(
        db,
        """
        insert into servers
        (hostname, ip, idc, rack, os_version, cpu_cores, memory_gb, disk_gb,
         ssh_port, owner, status, created_at, updated_at)
        values (:hostname, :ip, :idc, :rack, :os_version, :cpu_cores, :memory_gb,
                :disk_gb, :ssh_port, :owner, :status, systimestamp, systimestamp)
        returning id into :id
        """,
        data,
    )


def upsert_observer(db, payload):
    existing = one(
        db,
        """
        select id from ob_servers
        where cluster_id = :cluster_id and svr_ip = :svr_ip and sql_port = :sql_port
        """,
        payload,
    )
    if existing:
        execute(
            db,
            """
            update ob_servers
            set zone = :zone, rpc_port = :rpc_port, status = :status,
                disk_total_gb = :disk_total_gb, disk_used_gb = :disk_used_gb,
                updated_at = systimestamp
            where id = :id
            """,
            {**payload, "id": existing["id"]},
        )
        return existing["id"]
    return insert_returning_id(
        db,
        """
        insert into ob_servers
        (cluster_id, zone, svr_ip, sql_port, rpc_port, status,
         disk_total_gb, disk_used_gb, created_at, updated_at)
        values (:cluster_id, :zone, :svr_ip, :sql_port, :rpc_port, :status,
                :disk_total_gb, :disk_used_gb, systimestamp, systimestamp)
        returning id into :id
        """,
        payload,
    )


def upsert_tenant(db, payload):
    defaults = {
        "tenant_mode": "UNKNOWN",
        "primary_zone": "",
        "locality": "",
        "tenant_role": "",
        "zone_resource_summary": "",
        "unit_num": 0,
        "cpu_cores": None,
        "memory_gb": None,
        "last_full_backup_time": None,
        "data_disk_used_gb": None,
        "data_disk_total_gb": None,
        "data_disk_usage_pct": None,
        "log_disk_used_gb": None,
        "log_disk_total_gb": None,
        "log_disk_usage_pct": None,
        "last_success_merge_time": None,
        "last_merge_status": "",
        "status": "unknown",
    }
    payload = {**defaults, **payload}
    existing = one(
        db,
        "select id from tenants where cluster_id = :cluster_id and name = :name",
        {"cluster_id": payload["cluster_id"], "name": payload["name"]},
    )
    if existing:
        execute(
            db,
            """
            update tenants
            set tenant_mode = :tenant_mode, primary_zone = :primary_zone,
                locality = :locality, tenant_role = :tenant_role,
                zone_resource_summary = :zone_resource_summary,
                unit_num = :unit_num,
                cpu_cores = :cpu_cores,
                memory_gb = :memory_gb,
                last_full_backup_time = :last_full_backup_time,
                data_disk_used_gb = :data_disk_used_gb,
                data_disk_total_gb = :data_disk_total_gb,
                data_disk_usage_pct = :data_disk_usage_pct,
                log_disk_used_gb = :log_disk_used_gb,
                log_disk_total_gb = :log_disk_total_gb,
                log_disk_usage_pct = :log_disk_usage_pct,
                last_success_merge_time = :last_success_merge_time,
                last_merge_status = :last_merge_status,
                status = :status, active = 1, updated_at = systimestamp
            where id = :id
            """,
            {**payload, "id": existing["id"]},
        )
        return existing["id"]
    return insert_returning_id(
        db,
        """
        insert into tenants
        (cluster_id, name, tenant_mode, primary_zone, locality, tenant_role,
         zone_resource_summary, unit_num, cpu_cores, memory_gb, last_full_backup_time,
         data_disk_used_gb, data_disk_total_gb, data_disk_usage_pct,
         log_disk_used_gb, log_disk_total_gb, log_disk_usage_pct,
         last_success_merge_time, last_merge_status,
         status, created_at, updated_at)
        values (:cluster_id, :name, :tenant_mode, :primary_zone, :locality, :tenant_role,
                :zone_resource_summary, :unit_num, :cpu_cores, :memory_gb, :last_full_backup_time,
                :data_disk_used_gb, :data_disk_total_gb, :data_disk_usage_pct,
                :log_disk_used_gb, :log_disk_total_gb, :log_disk_usage_pct,
                :last_success_merge_time, :last_merge_status,
                :status,
                systimestamp, systimestamp)
        returning id into :id
        """,
        payload,
    )


def upsert_ob_parameter(db, payload):
    if not payload.get("name"):
        return None
    existing = one(
        db,
        """
        select id from ob_parameters
        where cluster_id = :cluster_id
          and nvl(tenant_id, -1) = nvl(:tenant_id, -1)
          and name = :name
        """,
        payload,
    )
    if existing:
        execute(
            db,
            """
            update ob_parameters
            set param_value = :param_value, info = :info, section = :section, scope = :scope,
                updated_at = systimestamp
            where id = :id
            """,
            {**payload, "id": existing["id"]},
        )
        return existing["id"]
    return insert_returning_id(
        db,
        """
        insert into ob_parameters
        (cluster_id, tenant_id, name, param_value, info, section, scope, created_at, updated_at)
        values (:cluster_id, :tenant_id, :name, :param_value, :info, :section, :scope,
                systimestamp, systimestamp)
        returning id into :id
        """,
        payload,
    )


def upsert_database(db, payload):
    existing = one(
        db,
        "select id from databases where tenant_id = :tenant_id and name = :name",
        {"tenant_id": payload["tenant_id"], "name": payload["name"]},
    )
    if existing:
        execute(
            db,
            """
            update databases
            set charset_name = :charset_name, collation_name = :collation_name,
                owner = :owner, updated_at = systimestamp
            where id = :id
            """,
            {**payload, "id": existing["id"]},
        )
        return existing["id"]
    return insert_returning_id(
        db,
        """
        insert into databases
        (tenant_id, name, charset_name, collation_name, owner, created_at, updated_at)
        values (:tenant_id, :name, :charset_name, :collation_name, :owner,
                systimestamp, systimestamp)
        returning id into :id
        """,
        payload,
    )


def find_database(db, tenant_id, name):
    return one(
        db,
        "select id, tenant_id, name from databases where tenant_id = :tenant_id and name = :name",
        {"tenant_id": tenant_id, "name": name},
    )


def get_ocp_config(db, connection_id):
    return one(
        db,
        """
        select id, name, base_url, ocp_version, auth_type, username, password, token,
               verify_ssl, api_prefix
        from ocp_connections
        where id = :id
        """,
        {"id": connection_id},
    )


def tenant_risk_flags(row, now):
    eligible = not (is_meta_tenant_row(row) or is_standby_tenant_row(row))
    backup = parse_flexible_datetime(row.get('last_full_backup_time'))
    status = str(row.get('status') or '').lower()
    return {
        'backup_overdue': eligible and (not backup or (now-backup).total_seconds() > 86400),
        'merge_missing': eligible and not parse_flexible_datetime(row.get('last_success_merge_time')),
        'collect_abnormal': eligible and bool(status) and status not in ('normal','online','running','available','active'),
    }


def tenant_risk_summary(db):
    rows = list_tenants(db)
    return {
        'tenants_backup_overdue': sum(bool(r['backup_overdue']) for r in rows),
        'tenants_merge_missing': sum(bool(r['merge_missing']) for r in rows),
        'tenants_collect_abnormal': sum(bool(r['collect_abnormal']) for r in rows),
    }


def parse_flexible_datetime(value):
    text = str(value or "").strip()[:19]
    if not text or text == "-":
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(text.replace("T", " "), fmt)
        except ValueError:
            pass
    return None


def is_meta_tenant_name(name):
    text = str(name or "").lower()
    return text.startswith("meta$") or "meta" in text


def is_standby_tenant_row(row):
    values = [row.get("tenant_role"), row.get("status"), row.get("name")]
    return any("standby" in str(value or "").lower() for value in values)


def list_observer_log_sources(db):
    return all_rows(
        db,
        """
        select s.id, s.cluster_id, c.name as cluster_name, s.observer_ip, s.ssh_user,
               case when s.ssh_password is null then 0 else 1 end as has_password,
               s.ssh_port, s.log_path, s.tail_lines, s.interval_minutes, s.enabled,
               s.last_run_at, s.last_error, s.created_at, s.updated_at
        from observer_log_sources s
        left join clusters c on c.id = s.cluster_id
        order by s.id desc
        """,
    )


def get_observer_log_source(db, source_id):
    return one(
        db,
        """
        select s.id, s.cluster_id, c.name as cluster_name, s.observer_ip, s.ssh_user,
               s.ssh_password, s.ssh_port, s.log_path, s.tail_lines, s.interval_minutes, s.enabled,
               s.last_run_at, s.last_error, s.created_at, s.updated_at
        from observer_log_sources s
        left join clusters c on c.id = s.cluster_id
        where s.id = :id
        """,
        {"id": source_id},
    )


def upsert_observer_log_source(db, payload):
    data = {
        "id": int(payload.get("id") or 0),
        "cluster_id": int(payload["cluster_id"]),
        "observer_ip": payload.get("observer_ip", ""),
        "ssh_user": payload.get("ssh_user", "admin"),
        "ssh_password": payload.get("ssh_password", ""),
        "ssh_port": int(payload.get("ssh_port") or 22),
        "log_path": payload.get("log_path", "/home/admin/oceanbase/log/observer.log"),
        "tail_lines": int(payload.get("tail_lines") or 1000),
        "interval_minutes": int(payload.get("interval_minutes") or 10),
        "enabled": 1 if payload.get("enabled", True) else 0,
    }
    if data["id"]:
        execute(
            db,
            """
            update observer_log_sources
            set cluster_id = :cluster_id, observer_ip = :observer_ip, ssh_user = :ssh_user,
                ssh_password = case when :ssh_password is null or :ssh_password = ''
                                    then ssh_password else :ssh_password end,
                ssh_port = :ssh_port, log_path = :log_path, tail_lines = :tail_lines,
                interval_minutes = :interval_minutes, enabled = :enabled,
                updated_at = systimestamp
            where id = :id
            """,
            data,
        )
        return data["id"]
    return insert_returning_id(
        db,
        """
        insert into observer_log_sources
        (cluster_id, observer_ip, ssh_user, ssh_password, ssh_port, log_path, tail_lines,
         interval_minutes, enabled, created_at, updated_at)
        values (:cluster_id, :observer_ip, :ssh_user, :ssh_password, :ssh_port, :log_path, :tail_lines,
                :interval_minutes, :enabled, systimestamp, systimestamp)
        returning id into :id
        """,
        data,
    )


def collect_due_observer_log_sources(db, force=False):
    sources = all_rows(db, "select * from observer_log_sources where enabled = 1 order by id")
    summary = {"total": len(sources), "collected": 0, "skipped": 0, "failed": 0, "inserted": 0, "parsed": 0, "results": []}
    now = local_now()
    for source in sources:
        if not force and not observer_log_source_due(source, now):
            summary["skipped"] += 1
            continue
        result = collect_observer_log_source(db, source)
        summary["collected"] += 1
        summary["inserted"] += result.get("inserted", 0)
        summary["parsed"] += result.get("parsed", 0)
        if result.get("status") == "failed":
            summary["failed"] += 1
        summary["results"].append(result)
    return summary


def observer_log_source_due(source, now):
    last_run = parse_db_timestamp(source.get("last_run_at"))
    if not last_run:
        return True
    interval_minutes = int(source.get("interval_minutes") or 10)
    return (now - last_run).total_seconds() >= interval_minutes * 60


def parse_db_timestamp(value):
    if isinstance(value, datetime):
        return value
    text = str(value or "")[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def collect_observer_log_source(db, source):
    started = local_now()
    try:
        raw_log = read_observer_log_tail(source)
        events = parse_ob_log(raw_log, source.get("cluster_id"), source.get("observer_ip", ""), source.get("log_path", ""))
        inserted = insert_ob_log_events(db, events)
        execute(
            db,
            """
            update observer_log_sources
            set last_run_at = systimestamp, last_error = null, updated_at = systimestamp
            where id = :id
            """,
            {"id": source["id"]},
        )
        message = f"observer日志读取完成，解析{len(events)}条，新增{inserted}条"
        record_collection_job(db, source.get("cluster_id"), "observer_log", "success", message, started)
        return {"id": source["id"], "status": "success", "parsed": len(events), "inserted": inserted, "message": message}
    except Exception as exc:
        message = str(exc)[:1000]
        execute(
            db,
            """
            update observer_log_sources
            set last_run_at = systimestamp, last_error = :last_error, updated_at = systimestamp
            where id = :id
            """,
            {"id": source["id"], "last_error": message},
        )
        record_collection_job(db, source.get("cluster_id"), "observer_log", "failed", message, started)
        log_event(logging.ERROR, "observer_log_collect_failed", source_id=source.get("id"), observer_ip=source.get("observer_ip"), error=message)
        return {"id": source["id"], "status": "failed", "parsed": 0, "inserted": 0, "message": message}


def read_observer_log_tail(source):
    host = source.get("observer_ip") or ""
    user = source.get("ssh_user") or "admin"
    password = source.get("ssh_password") or ""
    port = int(source.get("ssh_port") or 22)
    log_path = source.get("log_path") or "/home/admin/oceanbase/log/observer.log"
    tail_lines = int(source.get("tail_lines") or 1000)
    remote = f"tail -n {tail_lines} {shlex.quote(log_path)}"
    ssh_cmd = [
        "ssh",
        "-o",
        f"BatchMode={'no' if password else 'yes'}",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=no",
        "-p",
        str(port),
        f"{user}@{host}",
        remote,
    ]
    env = None
    cmd = ssh_cmd
    if password:
        cmd = ["sshpass", "-e", *ssh_cmd]
        env = {**os.environ, "SSHPASS": password}
    try:
        result = subprocess.run(cmd, text=True, capture_output=True, timeout=45, env=env)
    except FileNotFoundError as exc:
        if password:
            raise RuntimeError("服务器未安装 sshpass，无法使用页面配置的 SSH 密码读取日志；请安装 sshpass 或改用 SSH 免密") from exc
        raise
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"SSH读取observer日志失败：{user}@{host}:{port} {log_path}；{detail[:500]}")
    return result.stdout


def insert_ob_log_events(db, events):
    inserted = 0
    for event in events:
        if ob_log_event_exists(db, event):
            continue
        execute(
            db,
            """
            insert into ob_log_events
            (cluster_id, server_ip, log_path, event_time, severity, error_code,
             component, message, raw_line, created_at)
            values (:cluster_id, :server_ip, :log_path,
                    to_timestamp(:event_time, 'YYYY-MM-DD HH24:MI:SS'),
                    :severity, :error_code, :component, :message,
                    :raw_line, systimestamp)
            """,
            event,
        )
        inserted += 1
    return inserted


def ob_log_event_exists(db, event):
    return bool(
        one(
            db,
            """
            select id from ob_log_events
            where nvl(cluster_id, -1) = nvl(:cluster_id, -1)
              and nvl(server_ip, '-') = nvl(:server_ip, '-')
              and event_time = to_timestamp(:event_time, 'YYYY-MM-DD HH24:MI:SS')
              and severity = :severity
              and message = :message
              and rownum = 1
            """,
            event,
        )
    )


def parse_ob_log(raw_log, cluster_id=None, server_ip="", log_path=""):
    events = []
    for line in raw_log.splitlines():
        severity = detect_severity(line)
        if not severity:
            continue
        events.append(
            {
                "cluster_id": cluster_id,
                "server_ip": server_ip,
                "log_path": log_path,
                "event_time": parse_log_time(line),
                "severity": severity,
                "error_code": detect_error_code(line),
                "component": detect_component(line),
                "message": line[:1000],
                "raw_line": line,
            }
        )
    return events


def detect_severity(line):
    log_level = detect_ob_log_level(line)
    if log_level in ("INFO", "TRACE", "DEBUG", "WDIAG", "TDIAG", "EDIAG", "DIAG"):
        return None
    if re.search(r"\b(FATAL|CRITICAL)\b", line, re.IGNORECASE):
        return "FATAL"
    if re.search(r"\b(ERROR|ERR)\b", line, re.IGNORECASE):
        return "ERROR"
    if re.search(r"\b(WARN|WARNING)\b", line, re.IGNORECASE):
        return "WARN"
    if re.search(r"OB-\d{4,}", line):
        return "ERROR"
    return None


def detect_ob_log_level(line):
    match = re.search(r"^\[[^\]]+\]\s+([A-Z]+)\b", line)
    if match:
        return match.group(1).upper()
    match = re.search(r"\b(INFO|TRACE|DEBUG|WDIAG|TDIAG|EDIAG|DIAG|WARN|WARNING|ERROR|FATAL)\b", line, re.IGNORECASE)
    return match.group(1).upper() if match else ""


def parse_log_time(line):
    match = re.search(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", line)
    if match:
        return match.group(1).replace("T", " ")[:19]
    return local_now().strftime("%Y-%m-%d %H:%M:%S")


def detect_error_code(line):
    match = re.search(r"\b(OB-\d{4,}|ORA-\d{4,}|ERROR\s+\d+)\b", line, re.IGNORECASE)
    return match.group(1).upper() if match else ""


def detect_component(line):
    match = re.search(r"\[([A-Za-z0-9_./:-]+)\]", line)
    return match.group(1)[:80] if match else ""


def init_db(seed=False):
    conn = oracledb.connect(user=ORACLE_USER, password=ORACLE_PASSWORD, dsn=ORACLE_DSN)
    try:
        ensure_schema(conn, force=True)
    finally:
        conn.close()


def ensure_schema(conn, force=False):
    global schema_initialized
    if schema_initialized and not force:
        return
    for ddl in DDL:
        execute_ddl(conn, ddl)
    schema_initialized = True


def execute_ddl(conn, ddl):
    try:
        execute(conn, ddl)
    except oracledb.DatabaseError as exc:
        error = exc.args[0]
        code = getattr(error, "code", None)
        if code not in (955, 1430):
            raise
    return True


DDL = [
    """
    create table clusters (
        id number generated by default on null as identity primary key,
        name varchar2(128) not null unique,
        environment varchar2(32) default 'prod' not null,
        region varchar2(128),
        endpoint varchar2(256),
        port number default 2881 not null,
        sys_user varchar2(128) default 'root@sys' not null,
        sys_password varchar2(512),
        version varchar2(64),
        status varchar2(32) default 'unknown' not null,
        owner varchar2(128),
        remark varchar2(1000),
        created_at timestamp not null,
        updated_at timestamp not null
    )
    """,
    """
    create table servers (
        id number generated by default on null as identity primary key,
        hostname varchar2(128) not null,
        ip varchar2(64) not null unique,
        idc varchar2(128),
        rack varchar2(128),
        os_version varchar2(128),
        cpu_cores number,
        memory_gb number,
        disk_gb number,
        ssh_port number default 22 not null,
        owner varchar2(128),
        status varchar2(32) default 'unknown' not null,
        created_at timestamp not null,
        updated_at timestamp not null
    )
    """,
    """
    create table ob_servers (
        id number generated by default on null as identity primary key,
        cluster_id number not null references clusters(id),
        server_id number references servers(id),
        zone varchar2(128),
        svr_ip varchar2(64) not null,
        sql_port number default 2881 not null,
        rpc_port number default 2882 not null,
        status varchar2(32) default 'unknown' not null,
        disk_total_gb number,
        disk_used_gb number,
        created_at timestamp not null,
        updated_at timestamp not null
    )
    """,
    """
    create table tenants (
        id number generated by default on null as identity primary key,
        cluster_id number not null references clusters(id),
        name varchar2(128) not null,
        tenant_mode varchar2(32) default 'MYSQL' not null,
        primary_zone varchar2(256),
        locality varchar2(1000),
        tenant_role varchar2(64),
        zone_resource_summary varchar2(1000),
        unit_num number,
        cpu_cores number,
        memory_gb number,
        last_full_backup_time varchar2(64),
        data_disk_used_gb number,
        data_disk_total_gb number,
        data_disk_usage_pct number,
        log_disk_used_gb number,
        log_disk_total_gb number,
        log_disk_usage_pct number,
        last_success_merge_time varchar2(64),
        last_merge_status varchar2(64),
        status varchar2(32) default 'unknown' not null,
        created_at timestamp not null,
        updated_at timestamp not null
    )
    """,
    """
    create table databases (
        id number generated by default on null as identity primary key,
        tenant_id number not null references tenants(id),
        name varchar2(128) not null,
        charset_name varchar2(64),
        collation_name varchar2(128),
        owner varchar2(128),
        created_at timestamp not null,
        updated_at timestamp not null
    )
    """,
    """
    create table collection_jobs (
        id number generated by default on null as identity primary key,
        cluster_id number references clusters(id),
        target_type varchar2(64) not null,
        status varchar2(32) not null,
        message varchar2(1000),
        started_at timestamp not null,
        finished_at timestamp
    )
    """,
    """
    create table ob_log_events (
        id number generated by default on null as identity primary key,
        cluster_id number references clusters(id),
        server_ip varchar2(64),
        log_path varchar2(512),
        event_time timestamp not null,
        severity varchar2(16) not null,
        error_code varchar2(64),
        component varchar2(128),
        message varchar2(1000),
        raw_line clob,
        created_at timestamp not null
    )
    """,
    """
    create table ocp_connections (
        id number generated by default on null as identity primary key,
        name varchar2(128) not null,
        base_url varchar2(512) not null,
        ocp_version varchar2(64) default '4.3.5-20250610160438',
        auth_type varchar2(32) default 'basic' not null,
        username varchar2(128),
        password varchar2(512),
        token varchar2(2000),
        verify_ssl number(1) default 1 not null,
        api_prefix varchar2(64) default '/api/v2' not null,
        status varchar2(32) default 'unknown' not null,
        last_sync_at timestamp,
        created_at timestamp not null,
        updated_at timestamp not null
    )
    """,
    """
    create table ocp_sync_runs (
        id number generated by default on null as identity primary key,
        connection_id number not null references ocp_connections(id),
        status varchar2(32) not null,
        cluster_count number default 0 not null,
        message varchar2(1000),
        started_at timestamp not null,
        finished_at timestamp
    )
    """,
    """
    create table ob_parameters (
        id number generated by default on null as identity primary key,
        cluster_id number not null references clusters(id),
        tenant_id number references tenants(id),
        name varchar2(256) not null,
        param_value varchar2(4000),
        info varchar2(1000),
        section varchar2(128),
        scope varchar2(128),
        created_at timestamp not null,
        updated_at timestamp not null
    )
    """,
    """
    create table tenant_connections (
        id number generated by default on null as identity primary key,
        tenant_id number not null references tenants(id),
        tenant_user varchar2(256) not null,
        tenant_password varchar2(512),
        database_name varchar2(128),
        created_at timestamp not null,
        updated_at timestamp not null
    )
    """,
    """
    create table tenant_top_objects (
        id number generated by default on null as identity primary key,
        tenant_id number not null references tenants(id),
        database_name varchar2(128),
        object_name varchar2(256) not null,
        object_type varchar2(64),
        data_gb number,
        index_gb number,
        total_gb number,
        table_rows number,
        collected_at timestamp not null
    )
    """,
    """
    create table tenant_runtime_metrics (
        id number generated by default on null as identity primary key,
        tenant_id number not null references tenants(id),
        current_processes number,
        max_processes number,
        collected_at timestamp not null
    )
    """,
    """
    create table tenant_collect_schedules (
        id number generated by default on null as identity primary key,
        tenant_id number not null references tenants(id),
        enabled number(1) default 1 not null,
        frequency varchar2(16) default 'daily' not null,
        run_time varchar2(8) default '07:00' not null,
        day_of_week number default 1,
        day_of_month number default 1,
        last_run_at timestamp,
        created_at timestamp not null,
        updated_at timestamp not null
    )
    """,
    """
    create table cluster_collect_schedules (
        id number generated by default on null as identity primary key,
        cluster_id number not null references clusters(id),
        enabled number(1) default 1 not null,
        run_time varchar2(8) default '07:00' not null,
        last_run_at timestamp,
        created_at timestamp not null,
        updated_at timestamp not null
    )
    """,
    """
    create table sys_tenant_checks (
        id number generated by default on null as identity primary key,
        cluster_id number not null references clusters(id),
        status varchar2(32) not null,
        message varchar2(1000),
        checked_at timestamp not null
    )
    """,
    """
    create table observer_log_sources (
        id number generated by default on null as identity primary key,
        cluster_id number not null references clusters(id),
        observer_ip varchar2(64) not null,
        ssh_user varchar2(128) default 'admin' not null,
        ssh_password varchar2(512),
        ssh_port number default 22 not null,
        log_path varchar2(512) default '/home/admin/oceanbase/log/observer.log' not null,
        tail_lines number default 1000 not null,
        interval_minutes number default 10 not null,
        enabled number(1) default 1 not null,
        last_run_at timestamp,
        last_error varchar2(1000),
        created_at timestamp not null,
        updated_at timestamp not null
    )
    """,
    """
    alter table observer_log_sources add ssh_password varchar2(512)
    """,
    """
    alter table clusters add sys_password varchar2(512)
    """,
    """
    alter table tenants add active number(1) default 1 not null
    """,
    """
    alter table tenants add locality varchar2(1000)
    """,
    """
    alter table tenants add tenant_role varchar2(64)
    """,
    """
    alter table tenants add zone_resource_summary varchar2(1000)
    """,
    """
    alter table tenants add cpu_cores number
    """,
    """
    alter table tenants add memory_gb number
    """,
    """
    alter table tenants add last_full_backup_time varchar2(64)
    """,
    """
    alter table tenants add data_disk_used_gb number
    """,
    """
    alter table tenants add data_disk_total_gb number
    """,
    """
    alter table tenants add data_disk_usage_pct number
    """,
    """
    alter table tenants add log_disk_used_gb number
    """,
    """
    alter table tenants add log_disk_total_gb number
    """,
    """
    alter table tenants add log_disk_usage_pct number
    """,
    """
    alter table tenants add last_success_merge_time varchar2(64)
    """,
    """
    alter table tenants add last_merge_status varchar2(64)
    """,
    """
    alter table ocp_connections add ocp_version varchar2(64) default '4.3.5-20250610160438'
    """,
    """
    create table cluster_merge_checks (
      id number generated by default on null as identity primary key,
      cluster_id number not null references clusters(id),
      status varchar2(32) not null,
      message varchar2(1000),
      checked_at timestamp not null
    )
    """,
    """
    create table tenant_cpu_counters (
      cluster_id number not null references clusters(id),
      ob_tenant_id number not null,
      svr_ip varchar2(64) not null,
      svr_port number not null,
      cpu_time_us number not null,
      quota_hundredths number not null,
      sampled_at timestamp not null,
      constraint pk_tenant_cpu_counter primary key(cluster_id,ob_tenant_id,svr_ip,svr_port)
    )
    """,
    """
    create table tenant_cpu_samples (
      id number generated by default on null as identity primary key,
      tenant_id number not null references tenants(id),
      sampled_at timestamp not null,
      cpu_used_cores number,
      cpu_limit_cores number,
      cpu_used_pct number,
      node_count number
    )
    """,
    """
    create index ix_tenant_cpu_samples on tenant_cpu_samples(tenant_id,sampled_at)
    """,
    """
    create table tenant_absence_checks (
      tenant_id number primary key references tenants(id),
      cluster_id number not null references clusters(id),
      first_missing_at timestamp not null
    )
    """,
    """
    create table cpu_collection_status (
      cluster_id number primary key references clusters(id),
      status varchar2(32) not null,
      message varchar2(1000),
      checked_at timestamp not null
    )
    """,

    """
    create table cluster_sys_cpu_samples (
      id number generated by default on null as identity primary key,
      cluster_id number not null references clusters(id),
      sampled_at timestamp not null,
      cpu_used_cores number,
      cpu_limit_cores number,
      cpu_used_pct number,
      node_count number
    )
    """,
    """
    create index ix_sys_cpu_samples on cluster_sys_cpu_samples(cluster_id,sampled_at)
    """,

]


app = create_app()


if __name__ == "__main__":
    init_db(seed=True)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", APP_CONFIG.get("port", 8000))), debug=True)
