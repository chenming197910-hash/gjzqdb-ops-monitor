from urllib.parse import urljoin

import requests


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

    def get(self, path):
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
            }
        )
    return clusters


def pick(source, *keys):
    for key in keys:
        if key in source and source[key] not in (None, ""):
            return source[key]
    return None
