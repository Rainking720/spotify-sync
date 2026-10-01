r"""Compare Spotify Liked Songs against the local mp3 library.

Phase 1: classify only. Every liked track lands in sync.db as either
'owned' (already in D:\Mp3) or 'pending' (missing -> Phase 2 downloads it).

sync.db is the one file that cannot be rebuilt: it records decisions, not facts
about disk. It is backed up before every run.
"""
import argparse, csv, os, re, shutil, sqlite3, sys
from datetime import datetime, timezone

import index_mp3
from norm import norm_artist, norm_title
from promote import MANUAL_OWNED

HERE = os.path.dirname(os.path.abspath(__file__))
SYNC_DB = os.path.join(HERE, "sync.db")

# Statuses that represent a settled decision; a re-run must not clobber them.
STICKY = ("downloaded", "needs_review", "skipped", "failed")


def connect(db=SYNC_DB):
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE IF NOT EXISTS spotify_tracks(
        track_id TEXT PRIMARY KEY,
        added_at TEXT, artist TEXT, all_artists TEXT, title TEXT, album TEXT,
        duration_ms INTEGER, track_number INTEGER, year TEXT,
        cover_url TEXT, spotify_url TEXT,
        n_artist TEXT, n_title TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        source TEXT NOT NULL DEFAULT 'liked',
        matched_path TEXT, yt_url TEXT, note TEXT,
        first_seen TEXT, updated_at TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_status ON spotify_tracks(status)")
    con.commit()
    ensure_downloaded_at(con)
    ensure_isrc(con)
    return con


def ensure_isrc(con):
    """Add the isrc column (Spotify's recording code, used to find the exact
    recording on YouTube) if missing."""
    if "isrc" not in [r[1] for r in con.execute("PRAGMA table_info(spotify_tracks)")]:
        try:
            con.execute("ALTER TABLE spotify_tracks ADD COLUMN isrc TEXT")
            con.commit()
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):     # lost a race: fine
                raise


_SPOTIFY_ID = re.compile(r"^[0-9A-Za-z]{22}$")


def backfill_isrc(con, sp, verbose=True):
    """One-time: fill isrc for rows that predate the column. Writes only isrc,
    matched by track_id -- unlike a --full sync, it re-classifies nothing, so it
    can't change any row's status. Liked songs come from a full walk of the
    likes (about one request per 50); anything else still waiting to download
    is looked up a track at a time."""
    con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    if con.execute("SELECT 1 FROM meta WHERE k='isrc_backfilled'").fetchone():
        return 0
    import spotify
    n = 0
    for t in spotify.fetch_liked(sp, full=True, progress=False):
        if t.get("isrc"):
            n += con.execute("UPDATE spotify_tracks SET isrc=? WHERE track_id=? "
                             "AND (isrc IS NULL OR isrc='')",
                             (t["isrc"], t["track_id"])).rowcount
    n += fill_missing_isrcs(con, sp)
    con.execute("INSERT OR REPLACE INTO meta VALUES('isrc_backfilled', ?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"),))
    con.commit()
    if verbose:
        print(f"isrc: filled in {n} existing row(s)")
    return n


def fill_missing_isrcs(con, sp, statuses=("pending", "needs_review", "failed"), cap=200):
    """Look up the isrc of rows about to be searched for (album-view rows have
    none: Spotify's album tracklists don't carry it). One request per track --
    Spotify refuses this app's batch /tracks endpoint (403)."""
    qs = ",".join("?" * len(statuses))
    rows = con.execute("SELECT track_id FROM spotify_tracks WHERE status IN (" + qs +
                       ") AND (isrc IS NULL OR isrc='')", statuses).fetchall()
    n = 0
    for (tid,) in rows[:cap]:
        isrc = lookup_isrc(sp, tid)
        if isrc:
            con.execute("UPDATE spotify_tracks SET isrc=? WHERE track_id=?", (isrc, tid))
            n += 1
    con.commit()
    return n


def lookup_isrc(sp, track_id):
    """The isrc of one Spotify track, or '' (not a Spotify id, or no answer)."""
    if not track_id or not _SPOTIFY_ID.match(track_id):
        return ""
    try:
        return ((sp.track(track_id) or {}).get("external_ids") or {}).get("isrc", "") or ""
    except Exception:
        return ""


