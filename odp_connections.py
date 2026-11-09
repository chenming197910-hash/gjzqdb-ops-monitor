"""Collect node-local ODP client session metrics from co-located OBServer nodes."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import ipaddress
import json
import math
from pathlib import Path
import re
import tempfile

import requests

from ob_collector import load_pymysql

SAMPLE = re.compile(r'^odp_current_session\{(?P<labels>.*)\}\s+(?P<value>\S+)(?:\s+\d+)?$')
LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')
SNAPSHOT = 'odp_connections.json'


def parse_client_sessions(payload):
    totals = {}
    seen = set()
    for line in payload.splitlines():
        match = SAMPLE.fullmatch(line)
        if not match:
            continue
        labels = dict(LABEL.findall(match['labels']))
        if labels.get('sessionType') != 'client':
            continue
        cluster = labels.get('cluster', '').casefold()
        value = float(match['value'])
        if not cluster or not math.isfinite(value) or value < 0 or not value.is_integer():
            raise ValueError('ODP client metric has an invalid cluster or value')
        key = tuple(sorted(labels.items()))
        if key in seen:
            raise ValueError('ODP client metric has duplicate series')
        seen.add(key)
        totals[cluster] = totals.get(cluster, 0) + int(value)
    return totals


def collect_node(ip, port):
    address = ipaddress.ip_address(ip)
    host = f'[{address}]' if address.version == 6 else str(address)
    with requests.Session() as session:
        session.trust_env = False
        with session.get(f'http://{host}:{port}/metrics', timeout=(2, 3),
                         allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise ValueError('ODP metrics returned an unexpected HTTP status')
            body = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                body.extend(chunk)
                if len(body) > 4 * 1024 * 1024:
                    raise ValueError('ODP metrics exceeds response size limit')
    return parse_client_sessions(body.decode('utf-8'))


def query_client_limit(ip, port, node):
    """Read the configuration of one ODP instance through its own SQL endpoint."""
    user = str(node.get('sys_user') or 'root@sys').strip()
    if '#' not in user and ':' not in user:
        if '@' not in user:
            user += '@sys'
        user += '#' + node['cluster_name']
    driver = load_pymysql()
    conn = driver.connect(host=str(ipaddress.ip_address(ip)), port=port, user=user,
                          password=node['sys_password'], charset='utf8mb4',
                          connect_timeout=2, read_timeout=3, write_timeout=3)
    try:
        with conn.cursor() as cursor:
            cursor.execute("SHOW PROXYCONFIG LIKE 'client_max_connections'")
            rows = cursor.fetchall()
            if len(rows) != 1 or str(rows[0][0]).casefold() != 'client_max_connections':
                raise ValueError('ODP did not return client_max_connections')
            value = int(rows[0][1])
            if value < 0:
                raise ValueError('ODP client_max_connections is invalid')
            return value
    finally:
        conn.close()


def collect_connections(nodes, config, directory, now=None):
    nodes_by_ip = {}
    for node in nodes:
        nodes_by_ip.setdefault(node['svr_ip'], {})[node['cluster_name'].casefold()] = node
    names_by_ip = {ip: set(owners) for ip, owners in nodes_by_ip.items()}
    port = int(config.get('metrics_port', 2884))
    if not 1 <= port <= 65535:
        raise ValueError('ODP metrics_port must be in 1..65535')
    sql_ports = config.get('sql_port_by_ip', {})
    default_sql_port = config.get('sql_port', 2883)
    aliases = config.get('cluster_labels', {})
    aliases = {name.casefold(): label.casefold() for name, label in aliases.items()}
    if len(set(aliases.values())) != len(aliases):
        raise ValueError('ODP cluster labels must not be reused for different clusters')

    def read(ip):
        try:
            sample = collect_node(ip, port)
        except (requests.RequestException, ValueError):
            return None, None, 'metrics_unavailable'
        # Use the credentials of the node's matching cluster once, without trying other passwords.
        owners = [node for name, node in nodes_by_ip[ip].items()
                  if aliases.get(name, name) in sample and node.get('sys_password')]
        if len(owners) != 1:
            return sample, None, 'no_unique_sys_credentials'
        try:
            sql_port = int(sql_ports.get(ip, default_sql_port))
            if not 1 <= sql_port <= 65535:
                raise ValueError('invalid SQL port')
            return sample, query_client_limit(ip, sql_port, owners[0]), None
        except (load_pymysql().MySQLError, OSError, ValueError, TypeError) as exc:
            # Persist only numeric codes, never driver messages or credentials.
            code = exc.args[0] if exc.args else None
            return sample, None, f'sql_{code}' if isinstance(code, int) else 'sql_unavailable'

    with ThreadPoolExecutor(max_workers=8) as pool:
        readings = dict(zip(names_by_ip, pool.map(read, names_by_ip)))
    samples = {ip: reading[0] for ip, reading in readings.items()}

    results = {}
    for name in sorted({name for names in names_by_ip.values() for name in names}):
        label = aliases.get(name, name)
        # Include extra known nodes where this cluster is actually present in the metrics.
        ips = {ip for ip, names in names_by_ip.items()
               if name in names or label in (samples[ip] or {})}
        counts = [samples[ip][label] for ip in ips if label in (samples[ip] or {})]
        known_caps = [readings[ip][1] for ip in ips if readings[ip][1] is not None]
        shared = any(len(names_by_ip[ip]) > 1 or
                     any(other != label and value > 0 for other, value in (samples[ip] or {}).items())
                     or name not in names_by_ip[ip] for ip in ips)
        results[name] = dict(
            connections=sum(counts) if counts else None,
            nodes=len(ips), sampled_nodes=len(counts),
            reachable_nodes=sum(samples[ip] is not None for ip in ips),
            limit=sum(known_caps) if len(known_caps) == len(ips) else None,
            limit_nodes=len(known_caps), shared=shared,
            limit_failures={ip: readings[ip][2] for ip in sorted(ips) if readings[ip][2]},
        )
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    snapshot = dict(sampled_at=(now or datetime.now()).strftime('%Y-%m-%d %H:%M:%S'), clusters=results)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory, delete=False) as fp:
        tmp = Path(fp.name)
        try:
            json.dump(snapshot, fp, ensure_ascii=False)
            fp.flush()
            tmp.replace(directory / SNAPSHOT)
        finally:
            if tmp.exists():
                tmp.unlink()
    return snapshot


def read_snapshot(directory):
    try:
        return json.loads((Path(directory) / SNAPSHOT).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
