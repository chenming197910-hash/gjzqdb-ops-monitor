# OB 运维监控平台

用于收集公司 OceanBase 资产、展示运维全景、捕获错误日志，并通过 OCP 同步 OB 集群信息。

## 现场版本

- OCP：`4.3.5-20250610160438`
- OB：`4.2.1.8`
- 后台资产库：Oracle 19c PDB
- 部署系统：RHEL 7.9

## 已实现功能

- 首页运维全景：物理机、虚拟机、故障主机、报警主机、正常主机。
- OB 集群看板：容量、CPU、内存、租户、数据库、OBServer 摘要。
- Oracle 资产库：保存集群、服务器、租户、数据库、OBServer、日志事件。
- OCP 接入配置：支持 OCP 地址、账号密码、Bearer Token、HTTPS 证书校验开关。
- OCP 同步：测试调用 `/api/v2/info`，集群同步调用 `/api/v2/ob/clusters`。
- OB 日志捕获：解析 `WARN`、`ERROR`、`FATAL`、`OB-xxxx`、`ORA-xxxx`。

## Oracle 19c PDB 准备

```sql
alter session set container = OBPDB;

create user ob_asset identified by "StrongPassword_123";
grant create session, create table, create sequence, create view to ob_asset;
grant unlimited tablespace to ob_asset;
```

## 环境变量

```bash
export ORACLE_USER=ob_asset
export ORACLE_PASSWORD='StrongPassword_123'
export ORACLE_DSN='10.10.10.20:1521/OBPDB'
export DEFAULT_OCP_VERSION='4.3.5-20250610160438'
export DEFAULT_OB_VERSION='4.2.1.8'
```

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

## 后续建议

- 补充 OCP 主机、OBServer、租户、数据库、告警、性能指标接口映射。
- 对 OCP 密码和 Token 做加密存储。
- 增加定时同步任务和同步历史页面。
- 根据 OCP 返回字段做现场适配，主要调整 `ocp_collector.py`。
