function renderTenantRankings({processes, tables}) {
  const escape = value => String(value ?? "—").replace(/[&<>"']/g, char =>
    ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"})[char]);
  const tenantLink = row => `<a href="/?tenant=${Number(row.tenant_id)}">${escape(row.cluster_name)} / ${escape(row.tenant_name)}</a>`;
  const render = (id, rows, columns) => {
    document.getElementById(id).innerHTML = rows.map((row, index) =>
      `<tr><td>${index + 1}</td><td>${tenantLink(row)}</td>${columns.map(key =>
        `<td>${escape(row[key])}</td>`).join("")}</tr>`
    ).join("") || `<tr><td colspan="${columns.length + 2}">暂无采集数据</td></tr>`;
  };
  render("tenantProcessRanks", processes, ["current_processes", "collected_at"]);
  render("tenantTableRanks", tables, ["database_name", "object_name", "total_gb", "collected_at"]);
}
