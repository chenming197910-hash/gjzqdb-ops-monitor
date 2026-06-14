const state = {
  refreshTimer: null,
  ocpConnections: [],
  clusters: [],
  tenants: [],
  servers: [],
  logs: [],
  jobs: [],
  sysChecks: [],
};

const els = {
  dbInfo: document.querySelector("#dbInfo"),
  statusBanner: document.querySelector("#statusBanner"),
  summaryCards: document.querySelector("#summaryCards"),
  cpuPie: document.querySelector("#cpuPie"),
  memoryPie: document.querySelector("#memoryPie"),
  cpuTenantList: document.querySelector("#cpuTenantList"),
  memoryTenantList: document.querySelector("#memoryTenantList"),
  clusterRows: document.querySelector("#clusterRows"),
  tenantRows: document.querySelector("#tenantRows"),
  serverRows: document.querySelector("#serverRows"),
  logRows: document.querySelector("#logRows"),
  jobRows: document.querySelector("#jobRows"),
  sysHealthRows: document.querySelector("#sysHealthRows"),
  ocpRows: document.querySelector("#ocpRows"),
  tenantClusterFilter: document.querySelector("#tenantClusterFilter"),
  exportTenants: document.querySelector("#exportTenants"),
  hideStandbyTenants: document.querySelector("#hideStandbyTenants"),
  hideMetaTenants: document.querySelector("#hideMetaTenants"),
  hideStandbyState: document.querySelector("#hideStandbyState"),
  hideMetaState: document.querySelector("#hideMetaState"),
  refreshSeconds: document.querySelector("#refreshSeconds"),
  toast: document.querySelector("#toast"),
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    cache: "no-store",
    ...options,
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) : {};
  if (!response.ok) {
    throw new Error(data.message || data.error || `请求失败: ${response.status}`);
  }
  return data;
}

async function loadAll() {
  try {
    const [health, summary, clusters, tenants, servers, logs, jobs, sysChecks, ocps] = await Promise.all([
      api("/api/health"),
      api("/api/summary"),
      api("/api/clusters"),
      api("/api/tenants"),
      api("/api/servers"),
      api("/api/logs"),
      api("/api/collection-jobs"),
      api("/api/sys-tenant-checks"),
      api("/api/ocp/connections"),
    ]);
    state.clusters = clusters;
    state.tenants = tenants;
    state.servers = servers;
    state.logs = logs;
    state.jobs = jobs;
    state.sysChecks = sysChecks;
    state.ocpConnections = ocps;
    els.dbInfo.textContent = `后台 Oracle 资产库已连接（${health.oracle_user}）`;
    setStatus("");
    renderSummary(summary);
    renderTenantClusterFilter(clusters);
    renderTenantResourceCharts(tenants);
    renderClusters(clusters);
    renderTenants(tenants);
    renderServers(servers);
    renderLogs(logs);
    renderJobs(jobs);
    renderSysHealth(sysChecks);
    renderOcpConnections(ocps);
  } catch (error) {
    els.dbInfo.textContent = "数据库连接异常";
    setStatus(`数据库未连接或资产表未初始化：${error.message}`);
    renderSummary({});
    renderTenantClusterFilter([]);
    renderTenantResourceCharts([]);
    renderClusters([]);
    renderTenants([]);
    renderServers([]);
    renderLogs([]);
    renderJobs([]);
    renderSysHealth([]);
    renderOcpConnections([]);
  }
}

function renderSummary(summary) {
  const cards = [
    ["OB集群", summary.clusters || 0, "clustersSection"],
    ["租户", summary.tenants || 0, "tenantsSection"],
    ["数据库", summary.databases || 0, "tenantsSection"],
    ["服务器", summary.servers || 0, "serversSection"],
    ["OBServer", summary.observers || 0, "clustersSection"],
    ["错误日志", summary.log_errors || 0, "logsSection"],
    ["采集任务", state.jobs.length || 0, "jobsSection"],
    ["OCP接入", summary.ocp_connections || 0, "configsSection"],
  ];
  els.summaryCards.innerHTML = cards.map(([label, value, target]) => `
    <article class="metric-card clickable" data-target="${target}">
      <span>${label}</span>
      <strong>${value}</strong>
    </article>
  `).join("");
  document.querySelectorAll("[data-target]").forEach((card) => {
    card.addEventListener("click", () => scrollToSection(card.dataset.target));
  });
}

function renderTenantResourceCharts(rows) {
  const visibleTenants = filterTenants(rows);
  renderResourceChart("cpu", visibleTenants, "cpu_cores", els.cpuPie, els.cpuTenantList, "C");
  renderResourceChart("memory", visibleTenants, "memory_gb", els.memoryPie, els.memoryTenantList, "GB");
}

function renderResourceChart(kind, rows, field, pieEl, listEl, unit) {
  const items = rows
    .map((item, index) => ({
      ...item,
      value: Number(item[field] || 0),
      color: chartColor(index),
    }))
    .filter((item) => item.value > 0)
    .sort((a, b) => b.value - a.value);
  const total = items.reduce((sum, item) => sum + item.value, 0);
  if (!items.length || total <= 0) {
    pieEl.innerHTML = `<div class="empty-chart">暂无${kind === "cpu" ? "CPU" : "内存"}规格数据，请先执行只读采集。</div>`;
    listEl.innerHTML = "";
    return;
  }
  let start = -90;
  const paths = items.map((item) => {
    const angle = (item.value / total) * 360;
    const path = pieSlicePath(60, 60, 48, start, start + angle);
    start += angle;
    return `<path d="${path}" fill="${item.color}" data-tenant-id="${safe(item.id)}"><title>${safe(item.name)} ${formatNumber(item.value)}${unit} ${formatPercent(item.value, total)}</title></path>`;
  }).join("");
  pieEl.innerHTML = `
    <svg class="pie-chart" viewBox="0 0 120 120" role="img">
      ${paths}
      <circle cx="60" cy="60" r="24" fill="#fff"></circle>
      <text x="60" y="57" text-anchor="middle" class="pie-total">${formatNumber(total)}</text>
      <text x="60" y="73" text-anchor="middle" class="pie-unit">${safe(unit)}</text>
    </svg>
  `;
  listEl.innerHTML = items.map((item) => `
    <button class="resource-item" data-tenant-id="${safe(item.id)}">
      <span class="resource-swatch" style="background:${item.color}"></span>
      <span class="resource-name">${safe(item.name)}</span>
      <strong>${formatNumber(item.value)}${safe(unit)}</strong>
      <em>${formatPercent(item.value, total)}</em>
    </button>
  `).join("");
  [...pieEl.querySelectorAll("[data-tenant-id]"), ...listEl.querySelectorAll("[data-tenant-id]")].forEach((node) => {
    node.addEventListener("click", () => openTenantDetail(node.dataset.tenantId));
  });
}

