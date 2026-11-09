# 实际集群版本与业务租户每日巡检

## 行为

集群版本不再默认填写4.2.1.8。SYS资产采集优先读取 `oceanbase.DBA_OB_SERVERS.BUILD_VERSION`，失败时才读取带OceanBase标识的版本字符串；成功后更新现有集群版本。滚动升级发现不同节点版本时并列显示。读取失败保留旧值并提示警告，不猜测版本。新增/OCP导入时没有真实版本就留空，页面显示“待采集”。已有错误缓存要等下一次成功的集群采集才更新，也可手动点一次集群只读采集。

首页新增“业务租户每日巡检”链接，路径 `/tenant-inspection`。页面包含Oracle/MySQL用户名、首选和次选两个密码框、每日时间、集群范围、分模式参数基线、结果与异常列表。它将原有业务租户对象/运行指标写入原表，原详情和历史继续可读。

## 登录状态机（以每个业务租户为单位）

1. 默认先试首选，认证成功立即停止密码尝试，在同一连接里完成对象、运行指标和参数读取，不再登录次选。
2. 只有明确的认证失败才允许尝试另一密码，各最多一次。网络异常、锁定账号、连接结果不确定、SQL失败不会触发换密码或重连；不确定状态要求人工核查。
3. 首选失败、次选成功后，记录“次选优先”，以后先使用次选。记住的优先密码认证失败时，仅允许另一密码一次。页面显示实际使用的密码槽位和后续优先顺序。
4. 两个密码均认证失败（或仅配置一个且失败）后永久锁住自动巡检，不因跨日、重启、重复点击、改参数基线或保存新密码而自动解锁。
5. 用户先在源端修正密码，再点击结果行“确认密码已修正”，勾选确认框，提交后最早次日恢复；当天已有尝试记录不删除、不重置。确认后从首选优先开始，再按成功槽位记忆。
6. 登录前先在Oracle提交每日唯一记录和密码槽位占用，重复进程无法重新领取当天预算。身份键按集群名+租户名规范化生成，不因本地资产ID重建而重置；记录和锁不随普通资产删除自动删除。中断且无法确认完成的旧任务阻止次日登录，需人工核查确认。
7. 每日任务跨日时停止后续旧巡检日登录；旧任务未确认的结果也不会盲目重试。只统计应用发起的连接，无法控制用户在其他客户端自行登录。

首选/次选是一对公共巡检密码，作用于页面所选集群内的有效业务租户；Oracle默认SYS，MySQL默认root。不是为每个租户自动猜账号或轮询更多密码。范围内原手工连接测试、详情采集及旧租户专项计划交由本巡检页管理，不能绕过当天预算与锁。SYS基础监控和CPU是不同的系统租户连接，仍按原频率运行。

## 单连接实现

MySQL仅调用一次PyMySQL连接，不使用连接池、不调用重连。Oracle沿用目标环境的obclient，但一次进程批量执行全部只读SELECT，显式 `--skip-reconnect`，不逐条查询重新登录；初始认证错误与已登录后的SQL错误分开处理。查询兼容性候选也在同一连接执行，不重新测试账号。Oracle客户端路径可通过OBCLIENT_BIN指定，部署前必须验证版本支持相关选项。

