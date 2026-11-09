# 部署、迁移与回滚

所有示例均使用占位配置。不要提交真实 config.json、日志、密钥或资产导出。本文不会自动连接或修改任何服务器。

## 1. 配置

在 Linux 安装目录创建虚拟环境并安装 requirements.txt，复制 config.example.json 为 config.json，填写 Oracle 资产库账号和 DSN。Oracle 账号需具备部署所需的建表/索引权限，日常 OB 采集账号仅使用所需视图的只读权限。各集群 SYS 凭据通过平台现有配置保存，不写入源码。

以下假设安装目录为 `/root/gjzqdb`，与 deploy/ 中模板一致。安装在其他位置时同时修改 unit 的 WorkingDirectory、ExecStart、运行账号及配置路径。Web 端口示例为 8000，按现有网络策略限制访问；此应用未内置完整登录与访问控制。

## 2. 初始化或升级 Oracle 资产库

新库与已有库二选一。执行前确认 APP_CONFIG/环境变量确实指向目标资产库。

新库初始化：

```sh
cd /root/gjzqdb
.venv/bin/python -c 'from app import init_db; init_db()'
```

已有库增量升级（先打印审阅，再明确执行）：

```sh
.venv/bin/python schema_migrate.py
.venv/bin/python schema_migrate.py --apply
.venv/bin/python schema_preflight.py
```

增量包括 ACTIVE、合并检查、CPU 计数器/租户样本、缺失确认、CPU 状态以及 SYS CPU 历史表/索引。`migrate_sys_cpu.py` 仅负责 SYS CPU 表/索引子集；不必在完整 schema_migrate 后重复执行。migrations/ 下 SQL 文件可由 DBA 审阅后替代对应 Python 迁移，注意完整增量 SQL 与 SYS 子集范围不同。

Oracle DDL 自动提交，不能用事务 ROLLBACK 撤销。重复运行仅忽略已有对象/列错误，不验证同名对象结构；schema_preflight 会报告缺字段、类型、关键约束及 ACTIVE 异常值。不会批量重置 ACTIVE、恢复计划或删除历史。

如果停用状态可疑，先运行只读核查：

```sh
.venv/bin/python audit_tenant_inventory.py
```

仅对源端仍存在且本地已停用的明确租户逐项决定恢复方式，不将所有 ACTIVE 或计划 ENABLED 统一设为 1。

## 3. Web

沿用现有服务管理方式，示例手动启动：

```sh
cd /root/gjzqdb
mkdir -p logs
nohup /root/gjzqdb/.venv/bin/gunicorn --chdir /root/gjzqdb \
  -w 2 --timeout 180 -b 0.0.0.0:8000 app:app >> logs/gunicorn.log 2>&1 &
curl -sS http://127.0.0.1:8000/api/health
```

不要使用 python app.py 作为生产启动方式，该入口含开发 debug 和初始化行为。升级应完整部署相关模块、静态文件和模板，保留现场配置与虚拟环境，然后重载实际 Web 服务。手动 Gunicorn、没有 --preload 时可向经核验的主进程发送 HUP；由 systemd 管理的 Web 使用原服务的重启方式，不批量终止其他应用进程。

## 4. 自动采集

部署前核对四个 unit 的路径、账号、TZ、配置来源。新增定时器不会自动安装，需明确执行：

```sh
cp deploy/ob-cpu-history.service deploy/ob-cpu-history.timer /etc/systemd/system/
cp deploy/ob-homepage-hourly.service deploy/ob-homepage-hourly.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ob-cpu-history.timer ob-homepage-hourly.timer
systemctl start ob-cpu-history.service
systemctl start ob-homepage-hourly.service
```

- CPU 每 5 分钟触发，按集群锁和 290 秒检查防重复。第一次仅建立基线，至少两次有效采样才有历史。
- 首页每小时刷新 SYS/合并、集群/租户清单与状态、备份/资源/磁盘/参数及已启用日志源，保留手工入口。OCP 资产导入不自动执行。
- 原 run_tenant_schedules.py 可继续由已有 cron 调用，保留每日集群与租户专项计划，并检查小时基础任务是否到期。小时任务与原调度共用本机锁，数据库任务记录避免重复小时批次；每日/专项计划可能额外执行。
- 已停用日志源及租户专项计划不被自动开启，缺少 SYS 凭据的集群会跳过并记录。停采租户保留历史，不显示在 CPU 选项中。
- CPU 专用服务独立于长耗时的首页/租户任务；小时任务等待原调度锁，最长运行时限见 service。

检查调度与真实样本时间：

```sh
systemctl list-timers --all | grep -E 'ob-cpu-history|ob-homepage-hourly'
journalctl -u ob-cpu-history.service -n 60 --no-pager
journalctl -u ob-homepage-hourly.service -n 60 --no-pager
curl -sS http://127.0.0.1:8000/api/cpu-history/status
```

服务正常退出也可能表示本轮因间隔不足而跳过。CPU 状态页目前需手工刷新；不要用停留页面的时间判断后台停采。

## 5. 验收

核对集群数量、SYS 列表及合并原因、风险卡片/明细/导出数量一致。对 ACTIVE 停用流程验证完整/空/失败/部分清单，至少两次确认且间隔 30 分钟才软停采；验证重新出现不自动恢复旧计划。某个指标查询失败不应清空已知备份/资源数据。

检查 CPU 普通/SYS 样本、meta 显示开关、隐藏停采租户、7/14/30/60 天、缺采断线和 CSV，并与 OCP 同时间段对照。抽查原资源饼图、详情、十大对象、运行指标、计划、日志和 OCP 同步。离线测试不能替代这些现场验收。

## 6. 保留数据回滚

暂停新定时器及正在运行的对应采集服务；恢复上线前已验证的完整源码和原服务配置，保留现场 config.json；重载 Web 并恢复原调度。保留扩展表及 CPU 历史，不默认 DROP，不批量改 ACTIVE。若需恢复特定租户/计划状态，使用明确记录和源端只读核验逐项处理。DDL 回滚或整库恢复另行评估，不混入源码替换操作。
