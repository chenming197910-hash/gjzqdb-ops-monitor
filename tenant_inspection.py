"""Daily persisted two-password budget, encrypted profiles and parameter drift reports."""
import hashlib,json,os,re
from datetime import datetime,timedelta
from decimal import Decimal,InvalidOperation
from pathlib import Path
from cryptography.fernet import Fernet
from inspection_client import collect_once,validate_rules,AuthenticationFailed,AccountLocked,SessionFailed


def api():
    import app
    return app


def now():return api().local_now()

def write(db,sql,params,clobs=()):
    params=api().bind_params(sql,params)
    with db.cursor() as cur:
        if clobs:cur.setinputsizes(**{k:api().oracledb.DB_TYPE_CLOB for k in clobs if k in params})
        cur.execute(sql,params)


def key(create=False):
    path=Path(os.environ.get('TENANT_INSPECTION_KEY_FILE',api().BASE_DIR/'.tenant_inspection.key'))
    if not path.exists() and create:
        try:
            fd=os.open(str(path),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as f:f.write(Fernet.generate_key())
        except FileExistsError:pass
    if not path.exists() or path.stat().st_mode & 0o077:raise ValueError('巡检密钥缺失或权限不安全，未发起登录')
    return Fernet(path.read_bytes().strip())


def config(db):return api().one(db,'select * from tenant_inspection_config where id=1') or {}

def parse_json(value,default):
    if value is None:return default
    if hasattr(value,'read'):value=value.read()
    return json.loads(value)


def view_config(db):
    row=config(db)
    return dict(enabled=bool(row.get('enabled')),run_time=row.get('run_time') or '02:00',
        oracle_user=row.get('oracle_user') or 'SYS',mysql_user=row.get('mysql_user') or 'root',
        has_primary=bool(row.get('primary_cipher')),has_secondary=bool(row.get('secondary_cipher')),
        rules=parse_json(row.get('baseline_json'),[]),cluster_ids=parse_json(row.get('cluster_ids_json'),[]),
        request_day=row.get('request_day'),last_run_day=row.get('last_run_day'))


def assert_budget_schema(db):
    rows=api().all_rows(db,"""select c.constraint_name,
      listagg(cc.column_name,',') within group(order by cc.position) as columns_list
      from user_constraints c join user_cons_columns cc on cc.constraint_name=c.constraint_name
      and cc.table_name=c.table_name
      where c.table_name='TENANT_INSPECTION_RUNS' and c.constraint_type in ('P','U') and c.status='ENABLED'
      group by c.constraint_name""")
    if not any(set(str(r.get('columns_list','')).split(','))=={'TENANT_IDENTITY','RUN_DAY'} for r in rows):
        raise ValueError('每日登录唯一约束缺失，停止巡检；请先修复数据库结构')


def save_config(db,payload):
    row=api().one(db,'select * from tenant_inspection_config where id=1 for update nowait')
    if not row:raise ValueError('请先执行每日巡检数据库迁移')
    run_time=str(payload.get('run_time',row['run_time']))
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',run_time):raise ValueError('每日时间无效')
    users={mode:str(payload.get(mode+'_user',row.get(mode+'_user') or default)).strip() for mode,default in [('oracle','SYS'),('mysql','root')]}
    if any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,127}',u) for u in users.values()):raise ValueError('用户名仅填写基础账号，不含租户/集群后缀')
    rules=validate_rules(payload.get('rules',parse_json(row.get('baseline_json'),[])))
    cluster_ids=payload.get('cluster_ids',parse_json(row.get('cluster_ids_json'),[]))
    if not isinstance(cluster_ids,list) or len(cluster_ids)>100 or any(type(i)!=int or i<=0 for i in cluster_ids):raise ValueError('集群选择无效')
    primary=row.get('primary_cipher');secondary=row.get('secondary_cipher')
    for name in ('primary_password','secondary_password'):
        value=payload.get(name)
        if value is not None and (not isinstance(value,str) or len(value)>512):raise ValueError('密码格式或长度无效')
    changed=bool(payload.get('primary_password') or payload.get('secondary_password'))
    cipher=key(create=changed) if changed else None
    if payload.get('primary_password'):primary=cipher.encrypt(payload['primary_password'].encode()).decode()
    if payload.get('clear_secondary'):secondary=None
    elif payload.get('secondary_password'):secondary=cipher.encrypt(payload['secondary_password'].encode()).decode()
    if primary and secondary:
        cipher=cipher or key()
        if cipher.decrypt(primary.encode())==cipher.decrypt(secondary.encode()):raise ValueError('首选和次选密码不能相同，以免同一密码重复登录')
    enabled=bool(payload.get('enabled',row.get('enabled')))
    if enabled and not primary:raise ValueError('启用前请设置首选密码')
    if enabled:assert_budget_schema(db)
    write(db,'''update tenant_inspection_config set enabled=:enabled,run_time=:run_time,oracle_user=:oracle_user,
      mysql_user=:mysql_user,primary_cipher=:primary,secondary_cipher=:secondary,baseline_json=:rules,
      cluster_ids_json=:clusters,updated_at=systimestamp where id=1''',
      dict(enabled=int(enabled),run_time=run_time,primary=primary,secondary=secondary,rules=json.dumps(rules,ensure_ascii=False),
           clusters=json.dumps(sorted(set(cluster_ids))),**{k+'_user':v for k,v in users.items()}),('rules','clusters'))
    db.commit()


