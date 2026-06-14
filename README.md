# OB 运维监控平台

用于收集公司 OceanBase 资产、展示运维全景、捕获错误日志，并通过 OCP 同步 OB 集群信息。

## 现场版本

- OCP：`4.3.5-20250610160438`
- OB：`4.2.1.8`
- 后台资产库：Oracle 19c PDB
- 部署系统：RHEL 7.9

## 已实现功能

- 首页运维全景：物理机、虚拟机、故障主机、报警主机、正常主机。
- Sys 租户健康：集中展示所有集群 sys 租户最近一次连接检查结果，默认每 60 分钟自动检查一次。
- OB 集群看板：容量、CPU、内存、租户、数据库、OBServer 摘要。
- Oracle 资产库：保存集群、服务器、租户、数据库、OBServer、日志事件。
- OCP 接入配置：支持 OCP 地址、账号密码、Bearer Token、HTTPS 证书校验开关。
- OCP 同步：测试调用 `/api/v2/info`，集群同步调用 `/api/v2/ob/clusters`。
- 手工 OB 集群采集：只读连接目标 OB SQL 入口，采集租户、OBServer、参数、租户全备份时间、数据盘/日志盘使用率和上次成功合并信息。
- Oracle 模式租户详情采集：默认调用 `obclient` 连接 OB Oracle 租户，用户名按 `用户@租户#集群` 拼接。
- OB 日志捕获：解析 `WARN`、`ERROR`、`FATAL`、`OB-xxxx`、`ORA-xxxx`。
- 服务器日志：Web 只显示采集失败摘要，详细错误写入 `logs/ob-ops-monitor.log`。

## Oracle 19c PDB 准备

```sql
alter session set container = gjzqdb;

create user gjzqdbsys identified by "现场输入的密码";
grant create session, create table, create sequence, create view to gjzqdbsys;
grant unlimited tablespace to gjzqdbsys;
```

## 配置文件

复制配置模板：

```bash
cp config.example.json config.json
vi config.json
```

现场默认配置：

```json
{
  "oracle": {
    "user": "gjzqdbsys",
    "password": "请在这里填写数据库密码",
    "dsn": "10.50.40.182:1521/gjzqdb"
  },
  "app": {
    "log_dir": "logs"
  }
}
```

密码包含特殊字符时可以直接写在 JSON 字符串里；如果密码里包含双引号 `"`，需要写成 `\"`；如果包含反斜杠 `\`，需要写成 `\\`。

## RHEL 7.9 部署

```bash
sudo yum install -y python3
sudo useradd -r -m obasset
sudo mkdir -p /opt/ob-asset
sudo chown -R obasset:obasset /opt/ob-asset

cd /opt/ob-asset
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python -c "from app import init_db; init_db(seed=True)"
gunicorn -w 2 -b 0.0.0.0:8000 app:app
tail -f logs/ob-ops-monitor.log
```

`python-oracledb` 默认使用 Thin 模式，通常不需要安装 Oracle Instant Client。

## OCP 接入方式

页面点击“ OCP接入 ”，填写：

- OCP 地址，例如 `https://ocp.example.com`
- OCP 版本，默认 `4.3.5-20250610160438`
- API 前缀，默认 `/api/v2`
- 认证方式：账号密码或 Bearer Token
- 如果 OCP 使用自签名证书，可以取消“校验HTTPS证书”

保存后点击“同步OCP”。程序会：

1. 调用 `GET /api/v2/info` 测试 OCP 连接。
2. 调用 `GET /api/v2/ob/clusters` 获取 OB 集群。
3. 将集群写入 Oracle 资产库 `clusters` 表。
4. 如果 OCP 返回里没有 OB 版本字段，默认写入 `4.2.1.8`。

## 只读原则

- 对 OCP 只发起 `GET` 请求，不修改 OCP。
- 对 OB 只执行 `SELECT`，不修改 OB。
- 只向本系统 Oracle 资产库写入资产、配置和采集任务记录。

## 后续建议

- 补充 OCP 主机、OBServer、租户、数据库、告警、性能指标接口映射。
- 对 OCP 密码和 Token 做加密存储。
- 增加定时同步任务和同步历史页面。
- 根据 OCP 返回字段做现场适配，主要调整 `ocp_collector.py`。