function pieSlicePath(cx, cy, r, startAngle, endAngle) {
  const start = polarToCartesian(cx, cy, r, endAngle);
  const end = polarToCartesian(cx, cy, r, startAngle);
  const largeArcFlag = endAngle - startAngle <= 180 ? "0" : "1";
  return `M ${cx} ${cy} L ${start.x} ${start.y} A ${r} ${r} 0 ${largeArcFlag} 0 ${end.x} ${end.y} Z`;
}

function polarToCartesian(cx, cy, r, angleInDegrees) {
  const angleInRadians = (angleInDegrees * Math.PI) / 180;
  return {
    x: cx + r * Math.cos(angleInRadians),
    y: cy + r * Math.sin(angleInRadians),
  };
}

function chartColor(index) {
  const colors = ["#2563eb", "#16a34a", "#dc2626", "#9333ea", "#ea580c", "#0891b2", "#4f46e5", "#65a30d", "#be123c", "#0f766e"];
  return colors[index % colors.length];
}

function formatNumber(value) {
  const number = Number(value || 0);
  return String(Math.round(number * 100) / 100);
}

function formatPercent(value, total) {
  if (!total) return "0%";
  return `${Math.round((Number(value || 0) * 10000) / total) / 100}%`;
}

function renderClusters(rows) {
  els.clusterRows.innerHTML = rows.length ? rows.map((item) => `
    <tr>
      <td>${safe(item.id)}</td>
      <td><button class="link-button" data-detail="${safe(item.id)}">${safe(item.name)}</button></td>
      <td>${safe(item.environment)}</td>
      <td>${safe(item.region)}</td>
      <td>${safe(item.endpoint)}</td>
      <td>${safe(item.port)}</td>
      <td>${safe(item.version)}</td>
      <td>${safe(item.tenant_count)}</td>
      <td>${safe(item.observer_count)}</td>
      <td>${badge(item.status)}</td>
      <td>${clusterScheduleText(item)}</td>
      <td>${safe(item.owner)}</td>
      <td class="row-actions">
        <button class="small" data-config="${encodeURIComponent(JSON.stringify({
          id: item.id,
          endpoint: item.endpoint,
          port: item.port,
          sys_user: item.sys_user,
        }))}">采集配置</button>
        <button class="small" data-cluster-schedule="${encodeURIComponent(JSON.stringify({
          id: item.id,
          enabled: item.schedule_enabled,
          run_time: item.schedule_run_time,
          last_run_at: item.schedule_last_run_at,
        }))}">定时采集</button>
        <button class="small" data-probe="${safe(item.id)}">测试连接</button>
        <button class="small" data-collect="${safe(item.id)}" data-has-password="${safe(item.has_password)}">${item.has_password ? "只读采集" : "补密码后采集"}</button>
        <button class="small danger" data-delete-cluster="${safe(item.id)}">删除</button>
      </td>
    </tr>
  `).join("") : emptyRow(13, "暂无 OB 集群，请点击“新增集群”手工录入，或配置 OCP 后同步。");
  document.querySelectorAll("[data-collect]").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.dataset.hasPassword === "0") {
        const configButton = button.parentElement.querySelector("[data-config]");
        openCollectConfig(JSON.parse(decodeURIComponent(configButton.dataset.config)));
        return;
      }
      collectCluster(button.dataset.collect);
    });
  });
  document.querySelectorAll("[data-config]").forEach((button) => {
    button.addEventListener("click", () => openCollectConfig(JSON.parse(decodeURIComponent(button.dataset.config))));
  });
  document.querySelectorAll("[data-cluster-schedule]").forEach((button) => {
    button.addEventListener("click", () => openClusterSchedule(JSON.parse(decodeURIComponent(button.dataset.clusterSchedule))));
  });
  document.querySelectorAll("[data-probe]").forEach((button) => {
    button.addEventListener("click", () => probeCluster(button.dataset.probe));
  });
  document.querySelectorAll("[data-detail]").forEach((button) => {
    button.addEventListener("click", () => openClusterDetail(button.dataset.detail));
  });
  document.querySelectorAll("[data-delete-cluster]").forEach((button) => {
    button.addEventListener("click", () => deleteCluster(button.dataset.deleteCluster));
  });
}

function clusterScheduleText(item) {
  if (Number(item.schedule_enabled || 0) !== 1) return "-";
  const lastRun = item.schedule_last_run_at ? `<br><small>上次 ${safe(item.schedule_last_run_at)}</small>` : "";
  return `每天 ${safe(item.schedule_run_time || "07:00")}${lastRun}`;
}

function renderTenants(rows) {
  const filteredRows = filterTenants(rows);
  els.tenantRows.innerHTML = filteredRows.length ? filteredRows.map((item) => `
    <tr class="${tenantRowClass(item)}">
      <td>${safe(item.id)}</td>
      <td>${safe(item.cluster_name)}</td>
      <td><button class="link-button ${tenantRoleClass(item)}" data-tenant-detail="${safe(item.id)}">${safe(item.name)}</button></td>
      <td>${safe(item.tenant_mode)}</td>
      <td class="message-cell">${primaryZoneCell(item)}</td>
      <td><span class="${tenantRoleClass(item)}">${safe(item.tenant_role || "-")}</span></td>
      <td>${safe(item.unit_num)}</td>
      <td>${backupTimeCell(item.last_full_backup_time)}</td>
      <td>${usageText(item.data_disk_used_gb, item.data_disk_total_gb, item.data_disk_usage_pct)}</td>
      <td>${usageText(item.log_disk_used_gb, item.log_disk_total_gb, item.log_disk_usage_pct)}</td>
      <td>${safe(item.last_success_merge_time || "-")}${item.last_merge_status ? `<br><small>${safe(item.last_merge_status)}</small>` : ""}</td>
      <td>${badge(item.status)}</td>
      <td class="message-cell">${safe(item.locality)}</td>
    </tr>
  `).join("") : emptyRow(13, rows.length ? "当前筛选条件下暂无租户。可取消屏蔽 standby/meta 租户查看全量。" : "暂无租户信息。手工集群请点击“只读采集”，OCP 集群请点击“同步 OCP”。");
  document.querySelectorAll("[data-tenant-detail]").forEach((button) => {
    button.addEventListener("click", () => openTenantDetail(button.dataset.tenantDetail));
  });
}