def managed(db,tenant):
    if tenant.get('active')!=1 or str(tenant.get('name','')).lower()=='sys' or str(tenant.get('name','')).lower().startswith('meta$'):return False
    try:row=config(db)
    except api().oracledb.DatabaseError as exc:
        if getattr(exc.args[0],'code',None)==942:return False
        raise
    identity=target_identity(tenant)
    locked=api().one(db,'select locked,not_before_day from tenant_inspection_locks where tenant_identity=:identity',{'identity':identity}) or {}
    if locked.get('locked') or (locked.get('not_before_day') and locked['not_before_day']>now().date().isoformat()):return True
    today=api().one(db,'select id from tenant_inspection_runs where tenant_identity=:identity and run_day=:day',{'identity':identity,'day':now().date().isoformat()})
    if today:return True
    unfinished=api().one(db,'''select id from tenant_inspection_runs where tenant_identity=:identity and status='running'
       and (primary_claimed=1 or secondary_claimed=1) fetch first 1 rows only''',{'identity':identity})
    if unfinished:return True
    scope=parse_json(row.get('cluster_ids_json'),[])
    return bool(row.get('primary_cipher')) and (not scope or tenant['cluster_id'] in scope)


def target_identity(tenant):
    return hashlib.sha256((str(tenant.get('cluster_name') or tenant['cluster_id']).casefold()+'\0'+tenant['name'].casefold()).encode()).hexdigest()


def claim_day(db,tenant_id,day,rules,identity):
    try:
        write(db,'''insert into tenant_inspection_runs(tenant_id,tenant_identity,run_day,status,baseline_json,started_at)
             values(:tid,:identity,:day,'running',:rules,systimestamp)''',
             {'tid':tenant_id,'identity':identity,'day':day,'rules':json.dumps(rules,ensure_ascii=False)},('rules',))
        db.commit();return True
    except api().oracledb.DatabaseError as exc:
        db.rollback()
        if getattr(exc.args[0],'code',None)==1:return False
        raise


def claim_slot(db,tid,day,slot):
    column='primary_claimed' if slot==1 else 'secondary_claimed'
    # Caller owns the unique daily record; commit intent BEFORE any source login.
    with db.cursor() as cur:
        cur.execute('update tenant_inspection_runs set '+column+'=1 where tenant_id=:tid and run_day=:day and '+column+'=0',{'tid':tid,'day':day})
        claimed=cur.rowcount==1
    db.commit();return claimed


