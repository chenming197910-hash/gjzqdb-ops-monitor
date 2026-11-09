const el=id=>document.getElementById(id),safe=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let resultRows=[],confirmId=null,requestBusy=false;
async function api(path,options={}){const response=await fetch(path,{cache:'no-store',headers:{'Content-Type':'application/json'},...options});const data=await response.json();if(!response.ok)throw Error(data.message||data.error||`HTTP ${response.status}`);return data;}
function readRules(mode,text){return text.split(/\r?\n/).map(x=>x.trim()).filter(Boolean).map(line=>{const at=line.indexOf('=');if(at<=0)throw Error('参数基线请使用 参数名=期望值');let name=line.slice(0,at).trim();let kind=mode==='ORACLE'?'parameter':'variable';if(name.startsWith('ob:')){kind='ob_parameter';name=name.slice(3);}return {mode,kind,name,expected:line.slice(at+1).trim()};});}
function ruleText(rules,mode){return rules.filter(r=>r.mode===mode).map(r=>(r.kind==='ob_parameter'?'ob:':'')+r.name+'='+r.expected).join('\n');}
async function loadConfig(){const [config,clusters]=await Promise.all([api('/api/tenant-inspection/config'),api('/api/clusters')]);el('enabled').checked=config.enabled;el('runTime').value=config.run_time;el('oracleUser').value=config.oracle_user;el('mysqlUser').value=config.mysql_user;el('primaryState').textContent=config.has_primary?'已加密保存，留空保留':'未配置';el('secondaryState').textContent=config.has_secondary?'已加密保存，留空保留':'未配置';el('oracleRules').value=ruleText(config.rules,'ORACLE');el('mysqlRules').value=ruleText(config.rules,'MYSQL');el('clusterChoices').innerHTML=clusters.map(c=>`<label class="inline"><input type="checkbox" value="${Number(c.id)}" ${config.cluster_ids.includes(Number(c.id))?'checked':''}>${safe(c.name)}</label>`).join('');renderBatchStatus(config);}
let manualByTenant = new Map();
function latestResult(row) {
  const manual = manualByTenant.get(Number(row.id));
  if (row.status !== 'running' && manual && manual.time >= (row.finished_at || row.run_day || '')) {
    return {...manual.row, time:manual.time, source:'手工采集'};
  }
  return {...row, time:row.finished_at || row.run_day, source:'定时巡检',
          checks:row.result?.checks, stats:row.result?.stats};
}

const resultLabels = {
  success:'正常', warning:'有提醒', blocked:'已保护', query_error:'入库失败',
  locked:'登录失败，已锁定', uncertain:'状态不确定，已锁定',
  running:'执行中或未确认完成', confirmed_interrupted:'已人工确认，次日恢复',
};

function resultMessage(result) {
  const differences = (result.checks || []).filter(check => check.status !== 'match').map(check => {
    if (check.status === 'missing') return check.name + ' 未返回';
    if (check.status === 'unreadable') return check.name + ' 无法读取';
    return `${check.name} 实际 ${(check.actual || []).join(' / ') || '—'}，期望 ${check.expected}`;
  });
  const details = [...differences, ...(result.stats?.warnings || [])];
  if (result.status === 'warning' && details.length) return details.join('；');
  return [result.message, ...details].filter(Boolean).join('；') || '暂无结果';
}

