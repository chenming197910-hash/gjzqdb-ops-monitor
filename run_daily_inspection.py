"""Poll daily schedule/manual request; persisted claims prevent duplicate logins."""
import fcntl
from pathlib import Path
from app import get_pool
from tenant_inspection import run_daily, run_manual


def main():
    with (Path(__file__).resolve().parent/'.tenant_inspection.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        db=get_pool().acquire()
        try:
            print(run_daily(db))
            result=run_manual(db)
            if result:print(result)
        finally:db.close()


if __name__=='__main__':main()