def finish(db,tid,day,status,message,result=None,slot=None,locked=False,identity=None):
    write(db,'''update tenant_inspection_runs set status=:status,message=:message,used_slot=:slot,
      result_json=:result,finished_at=systimestamp where tenant_id=:tid and run_day=:day''',
      dict(tid=tid,day=day,status=status,message=message,slot=slot,result=json.dumps(result or {},ensure_ascii=False,default=str)),('result',))
    write(db,'''merge into tenant_inspection_locks t using(select :identity target_key from dual) s on(t.tenant_identity=s.target_key)
      when matched then update set tenant_id=:tid,locked=:locked,not_before_day=null,preferred_slot=coalesce(:slot,t.preferred_slot),reason=:reason,updated_at=systimestamp
      when not matched then insert(tenant_identity,tenant_id,locked,preferred_slot,reason,updated_at)
      values(:identity,:tid,:locked,coalesce(:slot,1),:reason,systimestamp)''',
      dict(tid=tid,identity=identity,locked=int(locked),slot=slot,reason=message if locked else None))
    db.commit()


def normal_value(value):
    value=str(value if value is not None else '').strip()
    aliases={'true':'true','on':'true','yes':'true','false':'false','off':'false','no':'false'}
    if value.lower() in aliases:return ('boolean',aliases[value.lower()])
    if re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)',value):return ('number',Decimal(value))
    return ('text',value)


def compare_rules(mode,rules,collected):
    results=[]
    for rule in rules:
        if rule['mode']!=mode:continue
        rows=collected.get('params_'+rule['kind'],[])
        available=[r for r in rows if str(r.get('name','')).lower()==rule['name'].lower()]
        if any(r.get('_error') for r in rows):status='unreadable';actual=[]
        elif not available:status='missing';actual=[]
        else:
            actual=list(dict.fromkeys(str(r.get('value') if r.get('value') is not None else '') for r in available))
            status='match' if all(normal_value(v)==normal_value(rule['expected']) for v in actual) else 'mismatch'
        results.append(dict(rule,status=status,actual=actual))
    return results


def inspect_tenant(db,tenant,profile,passwords,day):
    if now().date().isoformat()!=day:return 'day_changed'
    rules=validate_rules(parse_json(profile.get('baseline_json'),[]))
    identity=target_identity(tenant)
    lock=api().one(db,'select * from tenant_inspection_locks where tenant_identity=:identity',{'identity':identity}) or {}
    def complete(*args,**kwargs):return finish(db,tenant['id'],day,*args,identity=identity,**kwargs)
    if lock.get('locked') or (lock.get('not_before_day') and lock['not_before_day']>day):return 'locked'
    interrupted=api().one(db,'''select run_day from tenant_inspection_runs where tenant_identity=:identity
       and status='running' and (primary_claimed=1 or secondary_claimed=1) and run_day<:day
       order by run_day desc fetch first 1 rows only''',{'identity':identity,'day':day})
    if interrupted:
        finish(db,tenant['id'],interrupted['run_day'],'uncertain','此前登录任务中断，未确认结果；需人工确认后恢复',locked=True,identity=identity)
        return 'uncertain'
    if not claim_day(db,tenant['id'],day,rules,identity):return 'already_attempted'
    preferred=int(lock.get('preferred_slot') or 1)
    order=[preferred,3-preferred]
    user=profile['oracle_user' if tenant['tenant_mode']=='ORACLE' else 'mysql_user']
    cfg=dict(tenant,login_user=api().build_tenant_collect_user(user,tenant))
    auth_failures=[]
    for slot in order:
        password=passwords.get(slot)
        if not password:continue
        if not claim_slot(db,tenant['id'],day,slot):return 'already_attempted'
        if now().date().isoformat()!=day:
            complete('uncertain','任务跨日，已停止后续登录；需人工核查后次日恢复',locked=True)
            return 'uncertain'
        try:collected=collect_once(cfg,password,rules)
        except AuthenticationFailed:
            auth_failures.append(slot);continue
        except AccountLocked:
            complete('locked','源端账号已锁定，停止所有后续登录；需人工确认',locked=True);return 'locked'
        except SessionFailed:
            complete('uncertain','会话/连接未完成，停止本日尝试并锁定，需人工核查',locked=True);return 'uncertain'
        except Exception:
            complete('uncertain','巡检异常，登录结果不确定；停止重试，需人工核查',locked=True);return 'uncertain'
        # Authentication succeeded. Never try another password for SQL/query/storage errors.
        try:
            checks=compare_rules(tenant['tenant_mode'],rules,collected)
            stats=api().store_tenant_detail(db,tenant['id'],collected)
            bad=[r for r in checks if r['status']!='match']
            result={'checks':checks,'stats':stats,'fallback':bool(auth_failures),'next_slot':slot}
            status='warning' if bad or stats.get('warnings') or auth_failures else 'success'
            message=('次选密码登录成功，以后优先使用次选' if slot==2 else '首选密码登录成功')
            if bad:message+='；存在参数不匹配或无法读取项'
            complete(status,message,result,slot=slot);return status
        except Exception:
            db.rollback()
            complete('query_error','登录成功，但采集/保存失败；不会切换密码或重复登录',slot=slot)
            return 'query_error'
    complete('locked','已配置密码全部认证失败；不再自动登录，需确认修正后次日恢复',locked=True)
    return 'locked'


