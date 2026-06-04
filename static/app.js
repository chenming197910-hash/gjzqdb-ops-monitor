const heroMetricsEl = document.querySelector("#heroMetrics");
const clusterCardsEl = document.querySelector("#clusterCards");
const systemCardsEl = document.querySelector("#systemCards");
const eventRowsEl = document.querySelector("#eventRows");
const toastEl = document.querySelector("#toast");
const autoRefreshEl = document.querySelector("#autoRefresh");
const refreshSecondsEl = document.querySelector("#refreshSeconds");
const logDialog = document.querySelector("#logDialog");
const ocpDialog = document.querySelector("#ocpDialog");

let refreshTimer = null;
let ocpConnections = [];

const demo = {
  summary: { clusters: 3, tenants: 18, databases: 96, servers: 159, observers: 150, log_errors: 0 },
  clusters: [
    { id: 1, name: "GP", status: "online", tenant_count: 8, observer_count: 30 },
    { id: 2, name: "CDH", status: "online", tenant_count: 6, observer_count: 26 },
    { id: 3, name: "ODS", status: "online", tenant_count: 4, observer_count: 24 },
  ],
  servers: [
    { hostname: "ob01", ip: "10.10.20.11", status: "online" },
    { hostname: "ob02", ip: "10.10.20.12", status: "online" },
  ],
  logs: [],
};

const systems = [
  { name: "火山数智化营销系统", hosts: 2, vm: 2, faults: 0, alerts: 0 },
  { name: "数据门户", hosts: 0, vm: 2, faults: 0, alerts: 0 },
  { name: "数字化合规监测系统", hosts: 0, vm: 9, faults: 0, alerts: 0 },
  { name: "营销标准化系统", hosts: 0, vm: 1, faults: 0, alerts: 0 },
  { name: "企业客户标签系统", hosts: 0, vm: 10, faults: 0, alerts: 0 },
];

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "请求失败");
  return data;
}

async function loadAll() {
  let data;
  try {
    const [summary, clusters, servers, logs, ocps] = await Promise.all([
      api("/api/summary"),
      api("/api/clusters"),
      api("/api/servers"),
      api("/api/logs"),
      api("/api/ocp/connections"),
    ]);
    ocpConnections = ocps;
    data = { summary, clusters, servers, logs };
  } catch (error) {
    data = demo;
    showToast("未连接Oracle，当前显示演示数据");
  }
  renderHero(data.summary, data.servers);
  renderClusters(data.clusters, data.summary);
  renderSystems(data.summary);
  renderEvents(data.logs);
}

function renderHero(summary, servers) {
  const faultyHosts = servers.filter((item) => item.status && item.status !== "online").length;
  const normalHosts = Math.max((summary.servers || 0) - faultyHosts, 0);
  const cards = [
    { label: "物理机", value: summary.servers || 0, kind: "blue", icon: "▦" },
    { label: "虚拟机", value: summary.observers || 0, kind: "cyan", icon: "▤" },
    { label: "故障主机", value: faultyHosts, kind: "red", icon: "×" },
    { label: "报警主机", value: summary.log_errors || 0, kind: "orange", icon: "!" },
    { label: "正常主机", value: normalHosts, kind: "green", icon: "▶" },
  ];
  heroMetricsEl.innerHTML = cards.map((card) => `
    <article class="hero-card ${card.kind}">
      <div class="hero-icon">${card.icon}</div>
      <div><span>${card.label}</span><strong>${card.value}</strong></div>
    </article>
  `).join("");
}

function renderClusters(clusters, summary) {
  const source = clusters.length ? clusters : demo.clusters;
  clusterCardsEl.innerHTML = source.slice(0, 3).map((cluster, index) => {
    const total = 680 + index * 229;
    const used = Math.round(total * ([0.82, 0.71, 0.79][index] || 0.75));
    const cpu = [58, 95, 36][index] || 62;
    const mem = [10, 81, 48][index] || 50;
    const dbCount = summary.databases || cluster.tenant_count * 8 || 0;
    return `
      <article class="cluster-card">
        <div class="cluster-title"><b>${escapeHtml(cluster.name)}</b><span>?</span></div>
        <div class="cluster-main">
          ${donut("总容量", `${total}T`, 74, "green")}
          <div class="capacity">
            <p><b>已用容量: ${used}(T)</b></p>
            <p>近30天: ${index}(T) <em class="up">↑</em></p>
            <p>近7天: ${index + 5}(T) <em class="down">↓</em></p>
            <p><b>表数量: ${24105 + index * 5030}</b></p>
            <p>近30天: ${index * 2} <em class="up">↑</em></p>
            <p>近7天: ${index} <em class="down">↓</em></p>
          </div>
          ${donut("CPU使用率", `${cpu}%`, cpu, "blue")}
          ${donut("内存使用率", `${mem}%`, mem, "cyan")}
        </div>
        <div class="legend"><i class="cpu"></i>CPU使用率 <i class="mem"></i>内存使用率</div>
        ${trend(index)}
        <div class="cluster-foot">
          <span>租户 ${cluster.tenant_count || 0}</span>
          <span>数据库 ${dbCount}</span>
          <span>OBServer ${cluster.observer_count || 0}</span>
          <span>${escapeHtml(cluster.status || "unknown")}</span>
        </div>
      </article>
    `;
  }).join("");
}

function donut(label, value, percent, color) {
  return `<div class="donut ${color}" style="--value:${percent}"><div><span>${label}</span><strong>${value}</strong></div></div>`;
}

