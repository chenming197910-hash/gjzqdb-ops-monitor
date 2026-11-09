"""Extension migration only. Default prints DDL; --apply is an explicit local action."""
import argparse
from app import DDL,oracledb,ORACLE_USER,ORACLE_PASSWORD,ORACLE_DSN,execute_ddl

EXTENSION_DDL=[sql for sql in DDL if any(token in sql.lower() for token in (
    'alter table tenants add active ', 'create table cluster_merge_checks ',
    'create table tenant_cpu_counters ', 'create table tenant_cpu_samples ',
    'create index ix_tenant_cpu_samples ', 'create table tenant_absence_checks ',
    'create table cluster_sys_cpu_samples ', 'create index ix_sys_cpu_samples ', 'create table cpu_collection_status '))]


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    if not args.apply:
        for sql in EXTENSION_DDL:print(sql.strip()+';\n')
        return
    db=oracledb.connect(user=ORACLE_USER,password=ORACLE_PASSWORD,dsn=ORACLE_DSN)
    try:
        for sql in EXTENSION_DDL:execute_ddl(db,sql)
        print('扩展迁移执行完毕。请运行 schema_preflight.py 核验已有对象的字段类型和约束。')
    finally:db.close()


if __name__=='__main__':main()