function renderTenantClusterFilter(clusters) {
  const current = els.tenantClusterFilter.value;
  const options = clusters.map((item) => `<option value="${safe(item.id)}">${safe(item.name)}</option>`).join("");
  els.tenantClusterFilter.innerHTML = `<option value="">全部集群</option>${options}`;
  if ([...els.tenantClusterFilter.options].some((option) => option.value === current)) {
    els.tenantClusterFilter.value = current;
  }
}

function filterTenants(rows) {
  return rows.filter((item) => {
    if (els.tenantClusterFilter.value && String(item.cluster_id) !== els.tenantClusterFilter.value) return false;
    if (els.hideStandbyTenants.checked && isStandbyTenant(item)) return false;
    if (els.hideMetaTenants.checked && isMetaTenant(item)) return false;
    return true;
  });
}

function isStandbyTenant(item) {
  const values = [item.tenant_role, item.status, item.name].map((value) => String(value || "").toLowerCase());
  return values.some((value) => value.includes("standby"));
}

function isMetaTenant(item) {
  const name = String(item.name || "").toLowerCase();
  return name.startsWith("meta$") || name.includes("meta");
}

function backupTimeCell(value) {
  if (!value) return "-";
  if (!isBackupOverdue(value)) return safe(value);
  return `<span class="backup-overdue">${safe(value)}<br><small>超过24小时未全备</small></span>`;
}

function isBackupOverdue(value) {
  const now = new Date();
  const day = now.getDay();
  if (day === 0 || day === 6) return false;
  const backupTime = parseLocalDateTime(value);
  if (!backupTime) return false;
  return now.getTime() - backupTime.getTime() > 24 * 60 * 60 * 1000;
}

function parseLocalDateTime(value) {
  const text = String(value || "").trim();
  const match = text.match(/^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}):(\d{2}))?/);
  if (!match) return null;
  return new Date(
    Number(match[1]),
    Number(match[2]) - 1,
    Number(match[3]),
    Number(match[4] || 0),
    Number(match[5] || 0),
    Number(match[6] || 0),
  );
}

function isPrimaryTenant(item) {
  const role = String(item.tenant_role || "").toLowerCase();
  return role.includes("primary") || role === "主" || role.includes("leader");
}

function tenantRoleClass(item) {
  if (isPrimaryTenant(item)) return "tenant-primary";
  if (isStandbyTenant(item)) return "tenant-standby";
  return "";
}

function tenantRowClass(item) {
  if (isPrimaryTenant(item)) return "tenant-row-primary";
  if (isStandbyTenant(item)) return "tenant-row-standby";
  return "";
}

function primaryZoneCell(item) {
  const zone = safe(item.primary_zone || "-");
  const resources = item.zone_resource_summary ? `<br><small>${safe(item.zone_resource_summary)}</small>` : "";
  return `${zone}${resources}`;
}

function renderServers(rows) {
  els.serverRows.innerHTML = rows.length ? rows.map((item) => `
    <tr>
      <td>${safe(item.id)}</td>
      <td>${safe(item.hostname)}</td>
      <td>${safe(item.ip)}</td>
      <td>${safe(item.idc)}</td>
      <td>${safe(item.rack)}</td>
      <td>${safe(item.os_version)}</td>
      <td>${safe(item.cpu_cores)}</td>
      <td>${safe(item.memory_gb)}</td>
      <td>${safe(item.disk_gb)}</td>
      <td>${badge(item.status)}</td>
      <td>${safe(item.owner)}</td>
    </tr>
  `).join("") : emptyRow(11, "暂无服务器资产，请点击“新增服务器”录入。");
}

function renderLogs(rows) {
  els.logRows.innerHTML = rows.length ? rows.map((item) => `
    <tr>
      <td>${safe(item.event_time || item.created_at)}</td>
      <td>${badge(item.severity)}</td>
      <td>${safe(item.cluster_name || item.cluster_id)}</td>
      <td>${safe(item.server_ip)}</td>
      <td>${safe(item.error_code)}</td>
      <td>${safe(item.component)}</td>
      <td class="message-cell">${safe(item.message)}</td>
    </tr>
  `).join("") : emptyRow(7, "暂无日志事件。");
}

function renderJobs(rows) {
  els.jobRows.innerHTML = rows.length ? rows.map((item) => `
    <tr>
      <td>${safe(item.id)}</td>
      <td>${safe(item.started_at)}</td>
      <td>${safe(item.cluster_name || item.cluster_id || "-")}</td>
      <td>${safe(item.target_type)}</td>
      <td>${badge(item.status)}</td>
      <td class="message-cell" title="${safe(item.message)}">${safe(item.message)}</td>
    </tr>
  `).join("") : emptyRow(6, "暂无采集任务。点击集群行的“测试连接”或“只读采集”后会在这里记录结果。");
}

function renderSysHealth(rows) {
  els.sysHealthRows.innerHTML = rows.length ? rows.map((item) => `
    <tr class="${item.status && item.status !== "success" ? "health-row-bad" : ""}">
      <td>${safe(item.cluster_name)}</td>
      <td>${safe(item.endpoint)}:${safe(item.port)}</td>
      <td>${safe(item.sys_user)}</td>
      <td>${badge(item.status || (Number(item.has_password || 0) ? "unchecked" : "failed"))}</td>
      <td>${safe(item.checked_at || "-")}</td>
      <td class="message-cell" title="${safe(item.message || "")}">${safe(item.message || (Number(item.has_password || 0) ? "尚未检查" : "未配置密码"))}</td>
    </tr>
  `).join("") : emptyRow(6, "暂无集群。");
}

