const $=id=>document.getElementById(id);
const escapeHtml=text=>String(text??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const palette=['#2563eb','#db2777','#059669','#ea580c','#7c3aed','#0891b2','#a16207','#be123c','#4f46e5','#16a34a','#475569','#a855f7'];
let available=[],selected=new Set(),lastRows=[],generation=0;
async function request(url){const r=await fetch(url,{cache:'no-store'});const data=await r.json();if(!r.ok)throw Error(data.message||data.error||`HTTP ${r.status}`);return data;}
function isMeta(t){return /^meta\$/i.test(t.name||'');}
function matches(t){return Number(t.active)===1&&($('showMeta').checked||!isMeta(t))&&(!$('clusterFilter').value||String(t.cluster_id)===$('clusterFilter').value)&&t.name.toLowerCase().includes($('tenantSearch').value.toLowerCase());}
function choices(){
 $('cpuChoices').innerHTML=available.filter(matches).map(t=>`<label><input type="checkbox" value="${Number(t.id)}" ${selected.has(Number(t.id))?'checked':''}> <span>${escapeHtml(t.cluster_name)} / ${escapeHtml(t.name)}</span></label>`).join('')||'没有匹配的租户';
 $('selectedCount').textContent=`已选 ${selected.size} 个租户`;
}
async function loadStatus(){
 try{const statuses=await request('/api/cpu-history/status');const labels={success:'采集正常',baseline:'基线已建立，等待下一次采样',failed:'采集失败',unconfigured:'未配置凭据'};
 $('collectionStatus').innerHTML=statuses.map(s=>{const at=s.checked_at?new Date(s.checked_at.replace(' ','T')):null;const stale=!at||Date.now()-at.getTime()>900000;const good=s.status==='success'&&!stale;
 return `<article class="${good?'ok':s.status==='baseline'&&!stale?'pending':'bad'}"><strong>${escapeHtml(s.cluster_name)}</strong>${escapeHtml(labels[s.status]||'尚无采集记录')}${stale?' · 状态已过期/未运行':''}<small>最近检查：${escapeHtml(s.checked_at||'—')}<br>最近样本：${escapeHtml(s.last_sample_at||'—')}<br>${escapeHtml(s.message||'请确认定时采集服务已启动')}</small></article>`;}).join('')||'暂无集群';
 }catch(e){$('collectionStatus').textContent=`采集状态读取失败：${e.message}。请检查迁移与服务日志。`;}
}
async function loadTenants(){
 try{available=await request('/api/cpu-history/tenants');
 const clusters=new Map(available.map(t=>[String(t.cluster_id),t.cluster_name]));
 $('clusterFilter').innerHTML='<option value="">全部集群</option>'+[...clusters].map(([id,name])=>`<option value="${Number(id)}">${escapeHtml(name)}</option>`).join('');
 selected=new Set(available.filter(t=>Number(t.active)!==0&&matches(t)).sort((a,b)=>Number(b.id<0)-Number(a.id<0)).slice(0,3).map(t=>Number(t.id)));choices();await refresh();
 }catch(e){$('cpuChoices').textContent='租户清单读取失败';$('cpuStatus').textContent=`加载失败：${e.message}`;}
}
function sampleTime(hour){return new Date(hour.replace(' ','T')+':00').getTime();}
function draw(rows,keys){
 const plot=$('cpuPlot');$('cpuLegend').innerHTML='';
 if(!rows.length){plot.innerHTML='<div class="cpu-empty">所选时间范围没有有效 CPU 历史样本<br>请查看上方采集运行状态；历史从首次成功采样开始。</div>';return;}
 const times=rows.map(r=>sampleTime(r.sample_hour));const start=Math.min(...times),end=Math.max(...times),span=Math.max(3600000,end-start);
 const w=1100,h=360,l=58,r=22,t=22,b=52,iw=w-l-r,ih=h-t-b;
 const ymax=Math.max(100,Math.ceil(Math.max(...rows.map(r=>Number(r.cpu_avg_pct)||0))/20)*20);
 const x=v=>l+(v-start)/span*iw,y=v=>t+ih-Number(v)/ymax*ih;
 const parts=[`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${h}" role="img" aria-label="租户 CPU 使用率历史趋势">`];
 for(let i=0;i<=5;i++){const val=ymax*i/5,Y=y(val);parts.push(`<line x1="${l}" y1="${Y}" x2="${w-r}" y2="${Y}" stroke="#e4eaf2"/><text x="${l-10}" y="${Y+4}" text-anchor="end" font-size="12" fill="#72849a">${val.toFixed(0)}%</text>`);}
 for(let i=0;i<6;i++){const time=start+span*i/5,at=new Date(time);const label=`${at.getMonth()+1}/${at.getDate()} ${String(at.getHours()).padStart(2,'0')}:00`;parts.push(`<text x="${x(time)}" y="${h-18}" font-size="11" text-anchor="middle" fill="#72849a">${label}</text>`);}
 keys.forEach((tenant,index)=>{
   const color=palette[index%palette.length],series=rows.filter(r=>Number(r.tenant_id)===Number(tenant.id)).sort((a,b)=>sampleTime(a.sample_hour)-sampleTime(b.sample_hour));let path='',previous=null;
   series.forEach(row=>{const at=sampleTime(row.sample_hour),X=x(at).toFixed(1),Y=y(row.cpu_avg_pct).toFixed(1);path+=`${previous!==null&&at-previous<=3600000?' L':' M'} ${X} ${Y}`;previous=at;
   parts.push(`<circle cx="${X}" cy="${Y}" r="2.3" fill="${color}"><title>${escapeHtml(tenant.cluster_name+'/'+tenant.name)} · ${escapeHtml(row.sample_hour)} · ${Number(row.cpu_avg_pct).toFixed(2)}% · ${Number(row.sample_count)} 个样本</title></circle>`);});
   parts.push(`<path d="${path}" fill="none" stroke="${color}" stroke-width="1.8"/>`);
 });plot.innerHTML=parts.join('')+'</svg>';
 $('cpuLegend').innerHTML=keys.map((t,i)=>`<span><i style="background:${palette[i%palette.length]}"></i>${escapeHtml(t.cluster_name+'/'+t.name)}</span>`).join('');
}
function summary(rows,keys){
 let covered=0,total=0,peak=0;
 $('cpuSummary').innerHTML=keys.map(t=>{const samples=rows.filter(x=>Number(x.tenant_id)===Number(t.id));if(!samples.length)return `<tr><td>${escapeHtml(t.cluster_name+'/'+t.name)}</td><td colspan="6">所选范围无有效样本</td></tr>`;
 covered++;const count=samples.reduce((a,v)=>a+Number(v.sample_count),0);total+=count;
 const mean=k=>samples.reduce((a,v)=>a+Number(v[k]||0)*Number(v.sample_count),0)/count;
 const max=Math.max(...samples.map(s=>Number(s.cpu_sampled_peak_pct)||0));peak=Math.max(peak,max);
 return `<tr><td>${escapeHtml(t.cluster_name+'/'+t.name)}</td><td>${samples.length}</td><td>${mean('cpu_avg_pct').toFixed(2)}%</td><td>${max.toFixed(2)}%</td><td>${mean('cpu_used_cores').toFixed(2)}</td><td>${mean('cpu_limit_cores').toFixed(2)}</td><td>${count}</td></tr>`;
 }).join('')||'<tr><td colspan="7">请选择租户</td></tr>';
 $('tenantCoverage').textContent=`${covered} / ${keys.length}`;$('sampleTotal').textContent=total.toLocaleString();$('peakCpu').textContent=total?`${peak.toFixed(2)}%`:'—';
}
async function refresh(){
 const version=++generation,ids=[...selected],keys=available.filter(t=>selected.has(Number(t.id)));
 if(!ids.length){$('refreshCpu').disabled=false;$('cpuStatus').textContent='请至少选择一个租户';lastRows=[];draw([],[]);summary([],[]);return;}
 if(ids.length>12){$('cpuStatus').textContent='最多同时对比 12 个租户';return;}
 $('cpuStatus').textContent='正在读取历史数据…';$('refreshCpu').disabled=true;
 try{const rows=await request(`/api/cpu-history?days=${$('days').value}&tenants=${ids.join(',')}`);if(version!==generation)return;
 lastRows=rows;draw(rows,keys);summary(rows,keys);$('cpuStatus').textContent=`已加载 ${rows.length} 条小时记录 · 最近 ${$('days').value} 天 · 空白表示无有效样本`;
 }catch(e){if(version!==generation)return;lastRows=[];draw([],[]);summary([],[]);$('cpuStatus').textContent=`查询失败：${e.message}`;
 }finally{if(version===generation)$('refreshCpu').disabled=false;}
}
$('cpuChoices').onchange=e=>{if(e.target.type!=='checkbox')return;const id=Number(e.target.value);if(e.target.checked){if(selected.size>=12){e.target.checked=false;$('cpuStatus').textContent='最多同时对比 12 个租户';return;}selected.add(id);}else selected.delete(id);$('selectedCount').textContent=`已选 ${selected.size} 个租户（点击查询更新图表）`;};
$('selectAll').onclick=()=>{available.filter(matches).forEach(t=>{if(selected.size<12)selected.add(Number(t.id));});choices();};
$('clearAll').onclick=()=>{selected.clear();choices();};
$('showMeta').onchange=()=>{
 if(!$('showMeta').checked){available.filter(isMeta).forEach(t=>selected.delete(Number(t.id)));}
 choices();refresh();
};
$('clusterFilter').onchange=choices;$('tenantSearch').oninput=choices;$('refreshCpu').onclick=refresh;$('refreshStatus').onclick=loadStatus;
$('exportCsv').onclick=()=>{if(!lastRows.length){$('cpuStatus').textContent='暂无已查询的数据可导出';return;}const fields=['cluster_name','tenant_name','sample_hour','cpu_avg_pct','cpu_sampled_peak_pct','cpu_used_cores','cpu_limit_cores','sample_count'];const cell=v=>'"'+String(v??'').replace(/^[=+@-]/,"'$&").replace(/"/g,'""')+'"';const csv='\ufeff'+[fields,...lastRows.map(r=>fields.map(f=>r[f]))].map(r=>r.map(cell).join(',')).join('\r\n');const url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download='tenant-cpu-hourly.csv';a.click();URL.revokeObjectURL(url);};
loadStatus();loadTenants();
