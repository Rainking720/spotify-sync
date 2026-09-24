r"""Index D:\Mp3 into mp3_index.db, keyed on normalized (artist, title).

Only files with BOTH an artist and a title ID3 tag are indexed; filenames in this
library vary too much to parse reliably (compilations put the artist mid-name,
some files are '12_mirror_song.mp3'). Tags cover ~98.7% of the library.

Incremental: a file is re-read only when its mtime or size changed.
"""
import argparse, os, sqlite3, sys, time
from mutagen import File as MFile
from norm import norm_artist, norm_title, NORM_VERSION

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "mp3_index.db")
DEFAULT_ROOT = r"D:\Mp3"


def connect(db=DB):
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE IF NOT EXISTS tracks(
        path TEXT PRIMARY KEY, mtime REAL, size INTEGER,
        artist TEXT, title TEXT, album TEXT,
        n_artist TEXT, n_title TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_key ON tracks(n_artist, n_title)")
    con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    con.commit()
    rekey(con)
    return con


def rekey(con, verbose=True):
    """Recompute stored keys if norm.py changed since they were written.

    Incremental indexing only re-reads files whose mtime changed, so without this
    a normalizer fix would leave most rows holding stale keys -- songs you own
    would look missing and get re-downloaded.
    """
    row = con.execute("SELECT v FROM meta WHERE k='norm_version'").fetchone()
    have = int(row[0]) if row else 0
    if have == NORM_VERSION:
        return 0
    rows = con.execute("SELECT path, artist, title FROM tracks").fetchall()
    upd = [(norm_artist(a), norm_title(t), p) for p, a, t in rows]
    upd = [u for u in upd if u[0] and u[1]]
    con.executemany("UPDATE tracks SET n_artist=?, n_title=? WHERE path=?", upd)
    con.execute("INSERT OR REPLACE INTO meta VALUES('norm_version', ?)", (str(NORM_VERSION),))
    con.commit()
    if verbose:
        print(f"index: re-keyed {len(upd)} rows for normalizer v{NORM_VERSION} "
              f"(was v{have})")
    return len(upd)


def read_tags(path):
    """Return (artist, title, album) or None if the file has no usable tags."""
    try:
        a = MFile(path, easy=True)
        if a is None:
            return None
        artist = (a.get("artist") or [""])[0].strip()
        title = (a.get("title") or [""])[0].strip()
        album = (a.get("album") or [""])[0].strip()
    except Exception:
        return None
    if not artist or not title:
        return None
    return artist, title, album


def refresh(root=DEFAULT_ROOT, db=DB, verbose=True):
    # Config may say 'D:/Mp3' while stored paths use backslashes; without
    # this every path looks both new and deleted and the scan never goes warm.
    root = os.path.normpath(root)
    con = connect(db)
    known = {p: (m, s) for p, m, s in
             con.execute("SELECT path, mtime, size FROM tracks")}

    t0 = time.time()
    seen, added, updated, unchanged, skipped = set(), 0, 0, 0, 0
    rows = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if not fn.lower().endswith(".mp3"):
                continue
            p = os.path.normpath(os.path.join(dirpath, fn))
            seen.add(p)
            try:
                st = os.stat(p)
            except OSError:
                continue
            prev = known.get(p)
            if prev and abs(prev[0] - st.st_mtime) < 1e-6 and prev[1] == st.st_size:
                unchanged += 1
                continue
            tags = read_tags(p)
            if tags is None:
                skipped += 1
                continue
            artist, title, album = tags
            na, nt = norm_artist(artist), norm_title(title)
            if not na or not nt:
                skipped += 1
                continue
            rows.append((p, st.st_mtime, st.st_size, artist, title, album, na, nt))
            if prev:
                updated += 1
            else:
                added += 1

    if rows:
        con.executemany("INSERT OR REPLACE INTO tracks VALUES(?,?,?,?,?,?,?,?)", rows)
    gone = [p for p in known if p not in seen]
    if gone:
        con.executemany("DELETE FROM tracks WHERE path=?", [(p,) for p in gone])
    con.commit()

    total = con.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    distinct = con.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM tracks GROUP BY n_artist, n_title)"
    ).fetchone()[0]
    if verbose:
        print(f"index: {total} tracks / {distinct} distinct songs "
              f"(+{added} new, ~{updated} changed, ={unchanged} unchanged, "
              f"-{len(gone)} removed, {skipped} untagged) in {time.time()-t0:.1f}s")
    con.close()
    return total, distinct


def load_keys(db=DB):
    """Return {(n_artist, n_title): path} for matching."""
    con = connect(db)
    out = {}
    for na, nt, p in con.execute("SELECT n_artist, n_title, path FROM tracks"):
        out.setdefault((na, nt), p)
    con.close()
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Index the mp3 library by ID3 tags.")
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()
    if not os.path.isdir(args.root):
        sys.exit(f"library root not found: {args.root}")
    refresh(args.root, args.db)
