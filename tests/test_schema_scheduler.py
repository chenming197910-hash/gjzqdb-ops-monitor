import unittest,sys
from pathlib import Path
from unittest.mock import Mock,patch
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
from run_tenant_schedules import schedule_due,isolated,collect_one
from schema_migrate import EXTENSION_DDL

class Contracts(unittest.TestCase):
    def test_migration_additive_and_idempotent_error_handling(self):
        self.assertEqual(len(EXTENSION_DDL),9)
        self.assertFalse(any('drop ' in sql.lower() or 'update tenants' in sql.lower() for sql in EXTENSION_DDL))
        for code in [955,1430]:
            with patch.object(app,'execute',side_effect=app.oracledb.DatabaseError(SimpleNamespace(code=code))):
                self.assertTrue(app.execute_ddl(None,'create table example'))
        with patch.object(app,'execute',side_effect=app.oracledb.DatabaseError(SimpleNamespace(code=1031))):
            with self.assertRaises(app.oracledb.DatabaseError):app.execute_ddl(None,'create table example')
    def test_existing_daily_weekly_schedule_rules(self):
        from datetime import datetime
        now=datetime(2026,11,5,8)
        self.assertTrue(schedule_due({'run_time':'07:00','frequency':'daily'},now))
        self.assertFalse(schedule_due({'run_time':'09:00','frequency':'daily'},now))
        self.assertFalse(schedule_due({'run_time':'07:00','last_run_at':'2026-11-05 07:01'},now))
        self.assertTrue(schedule_due({'run_time':'07:00','frequency':'weekly','day_of_week':4},now))
    def test_daily_failure_does_not_rollback_prior_cpu_commit(self):
        db=Mock()
        with patch('run_tenant_schedules.log_event'):
            isolated(db,'cpu',lambda:None)
            isolated(db,'other',lambda:(_ for _ in ()).throw(RuntimeError('test')))
        db.commit.assert_called_once();db.rollback.assert_called_once()
    def test_inactive_scheduled_tenant_never_connects(self):
        with patch('run_tenant_schedules.get_tenant_with_cluster',return_value={'active':0}),patch('run_tenant_schedules.one',return_value={'tenant_password':'x'}),patch('run_tenant_schedules.collect_ob_tenant_detail') as collect:
            collect_one(None,{'tenant_id':7});collect.assert_not_called()
    def test_export_retains_active_scope_and_xlsx_valid(self):
        import zipfile,io
        with patch.object(app,'get_pool',return_value=Mock()),patch.object(app,'list_tenants',return_value=[dict(name='business',backup_overdue=True),dict(name='fine',backup_overdue=False)]):
            response=app.app.test_client().get('/api/tenants/export.xlsx?hide_meta=0&hide_standby=0&risk=backup_overdue')
            self.assertEqual(response.status_code,200)
            z=zipfile.ZipFile(io.BytesIO(response.data));sheet=z.read('xl/worksheets/sheet1.xml').decode()
            self.assertIn('business',sheet);self.assertNotIn('fine',sheet)

if __name__=='__main__':unittest.main()