function renderResults() {
  const latest = resultRows.map(row => ({row, result:latestResult(row)}));
  const visible = el('onlyIssues').checked
    ? latest.filter(({row,result}) => row.locked || result.status !== 'success')
    : latest;
  el('inspectionRows').innerHTML = visible.map(({row,result}) => `<tr class="${row.locked?'inspection-bad':''}">
    <td>${safe(row.cluster_name)} / ${safe(row.name)}</td>
    <td>${safe(row.tenant_mode)}</td>
    <td>${safe(result.time || '—')}<br><small>${safe(result.source)}</small></td>
    <td>${safe(resultLabels[result.status] || '尚未执行')}</td>
    <td><details><summary>定时记录</summary>${safe(row.finished_at || row.run_day || '—')}<br>
      ${safe(resultLabels[row.status] || '尚未执行')}<br>
      首选 ${Number(row.primary_claimed || 0)} 次 / 次选 ${Number(row.secondary_claimed || 0)} 次<br>
      ${safe(row.message || '')}</details></td>
    <td>${Number(row.preferred_slot) === 2 ? '次选优先' : '首选优先'}</td>
    <td>${safe(resultMessage(result))}
      ${row.reason ? '<br>定时保护：' + safe(row.reason) : ''}
      ${row.not_before_day ? '<br>最早恢复：' + safe(row.not_before_day) : ''}
      ${row.locked ? `<br><button data-confirm="${Number(row.id)}">确认密码已修正</button>` : ''}</td>
  </tr>`).join('') || '<tr><td colspan="7">暂无对应业务租户</td></tr>';
const checks=latest.flatMap(({row,result})=>(result.checks||[]).map(check=>({tenant:row,...check})));
const labels={match:'匹配',mismatch:'不匹配',missing:'参数不存在/未返回',unreadable:'无法读取'};
const kinds={variable:'系统变量',parameter:'初始化参数',ob_parameter:'OB配置参数'};
const unavailable=latest.filter(({result})=>!result.checks?.length);
el('parameterCoverage').textContent=`已保存 ${checks.length} 项核对结果；${unavailable.length} 个租户暂无参数结果（可能未配置基线、未执行或采集失败）。`;
el('parameterRows').innerHTML=checks.map(c=>`<tr class="${c.status==='match'?'':'inspection-bad'}"><td>${safe(c.tenant.cluster_name)} / ${safe(c.tenant.name)}</td><td>${safe(c.mode)} / ${safe(kinds[c.kind])}</td><td>${safe(c.name)}</td><td>${safe(c.expected)}</td><td>${safe((c.actual||[]).join(' / ')||'—')}</td><td>${safe(labels[c.status]||c.status)}</td></tr>`).join('')||'<tr><td colspan="6">暂无已保存的参数核对结果。</td></tr>';
el('inspectionRows').querySelectorAll('[data-confirm]').forEach(button=>button.onclick=()=>{confirmId=Number(button.dataset.confirm);const row=resultRows.find(r=>Number(r.id)===confirmId);el('confirmTenant').textContent=`${row.cluster_name} / ${row.name}`;el('passwordCorrected').checked=false;el('confirmResume').disabled=true;el('confirmDialog').showModal();});}
function renderBatchStatus(config) {
  const completed = `最近完成的巡检日：${config.last_run_day || '尚无'}`;
  const todayRows = resultRows.filter(row => row.run_day === config.today);
  const pending = config.request_day === config.today;
  let message = '';
  if (todayRows.some(row => row.status === 'running')) {
    message = '今日任务执行中，或上一次执行中断';
  } else if (pending) {
    message = todayRows.length ? '今日任务处理中' : '已入队，等待后台领取。若超过 2 分钟仍未开始，请检查巡检定时器和服务日志';
  } else if (config.request_day) {
    message = `上次队列日期 ${config.request_day} 已过期，可重新提交今日任务`;
  }
  el('batchStatus').textContent = message ? `${completed} · ${message}` : completed;
}

function renderManualCollection(job) {
  const states = {queued:'等待后台执行',running:'正在采集',complete:'采集批次已结束',failed:'批次执行失败',interrupted:'批次中断'};
  const labels = {match:'匹配',mismatch:'不匹配',missing:'未返回',unreadable:'无法读取'};
  el('manualStatus').textContent = job.id
    ? `#${job.id} · ${states[job.status] || job.status} · ${job.finished_at || job.queued_at}`
    : '暂无手工采集';
  el('manualRows').innerHTML = (job.results || []).map(row => `<tr>
    <td>${safe(row.cluster_name)} / ${safe(row.name)}</td>
    <td>${safe(({success:'正常',warning:'有提醒',blocked:'已保护',query_error:'入库失败'})[row.status] || row.status)}</td>
    <td>${safe(resultMessage(row))}</td>
    <td>${(row.checks || []).map(check => `${safe(check.name)} · 期望 ${safe(check.expected)} · 实际 ${safe(check.actual.join(' / ') || '—')} · ${safe(labels[check.status])}`).join('<br>') || '—'}</td>
  </tr>`).join('');
}