function renderOcpConnections(rows) {
  els.ocpRows.innerHTML = rows.length ? rows.map((item) => `
    <tr>
      <td>${safe(item.id)}</td>
      <td>${safe(item.name)}</td>
      <td>${safe(item.base_url)}</td>
      <td>${safe(item.auth_type)}</td>
      <td>${safe(item.username)}</td>
      <td>${badge(item.status)}</td>
      <td>${safe(item.last_sync_at || "-")}</td>
      <td class="row-actions"><button class="small danger" data-delete-ocp="${safe(item.id)}">删除本地配置</button></td>
    </tr>
  `).join("") : emptyRow(8, "暂无 OCP 接入配置。");
  document.querySelectorAll("[data-delete-ocp]").forEach((button) => {
    button.addEventListener("click", () => deleteOcpConnection(button.dataset.deleteOcp));
  });
}

function emptyRow(cols, text) {
  return `<tr><td class="empty" colspan="${cols}">${safe(text)}</td></tr>`;
}

function badge(value) {
  const text = String(value || "unknown");
  return `<span class="badge ${safe(text.toLowerCase())}">${safe(text)}</span>`;
}

function usageText(used, total, pct) {
  const usedText = valueText(used);
  const totalText = valueText(total);
  const pctText = valueText(pct);
  if (usedText === "-" && totalText === "-" && pctText === "-") return "-";
  const capacity = totalText === "-" ? `${usedText}GB` : `${usedText}/${totalText}GB`;
  return `${capacity}${pctText === "-" ? "" : `<br><small>${pctText}%</small>`}`;
}

function valueText(value) {
  if (value === null || value === undefined || value === "") return "-";
  const number = Number(value);
  if (Number.isFinite(number)) return String(Math.round(number * 100) / 100);
  return safe(value);
}

function usagePlain(used, total, pct) {
  const usedText = valueText(used);
  const totalText = valueText(total);
  const pctText = valueText(pct);
  if (usedText === "-" && totalText === "-" && pctText === "-") return "-";
  const capacity = totalText === "-" ? `${usedText}GB` : `${usedText}/${totalText}GB`;
  return `${capacity}${pctText === "-" ? "" : ` (${pctText}%)`}`;
}

function mergePlain(time, status) {
  if (!time && !status) return "-";
  return `${time || "-"}${status ? ` (${status})` : ""}`;
}

function formToPayload(form) {
  const payload = Object.fromEntries(new FormData(form).entries());
  if (payload.cluster_id === "") delete payload.cluster_id;
  if (form.id === "ocpForm") payload.verify_ssl = Boolean(form.elements.verify_ssl.checked);
  for (const key of ["port", "cpu_cores", "memory_gb", "disk_gb", "ssh_port"]) {
    if (payload[key] !== undefined) payload[key] = Number(payload[key] || 0);
  }
  return payload;
}

function openModal(id) {
  document.querySelector(`#${id}`).classList.add("open");
}

function scrollToSection(id) {
  const section = document.querySelector(`#${id}`);
  if (section) section.scrollIntoView({ behavior: "smooth", block: "start" });
}

function closeModal(id) {
  document.querySelector(`#${id}`).classList.remove("open");
}

function setStatus(message) {
  if (!message) {
    els.statusBanner.classList.add("hidden");
    els.statusBanner.textContent = "";
    return;
  }
  els.statusBanner.textContent = message;
  els.statusBanner.classList.remove("hidden");
}

function showToast(message) {
  els.toast.textContent = message;
  els.toast.classList.add("show");
  setTimeout(() => els.toast.classList.remove("show"), 2600);
}

function safe(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  }[char]));
}

function resetClusterDefaults(form) {
  form.elements.port.value = 2881;
  form.elements.sys_user.value = "root@sys";
  form.elements.sys_password.value = "";
  form.elements.version.value = "4.2.1.8";
  form.elements.owner.value = "DBA";
}

function resetServerDefaults(form) {
  form.elements.os_version.value = "RHEL 7.9";
  form.elements.cpu_cores.value = 0;
  form.elements.memory_gb.value = 0;
  form.elements.disk_gb.value = 0;
  form.elements.ssh_port.value = 22;
  form.elements.owner.value = "DBA";
}

document.querySelectorAll("[data-open]").forEach((button) => {
  button.addEventListener("click", () => openModal(button.dataset.open));
});

document.querySelectorAll("[data-close]").forEach((button) => {
  button.addEventListener("click", () => closeModal(button.dataset.close));
});

document.querySelector("#refreshNow").addEventListener("click", loadAll);
document.querySelector("#checkSysNow").addEventListener("click", async () => {
  try {
    showToast("正在检查所有集群 sys 租户...");
    const result = await api("/api/sys-tenant-checks/run", { method: "POST", body: "{}" });
    showToast(`sys租户检查完成：成功${result.success || 0}，失败${result.failed || 0}`);
    await loadAll();
  } catch (error) {
    showToast(error.message);
    setStatus(`sys租户检查失败：${error.message}`);
  }
});
els.refreshSeconds.addEventListener("change", () => {
  clearInterval(state.refreshTimer);
  state.refreshTimer = setInterval(loadAll, Number(els.refreshSeconds.value) * 1000);
});

document.querySelector("#syncOcp").addEventListener("click", async () => {
  try {
    if (!state.ocpConnections.length) {
      showToast("请先配置 OCP 接入");
      openModal("ocpModal");
      return;
    }
    const latest = state.ocpConnections[0];
    let result = await syncOcpConnection(latest.id, false);
    if (result.duplicate_confirmation_required) {
      const examples = (result.duplicate_database_examples || [])
        .map((item) => `${item.cluster}/${item.tenant}/${item.database}`)
        .join("\n");
      const more = result.duplicate_databases > (result.duplicate_database_examples || []).length
        ? `\n...另有 ${result.duplicate_databases - result.duplicate_database_examples.length} 个重复数据库`
        : "";
      const confirmed = confirm(
        `发现 ${result.duplicate_databases || 0} 个同集群、同租户、同名数据库。\n` +
        "本次已跳过这些重复数据库，并继续导入其它资产。\n\n" +
        `${examples}${more}\n\n是否确认更新这些重复数据库的信息？`
      );
      if (confirmed) {
        result = await syncOcpConnection(latest.id, true);
      }
    }
    showToast(`OCP同步完成：集群${result.clusters || 0}，OBServer${result.observers || 0}，租户${result.tenants || 0}，数据库${result.databases || 0}，重复数据库${result.duplicate_databases || 0}`);
    await loadAll();
  } catch (error) {
    showToast(error.message);
  }
});

