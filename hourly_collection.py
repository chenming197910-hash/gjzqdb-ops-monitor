"""Hourly homepage refresh. Read-only OB queries; Oracle job history is the clock."""
import json
import logging
from app import (all_rows,one,execute,local_now,parse_db_timestamp,record_collection_job,
                 collect_ob_cluster,collect_cluster_assets,run_sys_tenant_checks,collect_due_observer_log_sources,log_event)


def due(db,kind,cluster_id,now):
    row=one(db,'''select max(finished_at) last_at from collection_jobs
      where target_type=:kind and (cluster_id=:cid or (cluster_id is null and :cid is null))''',
      {'kind':kind,'cid':cluster_id})
    last=parse_db_timestamp((row or {}).get('last_at'))
    return last is None or (now-last).total_seconds() >= 3600


def task(db,kind,cluster_id,callback):
    started=local_now()
    if not due(db,kind,cluster_id,started):return 'skipped'
    try:
        result=callback() or {}
        status=result.get('status') or ('warning' if result.get('failed') else 'success')
        if status=='failed':
            # Never commit partial asset writes from a failed collection.
            db.rollback()
        message=json.dumps(result,ensure_ascii=False,default=str).encode('utf-8')[:1000].decode('utf-8',errors='ignore')
    except Exception as exc:
        db.rollback()
        status='failed';message='每小时采集失败，请查看服务器 ob-ops-monitor.log 日志'
        log_event(logging.ERROR,'hourly_collection_failed',task=kind,cluster_id=cluster_id,error=str(exc),exc_info=True)
    record_collection_job(db,cluster_id,kind,status,message,started)
    db.commit()
    return status


def collect_assets(db,cluster):
    data=collect_ob_cluster(cluster)
    stats=collect_cluster_assets(db,cluster['id'],data)
    return {'status':'warning' if stats.get('warnings') else 'success','stats':stats}


def collect_homepage(db):
    summary={'success':0,'warning':0,'failed':0,'skipped':0}
    def count(status):summary[status]=summary.get(status,0)+1
    # Keep connectivity checks independent from asset/backup SQL failures.
    count(task(db,'homepage_sys_hourly',None,lambda:run_sys_tenant_checks(db,force=True)))
    clusters=all_rows(db,'select id,name,endpoint,port,sys_user,sys_password from clusters order by name')
    for cluster in clusters:
        if not cluster.get('sys_password'):
            callback=lambda:{'status':'skipped','message':'未配置 SYS 采集凭据，本轮跳过'}
        else:
            callback=lambda cluster=cluster:collect_assets(db,cluster)
        count(task(db,'homepage_assets_hourly',cluster['id'],callback))
    # Source-level ENABLED remains respected; source polling now runs hourly.
    count(task(db,'homepage_logs_hourly',None,lambda:collect_due_observer_log_sources(db,force=True)))
    return summary
