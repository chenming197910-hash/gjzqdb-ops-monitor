from urllib.parse import urljoin

import requests


READONLY_METHOD = "GET"


class OcpClient:
    def __init__(self, config):
        self.base_url = config["base_url"].rstrip("/") + "/"
        self.api_prefix = (config.get("api_prefix") or "/api/v2").strip("/")
        self.ocp_version = config.get("ocp_version") or "4.3.5-20250610160438"
        self.auth_type = config.get("auth_type") or "basic"
        self.username = config.get("username") or ""
        self.password = config.get("password") or ""
        self.token = config.get("token") or ""
        self.verify_ssl = bool(config.get("verify_ssl", 1))
        self.session = requests.Session()
        if self.auth_type == "basic":
            self.session.auth = (self.username, self.password)
        elif self.token:
            self.session.headers.update({"Authorization": f"Bearer {self.token}"})

    def test(self):
        try:
            data = self.get("info")
            return {"ok": True, "endpoint": self.url("info"), "data": data}
        except requests.RequestException as exc:
            return {"ok": False, "endpoint": self.url("info"), "message": str(exc)}

    def fetch_clusters(self):
        data = self.get("ob/clusters")
        return extract_items(data)

    def fetch_first(self, paths):
        errors = []
        for path in paths:
            try:
                return self.get(path), path
            except requests.RequestException as exc:
                errors.append(f"{path}: {exc}")
        return [], "; ".join(errors)

    def fetch_cluster_assets(self, cluster):
        cluster_ref = pick(cluster, "ocp_cluster_id", "cluster_id", "id", "name")
        if cluster_ref in (None, ""):
            cluster_ref = cluster.get("name")
        cluster_ref = str(cluster_ref)
        observers, observer_path = self.fetch_first(
            [
                f"ob/clusters/{cluster_ref}/observers",
                f"ob/clusters/{cluster_ref}/servers",
                f"ob/clusters/{cluster_ref}/observers/list",
            ]
        )
        tenants, tenant_path = self.fetch_first(
            [
                f"ob/clusters/{cluster_ref}/tenants",
                f"ob/clusters/{cluster_ref}/tenant",
            ]
        )
        databases, database_path = self.fetch_first(
            [
                f"ob/clusters/{cluster_ref}/databases",
                f"ob/clusters/{cluster_ref}/tenant/databases",
            ]
        )
        return {
            "observers": extract_items(observers),
            "tenants": extract_items(tenants),
            "databases": extract_items(databases),
            "paths": {
                "observers": observer_path,
                "tenants": tenant_path,
                "databases": database_path,
            },
        }

    def fetch_tenant_databases(self, cluster, tenant):
        cluster_ref = str(pick(cluster, "ocp_cluster_id", "cluster_id", "id", "name") or cluster.get("name"))
        tenant_ref = str(pick(tenant, "ocp_tenant_id", "tenant_id", "id", "name") or tenant.get("name"))
        data, path = self.fetch_first(
            [
                f"ob/clusters/{cluster_ref}/tenants/{tenant_ref}/databases",
                f"ob/clusters/{cluster_ref}/tenants/{tenant_ref}/database",
            ]
        )
        return extract_items(data), path

    def get(self, path):
        ensure_readonly_method(READONLY_METHOD)
        response = self.session.get(self.url(path), verify=self.verify_ssl, timeout=20)
        response.raise_for_status()
        return response.json()

    def url(self, path):
        return urljoin(self.base_url, f"{self.api_prefix}/{path.lstrip('/')}")


def extract_items(payload):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("contents", "items", "list", "clusters"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def ensure_readonly_method(method):
    if method.upper() != "GET":
        raise RuntimeError("拒绝执行非 GET 请求，OCP 接入必须只读")


def normalize_ocp_clusters(items):
    clusters = []
    for item in items:
        name = pick(item, "name", "clusterName", "cluster_name")
        if not name:
            continue
        endpoint = pick(item, "endpoint", "rootService", "rootserver", "serviceUrl", "address") or ""
        clusters.append(
            {
                "name": str(name),
                "environment": pick(item, "environment", "env") or "prod",
                "region": pick(item, "region", "idcName", "zone") or "",
                "endpoint": str(endpoint),
                "port": int(pick(item, "port", "sqlPort") or 2881),
                "sys_user": "root@sys",
                "version": str(pick(item, "version", "obVersion", "clusterVersion") or "4.2.1.8"),
                "status": str(pick(item, "status", "state") or "unknown").lower(),
                "owner": "OCP",
                "remark": f"OCP cluster id: {pick(item, 'id', 'clusterId') or ''}",
                "ocp_cluster_id": pick(item, "id", "clusterId"),
                "_raw": item,
            }
        )
    return clusters


def normalize_ocp_observers(items, cluster_id):
    observers = []
    for item in items:
        ip = pick(item, "svrIp", "serverIp", "ip", "hostIp", "address")
        if not ip:
            continue
        observers.append(
            {
                "cluster_id": cluster_id,
                "zone": pick(item, "zone", "zoneName", "zone_name") or "",
                "svr_ip": str(ip),
                "sql_port": int(pick(item, "sqlPort", "sql_port", "port") or 2881),
                "rpc_port": int(pick(item, "rpcPort", "rpc_port") or 2882),
                "status": str(pick(item, "status", "state") or "unknown").lower(),
                "disk_total_gb": to_number(pick(item, "diskTotalGb", "disk_total_gb", "diskTotal", "totalDisk")),
                "disk_used_gb": to_number(pick(item, "diskUsedGb", "disk_used_gb", "diskUsed", "usedDisk")),
            }
        )
    return observers


def normalize_ocp_tenants(items, cluster_id):
    tenants = []
    for item in items:
        name = pick(item, "name", "tenantName", "tenant_name")
        if not name:
            continue
        tenants.append(
            {
                "cluster_id": cluster_id,
                "name": str(name),
                "tenant_mode": str(pick(item, "mode", "tenantMode", "compatibilityMode") or "MYSQL").upper(),
                "primary_zone": pick(item, "primaryZone", "primary_zone") or "",
                "locality": pick(item, "locality") or "",
                "tenant_role": pick(item, "tenantRole", "tenant_role", "role") or "",
                "unit_num": int(pick(item, "unitNum", "unit_num", "unitCount") or 0),
                "status": str(pick(item, "status", "state") or "unknown").lower(),
                "ocp_tenant_id": pick(item, "id", "tenantId"),
            }
        )
    return tenants


def normalize_ocp_databases(items, tenant_id=None):
    databases = []
    for item in items:
        name = pick(item, "name", "databaseName", "dbName")
        if not name:
            continue
        databases.append(
            {
                "tenant_id": tenant_id,
                "tenant_name": pick(item, "tenantName", "tenant_name"),
                "name": str(name),
                "charset_name": pick(item, "charset", "charsetName", "charset_name") or "",
                "collation_name": pick(item, "collation", "collationName", "collation_name") or "",
                "owner": pick(item, "owner", "creator") or "OCP",
            }
        )
    return databases


def to_number(value):
    if value in (None, ""):
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def pick(source, *keys):
    for key in keys:
        if key in source and source[key] not in (None, ""):
            return source[key]
    return None
