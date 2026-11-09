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


def collect_connections(nodes, config, directory, now=None):
    names_by_ip = {}
    for node in nodes:
        names_by_ip.setdefault(node['svr_ip'], set()).add(node['cluster_name'].casefold())
    port = int(config.get('metrics_port', 2884))
    if not 1 <= port <= 65535:
        raise ValueError('ODP metrics_port must be in 1..65535')
    limits = config.get('client_max_connections_by_ip', {})
    aliases = config.get('cluster_labels', {})
    aliases = {name.casefold(): label.casefold() for name, label in aliases.items()}
    if len(set(aliases.values())) != len(aliases):
        raise ValueError('ODP cluster labels must not be reused for different clusters')

    def read(ip):
        try:
            return collect_node(ip, port)
        except (requests.RequestException, ValueError):
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        samples = dict(zip(names_by_ip, pool.map(read, names_by_ip)))

    results = {}
    for name in sorted({name for names in names_by_ip.values() for name in names}):
        label = aliases.get(name, name)
        # Include extra known nodes where this cluster is actually present in the metrics.
        ips = {ip for ip, names in names_by_ip.items()
               if name in names or label in (samples[ip] or {})}
        counts = [samples[ip][label] for ip in ips if label in (samples[ip] or {})]
        caps = [limits.get(ip) for ip in ips]
        known_caps = [cap for cap in caps if type(cap) is int and cap > 0]
        shared = any(len(names_by_ip[ip]) > 1 or
                     any(other != label and value > 0 for other, value in (samples[ip] or {}).items())
                     or name not in names_by_ip[ip] for ip in ips)
        results[name] = dict(
            connections=sum(counts) if counts else None,
            nodes=len(ips), sampled_nodes=len(counts),
            reachable_nodes=sum(samples[ip] is not None for ip in ips),
            limit=sum(known_caps) if len(known_caps) == len(ips) else None,
            limit_nodes=len(known_caps), shared=shared,
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