# When each track was downloaded, kept by triggers so every path that marks a
# row downloaded (nightly run, Retry, pasted URL, reconcile, Add song) records
# it without having to remember to. It is stamped when a row becomes
# 'downloaded', or gets a different YouTube URL while downloaded (a
# re-download) -- not when its path changes, so Move to library keeps it.
_STAMP = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"
TRIGGERS = (
    "CREATE TRIGGER IF NOT EXISTS stamp_downloaded_upd "
    "AFTER UPDATE OF status, yt_url ON spotify_tracks "
    "WHEN NEW.status = 'downloaded' AND (OLD.status IS NOT 'downloaded' "
    "OR NEW.yt_url IS NOT OLD.yt_url) BEGIN "
    "UPDATE spotify_tracks SET downloaded_at = " + _STAMP +
    " WHERE track_id = NEW.track_id; END",
    "CREATE TRIGGER IF NOT EXISTS stamp_downloaded_ins "
    "AFTER INSERT ON spotify_tracks "
    "WHEN NEW.status = 'downloaded' AND NEW.downloaded_at IS NULL BEGIN "
    "UPDATE spotify_tracks SET downloaded_at = " + _STAMP +
    " WHERE track_id = NEW.track_id; END",
)


def ensure_downloaded_at(con):
    """Add the downloaded_at column and its triggers if missing. The first time,
    rows already downloaded get their file's modified time -- the closest
    record there is of when it was fetched."""
    if "downloaded_at" in [r[1] for r in con.execute("PRAGMA table_info(spotify_tracks)")]:
        return
    con.execute("BEGIN IMMEDIATE")      # the GUI and the nightly run may race here
    try:
        if "downloaded_at" not in [r[1] for r in
                                   con.execute("PRAGMA table_info(spotify_tracks)")]:
            con.execute("ALTER TABLE spotify_tracks ADD COLUMN downloaded_at TEXT")
            for tid, p in con.execute(
                    "SELECT track_id, matched_path FROM spotify_tracks "
                    "WHERE status = 'downloaded'").fetchall():
                if p and os.path.exists(p):
                    when = datetime.fromtimestamp(os.path.getmtime(p), timezone.utc)
                    con.execute("UPDATE spotify_tracks SET downloaded_at = ? "
                                "WHERE track_id = ?",
                                (when.strftime("%Y-%m-%dT%H:%M:%SZ"), tid))
        for sql in TRIGGERS:
            con.execute(sql)
        con.commit()
    except Exception:
        con.rollback()
        raise


def backup(db=SYNC_DB):
    if os.path.exists(db):
        dst = db + ".bak"
        shutil.copy2(db, dst)
        return dst
    return None


