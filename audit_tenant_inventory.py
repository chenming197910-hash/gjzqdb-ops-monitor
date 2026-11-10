"""Read-only Oracle/OB inventory comparison. Reports candidates; changes nothing."""
import json
from app import get_pool,all_rows
from ob_collector import open_ob_connection,load_pymysql,query_first


def main():
    db=get_pool().acquire();report=[]
    try:
        clusters=all_rows(db,'select id,name,endpoint,port,sys_user,sys_password from clusters order by name')
        for c in clusters:
            item={'cluster_id':c['id'],'cluster_name':c['name']}
            try:
                conn=open_ob_connection(load_pymysql(),c)
                try:rows=query_first(conn,['select tenant_id,tenant_name as name from oceanbase.DBA_OB_TENANTS'])
                finally:conn.close()
                if any(r.get('_error') for r in rows) or not any(str(r.get('tenant_id'))=='1' and r.get('name')=='sys' for r in rows):
                    raise RuntimeError('完整SYS租户清单核验失败，不能判断租户删除')
                names={r['name'] for r in rows if r['name']!='sys'}
                assets=all_rows(db,'select id,name,active from tenants where cluster_id=:cid',{'cid':c['id']})
                item['live_count']=len(names);item['asset_count']=len(assets)
                item['recovery_review_candidates']=[r for r in assets if r['active']==0 and r['name'] in names]
                item['absence_review_candidates']=[r for r in assets if r['active']==1 and r['name'] not in names]
                item['new_live_names']=sorted(names-{r['name'] for r in assets})
            except Exception as exc:item['error_type']=type(exc).__name__;item['error']='核验失败；未更改数据。请在服务器安全环境检查连接与权限。'
            report.append(item)
        print(json.dumps(report,ensure_ascii=False,indent=2))
    finally:db.close()


if __name__=='__main__':main()