function syncOcpConnection(connectionId, confirmDuplicateDatabases) {
  return api(`/api/ocp/connections/${connectionId}/sync`, {
    method: "POST",
    body: JSON.stringify({ confirm_duplicate_databases: confirmDuplicateDatabases }),
  });
}

function exportTenantExcel() {
  const params = new URLSearchParams({
    cluster_id: els.tenantClusterFilter.value || "",
    hide_standby: els.hideStandbyTenants.checked ? "1" : "0",
    hide_meta: els.hideMetaTenants.checked ? "1" : "0",
  });
  window.location.href = `/api/tenants/export.xlsx?${params.toString()}`;
}

async function collectCluster(clusterId) {
  try {
    showToast("正在只读采集 OB 元数据...");
    const result = await api(`/api/clusters/${clusterId}/collect`, { method: "POST", body: "{}" });
    showToast(`只读采集完成：OBServer${result.observers || 0}，租户${result.tenants || 0}，备份${result.tenant_backups || 0}，磁盘${result.tenant_disk_usage || 0}，合并${result.tenant_merges || 0}`);
    if (result.warnings && result.warnings.length) {
      setStatus(`采集提示：${result.warnings.join("；")}`);
    }
    await loadAll();
  } catch (error) {
    showToast(error.message);
    setStatus(`只读采集失败：${error.message}`);
    await loadAll();
    scrollToSection("jobsSection");
  }
}

async function probeCluster(clusterId) {
  try {
    showToast("正在测试 OB 只读连接...");
    const result = await api(`/api/clusters/${clusterId}/probe`, { method: "POST", body: "{}" });
    showToast(result.message || "OB 连接测试通过");
    await loadAll();
    scrollToSection("jobsSection");
  } catch (error) {
    showToast(error.message);
    setStatus(`连接测试失败：${error.message}`);
    await loadAll();
    scrollToSection("jobsSection");
  }
}

async function openClusterDetail(clusterId) {
  try {
    const data = await api(`/api/clusters/${clusterId}`);
    document.querySelector("#detailTitle").textContent = `集群详情：${data.cluster.name}`;
    document.querySelector("#detailBody").innerHTML = renderClusterDetail(data);
    openModal("detailModal");
  } catch (error) {
    showToast(error.message);
  }
}

async function openTenantDetail(tenantId) {
  try {
    const data = await api(`/api/tenants/${tenantId}`);
    document.querySelector("#detailTitle").textContent = `租户详情：${data.tenant.name}`;
    document.querySelector("#detailBody").innerHTML = renderTenantDetail(data);
    openModal("detailModal");
    bindTenantDetailActions(tenantId);
  } catch (error) {
    showToast(error.message);
  }
}

function renderTenantDetail(data) {
  const t = data.tenant;
  const conn = data.connection || {};
  const runtime = data.runtime || {};
  const schedule = data.schedule || {};
  return `
    <div class="detail-grid">
      <div><span>集群</span><b>${safe(t.cluster_name)}</b></div>
      <div><span>租户</span><b>${safe(t.name)}</b></div>
      <div><span>角色</span><b class="${tenantRoleClass(t)}">${safe(t.tenant_role || "-")}</b></div>
      <div><span>模式</span><b>${safe(t.tenant_mode)}</b></div>
      <div><span>Unit数</span><b>${safe(t.unit_num || "-")}</b></div>
      <div><span>CPU</span><b>${valueText(t.cpu_cores)}</b></div>
      <div><span>内存GB</span><b>${valueText(t.memory_gb)}</b></div>
      <div><span>最近采集</span><b>${safe(runtime.collected_at || "-")}</b></div>
      <button class="detail-metric" data-runtime-history="${safe(t.id)}"><span>当前进程数</span><b>${safe(runtime.current_processes ?? "-")}</b></button>
      <button class="detail-metric" data-runtime-history="${safe(t.id)}"><span>最大进程数</span><b>${safe(runtime.max_processes ?? "-")}</b></button>
    </div>
    <section class="detail-section">
      <h3>租户连接</h3>
      <form id="tenantConnectionForm" class="inline-form" data-oracle-tenant="${isOracleTenant(t) ? "1" : "0"}">
        <label>用户名<input name="tenant_user" value="${safe(baseTenantUser(conn.tenant_user || ""))}" placeholder="例如 root"></label>
        <label>固定租户/集群<input value="@${safe(t.name)}#${safe(t.cluster_name)}" disabled></label>
        <label>实际登录用户<input id="tenantLoginPreview" value="${safe(buildTenantLoginPreview(conn.tenant_user || "", t))}" disabled></label>
        <label>${isOracleTenant(t) ? "服务名" : "默认库"}<input name="database_name" value="${safe(conn.database_name || "")}" placeholder="${isOracleTenant(t) ? safe(t.name) : "可留空"}"></label>
        <label>密码<input name="tenant_password" type="password" placeholder="${conn.has_password ? "留空保留原密码" : "请输入密码"}"></label>
        <button type="submit" class="primary">保存连接</button>
        <button type="button" data-tenant-test="${safe(t.id)}">测试连接</button>
        <button type="button" data-tenant-collect="${safe(t.id)}">立刻采集</button>
      </form>
    </section>
    <section class="detail-section">
      <h3>定时采集</h3>
      <form id="tenantScheduleForm" class="inline-form">
        <label class="check"><input name="enabled" type="checkbox" ${Number(schedule.enabled ?? 0) ? "checked" : ""}> 启用</label>
        <label>频率
          <select name="frequency">
            <option value="daily" ${schedule.frequency === "daily" ? "selected" : ""}>每天</option>
            <option value="workday" ${schedule.frequency === "workday" ? "selected" : ""}>工作日</option>
            <option value="weekly" ${schedule.frequency === "weekly" ? "selected" : ""}>每周</option>
            <option value="monthly" ${schedule.frequency === "monthly" ? "selected" : ""}>每月</option>
          </select>
        </label>
        <label>时间<input name="run_time" type="time" value="${safe(schedule.run_time || "07:00")}"></label>
        <label data-weekly-field>每周
          <select name="day_of_week">
            ${[1, 2, 3, 4, 5, 6, 7].map((day) => `<option value="${day}" ${Number(schedule.day_of_week || 1) === day ? "selected" : ""}>周${"一二三四五六日"[day - 1]}</option>`).join("")}
          </select>
        </label>
        <label data-monthly-field>每月几号<input name="day_of_month" type="number" min="1" max="31" value="${safe(schedule.day_of_month || 1)}"></label>
        <button type="submit" class="primary">保存计划</button>
        <p class="schedule-summary" id="scheduleSummary"></p>
      </form>
    </section>
    ${detailTable("十大容量对象", ["数据库", "对象", "类型", "数据GB", "索引GB", "总GB", "行数", "采集时间"], data.top_objects.map((o) => [
      o.database_name,
      htmlCell(`<button class="link-button" data-object-history="${encodeURIComponent(JSON.stringify({tenant_id: t.id, database_name: o.database_name, object_name: o.object_name, object_type: o.object_type}))}">${safe(o.object_name)}</button>`),
      o.object_type,
      o.data_gb,
      o.index_gb,
      o.total_gb,
      o.table_rows,
      o.collected_at,
    ]))}
    ${detailTable("近一天租户报错", ["时间", "级别", "主机", "错误码", "组件", "事件"], data.errors.map((e) => [
      e.event_time,
      e.severity,
      e.server_ip,
      e.error_code,
      e.component,
      e.message,
    ]))}
  `;
}