def classify(con, tracks, lib_keys):
    """Upsert tracks, marking each 'owned' or 'pending'. Returns counts."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # A row you marked owned by hand must survive re-classification. These are
    # exactly the tracks the matcher can't find (that's why they were downloaded),
    # so without this they'd fall back to 'pending' and download all over again.
    existing, manual = {}, set()
    for tid, st, note in con.execute(
            "SELECT track_id, status, note FROM spotify_tracks"):
        existing[tid] = st
        if st == "owned" and note == MANUAL_OWNED:
            manual.add(tid)
    new = owned = pending = preserved = 0

    for t in tracks:
        na, nt = norm_artist(t["artist"]), norm_title(t["title"])
        hit = lib_keys.get((na, nt))
        prior = existing.get(t["track_id"])

        if prior in STICKY or t["track_id"] in manual:
            status, matched = prior, None
            preserved += 1
        elif hit:
            status, matched = "owned", hit
            owned += 1
        else:
            status, matched = "pending", None
            pending += 1
        if prior is None:
            new += 1

        con.execute("""INSERT INTO spotify_tracks
            (track_id, added_at, artist, all_artists, title, album, duration_ms,
             track_number, year, cover_url, spotify_url, n_artist, n_title,
             status, matched_path, first_seen, updated_at, isrc)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(track_id) DO UPDATE SET
              added_at=excluded.added_at, artist=excluded.artist,
              all_artists=excluded.all_artists, title=excluded.title,
              album=excluded.album, duration_ms=excluded.duration_ms,
              track_number=excluded.track_number, year=excluded.year,
              cover_url=excluded.cover_url, spotify_url=excluded.spotify_url,
              n_artist=excluded.n_artist, n_title=excluded.n_title,
              status=CASE WHEN spotify_tracks.status IN
                       ('downloaded','needs_review','skipped','failed')
                       OR (spotify_tracks.status='owned'
                           AND spotify_tracks.note=?)
                     THEN spotify_tracks.status ELSE excluded.status END,
              matched_path=COALESCE(excluded.matched_path, spotify_tracks.matched_path),
              isrc=COALESCE(NULLIF(excluded.isrc, ''), spotify_tracks.isrc),
              updated_at=excluded.updated_at""",
            (t["track_id"], t["added_at"], t["artist"], t["all_artists"], t["title"],
             t["album"], t["duration_ms"], t["track_number"], t["year"],
             t["cover_url"], t["spotify_url"], na, nt, status, matched, now, now,
             t.get("isrc") or None, MANUAL_OWNED))
    con.commit()
    return {"new": new, "owned": owned, "pending": pending, "preserved": preserved}


def report(con, csv_path):
    rows = con.execute("""SELECT artist, title, album, year, added_at, spotify_url
                          FROM spotify_tracks WHERE status='pending'
                          ORDER BY added_at DESC""").fetchall()
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["artist", "title", "album", "year", "added_at", "spotify_url"])
        w.writerows(rows)
    return rows


def main():
    # YouTube titles carry characters the Windows console codepage can't encode
    # (e.g. the o-slash in "Lo Spirit"); without this, printing one kills the run.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Sync Spotify Liked Songs against the local mp3 library")
    ap.add_argument("--full", action="store_true",
                    help="walk all liked songs, not just new ones")
    ap.add_argument("--skip-index", action="store_true",
                    help="reuse mp3_index.db without rescanning disk")
    ap.add_argument("--db", default=SYNC_DB)
    ap.add_argument("--skip-fetch", action="store_true",
                    help="don't call Spotify; work from what's already in sync.db")
    ap.add_argument("--download", action="store_true",
                    help="download pending tracks (default is classify only)")
    ap.add_argument("--limit", type=int, default=None,
                    help="max tracks to download this run")
    ap.add_argument("--pick-only", action="store_true",
                    help="with --download, choose videos but don't download")
    ap.add_argument("--parallel", type=int, default=None, metavar="N",
                    help="with --download, fetch N tracks at once "
                         "(default: the sync_parallel_downloads setting)")
    args = ap.parse_args()

    import spotify  # imported here so --help works without credentials

    cfg = spotify.load_config()
    if not args.skip_index:
        root = index_mp3.library_root()
        if not os.path.isdir(root):
            sys.exit(f"library root not found: {root}")
        index_mp3.refresh(root)

    lib_keys = index_mp3.load_keys()
    print(f"library: {len(lib_keys)} distinct songs")

    b = backup(args.db)
    if b:
        print(f"backed up sync.db -> {os.path.basename(b)}")
    con = connect(args.db)

    if args.skip_fetch:
        counts = {"new": 0, "owned": 0, "pending": 0, "preserved": 0}
        print("skipping Spotify fetch")
    else:
        known = {tid for (tid,) in con.execute(
            "SELECT track_id FROM spotify_tracks WHERE source='liked'")}
        sp = spotify.client(cfg)
        tracks = spotify.fetch_liked(sp, known_ids=known, full=args.full)
        print(f"fetched {len(tracks)} tracks from Spotify")
        counts = classify(con, tracks, lib_keys)
        try:
            backfill_isrc(con, sp)             # once, for rows from before the column
        except Exception as e:                 # never worth failing the sync over
            print(f"isrc backfill skipped: {e}")
    csv_path = os.path.join(HERE, "missing.csv")
    missing = report(con, csv_path)

    tot = con.execute("SELECT COUNT(*) FROM spotify_tracks").fetchone()[0]
    print("\n" + "=" * 58)
    print(f"  liked songs tracked : {tot}")
    print(f"  new this run        : {counts['new']}")
    print(f"  already owned       : {counts['owned']}")
    print(f"  MISSING (pending)   : {counts['pending']}")
    if counts["preserved"]:
        print(f"  prior decisions kept: {counts['preserved']}")
    print("=" * 58)
    for r in missing[:15]:
        print(f"    {r[0]} - {r[1]}")
    if len(missing) > 15:
        print(f"    ... and {len(missing)-15} more")
    print(f"\nfull list -> {csv_path}")
    if args.download:
        import download
        if not args.skip_fetch:
            try:
                fill_missing_isrcs(con, sp, statuses=("pending",))   # e.g. album-view rows
            except Exception as e:
                print(f"isrc lookup skipped: {e}")
        print("")
        print(f"downloading (limit={args.limit or 'none'})"
              f"{' [pick-only]' if args.pick_only else ''}...")
        st = download.run(con, limit=args.limit, dry=args.pick_only,
                          workers=args.parallel)
        print("")
        print("=" * 58)
        print(f"  downloaded   : {st['downloaded']}")
        print(f"  needs review : {st['needs_review']}")
        print(f"  failed       : {st['failed']}")
        print("=" * 58)
    else:
        print("Nothing was downloaded. Re-run with --download to fetch them.")
    con.close()


if __name__ == "__main__":
    main()
