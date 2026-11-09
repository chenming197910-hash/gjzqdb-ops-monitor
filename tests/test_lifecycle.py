import unittest
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from unittest.mock import Mock
from datetime import datetime,timedelta
from tenant_lifecycle import reconcile_tenants
from ob_collector import check_ob_major_compaction
from test_sampling import FakeConnection
from unittest.mock import patch

class Lifecycle(unittest.TestCase):
    def check(self, data, previous=None):
        writes=[];now=datetime(2026,11,5,12)
        result=reconcile_tenants(None,1,data,lambda *a:[{'id':7,'name':'gone'},{'id':8,'name':'live'}],lambda *a:previous,lambda db,sql,args:writes.append((sql,args)),now)
        return result,writes
    def data(self):return dict(inventory_authoritative=True,inventory_names=['live'],tenants=[{'name':'live'}])
    def test_first_missing_only_records_candidate(self):
        result,writes=self.check(self.data());self.assertEqual(result['inactive_tenants'],0)
        self.assertFalse(any('update tenants' in sql for sql,_ in writes))
    def test_second_confirmed_absence_after_30min_stops_only_gone(self):
        result,writes=self.check(self.data(),{'first_missing_at':datetime(2026,11,5,11)})
        self.assertEqual(result['inactive_tenants'],1)
        self.assertEqual([p['tid'] for sql,p in writes if 'update tenants' in sql],[7])
        self.assertTrue(any('enabled=0' in sql for sql,_ in writes))
        self.assertFalse(any('delete from tenants' in sql for sql,_ in writes))
    def test_fast_second_observation_does_not_disable(self):
        result,_=self.check(self.data(),{'first_missing_at':datetime(2026,11,5,11,59)})
        self.assertEqual(result['inactive_tenants'],0)
    def test_empty_partial_failed_and_fallback_lists_do_not_disable(self):
        for data in [{},dict(self.data(),tenants=[]),dict(self.data(),inventory_authoritative=False),dict(self.data(),inventory_names=['live','other']),dict(self.data(),tenants=[{'_error':'timeout'}]),dict(self.data(),tenants=[{'name':'live'},{'name':'live'}])]:
            result,writes=self.check(data,{'first_missing_at':datetime(2026,11,5,11)})
            self.assertEqual(result['inactive_tenants'],0)
            self.assertFalse(any('update tenants' in sql for sql,_ in writes))
            self.assertTrue(any('delete from tenant_absence_checks' in sql for sql,_ in writes))
    def test_reappearance_clears_absence(self):
        _,writes=self.check(self.data())
        self.assertTrue(any('delete from tenant_absence_checks where tenant_id' in sql and p['tid']==8 for sql,p in writes))

class Compaction(unittest.TestCase):
    def check(self,rows,hour=13):
        with patch('ob_collector.load_pymysql'),patch('ob_collector.open_ob_connection',return_value=FakeConnection(rows)):
            return check_ob_major_compaction({},datetime(2026,11,5,hour))
    def test_yesterday_outstanding_is_warning_after_deadline(self):
        row=dict(tenant_id=1001,frozen_time=datetime(2026,11,4),last_finish_time=datetime(2026,11,3),frozen_scn=10,last_scn=9,status='MERGING')
        self.assertEqual(self.check([row])['status'],'warning')
        self.assertEqual(self.check([row],8)['status'],'pending')
    def test_today_complete_success(self):
        row=dict(tenant_id=1001,frozen_time=datetime(2026,11,5,1),last_finish_time=datetime(2026,11,5,2),frozen_scn=10,last_scn=10,status='IDLE')
        self.assertEqual(self.check([row])['status'],'success')
    def test_empty_unknown_not_success(self):self.assertEqual(self.check([])['status'],'unknown')

if __name__=='__main__':unittest.main()