function bindTenantDetailActions(tenantId) {
  const form = document.querySelector("#tenantConnectionForm");
  bindScheduleForm();
  bindTenantLoginPreview();
  document.querySelectorAll("[data-runtime-history]").forEach((button) => {
    button.addEventListener("click", () => openRuntimeHistory(tenantId));
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api(`/api/tenants/${tenantId}/connection`, { method: "POST", body: JSON.stringify(formToPayload(form)) });
      showToast("租户采集账号已保存");
      await openTenantDetail(tenantId);
    } catch (error) {
      showToast(error.message);
    }
  });
  document.querySelector("[data-tenant-test]").addEventListener("click", async () => {
    try {
      const result = await api(`/api/tenants/${tenantId}/connection/test`, { method: "POST", body: JSON.stringify(formToPayload(form)) });
      showToast(result.message || "租户连接测试成功");
      await loadAll();
    } catch (error) {
      showToast(error.message);
      setStatus(`租户连接测试失败：${error.message}`);
      await loadAll();
      scrollToSection("jobsSection");
    }
  });
  document.querySelector("#tenantScheduleForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const payload = formToPayload(event.currentTarget);
      payload.enabled = Boolean(event.currentTarget.elements.enabled.checked);
      await api(`/api/tenants/${tenantId}/schedule`, { method: "POST", body: JSON.stringify(payload) });
      showToast("定时采集计划已保存");
      await openTenantDetail(tenantId);
    } catch (error) {
      showToast(error.message);
      setStatus(`租户详情采集失败：${error.message}`);
      await loadAll();
      scrollToSection("jobsSection");
    }
  });
  document.querySelector("[data-tenant-collect]").addEventListener("click", async () => {
    try {
      showToast("正在只读采集租户详情...");
      const result = await api(`/api/tenants/${tenantId}/collect-detail`, { method: "POST", body: "{}" });
      showToast(`租户采集完成：十大对象${result.top_objects || 0}，运行指标${result.runtime_metrics || 0}`);
      await openTenantDetail(tenantId);
    } catch (error) {
      showToast(error.message);
    }
  });
  document.querySelectorAll("[data-object-history]").forEach((button) => {
    button.addEventListener("click", () => openObjectHistory(JSON.parse(decodeURIComponent(button.dataset.objectHistory))));
  });
}

function bindTenantLoginPreview() {
  const form = document.querySelector("#tenantConnectionForm");
  const input = form.elements.tenant_user;
  const preview = document.querySelector("#tenantLoginPreview");
  const suffix = form.querySelector("input[disabled]").value;
  const oracleTenant = form.dataset.oracleTenant === "1";
  const update = () => {
    input.value = baseTenantUser(input.value);
    preview.value = `${input.value || ""}${suffix}`;
  };
  input.addEventListener("input", update);
  input.addEventListener("blur", update);
  update();
}

function baseTenantUser(user) {
  return String(user || "").split("@")[0].split("#")[0].trim();
}

function buildTenantLoginPreview(user, tenant) {
  const base = baseTenantUser(user);
  return `${base || ""}@${tenant.name}#${tenant.cluster_name}`;
}

function isOracleTenant(tenant) {
  return String(tenant.tenant_mode || "").toUpperCase() === "ORACLE";
}

function bindScheduleForm() {
  const form = document.querySelector("#tenantScheduleForm");
  const frequency = form.elements.frequency;
  const update = () => updateScheduleVisibility(form);
  frequency.addEventListener("change", update);
  form.elements.run_time.addEventListener("change", update);
  form.elements.day_of_week.addEventListener("change", update);
  form.elements.day_of_month.addEventListener("input", update);
  update();
}

function updateScheduleVisibility(form) {
  const frequency = form.elements.frequency.value;
  const weeklyField = form.querySelector("[data-weekly-field]");
  const monthlyField = form.querySelector("[data-monthly-field]");
  weeklyField.classList.toggle("hidden", frequency !== "weekly");
  monthlyField.classList.toggle("hidden", frequency !== "monthly");
  const runTime = form.elements.run_time.value || "07:00";
  const dayText = form.elements.day_of_week.options[form.elements.day_of_week.selectedIndex]?.textContent || "周一";
  const monthDay = form.elements.day_of_month.value || "1";
  const summaryByFrequency = {
    daily: `每天 ${runTime} 执行一次租户只读采集。`,
    workday: `每个工作日（周一到周五）${runTime} 执行一次租户只读采集。`,
    weekly: `每周${dayText.replace("周", "")} ${runTime} 执行一次租户只读采集。`,
    monthly: `每月 ${monthDay} 号 ${runTime} 执行一次租户只读采集。`,
  };
  form.querySelector("#scheduleSummary").textContent = summaryByFrequency[frequency] || "";
}

