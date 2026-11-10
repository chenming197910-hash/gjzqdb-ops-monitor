"""Independent hourly homepage collector; also shares the legacy scheduler lock."""
import fcntl
from pathlib import Path
from app import BASE_DIR, CONFIG, all_rows, get_pool
from odp_connections import collect_connections
from hourly_collection import collect_homepage


def main():
    with (Path(__file__).resolve().parent/'.scheduler.lock').open('a') as lock:
        # Wait for an existing daily tenant task rather than overlap it.
        fcntl.flock(lock,fcntl.LOCK_EX)
        db=get_pool().acquire()
        try:
            result=collect_homepage(db)
            print(result)
            nodes=all_rows(db, """select distinct c.name as cluster_name, o.svr_ip, c.sys_user, c.sys_password
                from ob_servers o join clusters c on c.id = o.cluster_id
                where o.svr_ip is not null""")
            config=CONFIG.get('odp_monitor', {})
            if config.get('enabled', True):
                snapshot=collect_connections(nodes, config, BASE_DIR / 'data')
                print('ODP clusters sampled:', len(snapshot['clusters']))
                missing = {ip: reason for row in snapshot['clusters'].values()
                           for ip, reason in row['limit_failures'].items()}
                if missing:
                    print('ODP connection limits unavailable:', missing)
            if result['failed']:raise SystemExit(1)
        finally:db.close()


if __name__=='__main__':main()