function trend(index) {
  const cpu = [
    "92,90 170,88 250,90 330,86 410,90 490,94 560,82",
    "92,70 150,88 210,60 270,92 330,48 410,42 500,55 560,92",
    "92,84 170,84 250,84 330,84 410,84 490,84 560,84",
  ][index] || "92,78 170,74 250,70 330,68 410,64 490,70 560,76";
  const mem = [
    "92,120 170,112 250,112 330,112 410,118 490,126 560,120",
    "92,78 150,70 210,92 270,62 330,48 410,44 500,60 560,88",
    "92,124 170,124 250,124 330,124 410,124 490,124 560,124",
  ][index] || "92,112 170,108 250,102 330,96 410,92 490,96 560,108";
  return `
    <svg class="trend" viewBox="0 0 600 150">
      <g class="grid"><line x1="70" y1="20" x2="580" y2="20"></line><line x1="70" y1="45" x2="580" y2="45"></line><line x1="70" y1="70" x2="580" y2="70"></line><line x1="70" y1="95" x2="580" y2="95"></line><line x1="70" y1="120" x2="580" y2="120"></line></g>
      <g class="axis"><text x="22" y="25">100</text><text x="32" y="50">80</text><text x="32" y="75">60</text><text x="32" y="100">40</text><text x="32" y="125">20</text><text x="70" y="143">08:00</text><text x="165" y="143">10:00</text><text x="260" y="143">12:00</text><text x="355" y="143">14:00</text><text x="450" y="143">16:00</text><text x="535" y="143">10:00</text></g>
      <polyline class="cpu-line" points="${cpu}"></polyline><polyline class="mem-line" points="${mem}"></polyline>
    </svg>
  `;
}

function renderSystems(summary) {
  systemCardsEl.innerHTML = systems.map((system, index) => `
    <article class="system-card">
      <h3>${system.name}</h3>
      <div><span>故障数</span><b>${system.faults}</b><span>告警数</span><b>${index === 0 ? summary.log_errors || 0 : system.alerts}</b></div>
      <div><span>物理机</span><b>${system.hosts}</b><span>虚拟机</span><b>${system.vm}</b></div>
    </article>
  `).join("") + `<div class="pager"><i></i><i></i><i class="active"></i></div>`;
}

function renderEvents(logs) {
  const rows = logs.length ? logs : [
    { event_time: "2026-06-04 10:00:00", severity: "INFO", server_ip: "10.10.20.11", message: "平台运行正常，暂无OB错误日志", cluster_name: "GP" },
  ];
  eventRowsEl.innerHTML = rows.slice(0, 8).map((item) => `
    <tr>
      <td>${escapeHtml(item.event_time || item.created_at || "-")}</td>
      <td><span class="level ${escapeHtml((item.severity || "INFO").toLowerCase())}">${escapeHtml(item.severity || "INFO")}</span></td>
      <td>${escapeHtml(item.server_ip || "-")}</td>
      <td>${escapeHtml(item.message || "-")}</td>
      <td>${escapeHtml(item.cluster_name || item.cluster_id || "OB资产平台")}</td>
      <td>DBA</td>
    </tr>
  `).join("");
}

function setupRefresh() {
  clearInterval(refreshTimer);
  if (autoRefreshEl.checked) refreshTimer = setInterval(loadAll, Number(refreshSecondsEl.value) * 1000);
}

function formToPayload(form) {
  const payload = Object.fromEntries(new FormData(form).entries());
  if (payload.cluster_id === "") delete payload.cluster_id;
  if (form.id === "ocpForm") payload.verify_ssl = Boolean(form.elements.verify_ssl.checked);
  return payload;
}

function showToast(message) {
  toastEl.textContent = message;
  toastEl.classList.add("show");
  setTimeout(() => toastEl.classList.remove("show"), 2200);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[char]));
}

document.querySelector("#runCollect").addEventListener("click", async () => {
  try {
    await api("/api/collect", { method: "POST", body: JSON.stringify({ target_type: "all" }) });
    showToast("采集任务已执行");
    await loadAll();
  } catch {
    showToast("演示模式：采集入口已保留");
  }
});

document.querySelector("#syncOcp").addEventListener("click", async () => {
  try {
    if (!ocpConnections.length) {
      showToast("请先配置OCP接入");
      ocpDialog.showModal();
      return;
    }
    const latest = ocpConnections[0];
    const result = await api(`/api/ocp/connections/${latest.id}/sync`, { method: "POST", body: "{}" });
    showToast(`OCP同步完成：${result.cluster_count} 个集群`);
    await loadAll();
  } catch (error) {
    showToast(error.message);
  }
});

document.querySelector("#openOcp").addEventListener("click", () => ocpDialog.showModal());
document.querySelector("#closeOcpDialog").addEventListener("click", () => ocpDialog.close());
document.querySelector("#openLogCapture").addEventListener("click", () => logDialog.showModal());
document.querySelector("#closeLogDialog").addEventListener("click", () => logDialog.close());
autoRefreshEl.addEventListener("change", setupRefresh);
refreshSecondsEl.addEventListener("change", setupRefresh);

document.querySelector("#ocpForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const result = await api("/api/ocp/connections", {
      method: "POST",
      body: JSON.stringify(formToPayload(event.currentTarget)),
    });
    await api(`/api/ocp/connections/${result.id}/test`, { method: "POST", body: "{}" });
    showToast("OCP配置已保存并测试");
    ocpDialog.close();
    event.currentTarget.reset();
    await loadAll();
  } catch (error) {
    showToast(error.message);
  }
});

document.querySelector("#logForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const result = await api("/api/logs", { method: "POST", body: JSON.stringify(formToPayload(event.currentTarget)) });
    showToast(`已入库 ${result.inserted} 条日志`);
    logDialog.close();
    event.currentTarget.reset();
    await loadAll();
  } catch (error) {
    showToast(error.message);
  }
});

loadAll().then(setupRefresh);