async function openRuntimeHistory(tenantId) {
  try {
    const rows = await api(`/api/tenants/${tenantId}/runtime/history`);
    document.querySelector("#detailTitle").textContent = "进程数近10次采集";
    document.querySelector("#detailBody").innerHTML = `
      <div class="history-chart">${renderRuntimeHistoryChart([...rows].reverse())}</div>
      ${detailTable("近10次进程快照", ["采集时间", "当前进程数", "最大进程数"], rows.map((row) => [
        row.collected_at,
        row.current_processes,
        row.max_processes,
      ]))}
    `;
  } catch (error) {
    showToast(error.message);
  }
}

async function openObjectHistory(config) {
  try {
    const params = new URLSearchParams({
      database_name: config.database_name || "",
      object_name: config.object_name || "",
      object_type: config.object_type || "",
    });
    const rows = await api(`/api/tenants/${config.tenant_id}/objects/history?${params.toString()}`);
    document.querySelector("#detailTitle").textContent = `对象容量历史：${config.object_name}`;
    document.querySelector("#detailBody").innerHTML = renderObjectHistory(config, rows);
  } catch (error) {
    showToast(error.message);
  }
}

function renderObjectHistory(config, rows) {
  const chronological = [...rows].reverse();
  return `
    <div class="detail-grid">
      <div><span>数据库</span><b>${safe(config.database_name || "-")}</b></div>
      <div><span>对象</span><b>${safe(config.object_name)}</b></div>
      <div><span>类型</span><b>${safe(config.object_type)}</b></div>
    </div>
    <div class="history-chart">${renderLineChart(chronological)}</div>
    ${detailTable("近10次容量快照", ["采集时间", "数据GB", "索引GB", "总GB", "行数"], rows.map((row) => [
      row.collected_at,
      row.data_gb,
      row.index_gb,
      row.total_gb,
      row.table_rows,
    ]))}
  `;
}

function renderLineChart(rows) {
  if (!rows.length) return `<div class="empty-chart">暂无历史数据</div>`;
  const width = 720;
  const height = 220;
  const pad = 28;
  const values = rows.map((row) => Number(row.total_gb || 0));
  const max = Math.max(...values, 1);
  const points = values.map((value, index) => {
    const x = rows.length === 1 ? width / 2 : pad + (index * (width - pad * 2)) / (rows.length - 1);
    const y = height - pad - (value * (height - pad * 2)) / max;
    return { x, y, value, label: rows[index].collected_at };
  });
  const polyline = points.map((point) => `${point.x},${point.y}`).join(" ");
  return `
    <svg class="line-chart" viewBox="0 0 ${width} ${height}" role="img">
      <line x1="${pad}" y1="${height - pad}" x2="${width - pad}" y2="${height - pad}" class="axis"></line>
      <line x1="${pad}" y1="${pad}" x2="${pad}" y2="${height - pad}" class="axis"></line>
      <polyline points="${polyline}" class="capacity-line"></polyline>
      ${points.map((point) => `<circle cx="${point.x}" cy="${point.y}" r="4" class="capacity-point"><title>${safe(point.label)} ${formatNumber(point.value)}GB</title></circle>`).join("")}
      <text x="${pad}" y="18" class="chart-label">总容量GB，最大 ${formatNumber(max)}GB</text>
    </svg>
  `;
}

function renderRuntimeHistoryChart(rows) {
  if (!rows.length) return `<div class="empty-chart">暂无进程历史数据</div>`;
  const width = 720;
  const height = 220;
  const pad = 28;
  const currentValues = rows.map((row) => Number(row.current_processes || 0));
  const maxValues = rows.map((row) => Number(row.max_processes || 0));
  const max = Math.max(...currentValues, ...maxValues, 1);
  const currentPoints = chartPoints(currentValues, width, height, pad, max);
  const maxPoints = chartPoints(maxValues, width, height, pad, max);
  return `
    <svg class="line-chart" viewBox="0 0 ${width} ${height}" role="img">
      <line x1="${pad}" y1="${height - pad}" x2="${width - pad}" y2="${height - pad}" class="axis"></line>
      <line x1="${pad}" y1="${pad}" x2="${pad}" y2="${height - pad}" class="axis"></line>
      <polyline points="${currentPoints.map((point) => `${point.x},${point.y}`).join(" ")}" class="capacity-line"></polyline>
      <polyline points="${maxPoints.map((point) => `${point.x},${point.y}`).join(" ")}" class="capacity-line secondary"></polyline>
      ${currentPoints.map((point, index) => `<circle cx="${point.x}" cy="${point.y}" r="4" class="capacity-point"><title>${safe(rows[index].collected_at)} 当前 ${formatNumber(currentValues[index])}</title></circle>`).join("")}
      <text x="${pad}" y="18" class="chart-label">当前进程数 / 最大进程数，最大 ${formatNumber(max)}</text>
    </svg>
  `;
}

function chartPoints(values, width, height, pad, max) {
  return values.map((value, index) => {
    const x = values.length === 1 ? width / 2 : pad + (index * (width - pad * 2)) / (values.length - 1);
    const y = height - pad - (value * (height - pad * 2)) / max;
    return { x, y };
  });
}

function renderClusterDetail(data) {
  const c = data.cluster;
  return `
    <div class="detail-grid">
      <div><span>环境</span><b>${safe(c.environment)}</b></div>
      <div><span>区域</span><b>${safe(c.region)}</b></div>
      <div><span>连接地址</span><b>${safe(c.endpoint)}:${safe(c.port)}</b></div>
      <div><span>系统用户</span><b>${safe(c.sys_user)}</b></div>
      <div><span>版本</span><b>${safe(c.version)}</b></div>
      <div><span>状态</span><b>${safe(c.status)}</b></div>
    </div>
    ${detailTable("租户", ["租户", "模式", "Primary Zone", "主备角色", "Unit数", "上次全备份", "数据盘", "日志盘", "上次成功合并", "状态", "Locality"], data.tenants.map((t) => [
      t.name,
      t.tenant_mode,
      htmlCell(primaryZoneCell(t)),
      t.tenant_role,
      t.unit_num,
      t.last_full_backup_time || "-",
      usagePlain(t.data_disk_used_gb, t.data_disk_total_gb, t.data_disk_usage_pct),
      usagePlain(t.log_disk_used_gb, t.log_disk_total_gb, t.log_disk_usage_pct),
      mergePlain(t.last_success_merge_time, t.last_merge_status),
      t.status,
      t.locality,
    ]))}
    ${detailTable("OBServer", ["Zone", "IP", "SQL端口", "RPC端口", "状态", "磁盘GB"], data.observers.map((o) => [o.zone, o.svr_ip, o.sql_port, o.rpc_port, o.status, o.disk_total_gb]))}
    ${detailTable("参数", ["租户", "参数", "值", "范围", "更新时间"], data.parameters.map((p) => [p.tenant_name || "-", p.name, p.param_value, p.scope, p.updated_at]))}
    ${detailTable("日志", ["时间", "级别", "主机", "错误码", "事件"], data.logs.map((l) => [l.event_time, l.severity, l.server_ip, l.error_code, l.message]))}
  `;
}

