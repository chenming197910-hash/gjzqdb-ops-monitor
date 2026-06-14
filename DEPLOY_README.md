# OB 运维监控平台部署说明

本文档用于现场部署 `gjzqdb-ops-monitor`。系统通过 OCP API 只读获取 OB 集群信息，不修改 OCP 原有配置；本系统自己的数据写入 Oracle 19c PDB 资产库。

## 1. 部署架构

- Web 服务：Flask + Gunicorn
- 数据库：Oracle 19c PDB
- OCP 接口：只读调用 `GET /api/v2/info` 与 `GET /api/v2/ob/clusters`
- OB 接口：只读 SQL 采集，仅允许 `SELECT`
- 默认端口：`8000`
- 推荐系统：RHEL 7.9
- 应用日志：默认写入程序目录 `logs/ob-ops-monitor.log`

## 2. Oracle PDB 准备

现场信息：

- Oracle 地址：`10.50.40.182`
- PDB/Service Name：`gjzqdb`
- 业务用户：`gjzqdbsys`
- 密码：部署时手动输入，不写入文档

如 `gjzqdbsys` 用户尚未创建，使用具备 DBA 权限的账号执行：

```sql
alter session set container = gjzqdb;

create user gjzqdbsys identified by "现场输入的密码";
grant create session, create table, create sequence, create view to gjzqdbsys;
grant unlimited tablespace to gjzqdbsys;
```

如现场已有专用用户，可以跳过创建用户步骤，但需要保证该用户具备建表、建序列、建视图和连接权限。

## 3. 上传程序包

将部署包上传到服务器，例如：

```bash
mkdir -p /opt/ob-asset
tar -xzf gjzqdb-ops-monitor-deploy.tar.gz -C /opt/ob-asset --strip-components=1
cd /opt/ob-asset
```

## 4. 安装 Python 依赖

```bash
sudo yum install -y python3

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

依赖包括：

- Flask
- gunicorn
- oracledb
- requests

`python-oracledb` 默认使用 Thin 模式，通常不需要安装 Oracle Instant Client。

## 5. 修改配置文件

复制配置模板：

```bash
cp config.example.json config.json
vi config.json
```

配置文件内容示例：

```json
{
  "oracle": {
    "user": "gjzqdbsys",
    "password": "请在这里填写数据库密码",
    "dsn": "10.50.40.182:1521/gjzqdb",
    "pool": {
      "min": 1,
      "max": 5,
      "increment": 1
    }
  },
  "app": {
    "port": 8000,
    "log_dir": "logs",
    "default_ocp_version": "4.3.5-20250610160438",
    "default_ob_version": "4.2.1.8"
  }
}
```

密码包含特殊字符时可以直接写在 JSON 字符串里。只需注意：

- 如果密码里包含双引号 `"`，写成 `\"`
- 如果密码里包含反斜杠 `\`，写成 `\\`

例如密码是 `Abc"123\xyz`，JSON 中写成：

```json
"password": "Abc\"123\\xyz"
```

## 6. 初始化资产库

首次部署执行：

```bash
source .venv/bin/activate
python -c "from app import init_db; init_db(seed=True)"
```

该命令会自动创建以下表：

- `clusters`
- `servers`
- `ob_servers`
- `tenants`
- `databases`
- `collection_jobs`
- `ob_log_events`
- `ocp_connections`
- `ocp_sync_runs`

重复执行通常不会破坏已有数据。

## 7. 启动服务

测试启动：

```bash
source .venv/bin/activate
gunicorn -w 2 -b 0.0.0.0:8000 app:app
```

查看服务器侧错误日志：

```bash
tail -f logs/ob-ops-monitor.log
```

浏览器访问：

```text
http://服务器IP:8000/
```

## 8. OCP 接入

页面点击“ OCP接入 ”，填写：

- OCP 地址：例如 `https://ocp.example.com`
- OCP 版本：默认 `4.3.5-20250610160438`
- API 前缀：默认 `/api/v2`
- 认证方式：账号密码或 Bearer Token
- 如果 OCP 使用自签名证书，可以取消“校验HTTPS证书”

保存后系统会测试：

```text
GET /api/v2/info
```

点击“同步OCP”后系统会读取：

