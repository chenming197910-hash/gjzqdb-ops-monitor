"""Read-only Oracle schema and tenant-state audit; never executes DDL or DML."""
import json
from app import get_pool,all_rows

REQUIRED={
 'TENANTS':{'ID':'NUMBER','ACTIVE':'NUMBER','CLUSTER_ID':'NUMBER'},
 'CLUSTER_SYS_CPU_SAMPLES':{'ID':'NUMBER','CLUSTER_ID':'NUMBER','SAMPLED_AT':'TIMESTAMP','CPU_USED_CORES':'NUMBER','CPU_LIMIT_CORES':'NUMBER','CPU_USED_PCT':'NUMBER','NODE_COUNT':'NUMBER'},
 'CLUSTERS':{'ID':'NUMBER'},
 'SYS_TENANT_CHECKS':{'ID':'NUMBER','CLUSTER_ID':'NUMBER','STATUS':'VARCHAR2','MESSAGE':'VARCHAR2','CHECKED_AT':'TIMESTAMP'},
 'CLUSTER_MERGE_CHECKS':{'ID':'NUMBER','CLUSTER_ID':'NUMBER','STATUS':'VARCHAR2','MESSAGE':'VARCHAR2','CHECKED_AT':'TIMESTAMP'},
 'TENANT_CPU_COUNTERS':{'CLUSTER_ID':'NUMBER','OB_TENANT_ID':'NUMBER','SVR_IP':'VARCHAR2','SVR_PORT':'NUMBER','CPU_TIME_US':'NUMBER','QUOTA_HUNDREDTHS':'NUMBER','SAMPLED_AT':'TIMESTAMP'},
 'TENANT_CPU_SAMPLES':{'TENANT_ID':'NUMBER','SAMPLED_AT':'TIMESTAMP','CPU_USED_CORES':'NUMBER','CPU_LIMIT_CORES':'NUMBER','CPU_USED_PCT':'NUMBER','NODE_COUNT':'NUMBER'},
 'TENANT_ABSENCE_CHECKS':{'TENANT_ID':'NUMBER','CLUSTER_ID':'NUMBER','FIRST_MISSING_AT':'TIMESTAMP'},
 'CPU_COLLECTION_STATUS':{'CLUSTER_ID':'NUMBER','STATUS':'VARCHAR2','MESSAGE':'VARCHAR2','CHECKED_AT':'TIMESTAMP'},
}

def audit(db):
    columns=all_rows(db,'select table_name,column_name,data_type,nullable,data_default from user_tab_columns')
    lookup={(r['table_name'],r['column_name']):r for r in columns};issues=[]
    for table,fields in REQUIRED.items():
        for field,typ in fields.items():
            actual=lookup.get((table,field))
            if not actual or not actual['data_type'].startswith(typ):issues.append('%s.%s 应为 %s，实际 %s'%(table,field,typ,actual['data_type'] if actual else '缺失'))
    report={'schema_issues':issues,'active_column':lookup.get(('TENANTS','ACTIVE')),'indexes':all_rows(db,"select table_name,index_name,uniqueness,status from user_indexes where table_name in ('TENANT_CPU_SAMPLES','TENANT_CPU_COUNTERS','TENANT_ABSENCE_CHECKS','CPU_COLLECTION_STATUS')"),'constraints':all_rows(db,"select table_name,constraint_name,constraint_type,status from user_constraints where table_name in ('TENANT_CPU_COUNTERS','TENANT_ABSENCE_CHECKS','CPU_COLLECTION_STATUS')")}
    for table in ('TENANT_CPU_COUNTERS','TENANT_ABSENCE_CHECKS','CPU_COLLECTION_STATUS'):
        if not any(r['table_name']==table and r['constraint_type']=='P' and r['status']=='ENABLED' for r in report['constraints']):
            issues.append(table+' 缺失启用的主键约束')
    if ('TENANTS','ACTIVE') in lookup:
        report['counts_by_cluster']=all_rows(db,'''select c.id,c.name,count(t.id) total,
          sum(case when t.active=1 then 1 else 0 end) active,
          sum(case when t.active=0 then 1 else 0 end) inactive,
          sum(case when t.id is not null and (t.active is null or t.active not in (0,1)) then 1 else 0 end) invalid_active
          from clusters c left join tenants t on t.cluster_id=c.id group by c.id,c.name order by c.name''')
        if any(r['invalid_active'] for r in report['counts_by_cluster']):issues.append('ACTIVE 存在 NULL 或非0/1值，需人工核查，禁止批量重置')
        report['inactive_tenants']=all_rows(db,'''select t.id,t.cluster_id,t.name,t.active,t.updated_at,s.enabled schedule_enabled
          from tenants t left join tenant_collect_schedules s on s.tenant_id=t.id
          where t.active=0 order by t.cluster_id,t.name''')
    return report


def main():
    db=get_pool().acquire()
    try:
        result=audit(db);print(json.dumps(result,ensure_ascii=False,indent=2,default=str))
        if result['schema_issues']:raise SystemExit(1)
    finally:db.close()


if __name__=='__main__':main()