function detailTable(title, headers, rows) {
  return `
    <section class="detail-section">
      <h3>${safe(title)}</h3>
      <div class="table-wrap">
        <table>
          <thead><tr>${headers.map((h) => `<th>${safe(h)}</th>`).join("")}</tr></thead>
          <tbody>${rows.length ? rows.map((row) => `<tr>${row.map((cell) => `<td>${renderCell(cell)}</td>`).join("")}</tr>`).join("") : emptyRow(headers.length, "暂无数据")}</tbody>
        </table>
      </div>
    </section>
  `;
}

function htmlCell(html) {
  return { html };
}

function renderCell(cell) {
  if (cell && typeof cell === "object" && Object.prototype.hasOwnProperty.call(cell, "html")) {
    return cell.html;
  }
  return safe(cell);
}

async function deleteCluster(clusterId) {
  if (!confirm("只删除本程序后台资产库中的该集群及关联采集数据，不会修改 OB/OCP。确认删除？")) return;
  try {
    await api(`/api/clusters/${clusterId}`, { method: "DELETE" });
    showToast("本地集群资产已删除");
    await loadAll();
  } catch (error) {
    showToast(error.message);
  }
}

async function deleteOcpConnection(connectionId) {
  if (!confirm("只删除本程序后台资产库中保存的 OCP 接入配置和同步记录，不会修改 OCP。确认删除？")) return;
  try {
    await api(`/api/ocp/connections/${connectionId}`, { method: "DELETE" });
    showToast("本地 OCP 配置已删除");
    await loadAll();
  } catch (error) {
    showToast(error.message);
  }
}

document.querySelector("#clusterForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/api/clusters", { method: "POST", body: JSON.stringify(formToPayload(event.currentTarget)) });
    showToast("OB 集群已保存");
    closeModal("clusterModal");
    event.currentTarget.reset();
    resetClusterDefaults(event.currentTarget);
    await loadAll();
  } catch (error) {
    showToast(error.message);
  }
});

document.querySelector("#serverForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/api/servers", { method: "POST", body: JSON.stringify(formToPayload(event.currentTarget)) });
    showToast("服务器已保存");
    closeModal("serverModal");
    event.currentTarget.reset();
    resetServerDefaults(event.currentTarget);
    await loadAll();
  } catch (error) {
    showToast(error.message);
  }
});

function openCollectConfig(config) {
  const form = document.querySelector("#collectConfigForm");
  form.elements.cluster_id.value = config.id || "";
  form.elements.endpoint.value = config.endpoint || "";
  form.elements.port.value = config.port || 2881;
  form.elements.sys_user.value = config.sys_user || "root@sys";
  form.elements.sys_password.value = "";
  openModal("collectConfigModal");
}

function openClusterSchedule(config) {
  const form = document.querySelector("#clusterScheduleForm");
  form.elements.cluster_id.value = config.id || "";
  form.elements.enabled.checked = Number(config.enabled || 0) === 1;
  form.elements.run_time.value = config.run_time || "07:00";
  openModal("clusterScheduleModal");
}

document.querySelector("#collectConfigForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = formToPayload(event.currentTarget);
  const clusterId = payload.cluster_id;
  delete payload.cluster_id;
  try {
    await api(`/api/clusters/${clusterId}/collect-config`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    showToast("采集配置已保存");
    closeModal("collectConfigModal");
    event.currentTarget.reset();
    await loadAll();
  } catch (error) {
    showToast(error.message);
  }
});

document.querySelector("#clusterScheduleForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = formToPayload(event.currentTarget);
  const clusterId = payload.cluster_id;
  payload.enabled = Boolean(event.currentTarget.elements.enabled.checked);
  delete payload.cluster_id;
  try {
    await api(`/api/clusters/${clusterId}/schedule`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    showToast("集群每日采集计划已保存");
    closeModal("clusterScheduleModal");
    event.currentTarget.reset();
    await loadAll();
  } catch (error) {
    showToast(error.message);
  }
});

document.querySelector("#ocpForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const result = await api("/api/ocp/connections", { method: "POST", body: JSON.stringify(formToPayload(event.currentTarget)) });
    await api(`/api/ocp/connections/${result.id}/test`, { method: "POST", body: "{}" });
    showToast("OCP 配置已保存并测试");
    closeModal("ocpModal");
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
    closeModal("logModal");
    event.currentTarget.reset();
    await loadAll();
  } catch (error) {
    showToast(error.message);
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    document.querySelectorAll(".modal.open").forEach((modal) => modal.classList.remove("open"));
  }
});

function updateTenantFilterStates() {
  setFilterState(els.hideStandbyState, els.hideStandbyTenants.checked);
  setFilterState(els.hideMetaState, els.hideMetaTenants.checked);
}

function setFilterState(element, enabled) {
  element.textContent = enabled ? "已启用" : "未启用";
  element.classList.toggle("enabled", enabled);
  element.classList.toggle("disabled", !enabled);
}

els.hideStandbyTenants.addEventListener("change", () => {
  updateTenantFilterStates();
  renderTenantResourceCharts(state.tenants);
  renderTenants(state.tenants);
});
els.hideMetaTenants.addEventListener("change", () => {
  updateTenantFilterStates();
  renderTenantResourceCharts(state.tenants);
  renderTenants(state.tenants);
});
els.tenantClusterFilter.addEventListener("change", () => {
  renderTenantResourceCharts(state.tenants);
  renderTenants(state.tenants);
});
els.exportTenants.addEventListener("click", exportTenantExcel);

updateTenantFilterStates();
loadAll();
state.refreshTimer = setInterval(loadAll, Number(els.refreshSeconds.value) * 1000);
