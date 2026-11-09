"""Strict single-login adapter. No pools, probes, reconnects or per-query connections."""
import os,re,shutil,subprocess,tempfile,uuid
from ob_collector import (load_pymysql,split_endpoint,query_first,ensure_readonly_select,
    parse_tabular_output,TENANT_TOP_TABLE_QUERIES,TENANT_RUNTIME_QUERIES,
    ORACLE_TENANT_TOP_TABLE_QUERIES,ORACLE_TENANT_RUNTIME_QUERIES)

class AuthenticationFailed(Exception):pass
class AccountLocked(Exception):pass
class SessionFailed(Exception):pass


def validate_rules(rules):
    if not isinstance(rules,list) or len(rules)>200:raise ValueError('参数基线最多200条')
    seen=set();clean=[]
    for row in rules:
        if not isinstance(row,dict):raise ValueError('参数基线格式无效')
        mode=str(row.get('mode','')).upper();kind=row.get('kind','parameter' if mode=='ORACLE' else 'variable')
        name=str(row.get('name','')).strip()
        if mode not in ('ORACLE','MYSQL') or kind not in ('variable','parameter','ob_parameter'):raise ValueError('参数模式或类别无效')
        if (mode=='MYSQL' and kind=='parameter') or (mode=='ORACLE' and kind=='variable'):raise ValueError('参数类别与租户模式不匹配')
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$#]{0,127}',name):raise ValueError('参数名称无效')
        if name.lower() == '_publish_schema_mode':
            kind = 'ob_parameter'
        key=(mode,kind,name.lower())
        if key in seen:raise ValueError('参数名称重复')
        seen.add(key)
        expected=str(row.get('expected',''))
        if len(expected)>2000:raise ValueError('参数值过长')
        clean.append(dict(mode=mode,kind=kind,name=name,expected=expected))
    return clean


def parameter_sql(mode,kind,names,mysql=False):
    # Names were validated above. Values are never interpolated into source SQL.
    if mode=='MYSQL' and kind=='variable':table='information_schema.GLOBAL_VARIABLES';cols='variable_name as name,variable_value as value';field='variable_name'
    elif kind=='ob_parameter':table='SYS.V$OB_PARAMETERS' if mode=='ORACLE' else 'oceanbase.GV$OB_PARAMETERS';cols='name,value';field='name'
    else:table='SYS.V$PARAMETER';cols='name,value';field='name'
    literals=','.join("'%s'"%name.lower() for name in names)
    return 'select %s from %s where lower(%s) in (%s)'%(cols,table,field,literals)


def groups(mode,rules):
    rules=validate_rules(rules)
    result=[('top_objects',ORACLE_TENANT_TOP_TABLE_QUERIES if mode=='ORACLE' else TENANT_TOP_TABLE_QUERIES),
            ('runtime_metrics',ORACLE_TENANT_RUNTIME_QUERIES if mode=='ORACLE' else TENANT_RUNTIME_QUERIES)]
    for kind in ('variable','parameter','ob_parameter'):
        names=[r['name'] for r in rules if r['mode']==mode and r['kind']==kind]
        if names:result.append(('params_'+kind,[parameter_sql(mode,kind,names)]))
    fallback = [r['name'] for r in rules if r['mode'] == mode and r['kind'] in ('parameter', 'variable')]
    if fallback:
        result.append(('params_ob_fallback', [parameter_sql(mode, 'ob_parameter', fallback)]))
    return result


def classify_connect(exc):
    code=exc.args[0] if exc.args else None
    if code in (3118,3952,28000):raise AccountLocked('账号已锁定，停止后续尝试') from None
    if code==1045:raise AuthenticationFailed('账号或密码认证失败') from None
    raise SessionFailed('连接未完成或状态不确定，不尝试其他密码') from None


def collect_mysql(config,password,rules):
    driver=load_pymysql();host,port=split_endpoint(config['endpoint'],config['port'])
    try:
        conn=driver.connect(host=host,port=int(port),user=config['login_user'],password=password,
            charset='utf8mb4',connect_timeout=6,read_timeout=45,write_timeout=15,
            cursorclass=driver.cursors.DictCursor)
    except Exception as exc:classify_connect(exc)
    try:
        return {name:query_first(conn,queries) for name,queries in groups('MYSQL',rules)}
    except Exception:raise SessionFailed('已登录，但会话读取失败；不重连、不切换密码') from None
    finally:conn.close()


