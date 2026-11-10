"""Conservative soft stop: SYS inventory + two observations at least 30 min apart."""
from datetime import datetime


def reconcile_tenants(db, cluster_id, collected, all_rows, one, execute, now):
    tenants = collected.get('tenants', [])
    names = {t.get('name') for t in tenants if t.get('name')}
    verified = set(collected.get('inventory_names') or [])
    valid = (collected.get('inventory_authoritative') is True and names and names == verified
             and len(names) == len(tenants) and not any(t.get('_error') for t in tenants))
    if not valid:
        # An interrupted or partial inventory breaks consecutive absence evidence.
        execute(db, 'delete from tenant_absence_checks where cluster_id=:cid', {'cid': cluster_id})
        return {'inactive_tenants': 0, 'lifecycle_note': '清单为空、失败或未经SYS完整清单核验：保留全部租户及采集计划'}
    current = all_rows(db, 'select id,name from tenants where cluster_id=:cid and active=1', {'cid': cluster_id})
    stopped = 0
    for tenant in current:
        args = {'tid': tenant['id'], 'cid': cluster_id}
        if tenant['name'] in names:
            execute(db, 'delete from tenant_absence_checks where tenant_id=:tid', {'tid': tenant['id']})
            continue
        previous = one(db, 'select first_missing_at from tenant_absence_checks where tenant_id=:tid', {'tid': tenant['id']})
        if not previous:
            execute(db, '''insert into tenant_absence_checks(tenant_id,cluster_id,first_missing_at)
                           values(:tid,:cid,:now)''', dict(args, now=now))
            continue
        first = previous['first_missing_at']
        if isinstance(first, str): first = datetime.fromisoformat(first)
        if (now-first).total_seconds() < 1800:
            continue
        execute(db, 'update tenants set active=0,updated_at=systimestamp where id=:tid', {'tid': tenant['id']})
        execute(db, 'update tenant_collect_schedules set enabled=0,updated_at=systimestamp where tenant_id=:tid', {'tid': tenant['id']})
        stopped += 1
    return {'inactive_tenants': stopped, 'lifecycle_note': '仅停用经两次完整SYS清单确认缺失至少30分钟的租户；保留历史'}
