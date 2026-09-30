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
DEFAULT_ROOT = None     # None -> the library_root setting (see settings.py)


def library_root():
    import settings
    return settings.get("library_root")


def connect(db=DB):
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE IF NOT EXISTS tracks(
        path TEXT PRIMARY KEY, mtime REAL, size INTEGER,
        artist TEXT, title TEXT, album TEXT,
        n_artist TEXT, n_title TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_key ON tracks(n_artist, n_title)")
    # Files without a usable artist+title. Never matched, but remembered so a warm
    # scan skips them like any unchanged file -- otherwise all ~460 were opened and
    # re-read on every run.
    con.execute("""CREATE TABLE IF NOT EXISTS untagged(
        path TEXT PRIMARY KEY, mtime REAL, size INTEGER)""")
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


def scan(root):
    """Yield (path, mtime, size) for every .mp3 under root.

    os.scandir rather than os.walk + os.stat: on Windows each entry's size and
    mtime arrive with the directory listing, so a warm scan of a network share
    costs a round trip per folder instead of one per file. Like os.walk, it
    doesn't descend into symlinked folders.
    """
    stack = [root]
    while stack:
        try:
            it = os.scandir(stack.pop())
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    elif e.name.lower().endswith(".mp3"):
                        st = e.stat()
                        yield os.path.normpath(e.path), st.st_mtime, st.st_size
                except OSError:
                    continue


def refresh(root=None, db=DB, verbose=True):
    # Config may say 'D:/Mp3' while stored paths use backslashes; without
    # this every path looks both new and deleted and the scan never goes warm.
    root = os.path.normpath(root or library_root())
    con = connect(db)
    known = {p: (m, s) for p, m, s in
             con.execute("SELECT path, mtime, size FROM tracks")}
    known_untagged = {p: (m, s) for p, m, s in
                      con.execute("SELECT path, mtime, size FROM untagged")}

    def same(prev, mtime, size):
        return prev and abs(prev[0] - mtime) < 1e-6 and prev[1] == size

    t0 = time.time()
    seen, added, updated, unchanged = set(), 0, 0, 0
    rows, untagged = [], []
    for p, mtime, size in scan(root):
        seen.add(p)
        prev = known.get(p)
        if same(prev, mtime, size) or same(known_untagged.get(p), mtime, size):
            unchanged += 1
            continue
        tags = read_tags(p)
        na = nt = None
        if tags:
            artist, title, album = tags
            na, nt = norm_artist(artist), norm_title(title)
        if not na or not nt:
            untagged.append((p, mtime, size))
            continue
        rows.append((p, mtime, size, artist, title, album, na, nt))
        if prev:
            updated += 1
        else:
            added += 1

    # A file that gained tags leaves the untagged list; one that lost them leaves
    # the index. Each path lives in exactly one of the two tables.
    if rows:
        con.executemany("INSERT OR REPLACE INTO tracks VALUES(?,?,?,?,?,?,?,?)", rows)
        con.executemany("DELETE FROM untagged WHERE path=?", [(r[0],) for r in rows])
    if untagged:
        con.executemany("INSERT OR REPLACE INTO untagged VALUES(?,?,?)", untagged)
        con.executemany("DELETE FROM tracks WHERE path=?", [(u[0],) for u in untagged])
    gone = [p for p in known if p not in seen]
    if gone:
        con.executemany("DELETE FROM tracks WHERE path=?", [(p,) for p in gone])
    gone_untagged = [(p,) for p in known_untagged if p not in seen]
    if gone_untagged:
        con.executemany("DELETE FROM untagged WHERE path=?", gone_untagged)
    con.commit()

    total = con.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    distinct = con.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM tracks GROUP BY n_artist, n_title)"
    ).fetchone()[0]
    n_untagged = con.execute("SELECT COUNT(*) FROM untagged").fetchone()[0]
    if verbose:
        print(f"index: {total} tracks / {distinct} distinct songs "
              f"(+{added} new, ~{updated} changed, ={unchanged} unchanged, "
              f"-{len(gone)} removed, {n_untagged} untagged) in {time.time()-t0:.1f}s")
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
    ap.add_argument("--root", default=None, help="default: library_root setting")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()
    root = args.root or library_root()
    if not os.path.isdir(root):
        sys.exit(f"library root not found: {root}")
    refresh(root, args.db)