def confirm_unlock(db,tid):
    tenant=api().one(db,'select t.id,t.cluster_id,t.name,c.name as cluster_name from tenants t join clusters c on c.id=t.cluster_id where t.id=:tid',{'tid':tid})
    if not tenant:raise ValueError('租户不存在')
    identity=target_identity(tenant)
    tomorrow=(now().date()+timedelta(days=1)).isoformat()
    # Never remove daily claims. Confirmation cannot reopen today's budget.
    write(db,'''merge into tenant_inspection_locks t using(select :identity target_key from dual) s on(t.tenant_identity=s.target_key)
      when matched then update set locked=0,preferred_slot=1,not_before_day=:day,
        reason='人工确认已修正密码，次日起允许巡检',updated_at=systimestamp
      when not matched then insert(tenant_identity,tenant_id,locked,preferred_slot,not_before_day,reason,updated_at)
        values(:identity,:tid,0,1,:day,'人工确认已修正密码，次日起允许巡检',systimestamp)''',
      {'day':tomorrow,'identity':identity,'tid':tid})
    write(db,'''update tenant_inspection_runs set status='confirmed_interrupted',finished_at=systimestamp
       where tenant_identity=:identity and status='running' ''',{'identity':identity})
    db.commit()
    return tomorrow


def run_daily(db):
    profile=config(db);moment=now();day=moment.date().isoformat()
    requested=profile.get('request_day')==day
    if not profile.get('primary_cipher'):return {'state':'unconfigured'}
    if not requested and (not profile.get('enabled') or moment.strftime('%H:%M')<profile['run_time'] or profile.get('last_run_day')==day):return {'state':'not_due'}
    assert_budget_schema(db)
    cipher=key();passwords={1:cipher.decrypt(profile['primary_cipher'].encode()).decode()}
    if profile.get('secondary_cipher'):passwords[2]=cipher.decrypt(profile['secondary_cipher'].encode()).decode()
    if passwords.get(1)==passwords.get(2):raise ValueError('两个密码相同，停止巡检')
    tenants=api().all_rows(db,'''select t.id,t.name,t.active,t.tenant_mode,t.cluster_id,c.name as cluster_name,c.endpoint,c.port
      from tenants t join clusters c on c.id=t.cluster_id where t.active=1 and t.tenant_mode in ('ORACLE','MYSQL')
      and lower(t.name)<>'sys' and lower(t.name) not like 'meta$%' order by c.name,t.name''')
    scope=parse_json(profile.get('cluster_ids_json'),[]);summary={}
    for tenant in tenants:
        if now().date().isoformat()!=day:
            summary['day_changed']=1;break
        if scope and tenant['cluster_id'] not in scope:continue
        status=inspect_tenant(db,tenant,profile,passwords,day);summary[status]=summary.get(status,0)+1
    write(db,'''update tenant_inspection_config set last_run_day=:day,request_day=case when request_day=:day then null else request_day end where id=1''',{'day':day});db.commit()
    return summary