async function refreshResults() {
  const [rows, config, rankings, manual] = await Promise.all([
    api('/api/tenant-inspection/results'),
    api('/api/tenant-inspection/config'),
    api('/api/tenants/inspection-rankings').catch(() => null),
    api('/api/tenant-inspection/manual'),
  ]);
  manualByTenant = new Map((manual.results || []).map(row => [
    Number(row.tenant_id), {row, time:manual.finished_at || manual.queued_at,
                            timeLabel:manual.finished_at ? '批次完成' : '批次提交'}
  ]));
  resultRows = rows;
  renderResults();
  renderBatchStatus(config);
  renderManualCollection(manual);
  if (rankings) {
    renderTenantRankings(rankings);
  } else {
    el('tenantProcessRanks').innerHTML = '<tr><td colspan="4">排名读取失败</td></tr>';
    el('tenantTableRanks').innerHTML = '<tr><td colspan="6">排名读取失败</td></tr>';
  }
}

async function saveInspectionForm() {
  const body = {
    enabled: el('enabled').checked,
    run_time: el('runTime').value,
    oracle_user: el('oracleUser').value,
    mysql_user: el('mysqlUser').value,
    cluster_ids: [...el('clusterChoices').querySelectorAll('input:checked')].map(input => Number(input.value)),
    rules: [...readRules('ORACLE', el('oracleRules').value), ...readRules('MYSQL', el('mysqlRules').value)],
    primary_password: el('primaryPassword').value,
    secondary_password: el('secondaryPassword').value,
    clear_secondary: el('clearSecondary').checked,
  };
  await api('/api/tenant-inspection/config', {method:'POST', body:JSON.stringify(body)});
  el('primaryPassword').value = '';
  el('secondaryPassword').value = '';
  el('clearSecondary').checked = false;
  await loadConfig();
}

el('inspectionForm').onsubmit = async event => {
  event.preventDefault();
  try {
    await saveInspectionForm();
    el('inspectionStatus').textContent = '配置已保存，密码留空可继续使用已保存的密码';
    await refreshResults();
  } catch (error) {
    el('inspectionStatus').textContent = error.message;
  }
};

el('runToday').onclick = async () => {
  if (requestBusy) return;
  requestBusy = true;
  el('runToday').disabled = true;
  try {
    if (el('primaryPassword').value || el('secondaryPassword').value || el('clearSecondary').checked) {
      await saveInspectionForm();
    }
    const result = await api('/api/tenant-inspection/run', {method:'POST', body:'{}'});
    el('inspectionStatus').textContent = result.message;
    await refreshResults();
  } catch (error) {
    el('inspectionStatus').textContent = error.message;
  } finally {
    requestBusy = false;
    el('runToday').disabled = false;
  }
};
el('passwordCorrected').onchange=()=>{el('confirmResume').disabled=!el('passwordCorrected').checked;};el('cancelResume').onclick=()=>el('confirmDialog').close();
el('confirmResume').onclick=async()=>{if(!el('passwordCorrected').checked)return;el('confirmResume').disabled=true;try{const result=await api(`/api/tenant-inspection/${confirmId}/confirm`,{method:'POST',body:JSON.stringify({password_corrected:true})});el('confirmDialog').close();el('inspectionStatus').textContent=`已确认，最早 ${result.resume_day} 恢复；今天不会重新登录`;await refreshResults();}catch(e){el('inspectionStatus').textContent=e.message;el('confirmResume').disabled=false;}};
el('onlyIssues').onchange=renderResults;el('refreshResults').onclick=()=>refreshResults().catch(e=>el('inspectionStatus').textContent=e.message);
Promise.all([loadConfig(),refreshResults()]).catch(e=>el('inspectionStatus').textContent='加载失败：'+e.message);
setInterval(()=>{if(!document.hidden)refreshResults().catch(e=>el('inspectionStatus').textContent=e.message);},30000);
