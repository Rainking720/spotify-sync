"""Apply new poller plays to iTunes and sync.db -- the 2-hourly scheduled job.

    python poll_to_itunes.py --dry-run     # show what would change, write nothing
    python poll_to_itunes.py               # apply
    python poll_to_itunes.py --no-snapshot # skip re-reading iTunes (faster)

A live run re-reads the iTunes library first so newly imported songs match, then
applies every poller play not already applied. Plays for songs that are in
neither iTunes nor the database stay pending until the song arrives. Each run is
recorded and can be reversed from the GUI's "Undo a run...".

A dry run executes the real apply code, but reads iTunes without assigning and
sends its database changes to a throwaway copy of sync.db -- so what it reports
is what a live run would do, not an approximation of it.
"""
import argparse, os, shutil, sqlite3, sys, tempfile, time, traceback
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.path.join(HERE, "logs")
LOG = os.path.join(LOGDIR, "poll_itunes.log")
LOCK = os.path.join(HERE, ".poll_itunes.lock")
STALE_LOCK_MIN = 60
MAX_LOG = 2 * 1024 * 1024


def _rotate():
    if os.path.exists(LOG) and os.path.getsize(LOG) > MAX_LOG:
        os.replace(LOG, LOG + ".1")


def _lock():
    if os.path.exists(LOCK):
        if (time.time() - os.path.getmtime(LOCK)) / 60 < STALE_LOCK_MIN:
            return False
    with open(LOCK, "w") as f:
        f.write(str(os.getpid()))
    return True


def _fmt_date(iso):
    import plays
    return plays.local_time(iso) if iso else "-"


def report(res, rows, dry):
    if dry:
        head = "DRY RUN - nothing written"
    elif res["itunes"] or res["syncdb"]:
        head = "APPLIED run " + res["run_id"]
    else:
        head = "nothing to apply"
    print("\n" + head)
    print("-" * len(head))
    ch = sorted(res["changes"], key=lambda c: (c["where"], -c["plays"],
                                               c["artist"].lower()))
    if ch:
        print("{:<9} {:>5}  {:>12}  {:<34} {}".format(
            "Where", "+plays", "count", "Artist - Track", "Last played"))
        for c in ch:
            last = (_fmt_date(c["last_after"]) + "  (was " +
                    _fmt_date(c["last_before"]) + ")") if c["date_changed"] \
                else "unchanged"
            print("{:<9} {:>5}  {:>12}  {:<34} {}".format(
                c["where"], "+" + str(c["plays"]),
                "{} -> {}".format(c["before"], c["after"]),
                (c["artist"] + " - " + c["name"])[:34], last))
    waiting = [r for r in rows if r["target"] == "none"]
    print("\n{} iTunes track(s), {} database row(s) updated{}".format(
        res["itunes"], res["syncdb"],
        ", {} skipped".format(res["skipped"]) if res["skipped"] else ""))
    for name, err in res["errors"]:
        print("   skipped {}: {}".format(name, err))
    if waiting:
        n = sum(r["spotify_plays"] for r in waiting)
        print("{} play(s) waiting - song not in iTunes or the database yet:".format(n))
        for r in waiting:
            print("   {}x  {} - {}".format(r["spotify_plays"], r["artist"], r["name"]))


def run(dry=False, snapshot=True):
    import itunes_sync as isync
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("\n" + "=" * 60 + "\n{} {}\n".format(
        "dry run" if dry else "run", stamp) + "=" * 60)

    con = sqlite3.connect(isync.SYNC)
    isync.ensure_schema(con)
    if not isync.get_meta(con, isync.WATERMARK_KEY):
        # Without the history import's watermark the poller would re-apply the
        # plays the export already covers, and a later import would count them
        # again.
        print("history import not applied yet - refusing to run")
        con.close()
        return 2
    con.close()

    if snapshot:
        t0 = time.time()
        n = isync.snapshot()
        print("re-read iTunes: {} tracks in {:.0f}s".format(n, time.time() - t0))

    rows, summary = isync.build_plan("poller")
    waiting = sum(r["spotify_plays"] for r in rows if r["target"] == "none")
    print("{} play(s) to apply, {} waiting for a song to exist".format(
        summary["plays"] - waiting, waiting))
    if not summary["plays"]:
        return 0

    if dry:
        tmp = tempfile.mkdtemp(prefix="poll_dry_")
        try:
            copy = os.path.join(tmp, "sync.db")
            shutil.copy2(isync.SYNC, copy)
            c = sqlite3.connect(copy)
            res = isync.apply_plan(rows, summary, con=c, dry=True)
            c.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    else:
        res = isync.apply_plan(rows, summary)
    report(res, rows, dry)
    return 0


def main():
    ap = argparse.ArgumentParser(description="Apply poller plays to iTunes")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-snapshot", action="store_true")
    args = ap.parse_args()

    to_log = sys.stdout is None          # pythonw (the scheduled task)
    fh = None
    if to_log:
        os.makedirs(LOGDIR, exist_ok=True)
        _rotate()
        fh = open(LOG, "a", encoding="utf-8", errors="replace", buffering=1)
        sys.stdout = sys.stderr = fh
    else:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    os.chdir(HERE)
    sys.path.insert(0, HERE)

    if not _lock():
        print("another run is in progress; exiting")
        return 0
    try:
        return run(dry=args.dry_run, snapshot=not args.no_snapshot)
    except Exception:
        print("RUN FAILED:")
        traceback.print_exc(file=sys.stdout)
        return 1
    finally:
        try:
            os.remove(LOCK)
        except OSError:
            pass
        if fh:
            fh.flush()


if __name__ == "__main__":
    sys.exit(main())