参考[OceanBase版本查询说明](https://oceanbase.github.io/docs/user_manual/quick_starts/en-US/chapter_05_operation_and_maintenance/o_m_by_sql)及[OBClient禁用自动重连说明](https://en.oceanbase.com/docs/common-obclient-doc-en-10000000003959823)。文档不能代替现场OB/客户端版本与权限验证。

## 参数基线

每行 `参数名=期望值`，Oracle默认核对SYS.V$PARAMETER，MySQL默认核对information_schema.GLOBAL_VARIABLES；`ob:参数名=期望值`核对对应模式OB参数视图。两种模式分开定义，最多200项，参数名受限，不接受任意SQL。不会执行ALTER/SET或修改源端参数。

比较会去除首尾空格，对常见TRUE/ON/YES、FALSE/OFF/NO以及纯数字作规范化；其他字符串区分大小写，不自动换算容量/时间单位。同一参数多个实际值必须全部匹配。不存在、无法读取、无权限不判为匹配，结果单独展示。参数与期望值的快照随当天记录保存，执行中的任务使用启动时配置。

## 密码保存与部署边界

密码使用Fernet加密存入资产库，API只返回“是否已设置”，不回显明文/密文。首次保存密码在安装目录生成0600权限的 `.tenant_inspection.key`，也可通过TENANT_INSPECTION_KEY_FILE指定受保护路径；该密钥不进Git、不进入交付包，不要覆盖或丢失。没有密钥时不发起租户登录。请沿用受控内网/现有反向代理认证与TLS保护访问，此应用尚无完整内置多用户权限体系。

POST只接受JSON并检查浏览器同源。页面没有“修改源端密码”的操作；确认框只是用户对源端已修正的声明。旧连接配置不会被公共巡检密码覆盖。

## 部署（现有安装目录示例 /root/gjzqdb）

上传 OB_monitor_daily_inspection_update.zip 到服务器 /tmp 后：

```sh
unzip -o /tmp/OB_monitor_daily_inspection_update.zip -d /tmp/ob-daily-inspection-update
APP_CONFIG=/root/gjzqdb/config.json /root/gjzqdb/.venv/bin/python /tmp/ob-daily-inspection-update/gjzqdb-daily-inspection-update/migrate_inspection.py --apply
```

本迁移新增三个巡检表并插入默认禁用配置，不连接源端，不清理历史，不写入真实密码；先前完整修复及SYS扩展应已安装。迁移失败停下，不覆盖。Oracle DDL自动提交，已有同名对象不等于结构正确。成功后完整替换本增量包中的文件（保留config.json、虚拟环境、日志和密钥）：

```sh
\cp -rf /tmp/ob-daily-inspection-update/gjzqdb-daily-inspection-update/. /root/gjzqdb/
/root/gjzqdb/.venv/bin/python -c 'from cryptography.fernet import Fernet; print("加密依赖可用")'
```

沿用已有Web管理方式重载。对当前无--preload、8000端口的手动Gunicorn，在核对主进程后可HUP重载，避免操作8002的其他服务：

```sh
pgrep -a -f '[g]unicorn.*0.0.0.0:8000 app:app'
kill -HUP "$(pgrep -o -f '[g]unicorn.*0.0.0.0:8000 app:app')"
curl -sS http://127.0.0.1:8000/api/health
```

如果pgrep为空，先按原方式启动，不执行kill。安装巡检轮询服务：

```sh
cp /root/gjzqdb/deploy/ob-tenant-inspection.service /etc/systemd/system/
cp /root/gjzqdb/deploy/ob-tenant-inspection.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ob-tenant-inspection.timer
```

定时器每分钟检查“每日执行时间/手工队列”，不代表每分钟登录；真正登录由Oracle每日预算控制，一天最多一次巡检。初始配置禁用且无密码，不会立即登录。打开新页面，选择集群、保存账号/密码与参数基线，启用每日任务；“执行今日巡检”只提交队列，后台下一轮处理，重复点击不增加尝试次数。密码留空保留原值，删除次选须勾选“清空次选”。两项密码不得相同。

检查：

```sh
systemctl list-timers --all | grep ob-tenant-inspection
journalctl -u ob-tenant-inspection.service -n 80 --no-pager
```

不要并行保留另一份旧版租户登录脚本/旧程序实例；本次已对当前Web和run_tenant_schedules.py入口增加托管拦截，但无法拦截服务器上独立的历史脚本。新页面每30秒刷新结果，不含明文密码。现有每小时首页、五分钟CPU定时器保持不变。

## 验证与撤回

本地使用模拟驱动/数据库验证登录次数、持久化预算、次选记忆、跨日阻断、人工确认、同连接查询、参数对比与版本读取；现场Oracle SQL、OBClient与目标OB版本仍须隔离验证，不能以离线测试认定生产已验收。

暂停新巡检只需停ob-tenant-inspection.timer/service。若撤回代码，须保持已托管业务租户的旧登录计划停止，不得因恢复旧代码绕过失败锁；保留巡检表、密钥、每日尝试记录和失败名单，不DROP或清空。确认账号修正后才安排后续日期恢复，不在当天手工重试。