def register_routes(flask_app):
    from flask import g,jsonify,request,render_template
    from urllib.parse import urlsplit
    @flask_app.before_request
    def inspection_write_origin():
        if request.path.startswith('/api/tenant-inspection/') and request.method=='POST':
            if not request.is_json:return jsonify({'message':'必须使用JSON请求'}),415
            origin=request.headers.get('Origin')
            if origin and urlsplit(origin).netloc.lower()!=request.host.lower():
                return jsonify({'message':'拒绝跨站修改巡检配置'}),403
    @flask_app.route('/tenant-inspection')
    def inspection_page():return render_template('tenant_inspection.html')
    @flask_app.route('/api/tenant-inspection/config',methods=['GET','POST'])
    def inspection_config():
        if request.method=='GET':return jsonify(view_config(g.db))
        try:save_config(g.db,request.get_json(force=True) or {})
        except ValueError as exc:g.db.rollback();return jsonify({'message':str(exc)}),400
        return jsonify({'saved':True})
    @flask_app.route('/api/tenant-inspection/run',methods=['POST'])
    def inspection_request():
        if not config(g.db).get('primary_cipher'):return jsonify({'message':'请先保存首选密码'}),400
        day=now().date().isoformat()
        write(g.db,'update tenant_inspection_config set request_day=:day where id=1',{'day':day});g.db.commit()
        return jsonify({'queued':True,'message':'已提交今日任务；由后台执行，重复点击不会增加登录次数'})
    @flask_app.route('/api/tenant-inspection/results')
    def inspection_results():
        rows=api().all_rows(g.db,'''select t.id,t.cluster_id,t.name,t.tenant_mode,c.name as cluster_name,
           r.run_day,r.status,r.message,r.primary_claimed,r.secondary_claimed,r.used_slot,r.result_json,r.finished_at
           from tenants t join clusters c on c.id=t.cluster_id
           left join (select x.*,row_number() over(partition by tenant_id order by run_day desc,id desc) rn from tenant_inspection_runs x) r
             on r.tenant_id=t.id and r.rn=1
           where t.active=1 and t.tenant_mode in ('ORACLE','MYSQL') and lower(t.name)<>'sys' and lower(t.name) not like 'meta$%'
           order by c.name,t.name''')
        scope=parse_json(config(g.db).get('cluster_ids_json'),[])
        if scope:
            ids={t['id'] for t in api().all_rows(g.db,'select id,cluster_id from tenants') if t['cluster_id'] in scope}
            rows=[r for r in rows if r['id'] in ids]
        locks={row['tenant_identity']:row for row in api().all_rows(g.db,'select * from tenant_inspection_locks')}
        for row in rows:
            lock=locks.get(target_identity(row),{})
            row.update({k:lock.get(k) for k in ('locked','preferred_slot','not_before_day','reason')})
            row['result']=parse_json(row.pop('result_json',None),{})
        return jsonify(rows)
    @flask_app.route('/api/tenant-inspection/<int:tid>/confirm',methods=['POST'])
    def inspection_confirm(tid):
        if (request.get_json(force=True) or {}).get('password_corrected') is not True:return jsonify({'message':'必须明确确认已修正密码'}),400
        return jsonify({'resume_day':confirm_unlock(g.db,tid)})