def defaults_password(password):
    return '"'+password.replace('\\','\\\\').replace('"','\\"').replace('\n','\\n').replace('\r','\\r')+'"'


def collect_oracle(config,password,rules):
    binary=shutil.which(os.environ.get('OBCLIENT_BIN','obclient'))
    if not binary:raise SessionFailed('未找到obclient；未发起登录')
    token='OBI_'+uuid.uuid4().hex;statements=["select '%s_AUTH' as marker from dual"%token];plans=[]
    index=0
    for name,queries in groups('ORACLE',rules):
        ids=[]
        for sql in queries:
            ensure_readonly_select(sql)
            statements += ["select '%s_BEGIN_%s' as marker from dual"%(token,index),sql.strip().rstrip(';'),"select '%s_END_%s' as marker from dual"%(token,index)]
            ids.append(index);index+=1
        plans.append((name,ids))
    host,port=split_endpoint(config['endpoint'],config['port'])
    path=None
    try:
        with tempfile.NamedTemporaryFile('w',encoding='utf-8',delete=False) as f:
            path=f.name;os.chmod(path,0o600);f.write('[client]\npassword='+defaults_password(password)+'\n')
        # defaults-file replaces other defaults; skip-reconnect forbids implicit re-login.
        args=[binary,'--defaults-file='+path,'--skip-reconnect','--protocol=TCP','--connect-timeout=6',
              '-h',host,'-P',str(int(port)),'-u',config['login_user'],'-A','-B','--raw','--force']
        try:run=subprocess.run(args,input=';\n'.join(statements)+';\n',capture_output=True,text=True,timeout=180)
        except Exception:raise SessionFailed('客户端运行中断，登录状态不确定；本次不再尝试') from None
    finally:
        if path:
            try:os.unlink(path)
            except OSError:pass
    output=run.stdout or '';error=run.stderr or ''
    if token+'_AUTH' not in output.splitlines():
        if re.search(r'(?:ERROR\s+(?:3118|3952|28000)\b|ORA-28000)',error,re.I):raise AccountLocked('账号已锁定，停止后续尝试')
        if re.search(r'(?:ERROR\s+1045\b|ORA-01017)',error,re.I):raise AuthenticationFailed('账号或密码认证失败')
        raise SessionFailed('未确认登录结果，不重试、不切换密码')
    # After authentication, even SQL/client errors must not cause a new login.
    result = {}
    lines = output.splitlines()
    for name, ids in plans:
        empty_verified = False
        malformed = False
        for index in ids:
            begin = f'{token}_BEGIN_{index}'
            end = f'{token}_END_{index}'
            if begin not in lines or end not in lines:
                continue
            start, finish = lines.index(begin) + 1, lines.index(end)
            if finish <= start or lines[finish - 1].strip().casefold() != 'marker':
                malformed = True
                continue
            content = lines[start:finish - 1]
            if not content:
                continue
            fields = [field.strip().casefold() for field in content[0].split('\t')]
            required = ({'database_name', 'table_name', 'total_gb'} if name == 'top_objects'
                        else {'current_processes'} if name == 'runtime_metrics' else {'name', 'value'})
            if not required.issubset(fields) or any(len(row.split('\t')) != len(fields) for row in content[1:]):
                malformed = True
                continue
            rows = parse_tabular_output('\n'.join(content))
            if name == 'top_objects' and any(not str(row.get('table_name') or '').strip() for row in rows):
                malformed = True
                continue
            if rows:
                result[name] = rows
                break
            empty_verified = True
        else:
            if malformed:
                result[name] = [{'_error': 'obclient 返回结构异常，整组数据未入库'}]
            elif empty_verified:
                result[name] = []
            else:
                result[name] = [{'_error': '未取得可用结果，请核查视图权限/会话状态'}]
    return result


def collect_once(config,password,rules):
    if config['tenant_mode']=='ORACLE':return collect_oracle(config,password,rules)
    return collect_mysql(config,password,rules)
