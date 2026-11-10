import sys,unittest
from pathlib import Path
from datetime import datetime,timedelta
from unittest.mock import patch,Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
from cpu_history import sample_cluster_cpu,cpu_history
from migrate_sys_cpu import STATEMENTS
from test_sampling import FakeConnection

class SysCPU(unittest.TestCase):
    def collect(self,previous):
        rows=[dict(tenant_id=1,svr_ip='test',svr_port=2882,stat_id=stat,value=value) for stat,value in [(140005,400),(140013,320000000)]]
        writes=[]
        with patch('cpu_history.load_pymysql'),patch('cpu_history.open_ob_connection',return_value=FakeConnection(rows)),patch('cpu_history.query_first',return_value=[]):
            result=sample_cluster_cpu(None,{'id':5},lambda *a:[],lambda *a:previous,lambda db,sql,args:writes.append((sql,args)),now=datetime(2026,11,5,12))
        return result,writes
    def test_sys_only_cluster_first_run_stores_baseline(self):
        result,writes=self.collect(None)
        self.assertEqual(result,{'counter_nodes':1,'samples':0})
        self.assertFalse(any('insert into cluster_sys_cpu_samples' in sql for sql,_ in writes))
    def test_sys_second_run_writes_own_table_without_business_asset(self):
        previous=dict(cpu_time_us=200000000,quota_hundredths=400,sampled_at=datetime(2026,11,5,11,55))
        result,writes=self.collect(previous)
        self.assertEqual(result['samples'],1)
        samples=[args for sql,args in writes if 'insert into cluster_sys_cpu_samples' in sql]
        self.assertEqual(samples[0]['cid'],5);self.assertEqual(samples[0]['used'],.4);self.assertEqual(samples[0]['pct'],10)
        self.assertFalse(any('insert into tenant_cpu_samples' in sql or 'insert into tenants' in sql for sql,_ in writes))
    def test_sys_and_business_history_use_separate_sources(self):
        fetch=Mock(return_value=[])
        cpu_history(None,fetch,[7,-5],60)
        sql,params=fetch.call_args[0][1:]
        self.assertIn('cluster_sys_cpu_samples',sql);self.assertIn('tenant_cpu_samples',sql)
        self.assertEqual(params['id0'],7);self.assertEqual(params['id1'],-5)
    def test_business_only_does_not_require_sys_table(self):
        fetch=Mock(return_value=[]);cpu_history(None,fetch,[7],7)
        self.assertNotIn('cluster_sys_cpu_samples',fetch.call_args[0][1])
    def test_api_allows_sys_identifier_and_returns_sys_list(self):
        with patch.object(app,'get_pool',return_value=Mock()),patch.object(app,'all_rows',return_value=[]) as fetch:
            response=app.app.test_client().get('/api/cpu-history?tenants=-5&days=60')
            self.assertEqual(response.status_code,200);self.assertIn('cluster_sys_cpu_samples',fetch.call_args[0][1])
            response=app.app.test_client().get('/api/cpu-history/tenants')
            self.assertEqual(response.status_code,200);self.assertIn("'sys' as name",fetch.call_args[0][1])
    def test_sys_migration_additive_only(self):
        self.assertEqual(len(STATEMENTS),2)
        self.assertTrue(all('create ' in sql.lower() for sql in STATEMENTS))
        self.assertFalse(any('alter table tenants' in sql.lower() for sql in STATEMENTS))

if __name__=='__main__':unittest.main()
