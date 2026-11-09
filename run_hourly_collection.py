"""Independent hourly homepage collector; also shares the legacy scheduler lock."""
import fcntl
from pathlib import Path
from app import get_pool
from hourly_collection import collect_homepage


def main():
    with (Path(__file__).resolve().parent/'.scheduler.lock').open('a') as lock:
        # Wait for an existing daily tenant task rather than overlap it.
        fcntl.flock(lock,fcntl.LOCK_EX)
        db=get_pool().acquire()
        try:
            result=collect_homepage(db)
            print(result)
            if result['failed']:raise SystemExit(1)
        finally:db.close()


if __name__=='__main__':main()
