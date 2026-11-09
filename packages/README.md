# 部署增量包

- [业务租户每日巡检与实际版本更新 ZIP](OB_monitor_daily_inspection_update.zip)
- [SHA-256 校验文件](SHA256SUMS.txt)
- [部署与登录规则](../DAILY_INSPECTION.md)

此增量包对应应用源码提交 `09fa74bd99382afb15137867d961e58f7f186ad5`，适用于已安装此前完整修复版本的服务器，不是全新安装包。压缩包中的代码、迁移和说明与本次交付一致，不包含真实配置、密码、密钥、日志或监控数据。

部署前先执行新增巡检迁移，再替换和启用定时器。默认未启用租户登录，需要在巡检页保存配置并启用。69 项离线后端测试及前端检查通过，真实 Oracle/OBClient/目标 OB 环境仍需验证。

## 2026-11-09 验收版

[巡检验收版 ZIP](OB_monitor_inspection_20261109.zip) 是增量包，适用于已部署 11:46 完整修复版（`681d0d2`）的环境，也可覆盖同日的巡检增量版本。压缩包保留应用相对目录。

该版本包含独立手工采集、首页容量与进程排名、租户日曲线、最新结果、容量快照完整性校验及参数视图回退。已有环境先用现有虚拟环境和资产库配置执行包内 `migrate_inspection.py --apply`，再备份并覆盖应用文件、重载 Web、更新并重启巡检 timer。迁移新增手工采集任务及保护状态表，保留现有定时巡检记录。

## 资产管理和 ODP 连接统计增量

部署包 `OB_monitor_assets_odp_20261109.zip` 基于同日巡检验收版。合并现场配置后覆盖包内文件，重载 Web 进程。ODP 统计随既有首页小时采集服务运行。此增量无需数据库迁移。

在现场 `config.json` 加入 `ob_assets` 数组，数组顺序即页面显示顺序。以下使用文档示例地址：

```json
{
  "ob_assets": [
    {"name":"demo_cluster","hosts":"192.0.2.11–13","lb":"192.0.2.20","purpose":"示例集群","port":2883},
    {"name":"demo_backup","hosts":"192.0.2.31","endpoint":"192.0.2.31","purpose":"示例单机","port":2881}
  ],
  "odp_monitor": {
    "metrics_port": 2884,
    "client_max_connections_by_ip": {"192.0.2.11": 30000}
  }
}
```

`oblb` 可选，用于标注独立入口。已有内网资产页的现场部署保留现有资产数据，覆盖源码前先将其迁入现场配置。

## ODP 连接上限自动采集增量

[ODP 自动采集增量包](OB_monitor_odp_auto_limits_update.zip) 基于资产管理和 ODP 连接统计增量。部署后重载 Web，运行一次首页小时采集服务。无需数据库迁移。

小时采集使用已有的集群 SYS 凭据逐台查询 ODP 的 `client_max_connections`。默认 SQL 端口为 2883，特殊节点可在 `odp_monitor.sql_port_by_ip` 中覆盖。此前的 `client_max_connections_by_ip` 静态上限配置已由自动查询取代。查询失败的节点和错误码可在首页小时采集服务日志中查看。
