"""Dedicated five-minute job; uses the same Oracle cluster lock as the daily runner."""
from app import get_pool,run_cpu_sampling,all_rows,one,execute,log_event


def main():
    db=get_pool().acquire()
    try:
        result=run_cpu_sampling(db,all_rows,one,execute,log_event)
        if result['failed']:
            raise SystemExit(1)
    finally:
        db.close()


if __name__=='__main__':main()
