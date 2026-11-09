"""Daily tenant inspection migration. Default prints DDL, --apply executes."""
import argparse,json,os
from pathlib import Path
import oracledb

from inspection_schema import DDL as STATEMENTS



def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    if not args.apply:
        print(';\n'.join(STATEMENTS)+';');return
    config_path=Path(os.environ.get('APP_CONFIG',Path(__file__).resolve().parent/'config.json'))
    config=json.loads(config_path.read_text()) if config_path.exists() else {}
    oracle=config.get('oracle',{})
    def value(key,env):return oracle.get(key) or os.environ.get(env,'')
    db=oracledb.connect(user=value('user','ORACLE_USER'),password=value('password','ORACLE_PASSWORD'),dsn=value('dsn','ORACLE_DSN'))
    try:
        with db.cursor() as cur:
            for sql in STATEMENTS:
                try:cur.execute(sql)
                except oracledb.DatabaseError as exc:
                    if getattr(exc.args[0],'code',None)!=955:raise
        print('每日巡检表迁移完成；默认未启用巡检，未发起租户登录。')
    finally:db.close()


if __name__=='__main__':main()
