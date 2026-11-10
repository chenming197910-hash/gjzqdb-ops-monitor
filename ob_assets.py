"""Internal OceanBase connection directory."""

from shlex import quote

def asset_directory(tenant_rows, clusters=()):
    by_cluster = {}
    for row in tenant_rows:
        name = row['tenant_name']
        mode = (row['tenant_mode'] or '').upper()
        if name.casefold() == 'sys' or name.casefold().startswith('meta$') or mode not in ('MYSQL', 'ORACLE'):
            continue
        by_cluster.setdefault(row['cluster_name'].casefold(), []).append({
            'name': name, 'mode': mode, 'role': row['tenant_role'] or '—',
            'user': 'sys' if mode == 'ORACLE' else 'root',
        })

    result = []
    for cluster in clusters:
        name = cluster['name']
        hosts = cluster['hosts']
        lb = cluster.get('lb')
        purpose = cluster.get('purpose', '')
        oblb = cluster.get('oblb')
        endpoint = lb or cluster.get('endpoint') or hosts
        port = int(cluster.get('port', 2883 if lb else 2881))
        tenants = [dict(name='sys', mode='MYSQL', role='系统租户', user='root')]
        tenants.extend(by_cluster.get(name.casefold(), []))
        for tenant in tenants:
            login = f"{tenant['user']}@{tenant['name']}"
            if lb:
                login += f'#{name}'
            tenant['command'] = f"obclient -h {quote(endpoint)} -P {port} -u {quote(login)} -p"
        result.append(dict(name=name, hosts=hosts, lb=lb, purpose=purpose, oblb=oblb, tenants=tenants))
    return result
