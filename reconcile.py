"""Repair statuses by looking at what is actually on disk.

A download can succeed while the status write doesn't (a crash, a mangled path,
a power cut). This re-reads the output tree and promotes any failed/pending row
whose file demonstrably exists.
"""
import os, sys
from mutagen import File as MFile
from norm import norm_artist, norm_title

DEFAULT_ROOT = None     # None -> the staging_root setting (see settings.py)


def _staging():
    import settings
    return settings.get("staging_root")


def scan(root=None):
    """Map (n_artist, n_title) -> path for everything in the output tree."""
    root = root or _staging()
    out = {}
    for dp, _dn, fns in os.walk(root):
        for fn in fns:
            if not fn.lower().endswith(".mp3"):
                continue
            p = os.path.normpath(os.path.join(dp, fn))
            try:
                a = MFile(p, easy=True)
                if a is None:
                    continue
                ar = (a.get("artist") or [""])[0].strip()
                ti = (a.get("title") or [""])[0].strip()
            except Exception:
                continue
            if ar and ti:
                out.setdefault((norm_artist(ar), norm_title(ti)), p)
    return out


def reconcile(con, root=None, statuses=("failed", "pending"), verbose=True):
    disk = scan(root or _staging())
    qs = ",".join("?" * len(statuses))
    rows = con.execute(
        f"SELECT track_id,artist,title,n_artist,n_title FROM spotify_tracks "
        f"WHERE status IN ({qs})", statuses).fetchall()
    fixed = 0
    for tid, a, t, na, nt in rows:
        p = disk.get((na, nt))
        if not p:
            continue
        con.execute("""UPDATE spotify_tracks SET status='downloaded', matched_path=?,
                       note='reconciled from disk', updated_at=datetime('now')
                       WHERE track_id=?""", (p, tid))
        fixed += 1
        if verbose:
            print(f"  reconciled: {a} - {t}")
    con.commit()
    if verbose:
        print(f"reconciled {fixed} of {len(rows)} {'/'.join(statuses)} rows "
              f"({len(disk)} files on disk)")
    return fixed


if __name__ == "__main__":
    import sqlite3
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    con = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync.db"))
    reconcile(con)
    con.close()