```text
GET /api/v2/ob/clusters
```

同步结果只写入本系统 Oracle 资产库，不会修改 OCP。

## 9. 功能说明

- 首页运维全景：展示物理机、虚拟机、故障主机、报警主机、正常主机。
- Sys 租户健康：首页展示所有集群 sys 租户最近一次检查结果，支持手工立即检查。
- OB 集群看板：展示集群、租户、数据库、OBServer 摘要。
- OCP 接入配置：保存 OCP 地址、认证方式、版本和 API 前缀。
- OCP 同步：从 OCP 读取 OB 集群信息并写入 `clusters` 表。
- 手工 OB 集群采集：对目标 OB SQL 入口只执行 `SELECT`，读取租户、OBServer、参数等信息并写入本系统 Oracle 资产库。
- 租户详情采集：MySQL 模式租户使用 PyMySQL；Oracle 模式租户默认调用 `obclient`。两者用户名都按 `用户@租户#集群` 拼接，只执行只读 SQL。
- 日志捕获：粘贴 OB 日志，解析 `WARN`、`ERROR`、`FATAL`、`OB-xxxx`、`ORA-xxxx` 并写入 `ob_log_events`。
- 采集任务与错误：Web 只显示成功/失败摘要；详细错误写入 `logs/ob-ops-monitor.log`。

Oracle 模式租户需要服务器能直接执行 `obclient`：

```bash
which obclient
obclient -h192.168.62.62 -P12883 -u'SYS@thdgxtdb#nqlobcluster' -p -A
```

如果 `obclient` 不在 PATH，可以设置：

```bash
export OBCLIENT_BIN=/path/to/obclient
```

默认不需要设置 `OB_ORACLE_TENANT_DRIVER`。只有现场明确改用其它驱动时才设置为 `oracledb` 或 `pymysql`。

## 10. 定时采集

页面集群列表中的“定时采集”用于配置集群级每日只读采集，只需要设置每天执行时间；“只读采集”按钮仍可随时手工执行。

租户详情页中的定时采集用于采集租户对象容量和运行指标。

sys 租户健康检查默认每 60 分钟执行一次，可在 `config.json` 中调整：

```json
"app": {
  "sys_check_interval_minutes": 60
}
```

建议使用 cron 每分钟调用一次调度脚本，脚本会自动判断当天是否到达配置时间并避免重复执行：

```bash
* * * * * cd /opt/ob-asset && . .venv/bin/activate && python run_tenant_schedules.py >> logs/schedule.log 2>&1
```

## 11. 只读原则

- 对 OCP：程序只发起 `GET` 请求，不创建、不更新、不删除 OCP 配置。
- 对 OB：程序只执行 `SELECT` 语句，不执行 `INSERT`、`UPDATE`、`DELETE`、`ALTER`、`DROP`、`CREATE`、`SET` 等变更语句。
- 对本系统 Oracle 资产库：允许写入本系统自己的资产表、配置表和采集任务表。

## 12. 常见问题

### 页面能打开，但显示演示数据

通常表示后端无法连接 Oracle。检查：

```bash
python -c "from app import ORACLE_USER, ORACLE_DSN; print(ORACLE_USER, ORACLE_DSN)"
python -c "from app import get_pool; db=get_pool().acquire(); print('oracle ok'); db.close()"
```

### OCP HTTPS 证书报错

如果 OCP 使用自签名证书，可以在页面 OCP 配置里取消“校验HTTPS证书”。

### OCP 同步没有集群

检查 OCP 账号权限，以及现场 OCP 的集群接口返回结构。当前代码主要适配：

```text
/api/v2/ob/clusters
```

如现场字段不同，需要调整 `ocp_collector.py` 中的 `normalize_ocp_clusters()`。

### Web 页面只显示失败摘要

这是设计行为，避免把数据库地址、账号、异常堆栈暴露在浏览器。请在服务器上查看：

```bash
tail -f logs/ob-ops-monitor.log
```

### 生产服务建议

建议后续补充：

- systemd 服务托管
- Nginx 反向代理
- 登录认证
- OCP 密码和 Token 加密存储
- OCP 主机、OBServer、租户、数据库、告警、指标接口映射
