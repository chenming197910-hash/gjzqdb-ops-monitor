"""Read-only OceanBase CPU sampling with Oracle persistence (OB 4.2.x).

Each OBServer's 140013 value is cumulative CPU microseconds. 140005 is
CPU quota in hundredths of a core. Interval utilisation is derived from
counter differences; no instantaneous values are treated as averages.
"""
from datetime import datetime, timedelta
import logging

from ob_collector import load_pymysql, open_ob_connection, query_first, TENANT_QUERIES

CPU_SQL = """
select con_id as tenant_id, svr_ip, svr_port, stat_id, value
from oceanbase.GV$SYSSTAT
where stat_id in (140013, 140005) and (con_id > 1000 or con_id = 1)
"""


def sample_cluster_cpu(db, cluster, all_rows, one, execute, now=None):
    conn = open_ob_connection(load_pymysql(), cluster)
    try:
        with conn.cursor() as cursor:
            cursor.execute(CPU_SQL)
            metrics = cursor.fetchall()
        tenant_rows = query_first(conn, TENANT_QUERIES)
    finally:
        conn.close()
    if not metrics or any(row.get('_error') for row in tenant_rows):
        raise RuntimeError('CPU统计或租户清单为空，未保存不完整样本')
    tenant_names = {str(t.get('tenant_id')): t.get('name') or t.get('tenant_name') for t in tenant_rows}
    # SYS is represented by -cluster_id in the history API, never as a business asset.
    tenant_names['1'] = 'sys'
    tenant_db_ids = {str(r['name']): r['id'] for r in all_rows(db, 'select id, name from tenants where cluster_id = :cid and active = 1', {'cid': cluster['id']})}
    tenant_db_ids['sys'] = -int(cluster['id'])
    observed = {}
    for row in metrics:
        tid = str(row['tenant_id'])
        key = (tid, str(row['svr_ip']), int(row['svr_port']))
        observed.setdefault(key, {})[int(row['stat_id'])] = int(row['value'])
    now = now or datetime.now()
    expected = {}
    for tid, ip, port in observed:
        tenant_id = tenant_db_ids.get(tenant_names.get(tid))
        if tenant_id: expected[tenant_id] = expected.get(tenant_id, 0) + 1
    totals = {}
    counters_saved = 0
    for (tid, ip, port), vals in observed.items():
        name = tenant_names.get(tid)
        tenant_id = tenant_db_ids.get(name)
        if not tenant_id or 140013 not in vals or 140005 not in vals:
            continue
        cpu_us, quota = vals[140013], vals[140005]
        if cpu_us < 0 or quota <= 0: continue
        previous = one(db, """
            select cpu_time_us, quota_hundredths, sampled_at
            from tenant_cpu_counters
            where cluster_id=:cid and ob_tenant_id=:otid and svr_ip=:ip and svr_port=:port
            """, {'cid':cluster['id'], 'otid':int(tid), 'ip':ip, 'port':port})
        # Update only after successfully reading the entire set of OB metrics.
        if previous:
            previous_at = previous['sampled_at']
            if isinstance(previous_at, str):
                previous_at = datetime.fromisoformat(previous_at[:26])
            elapsed = (now - previous_at).total_seconds()
            delta = cpu_us - int(previous['cpu_time_us'])
            if 30 <= elapsed <= 900 and delta >= 0 and quota == int(previous['quota_hundredths']):
                item = totals.setdefault(tenant_id, {'cores':0, 'quota':0, 'nodes':0})
                item['cores'] += delta / 1000000.0 / elapsed
                item['quota'] += quota / 100.0
                item['nodes'] += 1
        args = {'cid':cluster['id'], 'otid':int(tid), 'ip':ip, 'port':port,
                'cpu':cpu_us, 'quota':quota, 'now':now}
        execute(db, """
            merge into tenant_cpu_counters t
            using (select :cid cid, :otid otid, :ip ip, :port port from dual) s
            on (t.cluster_id=s.cid and t.ob_tenant_id=s.otid and t.svr_ip=s.ip and t.svr_port=s.port)
            when matched then update set cpu_time_us=:cpu, quota_hundredths=:quota, sampled_at=:now
            when not matched then insert(cluster_id,ob_tenant_id,svr_ip,svr_port,cpu_time_us,quota_hundredths,sampled_at)
            values (:cid,:otid,:ip,:port,:cpu,:quota,:now)
        """, args)
        counters_saved += 1
    totals = {tid:item for tid,item in totals.items() if item['nodes'] == expected.get(tid)}
    for tid, item in totals.items():
        # Sum each server's rate; different previous timestamps must not bias cores.
        if item['quota'] <= 0: continue
        cores = item['cores']
        used_pct = round(100 * cores / item['quota'], 3)
        params = {'now':now,'used':round(cores,4),'lim':item['quota'],'pct':used_pct,'cnt':item['nodes']}
        if tid < 0:
            execute(db, """
              insert into cluster_sys_cpu_samples(cluster_id,sampled_at,cpu_used_cores,cpu_limit_cores,cpu_used_pct,node_count)
              values(:cid,:now,:used,:lim,:pct,:cnt)
            """, dict(params,cid=-tid))
        else:
            execute(db, """
              insert into tenant_cpu_samples(tenant_id,sampled_at,cpu_used_cores,cpu_limit_cores,cpu_used_pct,node_count)
              values(:tid,:now,:used,:lim,:pct,:cnt)
            """, dict(params,tid=tid))
    if not counters_saved:
        raise RuntimeError('无可匹配的有效CPU计数器；请核验ACTIVE租户、权限及GV$SYSSTAT字段')
    return {'counter_nodes':counters_saved, 'samples':len(totals)}


