// Offline frontend regression checks. Run with Node 18+.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
function context(){const nodes=new Map();const node=id=>{if(!nodes.has(id))nodes.set(id,{value:'',checked:false,innerHTML:'',textContent:'',options:[],addEventListener(){},classList:{toggle(){},remove(){},add(){}}});return nodes.get(id);};return vm.createContext({document:{querySelector:node,querySelectorAll:()=>[],addEventListener(){}},console,Date,URL,Blob,setInterval(){},clearInterval(){},setTimeout(){},window:{},nodes});}
(async()=>{
 const c=context();let code=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8');code=code.slice(0,code.lastIndexOf('\nupdateTenantFilterStates();'));vm.runInContext(code,c);
 assert.match(vm.runInContext('backupTimeCell(null,true)',c),/未取得成功全备记录/);
 assert.equal(vm.runInContext('isMetaTenant({name:"metadata_service"})',c),false);
 assert.equal(vm.runInContext('isMetaTenant({name:"meta$1001"})',c),true);
 // One failing API must not erase cached SYS health or successful cluster data.
 vm.runInContext(`state.sysChecks=[{cluster_id:1}];state.clusters=[{id:0}];
 var rendered={};
 renderSummary=x=>rendered.summary=x;renderTenantClusterFilter=()=>{};renderClusters=x=>rendered.clusters=x;
 renderTenantResourceCharts=renderTenants=renderServers=renderLogs=renderTopAlerts=renderJobs=renderOcpConnections=renderObserverLogSources=()=>{};
 renderSysHealth=x=>rendered.sys=x;setStatus=x=>rendered.error=x;
 api=async path=>{if(path==='/api/sys-tenant-checks')throw Error('test failure');if(path==='/api/clusters')return [{id:5}];if(path==='/api/health')return {oracle_user:'test'};return [];};`,c);
 await vm.runInContext('loadAll()',c);
 assert.equal(vm.runInContext('state.sysChecks.length',c),1);assert.equal(vm.runInContext('rendered.clusters[0].id',c),5);assert.match(vm.runInContext('rendered.error',c),/sys-tenant-checks/);
 console.log('PASS dashboard: partial API failure, preserved SYS rows, backup missing record, meta filter');
 const cpu=context();cpu.document.getElementById=id=>cpu.document.querySelector(id);
 let cpuCode=fs.readFileSync(path.join(__dirname,'../static/cpu_history.js'),'utf8');cpuCode=cpuCode.replace('loadStatus();loadTenants();','');vm.runInContext(cpuCode,cpu);
 vm.runInContext(`available=Array.from({length:121},(_,i)=>({id:i+1,name:'t'+i,active:1,cluster_id:1,cluster_name:'test'}));
 request=async ()=>available;refresh=async()=>{};`,cpu);
 await vm.runInContext('loadTenants()',cpu);
 assert.equal(vm.runInContext('selected.size',cpu),3);
 vm.runInContext("$('selectAll').onclick()",cpu);assert.equal(vm.runInContext('selected.size',cpu),12);
 vm.runInContext(`summary([{tenant_id:1,sample_count:1,cpu_avg_pct:10,cpu_sampled_peak_pct:10,cpu_used_cores:1,cpu_limit_cores:10},{tenant_id:1,sample_count:3,cpu_avg_pct:90,cpu_sampled_peak_pct:90,cpu_used_cores:9,cpu_limit_cores:10}],[available[0]])`,cpu);
 assert.match(cpu.document.getElementById('cpuSummary').innerHTML,/70.00%/);
 vm.runInContext(`draw([{tenant_id:1,sample_hour:'2026-11-05 01:00',cpu_avg_pct:1},{tenant_id:1,sample_hour:'2026-11-05 03:00',cpu_avg_pct:3}],[available[0]])`,cpu);
 const html=cpu.document.getElementById('cpuPlot').innerHTML;
 assert.match(html,/<circle/);assert.equal((html.match(/ M /g)||[]).length,2);assert.equal((html.match(/ L /g)||[]).length,0);
 console.log('PASS CPU: 121 tenants default 3, selection max 12, weighted statistics, gap rendering');
 vm.runInContext(`available=[{id:1,name:'META$1002',active:1,cluster_id:1,cluster_name:'test'},{id:2,name:'metadata_service',active:1,cluster_id:1,cluster_name:'test'},{id:-1,name:'sys',active:1,cluster_id:1,cluster_name:'test'}];selected=new Set([1,2,-1]);$('showMeta').checked=false;$('clusterFilter').value='';$('tenantSearch').value='';choices();`,cpu);
 assert.equal(vm.runInContext('matches(available[0])',cpu),false);
 assert.equal(vm.runInContext('matches(available[1])',cpu),true);
 assert.equal(vm.runInContext('matches(available[2])',cpu),true);
 vm.runInContext("$('showMeta').checked=true;choices()",cpu);
 assert.equal(vm.runInContext('matches(available[0])',cpu),true);
 vm.runInContext("$('showMeta').checked=false;$('showMeta').onchange()",cpu);
 assert.equal(vm.runInContext('selected.has(1)',cpu),false);
 assert.equal(vm.runInContext('selected.has(-1)',cpu),true);
 await vm.runInContext('loadTenants()',cpu);
 assert.equal(vm.runInContext('selected.has(-1)',cpu),true);
 assert.equal(vm.runInContext('selected.has(1)',cpu),false);
 console.log('PASS SYS/meta: default hidden, case-insensitive meta prefix, business name retained, SYS visible/default selected, hiding clears meta selection');
 vm.runInContext(`available.push({id:99,name:'old_stopped',active:0,cluster_id:1,cluster_name:'test'});$('showMeta').checked=true;choices()`,cpu);
 assert.equal(vm.runInContext('matches(available[available.length-1])',cpu),false);
 assert.doesNotMatch(cpu.document.getElementById('cpuChoices').innerHTML,/old_stopped/);
 console.log('PASS inactive tenant hidden with meta switch both on and off');


})().catch(e=>{console.error(e);process.exitCode=1;});
