import os
import re
from datetime import datetime

import oracledb
from flask import Flask, g, jsonify, render_template, request

from ocp_collector import OcpClient, normalize_ocp_clusters


ORACLE_USER = os.environ.get("ORACLE_USER", "ob_asset")
ORACLE_PASSWORD = os.environ.get("ORACLE_PASSWORD", "ob_asset_password")
ORACLE_DSN = os.environ.get("ORACLE_DSN", "127.0.0.1:1521/OBPDB")
DEFAULT_OCP_VERSION = os.environ.get("DEFAULT_OCP_VERSION", "4.3.5-20250610160438")
DEFAULT_OB_VERSION = os.environ.get("DEFAULT_OB_VERSION", "4.2.1.8")
POOL_MIN = int(os.environ.get("ORACLE_POOL_MIN", "1"))
POOL_MAX = int(os.environ.get("ORACLE_POOL_MAX", "5"))
POOL_INCREMENT = int(os.environ.get("ORACLE_POOL_INCREMENT", "1"))

pool = None


def create_app():
    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False

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

    @app.route("/api/summary")
    def summary():
        db = g.db
        return jsonify(
            {
                "clusters": scalar(db, "select count(*) from clusters"),
                "tenants": scalar(db, "select count(*) from tenants"),
                "databases": scalar(db, "select count(*) from databases"),
                "servers": scalar(db, "select count(*) from servers"),
                "observers": scalar(db, "select count(*) from ob_servers"),
                "log_errors": scalar(db, "select count(*) from ob_log_events where severity in ('ERROR', 'FATAL')"),
                "ocp_connections": safe_scalar(db, "select count(*) from ocp_connections"),
            }
        )

    @app.route("/api/clusters", methods=["GET", "POST"])
    def clusters():
        if request.method == "POST":
            payload = request.get_json(force=True)
            missing = [key for key in ["name", "endpoint", "sys_user"] if not payload.get(key)]
            if missing:
                return jsonify({"error": "missing fields", "fields": missing}), 400
            new_id = upsert_cluster(g.db, payload)
            g.db.commit()
            return jsonify({"id": new_id}), 201

        return jsonify(
            all_rows(
                g.db,
                """
                select c.id, c.name, c.environment, c.region, c.endpoint, c.port,
                       c.sys_user, c.version, c.status, c.owner, c.remark,
                       c.created_at, c.updated_at,
                       count(distinct t.id) as tenant_count,
                       count(distinct o.id) as observer_count
                from clusters c
                left join tenants t on t.cluster_id = c.id
                left join ob_servers o on o.cluster_id = c.id
                group by c.id, c.name, c.environment, c.region, c.endpoint, c.port,
                         c.sys_user, c.version, c.status, c.owner, c.remark,
                         c.created_at, c.updated_at
                order by c.environment, c.name
                """,
            )
        )

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
            for event in events:
                execute(
                    g.db,
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
            g.db.commit()
            return jsonify({"inserted": len(events)}), 201

        return jsonify(
            all_rows(
                g.db,
                """
                select e.id, e.cluster_id, c.name as cluster_name, e.server_ip, e.log_path,
                       e.event_time, e.severity, e.error_code, e.component,
                       e.message, e.created_at
                from ob_log_events e
                left join clusters c on c.id = e.cluster_id
                order by e.event_time desc, e.id desc
                fetch first 100 rows only
                """,
            )
        )

    @app.route("/api/ocp/connections", methods=["GET", "POST"])
    def ocp_connections():
        if request.method == "POST":
            payload = request.get_json(force=True)
            missing = [key for key in ["name", "base_url"] if not payload.get(key)]
            if missing:
                return jsonify({"error": "missing fields", "fields": missing}), 400
            new_id = insert_returning_id(
                g.db,
                """
                insert into ocp_connections
                (name, base_url, ocp_version, auth_type, username, password, token, verify_ssl,
                 api_prefix, status, created_at, updated_at)
                values (:name, :base_url, :ocp_version, :auth_type, :username, :password, :token,
                        :verify_ssl, :api_prefix, 'unknown', systimestamp, systimestamp)
                returning id into :id
                """,
                {
                    "name": payload["name"],
                    "base_url": payload["base_url"].rstrip("/"),
                    "ocp_version": payload.get("ocp_version", DEFAULT_OCP_VERSION),
                    "auth_type": payload.get("auth_type", "basic"),
                    "username": payload.get("username", ""),
                    "password": payload.get("password", ""),
                    "token": payload.get("token", ""),
                    "verify_ssl": 1 if payload.get("verify_ssl", True) else 0,
                    "api_prefix": payload.get("api_prefix", "/api/v2"),
                },
            )
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

    @app.route("/api/ocp/connections/<int:connection_id>/sync", methods=["POST"])
    def ocp_sync(connection_id):
        config = get_ocp_config(g.db, connection_id)
        if not config:
            return jsonify({"error": "OCP connection not found"}), 404
        started = datetime.utcnow()
        client = OcpClient(config)
        raw_clusters = client.fetch_clusters()
        clusters = normalize_ocp_clusters(raw_clusters)
        for cluster in clusters:
            upsert_cluster(g.db, cluster)
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
                "status": "success",
                "cluster_count": len(clusters),
                "message": "OCP sync completed",
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
        return jsonify({"id": run_id, "cluster_count": len(clusters), "clusters": clusters})

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
        cur.execute(sql, params or {})


def scalar(db, sql, params=None):
    with db.cursor() as cur:
        cur.execute(sql, params or {})
        return cur.fetchone()[0]


def safe_scalar(db, sql, params=None):
    try:
        return scalar(db, sql, params)
    except oracledb.DatabaseError:
        return 0


def all_rows(db, sql, params=None):
    with db.cursor() as cur:
        cur.execute(sql, params or {})
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
        cur.execute(sql, {**params, "id": out_id})
        value = out_id.getvalue()
        if isinstance(value, list):
            value = value[0]
        return int(value)


def upsert_cluster(db, payload):
    existing = one(db, "select id from clusters where name = :name", {"name": payload["name"]})
    data = {
        "name": payload["name"],
        "environment": payload.get("environment", "prod"),
        "region": payload.get("region", ""),
        "endpoint": payload.get("endpoint", ""),
        "port": int(payload.get("port") or 2881),
        "sys_user": payload.get("sys_user", "root@sys"),
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
         status, owner, remark, created_at, updated_at)
        values (:name, :environment, :region, :endpoint, :port, :sys_user,
                :version, :status, :owner, :remark, systimestamp, systimestamp)
        returning id into :id
        """,
        data,
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
    if re.search(r"\b(FATAL|CRITICAL)\b", line, re.IGNORECASE):
        return "FATAL"
    if re.search(r"\b(ERROR|ERR)\b", line, re.IGNORECASE):
        return "ERROR"
    if re.search(r"\b(WARN|WARNING)\b", line, re.IGNORECASE):
        return "WARN"
    if re.search(r"OB-\d{4,}", line):
        return "ERROR"
    return None


def parse_log_time(line):
    match = re.search(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", line)
    if match:
        return match.group(1).replace("T", " ")[:19]
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def detect_error_code(line):
    match = re.search(r"\b(OB-\d{4,}|ORA-\d{4,}|ERROR\s+\d+)\b", line, re.IGNORECASE)
    return match.group(1).upper() if match else ""


def detect_component(line):
    match = re.search(r"\[([A-Za-z0-9_./:-]+)\]", line)
    return match.group(1)[:80] if match else ""


def init_db(seed=False):
    conn = oracledb.connect(user=ORACLE_USER, password=ORACLE_PASSWORD, dsn=ORACLE_DSN)
    try:
        for ddl in DDL:
            execute_ddl(conn, ddl)
        if seed and scalar(conn, "select count(*) from clusters") == 0:
            upsert_cluster(
                conn,
                {
                    "name": "ob-prod-core",
                    "environment": "prod",
                    "region": "shanghai",
                    "endpoint": "10.10.20.11",
                    "version": DEFAULT_OB_VERSION,
                    "status": "online",
                    "remark": "seed data",
                },
            )
            conn.commit()
    finally:
        conn.close()


def execute_ddl(conn, ddl):
    try:
        execute(conn, ddl)
    except oracledb.DatabaseError as exc:
        error = exc.args[0]
        if getattr(error, "code", None) not in (955, 1430):
            raise


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
        mode varchar2(32) default 'MYSQL' not null,
        primary_zone varchar2(256),
        unit_num number,
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
    alter table ocp_connections add ocp_version varchar2(64) default '4.3.5-20250610160438'
    """,
]


app = create_app()


if __name__ == "__main__":
    init_db(seed=True)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=True)
