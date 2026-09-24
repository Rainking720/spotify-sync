"""Scheduled-task entry point: rotate the log, run a sync, never raise.

Run under pythonw.exe (no console window), so sys.stdout is None at startup --
stdout is rebound to the log file before anything prints. A lock file stops a
long run from overlapping the next day's trigger.
"""
import os, sys, time, traceback
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.path.join(HERE, "logs")
LOG = os.path.join(LOGDIR, "sync.log")
LOCK = os.path.join(HERE, ".sync.lock")
MAX_BYTES = 5 * 1024 * 1024
KEEP = 5
STALE_LOCK_HOURS = 6


def rotate():
    if not os.path.exists(LOG) or os.path.getsize(LOG) < MAX_BYTES:
        return
    old = f"{LOG}.{KEEP}"
    if os.path.exists(old):
        os.remove(old)
    for i in range(KEEP - 1, 0, -1):
        src, dst = f"{LOG}.{i}", f"{LOG}.{i+1}"
        if os.path.exists(src):
            os.replace(src, dst)
    os.replace(LOG, f"{LOG}.1")


def take_lock():
    if os.path.exists(LOCK):
        age_h = (time.time() - os.path.getmtime(LOCK)) / 3600
        if age_h < STALE_LOCK_HOURS:
            return False
    with open(LOCK, "w") as f:
        f.write(str(os.getpid()))
    return True


def main():
    os.makedirs(LOGDIR, exist_ok=True)
    rotate()
    fh = open(LOG, "a", encoding="utf-8", errors="replace", buffering=1)
    sys.stdout = fh
    sys.stderr = fh
    os.chdir(HERE)

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*60}\nrun start {stamp}\n{'='*60}")

    if not take_lock():
        print("another run is still in progress; exiting")
        return 0
    rc = 0
    try:
        import sync
        sys.argv = ["sync.py", "--download"]
        sync.main()
        print(f"run ok {datetime.now().strftime('%H:%M:%S')}")
    except SystemExit as e:
        rc = int(e.code or 0)
    except Exception:
        rc = 1
        print("RUN FAILED:")
        traceback.print_exc(file=sys.stdout)
    finally:
        try:
            os.remove(LOCK)
        except OSError:
            pass
        fh.flush()
    return rc


if __name__ == "__main__":
    sys.exit(main())
