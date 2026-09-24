"""Work the needs_review queue.

    python review.py                    list the queue with candidate URLs
    python review.py --retry            re-run the picker on every queued track
    python review.py --accept 2 <url>   download a specific YouTube URL for item 2
    python review.py --skip 3 4         give up on items 3 and 4
    python review.py --requeue 3        undo a skip, put it back in the queue

YouTube search results vary between runs, so --retry alone often clears items.
"""
import argparse, json, os, sqlite3, sys

import download, ytpick

HERE = os.path.dirname(os.path.abspath(__file__))
SYNC_DB = os.path.join(HERE, "sync.db")
COLS = ["track_id", "artist", "title", "album", "duration_ms",
        "track_number", "year", "cover_url"]


def queue(con, status="needs_review"):
    rows = con.execute(
        f"SELECT {','.join(COLS)}, note FROM spotify_tracks "
        f"WHERE status=? ORDER BY artist, title", (status,)).fetchall()
    return [dict(zip(COLS + ["note"], r)) for r in rows]


def show(items):
    if not items:
        print("queue is empty.")
        return
    for i, t in enumerate(items, 1):
        print(f"\n[{i}] {t['artist']} - {t['title']}")
        print(f"    album {t['album']} | Spotify {round((t['duration_ms'] or 0)/1000)}s")
        try:
            d = json.loads(t["note"] or "{}")
        except (ValueError, TypeError):
            d = {}
        if d.get("reason"):
            print(f"    why: {d['reason']}")
        for cnd in (d.get("candidates") or [])[:5]:
            print(f"      {cnd.get('dur')}s  {str(cnd.get('title'))[:46]}")
            print(f"        {cnd.get('url')}")
    print("\naccept one with:  python review.py --accept <n> <youtube-url>")


def accept(con, t, url, music_root=None):
    ok, final, msg = download.invoke(t, url, music_root=music_root)
    if not ok:
        print(f"failed: {msg}")
        return False
    con.execute("""UPDATE spotify_tracks SET status='downloaded', yt_url=?,
                   matched_path=?, note='accepted manually', updated_at=datetime('now')
                   WHERE track_id=?""", (url, final, t["track_id"]))
    con.commit()
    print(f"downloaded: {os.path.basename(final)}")
    try:
        from mutagen.mp3 import MP3
        got, want = MP3(final).info.length, (t["duration_ms"] or 0) / 1000
        flag = "" if abs(got - want) <= 7 else "   <-- CHECK: differs from Spotify"
        print(f"  {got:.1f}s vs Spotify {want:.1f}s{flag}")
    except Exception:
        pass
    return True


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Work the needs_review queue")
    ap.add_argument("--retry", action="store_true", help="re-run the picker on the queue")
    ap.add_argument("--accept", nargs=2, metavar=("N", "URL"))
    ap.add_argument("--skip", type=int, nargs="+", metavar="N")
    ap.add_argument("--requeue", type=int, metavar="N")
    ap.add_argument("--db", default=SYNC_DB)
    args = ap.parse_args()
    con = sqlite3.connect(args.db)

    if args.requeue:
        items = queue(con, "skipped")
        t = items[args.requeue - 1]
        con.execute("UPDATE spotify_tracks SET status='needs_review' WHERE track_id=?",
                    (t["track_id"],))
        con.commit()
        print(f"requeued: {t['artist']} - {t['title']}")
    elif args.skip:
        # Resolve every index against ONE snapshot: skipping renumbers the queue,
        # so "--skip 1 2" run sequentially would hit the wrong second track.
        items = queue(con)
        chosen = [items[n - 1] for n in sorted(set(args.skip))]
        con.executemany("UPDATE spotify_tracks SET status='skipped' WHERE track_id=?",
                        [(t["track_id"],) for t in chosen])
        con.commit()
        for t in chosen:
            print(f"skipped: {t['artist']} - {t['title']}")
    elif args.accept:
        items = queue(con)
        t = items[int(args.accept[0]) - 1]
        print(f"{t['artist']} - {t['title']}")
        accept(con, t, args.accept[1])
    elif args.retry:
        items = queue(con)
        print(f"re-picking {len(items)} queued track(s)...")
        done = 0
        for t in items:
            best, cands, reason = ytpick.pick(t["artist"], t["title"], t["duration_ms"])
            if not best:
                print(f"  still stuck: {t['artist']} - {t['title']}  ({reason})")
                continue
            print(f"  resolved: {t['artist']} - {t['title']}")
            print(f"    -> [{best['score']}] {best['duration']}s {best['channel']}")
            if accept(con, t, best["url"]):
                done += 1
        print(f"\nresolved {done} of {len(items)}")
    else:
        show(queue(con))
    con.close()


if __name__ == "__main__":
    main()
