import os,sys,tempfile,unittest,json
from pathlib import Path
from datetime import datetime
from unittest.mock import Mock,patch
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
import tenant_inspection as ti
import inspection_client as client
from ob_collector import parse_cluster_version,read_cluster_version

class Inspection(unittest.TestCase):
    tenant=dict(id=7,cluster_id=1,name='business',cluster_name='cluster',active=1,tenant_mode='MYSQL',endpoint='db.example.internal',port=2881)
    profile=dict(oracle_user='SYS',mysql_user='root',baseline_json='[]')
    def setUp(self):
        self.clock=patch.object(ti,'now',return_value=datetime(2026,11,9,12));self.clock.start()
    def tearDown(self):self.clock.stop()
    def run_engine(self,side_effect=None,lock=None,claim=True,stats=None):
        db=Mock();events=[]
        def login(*args):
            events.append('connect')
            if side_effect:
                current=side_effect.pop(0)
                if isinstance(current,Exception):raise current
                return current
            return {'top_objects':[],'runtime_metrics':[]}
        def reserve(*args):events.append('claim');return True
        with patch.object(app,'one',side_effect=[lock or {},None]),patch.object(ti,'claim_day',return_value=claim),patch.object(ti,'claim_slot',side_effect=reserve),patch.object(ti,'finish') as finish,patch.object(ti,'collect_once',side_effect=login) as collect,patch.object(app,'store_tenant_detail',return_value=stats or {'warnings':[]}):
            status=ti.inspect_tenant(db,self.tenant,self.profile,{1:'first-secret',2:'second-secret'},'2026-11-09')
        return status,collect,finish,events
    def test_primary_success_does_not_try_secondary(self):
        status,collect,finish,events=self.run_engine()
        self.assertEqual(collect.call_count,1);self.assertEqual(collect.call_args[0][1],'first-secret');self.assertEqual(events,['claim','connect'])
        self.assertEqual(finish.call_args.kwargs['slot'],1)
    def test_primary_failure_secondary_success_remembers_secondary(self):
        status,collect,finish,events=self.run_engine([client.AuthenticationFailed('bad'),{'top_objects':[],'runtime_metrics':[]}])
        self.assertEqual([c.args[1] for c in collect.call_args_list],['first-secret','second-secret'])
        self.assertEqual(events,['claim','connect','claim','connect']);self.assertEqual(finish.call_args.kwargs['slot'],2)
        self.assertTrue(finish.call_args[0][5]['fallback'])
    def test_remembered_secondary_success_never_tries_primary(self):
        status,collect,finish,_=self.run_engine(lock={'preferred_slot':2})
        self.assertEqual(collect.call_count,1);self.assertEqual(collect.call_args[0][1],'second-secret')
    def test_both_fail_stop_at_two_and_lock(self):
        status,collect,finish,_=self.run_engine([client.AuthenticationFailed('a'),client.AuthenticationFailed('b')])
        self.assertEqual(status,'locked');self.assertEqual(collect.call_count,2);self.assertTrue(finish.call_args.kwargs['locked'])
    def test_locked_next_day_does_not_connect(self):
        status,collect,_,_=self.run_engine(lock={'locked':1});self.assertEqual(status,'locked');collect.assert_not_called()
    def test_confirmed_before_next_day_does_not_connect(self):
        _,collect,_,_=self.run_engine(lock={'not_before_day':'2026-11-10'});collect.assert_not_called()
    def test_duplicate_request_does_not_connect(self):
        _,collect,_,_=self.run_engine(claim=False);collect.assert_not_called()
    def test_unknown_session_or_locked_account_never_tries_secondary(self):
        for error in [client.SessionFailed('uncertain'),client.AccountLocked('locked')]:
            _,collect,finish,_=self.run_engine([error]);self.assertEqual(collect.call_count,1);self.assertTrue(finish.call_args.kwargs['locked'])
    def test_query_failure_after_login_never_tries_other_password(self):
        db=Mock()
        with patch.object(app,'one',side_effect=[{},None]),patch.object(ti,'claim_day',return_value=True),patch.object(ti,'claim_slot',return_value=True),patch.object(ti,'finish'),patch.object(ti,'collect_once',return_value={}) as collect,patch.object(app,'store_tenant_detail',side_effect=RuntimeError('query')):
            status=ti.inspect_tenant(db,self.tenant,self.profile,{1:'a',2:'b'},'2026-11-09')
        self.assertEqual(status,'query_error');collect.assert_called_once();db.rollback.assert_called_once()
    def test_prior_crashed_attempt_blocks_next_day(self):
        with patch.object(app,'one',side_effect=[{}, {'run_day':'2026-11-08'}]),patch.object(ti,'finish') as finish,patch.object(ti,'collect_once') as collect:
            status=ti.inspect_tenant(Mock(),self.tenant,self.profile,{1:'a',2:'b'},'2026-11-09')
        self.assertEqual(status,'uncertain');collect.assert_not_called();self.assertTrue(finish.call_args.kwargs['locked'])
    def test_failed_login_binds_null_slot_as_numeric(self):
        db=Mock()
        with patch.object(ti,'write') as write:
            ti.finish(db,7,'2026-11-09','locked','认证失败',locked=True,identity='identity')
        for call in write.call_args_list:
            self.assertIn('cast(:slot as number)',call.args[1])
            self.assertIsNone(call.args[2]['slot'])
        db.commit.assert_called_once()

    def test_recovers_same_day_claim_even_after_daily_batch_is_closed(self):
        db=Mock()
        profile={'primary_cipher':'cipher','enabled':True,'run_time':'02:00','last_run_day':'2026-11-09'}
        with patch.object(ti,'config',return_value=profile),patch.object(app,'all_rows',return_value=[
            {'tenant_id':7,'tenant_identity':'identity'}]),patch.object(ti,'finish') as finish,patch.object(ti,'collect_once') as login:
            self.assertEqual(ti.run_daily(db),{'state':'not_due'})
        finish.assert_called_once()
        self.assertEqual(finish.call_args.args[3],'uncertain')
        self.assertTrue(finish.call_args.kwargs['locked'])
        login.assert_not_called()

    def test_manual_collection_can_validate_after_confirmation_today(self):
        with patch.object(app,'one',side_effect=[{'locked':0,'not_before_day':'2026-11-10'},{}]),patch.object(ti,'collect_once',return_value={}) as login,patch.object(ti,'set_manual_target'),patch.object(app,'store_tenant_detail',return_value={}):
            result=ti.collect_manual_target(Mock(),self.tenant,self.profile,{1:'secret'},[])
        self.assertEqual(result['status'],'success')
        login.assert_called_once()

    def test_manual_collection_respects_unconfirmed_and_manual_locks(self):
        import hashlib
        profile=dict(self.profile,primary_cipher='cipher')
        credential=hashlib.sha256(json.dumps(['root','cipher',None]).encode()).hexdigest()
        for rows in ([{'locked':1}], [{'locked':0,'not_before_day':'2026-11-10'}, {'blocked':1,'credential_key':credential}]):
            with patch.object(app,'one',side_effect=rows),patch.object(ti,'collect_once') as login:
                self.assertEqual(ti.collect_manual_target(Mock(),self.tenant,profile,{1:'secret'},[])['status'],'blocked')
                login.assert_not_called()

    def test_manual_queue_rejects_cooldown_without_creating_job(self):
        db = Mock()
        with patch.object(app, 'one', side_effect=[{'primary_cipher':'configured'},
                {'status':'complete','in_cooldown':1}]) as query, patch.object(ti, 'write') as write:
            with self.assertRaises(ti.ManualCollectionCooldown):
                ti.queue_manual(db)
        self.assertIn("cast(systimestamp as timestamp) <= coalesce(finished_at, queued_at) + interval '5' minute", query.call_args.args[1])
        write.assert_not_called()
        db.rollback.assert_called_once()

    def test_manual_queue_reuses_active_job_and_accepts_after_cooldown(self):
        for status, expected in [('running',False),('queued',False),('complete',True)]:
            db = Mock()
            with patch.object(app,'one',side_effect=[{'primary_cipher':'configured'},
                    {'status':status,'in_cooldown':0}]),patch.object(ti,'write') as write:
                self.assertIs(ti.queue_manual(db),expected)
            self.assertEqual(write.call_count,int(expected))
            db.commit.assert_called_once()

    def test_midnight_stops_attempts_for_old_inspection_day(self):
        with patch.object(ti,'now',return_value=datetime(2026,11,10)),patch.object(ti,'collect_once') as collect:
            status=ti.inspect_tenant(Mock(),self.tenant,self.profile,{1:'a',2:'b'},'2026-11-09')
        self.assertEqual(status,'day_changed');collect.assert_not_called()

    def test_confirm_sets_tomorrow_and_never_deletes_claims(self):
        with patch.object(app,'one',return_value=self.tenant),patch.object(ti,'now',return_value=datetime(2026,11,9,12)),patch.object(ti,'write') as write:
            self.assertEqual(ti.confirm_unlock(Mock(),7),'2026-11-10')
        self.assertTrue(all('delete ' not in c.args[1].lower() for c in write.call_args_list))
        self.assertIn('not_before_day',write.call_args_list[0].args[1])
    def test_identity_survives_local_asset_id_recreation(self):
        self.assertEqual(ti.target_identity(self.tenant),ti.target_identity(dict(self.tenant,id=999)))
    def test_identity_survives_asset_recreation_and_case_changes(self):
        self.assertEqual(ti.target_identity(self.tenant),ti.target_identity(dict(self.tenant,id=999,cluster_id=999,name='BUSINESS',cluster_name='CLUSTER')))

    def test_claim_is_durable_and_unique_daily(self):
        db=Mock()
        with patch.object(ti,'write') as write:
            self.assertTrue(ti.claim_day(db,7,'2026-11-09',[],'identity'))
        self.assertIn('tenant_identity',write.call_args.args[1]);db.commit.assert_called_once()
        with patch.object(ti,'write',side_effect=app.oracledb.DatabaseError(SimpleNamespace(code=1))):
            self.assertFalse(ti.claim_day(db,7,'2026-11-09',[],'identity'))
    def test_encrypted_key_never_stores_plaintext_and_has_private_mode(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{'TENANT_INSPECTION_KEY_FILE':str(Path(folder)/'key')}):
            cipher=ti.key(create=True);encrypted=cipher.encrypt(b'secret-value')
            self.assertNotIn(b'secret-value',encrypted);self.assertEqual(cipher.decrypt(encrypted),b'secret-value')
            self.assertEqual((Path(folder)/'key').stat().st_mode&0o777,0o600)
    def test_config_read_never_returns_password_cipher(self):
        with patch.object(ti,'config',return_value={'primary_cipher':'cipher','secondary_cipher':'cipher2'}):row=ti.view_config(None)
        self.assertTrue(row['has_primary']);self.assertNotIn('cipher',json.dumps(row))
    def test_confirm_requires_explicit_checkbox(self):
        with patch.object(app,'get_pool',return_value=Mock()),patch.object(ti,'confirm_unlock') as unlock:
            response=app.app.test_client().post('/api/tenant-inspection/7/confirm',json={})
        self.assertEqual(response.status_code,400);unlock.assert_not_called()
    def test_missing_unique_budget_constraint_fails_closed(self):
        with patch.object(app,'all_rows',return_value=[]):
            with self.assertRaises(ValueError):ti.assert_budget_schema(None)
        with patch.object(app,'all_rows',return_value=[{'columns_list':'TENANT_IDENTITY,RUN_DAY'}]):ti.assert_budget_schema(None)

    def test_cross_site_or_non_json_cannot_change_inspection(self):
        with patch.object(app,'get_pool',return_value=Mock()),patch.object(ti,'write') as write:
            c=app.app.test_client()
            self.assertEqual(c.post('/api/tenant-inspection/run',json={},headers={'Origin':'https://other.example'}).status_code,403)
            self.assertEqual(c.post('/api/tenant-inspection/run',data='{}',content_type='text/plain').status_code,415)
        write.assert_not_called()

    def test_managed_old_manual_entry_never_connects(self):
        with patch.object(app,'get_pool',return_value=Mock()),patch.object(app,'get_tenant_with_cluster',return_value=self.tenant),patch.object(ti,'managed',return_value=True),patch.object(app,'probe_ob_tenant_connection') as probe,patch.object(app,'collect_ob_tenant_detail') as collect:
            c=app.app.test_client();self.assertEqual(c.post('/api/tenants/7/connection/test',json={}).status_code,409)
            self.assertEqual(c.post('/api/tenants/7/collect-detail',json={}).status_code,409)
        probe.assert_not_called();collect.assert_not_called()
    def test_rule_results_distinguish_missing_error_mismatch(self):
        rules=client.validate_rules([{'mode':'MYSQL','kind':'variable','name':'max_connections','expected':'1000'}])
        self.assertEqual(ti.compare_rules('MYSQL',rules,{'params_variable':[{'name':'MAX_CONNECTIONS','value':'1000.0'}]})[0]['status'],'match')
        self.assertEqual(ti.compare_rules('MYSQL',rules,{'params_variable':[{'name':'max_connections','value':'900'},{'name':'max_connections','value':'1000'}]})[0]['status'],'mismatch')
        self.assertEqual(ti.compare_rules('MYSQL',rules,{})[0]['status'],'missing')
        self.assertEqual(ti.compare_rules('MYSQL',rules,{'params_variable':[{'_error':'permission'}]})[0]['status'],'unreadable')
    def test_rule_names_reject_sql_injection_and_duplicates(self):
        for rules in [[{'mode':'MYSQL','name':"x');drop table t--"}],[{'mode':'MYSQL','name':'a'},{'mode':'MYSQL','name':'A'}]]:
            with self.assertRaises(ValueError):client.validate_rules(rules)
    def test_obclient_auth_failure_single_process_no_reconnect(self):
        cfg=dict(self.tenant,tenant_mode='ORACLE',login_user='SYS@business#cluster')
        with patch('inspection_client.shutil.which',return_value='/usr/bin/obclient'),patch('inspection_client.subprocess.run',return_value=SimpleNamespace(stdout='',stderr='ERROR 1045: denied',returncode=1)) as run:
            with self.assertRaises(client.AuthenticationFailed):client.collect_oracle(cfg,'private-password',[])
        run.assert_called_once();args=run.call_args.args[0];self.assertIn('--skip-reconnect',args)
        self.assertNotIn('private-password',' '.join(args));self.assertNotIn('private-password',run.call_args.kwargs['input'])
    def test_obclient_reuses_authenticated_session_and_parses_results(self):
        cfg=dict(self.tenant,tenant_mode='ORACLE',login_user='SYS@business#cluster')
        output='marker\nOBI_test_AUTH\nmarker\nOBI_test_BEGIN_0\nDATABASE_NAME\tTABLE_NAME\tTOTAL_GB\nAPP\tORDERS\t12.5\nMARKER\nOBI_test_END_0\n'
        with patch('inspection_client.shutil.which',return_value='/usr/bin/obclient'),patch('inspection_client.uuid.uuid4',return_value=SimpleNamespace(hex='test')),patch('inspection_client.subprocess.run',return_value=SimpleNamespace(stdout=output,stderr='',returncode=0)) as run:
            data=client.collect_oracle(cfg,'private-password',[])
        run.assert_called_once();self.assertEqual(data['top_objects'],[{'database_name':'APP','table_name':'ORDERS','total_gb':'12.5'}])

    def test_oracle_rejects_incomplete_result_without_discarding_individual_rows(self):
        cfg=dict(self.tenant,tenant_mode='ORACLE',login_user='SYS@business#cluster')
        output=('MARKER\nOBI_test_AUTH\nMARKER\nOBI_test_BEGIN_0\n'
                'DATABASE_NAME\tTABLE_NAME\tTOTAL_GB\nAPP\tORDERS\t12.5\nAPP\t\t99\nMARKER\nOBI_test_END_0\n')
        with patch('inspection_client.shutil.which',return_value='/usr/bin/obclient'),patch('inspection_client.uuid.uuid4',return_value=SimpleNamespace(hex='test')),patch('inspection_client.subprocess.run',return_value=SimpleNamespace(stdout=output,stderr='',returncode=0)):
            data=client.collect_oracle(cfg,'private-password',[])
        self.assertIn('_error',data['top_objects'][0])

    def test_incomplete_capacity_preserves_prior_snapshot_but_keeps_process_sample(self):
        data={'top_objects':[{'table_name':'ORDERS','total_gb':12.5},{'total_gb':99}],
              'runtime_metrics':[{'current_processes':3}]}
        with patch.object(app,'insert_returning_id') as insert:
            stats=app.store_tenant_detail(Mock(),7,data)
        self.assertEqual(stats['top_objects'],0)
        self.assertEqual(stats['runtime_metrics'],1)
        self.assertIn('整组未入库',stats['warnings'][0])
        self.assertIn('tenant_runtime_metrics',insert.call_args.args[1])

    def test_mysql_single_connection_even_for_multiple_queries(self):
        conn=Mock();driver=Mock();driver.connect.return_value=conn
        with patch('inspection_client.load_pymysql',return_value=driver),patch('inspection_client.query_first',return_value=[]):client.collect_mysql(dict(self.tenant,login_user='root@business#cluster'),'secret',[])
        driver.connect.assert_called_once();conn.close.assert_called_once()
    def test_real_cluster_build_versions_and_mysql_prefix(self):
        self.assertEqual(parse_cluster_version('4.2.5.6_101000-build'),'4.2.5.6')
        self.assertEqual(parse_cluster_version('5.7.25 OceanBase_CE 4.2.5.6 (build)'),'4.2.5.6')
        with patch('ob_collector.query_first',return_value=[{'build_version':'4.2.5.6_build'},{'build_version':'4.2.1.8_build'}]):
            self.assertEqual(read_cluster_version(None),'4.2.1.8, 4.2.5.6')

if __name__=='__main__':unittest.main()