def record_cpu_status(db, execute, cluster_id, status, message):
    execute(db, """merge into cpu_collection_status t using(select :cid cid from dual) s
      on(t.cluster_id=s.cid)
      when matched then update set status=:status,message=:message,checked_at=systimestamp
      when not matched then insert(cluster_id,status,message,checked_at)
      values(:cid,:status,:message,systimestamp)""",
      {'cid':cluster_id,'status':status,'message':str(message)[:250]})


def run_cpu_sampling(db, all_rows, one, execute, log_event, *, force=False):
    result = {'checked':0,'success':0,'failed':0,'details':[]}
    clusters = all_rows(db, 'select id,name,endpoint,port,sys_user,sys_password from clusters order by name')
    for cluster in clusters:
        execute(db, 'savepoint cpu_cluster')
        try:
            # Distributed exclusion; transaction releases this lock after this cluster.
            one(db, 'select id from clusters where id=:cid for update nowait', {'cid':cluster['id']})
            if not cluster.get('sys_password'):
                record_cpu_status(db, execute, cluster['id'], 'unconfigured', '未配置SYS凭据')
                db.commit()
                continue
            last = one(db, 'select max(sampled_at) as last_at from tenant_cpu_counters where cluster_id=:cid', {'cid':cluster['id']})
            latest = (last or {}).get('last_at')
            if isinstance(latest,str): latest=datetime.fromisoformat(latest[:26])
            if not force and latest and 0 <= (datetime.now()-latest).total_seconds()<290:
                db.commit()
                continue
            result['checked']+=1
            counts=sample_cluster_cpu(db,cluster,all_rows,one,execute)
            status = 'success' if counts['samples'] else 'baseline'
            record_cpu_status(db, execute, cluster['id'], status,
                              '有效样本：%s；基线节点：%s' % (counts['samples'], counts['counter_nodes']))
            db.commit()
            result['success']+=1
            result['details'].append({'cluster':cluster['name'],**counts})
        except Exception as exc:
            execute(db, 'rollback to savepoint cpu_cluster')
            # A peer owns this cluster: don't replace its status with a failure.
            if getattr(exc.args[0] if exc.args else None, 'code', None) == 54:
                db.rollback()
                continue
            result['failed']+=1
            result['details'].append({'cluster':cluster['name'],'error':str(exc)[:250]})
            log_event(logging.ERROR,'cpu_sample_failed',cluster_id=cluster['id'],error=str(exc),exc_info=True)
            record_cpu_status(db, execute, cluster['id'], 'failed', str(exc))
            db.commit()
    # No automatic deletion: retention cleanup requires an explicitly approved maintenance step.
    return result


def cpu_history(db, all_rows, tenant_ids, days):
    if not tenant_ids: return []
    params={'cutoff':datetime.now()-timedelta(days=days)}
    placeholders=[]
    for index, tid in enumerate(tenant_ids):
        params[f'id{index}']=tid
        placeholders.append(f':id{index}')
    sources=[]
    if any(tid > 0 for tid in tenant_ids):
        sources.append("""
          select s.tenant_id,t.name as tenant_name,c.name as cluster_name,s.sampled_at,
                 s.cpu_used_cores,s.cpu_limit_cores,s.cpu_used_pct
          from tenant_cpu_samples s join tenants t on t.id=s.tenant_id
          join clusters c on c.id=t.cluster_id
          where s.sampled_at>=:cutoff
        """)
    if any(tid < 0 for tid in tenant_ids):
        sources.append("""
          select -s.cluster_id as tenant_id,'sys' as tenant_name,c.name as cluster_name,s.sampled_at,
                 s.cpu_used_cores,s.cpu_limit_cores,s.cpu_used_pct
          from cluster_sys_cpu_samples s join clusters c on c.id=s.cluster_id
          where s.sampled_at>=:cutoff
        """)
    return all_rows(db, f"""
        select s.tenant_id,s.tenant_name,s.cluster_name,
               to_char(trunc(s.sampled_at,'HH24'),'YYYY-MM-DD HH24:MI') as sample_hour,
               round(avg(s.cpu_used_cores),3) as cpu_used_cores,
               round(avg(s.cpu_limit_cores),3) as cpu_limit_cores,
               round(avg(s.cpu_used_pct),3) as cpu_avg_pct,
               round(max(s.cpu_used_pct),3) as cpu_sampled_peak_pct,
               count(*) as sample_count
        from ({' union all '.join(sources)}) s
        where s.tenant_id in ({','.join(placeholders)})
        group by s.tenant_id,s.tenant_name,s.cluster_name,trunc(s.sampled_at,'HH24')
        order by sample_hour,s.tenant_id
    """,params)
