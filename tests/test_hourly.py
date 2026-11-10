import unittest,sys
from pathlib import Path
from datetime import datetime,timedelta
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
import hourly_collection as hourly

class Hourly(unittest.TestCase):
    def test_hourly_boundary_and_first_run(self):
        now=datetime(2026,11,6,1)
        for minutes,expected in [(59,False),(60,True),(61,True)]:
            with patch.object(hourly,'one',return_value={'last_at':now-timedelta(minutes=minutes)}):
                self.assertEqual(hourly.due(None,'test',1,now),expected)
        with patch.object(hourly,'one',return_value={'last_at':None}):self.assertTrue(hourly.due(None,'test',1,now))
    def test_recent_attempt_skips_callback(self):
        callback=Mock();db=Mock()
        with patch.object(hourly,'due',return_value=False):self.assertEqual(hourly.task(db,'x',1,callback),'skipped')
        callback.assert_not_called();db.commit.assert_not_called()
    def test_target_failure_rollback_and_job_record(self):
        db=Mock()
        with patch.object(hourly,'due',return_value=True),patch.object(hourly,'record_collection_job') as record,patch.object(hourly,'log_event'):
            status=hourly.task(db,'x',1,Mock(side_effect=RuntimeError('source timeout')))
        self.assertEqual(status,'failed');db.rollback.assert_called_once();db.commit.assert_called_once()
        self.assertEqual(record.call_args[0][3],'failed')
    def test_other_cluster_logs_and_sys_continue_after_failure(self):
        db=Mock();clusters=[{'id':1,'sys_password':'x'},{'id':2,'sys_password':'x'}]
        with patch.object(hourly,'due',return_value=True),patch.object(hourly,'all_rows',return_value=clusters),patch.object(hourly,'record_collection_job'),patch.object(hourly,'log_event'),patch.object(hourly,'run_sys_tenant_checks',return_value={'failed':0}) as syscheck,patch.object(hourly,'collect_assets',side_effect=[RuntimeError('bad'),{'status':'success'}]) as assets,patch.object(hourly,'collect_due_observer_log_sources',return_value={'failed':0}) as logs:
            result=hourly.collect_homepage(db)
        self.assertEqual(result['failed'],1);self.assertEqual(result['success'],3)
        self.assertEqual(assets.call_count,2);syscheck.assert_called_once_with(db,force=True);logs.assert_called_once_with(db,force=True)
    def test_missing_credentials_skips_asset_connection(self):
        with patch.object(hourly,'due',return_value=True),patch.object(hourly,'all_rows',return_value=[{'id':1,'sys_password':None}]),patch.object(hourly,'record_collection_job'),patch.object(hourly,'run_sys_tenant_checks',return_value={}),patch.object(hourly,'collect_due_observer_log_sources',return_value={}),patch.object(hourly,'collect_assets') as assets:
            result=hourly.collect_homepage(Mock())
        assets.assert_not_called();self.assertEqual(result['skipped'],1)
    def test_status_warning_keeps_valid_connection_checks(self):
        db=Mock()
        with patch.object(hourly,'due',return_value=True),patch.object(hourly,'record_collection_job'):
            status=hourly.task(db,'sys',None,lambda:{'failed':1,'success':4})
        self.assertEqual(status,'warning');db.rollback.assert_not_called();db.commit.assert_called_once()
    def test_cpu_list_excludes_inactive_and_keeps_sys(self):
        with patch.object(app,'get_pool',return_value=Mock()),patch.object(app,'all_rows',return_value=[]) as fetch:
            response=app.app.test_client().get('/api/cpu-history/tenants')
        self.assertEqual(response.status_code,200)
        self.assertIn('t.active=1',fetch.call_args[0][1]);self.assertIn("'sys' as name",fetch.call_args[0][1])
    def test_optional_backup_failure_preserves_existing_timestamp(self):
        data={'tenants':[{'tenant_id':1002,'name':'business','status':'normal'}],'tenant_backups':[{'_error':'view privilege missing'}]}
        with patch.object(app,'one',return_value={'last_full_backup_time':'2026-11-05 23:00:00'}),patch.object(app,'upsert_tenant',return_value=7) as upsert,patch.object(app,'execute'),patch('tenant_lifecycle.reconcile_tenants',return_value={}):
            app.collect_cluster_assets(None,1,data)
        self.assertEqual(upsert.call_args[0][1]['last_full_backup_time'],'2026-11-05 23:00:00')

if __name__=='__main__':unittest.main()
