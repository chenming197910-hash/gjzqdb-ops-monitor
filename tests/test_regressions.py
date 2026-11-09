"""Offline contracts: real Flask handlers, DB/OB calls replaced at boundaries."""
import os
import sys
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch, Mock
sys.path.insert(0, os.environ.get('APP_SOURCE', str(Path(__file__).resolve().parents[1])))
import app
from cpu_history import sample_cluster_cpu, run_cpu_sampling
from test_sampling import FakeConnection

class Regressions(unittest.TestCase):
    def test_sys_missing_extension_preserves_five_clusters(self):
        rows=[dict(cluster_id=i,cluster_name='cluster%s'%i) for i in range(5)]
        def fetch(db, sql, params=None):
            if 'cluster_merge_checks' in sql: raise app.oracledb.DatabaseError('ORA-00942')
            return rows
        with patch.object(app,'scalar',return_value=0),patch.object(app,'all_rows',side_effect=fetch):
            result=app.latest_sys_tenant_checks(None)
        self.assertEqual(len(result),5)
        self.assertTrue(all(r['merge_status']=='unknown' for r in result))

    def test_sys_db_failure_is_not_empty_success(self):
        with patch.object(app,'get_pool',return_value=Mock()),patch.object(app,'latest_sys_tenant_checks',side_effect=app.oracledb.DatabaseError('test')):
            response=app.app.test_client().get('/api/sys-tenant-checks')
        self.assertEqual(response.status_code,503)

    def test_risk_summary_only_counts_active_and_marks_missing_backups(self):
        def fetch(db,sql,params=None):
            rows=[dict(id=1,name='business',status='normal',last_full_backup_time=None),dict(id=2,name='inactive',status='normal',last_full_backup_time=None)]
            return rows[:1] if 'active' in sql else rows
        with patch.object(app,'all_rows',side_effect=fetch):
            result=app.tenant_risk_summary(None)
            self.assertEqual(result['tenants_backup_overdue'],1)
            self.assertTrue(app.list_tenants(None)[0]['backup_overdue'])

    def test_meta_business_name_not_hidden_as_meta(self):
        if not hasattr(app,'tenant_risk_flags'): self.fail('shared risk rules missing')
        flags=app.tenant_risk_flags(dict(name='metadata_service'),datetime.now())
        self.assertTrue(flags['backup_overdue'])
        self.assertFalse(app.tenant_risk_flags(dict(name='meta$1001'),datetime.now())['backup_overdue'])

    def test_merge_persist_error_does_not_mark_sys_connection_failed(self):
        cluster=dict(id=1,name='test',sys_password='x')
        with patch.object(app,'all_rows',return_value=[cluster]),patch.object(app,'probe_ob_cluster',return_value={'message':'ok'}),patch.object(app,'record_sys_tenant_check') as record,patch.object(app,'check_ob_major_compaction',return_value={'status':'success','message':'ok'}),patch.object(app,'record_merge_check',side_effect=app.oracledb.DatabaseError('ORA-00942')):
            result=app.run_sys_tenant_checks(None,force=True)
        self.assertEqual(result['success'],1);self.assertEqual(result['failed'],0)
        record.assert_called_once_with(None,1,'success','ok')

    def test_cpu_invalid_request_rejected(self):
        with patch.object(app,'get_pool',return_value=Mock()):
            client=app.app.test_client()
            for url in ['/api/cpu-history?days=1','/api/cpu-history?tenants=0','/api/cpu-history?tenants=x','/api/cpu-history?tenants='+','.join(map(str,range(1,102)))]:
                self.assertEqual(client.get(url).status_code,400)

    def test_cpu_60_days_and_bound_ids(self):
        with patch.object(app,'get_pool',return_value=Mock()),patch.object(app,'all_rows',return_value=[]) as fetch:
            response=app.app.test_client().get('/api/cpu-history?days=60&tenants=7,7,8')
            self.assertEqual(response.status_code,200)
            self.assertEqual(fetch.call_args[0][2]['id0'],7)
            self.assertEqual(fetch.call_args[0][2]['id1'],8)
            self.assertIn('avg(s.cpu_used_cores)',fetch.call_args[0][1])

    def test_cpu_no_automatic_history_deletion(self):
        writes=[]
        with patch('cpu_history.sample_cluster_cpu'):
            run_cpu_sampling(Mock(), lambda *a: [], Mock(), lambda db,sql,params=None:writes.append(sql), Mock())
        self.assertFalse(any('delete' in s.lower() for s in writes))

    def test_partial_cpu_write_rolled_back_then_next_cluster_runs(self):
        db=Mock();writes=[];clusters=[dict(id=i,name=str(i),sys_password='x') for i in (1,2)]
        def one(db,sql,args):return {'last_at':None} if 'max' in sql else {'id':args['cid']}
        def execute(db,sql,args=None):writes.append(sql)
        with patch('cpu_history.sample_cluster_cpu',side_effect=[RuntimeError('node failed'),{'samples':1,'counter_nodes':2}]):
            result=run_cpu_sampling(db,lambda *a:clusters,one,execute,Mock())
        self.assertEqual(result['failed'],1);self.assertEqual(result['success'],1)
        self.assertTrue(any('rollback to savepoint' in s.lower() for s in writes))

    def test_inactive_manual_collect_does_not_connect(self):
        with patch.object(app,'get_pool',return_value=Mock()),patch.object(app,'get_tenant_with_cluster',return_value={'active':0}),patch.object(app,'collect_ob_tenant_detail') as collect:
            response=app.app.test_client().post('/api/tenants/7/collect-detail')
            self.assertEqual(response.status_code,409);collect.assert_not_called()

    def sample(self, previous, rows):
        writes=[];now=datetime(2026,11,5,12)
        class Clock(datetime):
            @classmethod
            def now(cls): return now
        with patch('cpu_history.datetime', Clock),patch('cpu_history.load_pymysql'),patch('cpu_history.open_ob_connection',return_value=FakeConnection(rows)),patch('cpu_history.query_first',return_value=[{'tenant_id':1002,'name':'t1'}]):
            result=sample_cluster_cpu(None,{'id':1},lambda *a:[dict(id=7,name='t1')],lambda db,sql,args:previous[args['ip']],lambda db,sql,args:writes.append((sql,args)))
        return result,[p for sql,p in writes if 'insert into tenant_cpu_samples' in sql]

    def test_counter_reset_quota_change_and_long_gap_only_rebaseline(self):
        now=datetime(2026,11,5,12)
        rows=[dict(tenant_id=1002,svr_ip='a',svr_port=2882,stat_id=i,value=v) for i,v in [(140013,320000000),(140005,400)]]
        for cpu,quota,age in [(400000000,400,300),(200000000,800,300),(200000000,400,1800)]:
            result,samples=self.sample({'a':dict(cpu_time_us=cpu,quota_hundredths=quota,sampled_at=now-timedelta(seconds=age))},rows)
            self.assertEqual(result['counter_nodes'],1);self.assertEqual(samples,[])

    def test_multinode_different_intervals_sums_rates(self):
        now=datetime(2026,11,5,12);rows=[]
        for ip,cpu in [('a',320000000),('b',260000000)]:
            rows += [dict(tenant_id=1002,svr_ip=ip,svr_port=2882,stat_id=i,value=v) for i,v in [(140013,cpu),(140005,400)]]
        prior={ip:dict(cpu_time_us=200000000,quota_hundredths=400,sampled_at=now-timedelta(seconds=age)) for ip,age in [('a',300),('b',120)]}
        result,samples=self.sample(prior,rows)
        self.assertEqual(len(samples),1);self.assertAlmostEqual(samples[0]['used'],.9);self.assertAlmostEqual(samples[0]['pct'],11.25)

if __name__=='__main__': unittest.main()
