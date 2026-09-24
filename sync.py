r"""Compare Spotify Liked Songs against the local mp3 library.

Phase 1: classify only. Every liked track lands in sync.db as either
'owned' (already in D:\Mp3) or 'pending' (missing -> Phase 2 downloads it).

sync.db is the one file that cannot be rebuilt: it records decisions, not facts
about disk. It is backed up before every run.
"""
import argparse, csv, os, shutil, sqlite3, sys
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
    return con


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
             status, matched_path, first_seen, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
              updated_at=excluded.updated_at""",
            (t["track_id"], t["added_at"], t["artist"], t["all_artists"], t["title"],
             t["album"], t["duration_ms"], t["track_number"], t["year"],
             t["cover_url"], t["spotify_url"], na, nt, status, matched, now, now,
             MANUAL_OWNED))
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
    args = ap.parse_args()

    import spotify  # imported here so --help works without credentials

    cfg = spotify.load_config()
    if not args.skip_index:
        root = cfg.get("library_root", index_mp3.DEFAULT_ROOT)
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
        print("")
        print(f"downloading (limit={args.limit or 'none'})"
              f"{' [pick-only]' if args.pick_only else ''}...")
        st = download.run(con, limit=args.limit,
                          music_root=cfg.get("music_root"), dry=args.pick_only)
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
