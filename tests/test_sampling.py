"""Unit tests without database connectivity. Run: python -m unittest discover -s tests -v"""
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ob_collector import check_ob_major_compaction
from cpu_history import sample_cluster_cpu

class FakeCursor:
    def __init__(self, rows):self.rows=rows
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def execute(self,sql):pass
    def fetchall(self):return self.rows
class FakeConnection:
    def __init__(self, rows):self.rows=rows
    def cursor(self):return FakeCursor(self.rows)
    def close(self):pass

class Tests(unittest.TestCase):
    def test_compaction_error_is_warning(self):
        now=datetime(2026,11,5,13,0)
        rows=[dict(tenant_id=1001,is_error='YES',is_suspended='NO',info='checksum')]
        with patch('ob_collector.load_pymysql',return_value=object()),patch('ob_collector.open_ob_connection',return_value=FakeConnection(rows)):
            self.assertEqual(check_ob_major_compaction({},now)['status'],'warning')
    def test_compaction_no_new_freeze_unknown(self):
        rows=[dict(tenant_id=1001,is_error='NO',is_suspended='NO',frozen_time=datetime(2026,11,4),last_finish_time=datetime(2026,11,4),frozen_scn=9,last_scn=9,status='IDLE')]
        with patch('ob_collector.load_pymysql',return_value=object()),patch('ob_collector.open_ob_connection',return_value=FakeConnection(rows)):
            self.assertEqual(check_ob_major_compaction({},datetime(2026,11,5,13))['status'],'unknown')
    def test_cpu_counter_delta(self):
        rows=[dict(tenant_id=1002,svr_ip='10.0.0.1',svr_port=2882,stat_id=140013,value=320000000),
              dict(tenant_id=1002,svr_ip='10.0.0.1',svr_port=2882,stat_id=140005,value=400)]
        prior={'cpu_time_us':200000000,'quota_hundredths':400,'sampled_at':(datetime.now()-timedelta(seconds=300)).strftime('%Y-%m-%d %H:%M:%S')}
        writes=[]
        def allrows(db,sql,params):return [dict(name='t1',id=7)]
        def one(db,sql,params):return prior
        def execute(db,sql,params):writes.append((sql,params))
        with patch('cpu_history.load_pymysql',return_value=object()),patch('cpu_history.open_ob_connection',return_value=FakeConnection(rows)),patch('cpu_history.query_first',return_value=[{'tenant_id':1002,'name':'t1'}]):
            result=sample_cluster_cpu(None,dict(id=1),allrows,one,execute)
        self.assertEqual(result['samples'],1)
        metric=[p for sql,p in writes if 'insert into tenant_cpu_samples' in sql][0]
        self.assertAlmostEqual(metric['pct'],10.0,delta=0.15)

if __name__=='__main__':unittest.main()
