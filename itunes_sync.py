"""Push Spotify play counts into iTunes, and into sync.db for songs not yet there.

Two sources feed the same pipeline:

* **history** -- the one-time Extended Streaming History export (history.py).
* **poller**  -- the ongoing 2-hourly recently-played log (plays.py).

They overlap: the export runs to 2026-09-20 and the poller started 2026-09-19,
so 50 of its first 58 plays are already in the export. A watermark (the export's
last timestamp, stored in sync.db's meta table) is what stops those being applied
twice -- the poller source only ever considers plays after it.

Nothing is written without a plan being built and approved first, and every write
records the previous PlayedCount/PlayedDate so a run can be undone. iTunes itself
offers no undo for this.
"""
import os, sqlite3, time
from datetime import datetime, timezone

import history
import plays as plays_mod
from norm import norm_artist, norm_title

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "itunes_cache.db")
SYNC = os.path.join(HERE, "sync.db")

TO_ITUNES = "itunes"      # matched an iTunes track
TO_SYNCDB = "syncdb"      # not in iTunes, but a sync.db row exists
NO_TARGET = "none"        # nowhere to record it yet
WATERMARK_KEY = "history_watermark"


def _com_init():
    """COM has to be initialised on whichever thread is about to use it.

    pywin32 does it lazily for the first thread that touches COM, so without an
    explicit call this works or raises "CoInitialize has not been called"
    depending purely on the order the GUI happens to run things in -- the worker
    threads here and the main thread are both affected.
    Calling it again on an already-initialised thread is harmless.
    """
    try:
        import pythoncom
        pythoncom.CoInitialize()
    except Exception:
        pass


# --------------------------------------------------------------- iTunes cache
def snapshot(progress=None):
    """Read the whole iTunes library into itunes_cache.db (~2.5ms/track)."""
    import win32com.client as w
    _com_init()
    app = w.Dispatch("iTunes.Application")
    coll = app.LibraryPlaylist.Tracks
    total = coll.Count
    # Build into a temp file and swap it in at the end. Writing in place meant a
    # plan built during a re-read saw an empty library and marked every track
    # unmatched -- a silently wrong preview, not an error.
    tmp = CACHE + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.execute("""CREATE TABLE tracks(idx INTEGER, pid TEXT, artist TEXT,
                   name TEXT, album TEXT, played_count INTEGER,
                   played_date TEXT, n_artist TEXT, n_title TEXT)""")
    rows = []
    for i in range(1, total + 1):
        try:
            t = coll.Item(i)
            ar, nm = t.Artist or "", t.Name or ""
            pd = t.PlayedDate
            rows.append((i, str(t.TrackDatabaseID), ar, nm, t.Album or "",
                         t.PlayedCount or 0, pd.isoformat() if pd else "",
                         norm_artist(ar), norm_title(nm)))
        except Exception:
            continue
        if progress and i % 2000 == 0:
            progress(i, total)
    con.executemany("INSERT INTO tracks VALUES(?,?,?,?,?,?,?,?,?)", rows)
    con.execute("CREATE INDEX ix ON tracks(n_artist, n_title)")
    con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    con.execute("INSERT OR REPLACE INTO meta VALUES('taken', ?)",
                (datetime.now().isoformat(timespec="seconds"),))
    con.commit()
    con.close()
    os.replace(tmp, CACHE)      # atomic: readers see the old cache until now
    return len(rows)


def snapshot_age():
    """(rows, taken_iso) for the cached library, or (0, None)."""
    if not os.path.exists(CACHE):
        return 0, None
    con = sqlite3.connect(CACHE)
    try:
        n = con.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    except sqlite3.Error:
        con.close()
        return 0, None
    taken = None
    try:      # a cache written before meta existed still has usable tracks
        row = con.execute("SELECT v FROM meta WHERE k='taken'").fetchone()
        taken = row[0] if row else None
    except sqlite3.Error:
        pass
    con.close()
    return n, taken


def load_snapshot(con=None):
    """{(n_artist, n_title): [track, ...]} -- a list, because the same song can
    sit on an album, a compilation and a live record.

    Manual links are folded in: where a name differs too much to match by itself
    (an iTunes typo, a different edit of the title), the link points that key at
    the chosen iTunes track and wins outright.
    """
    c = sqlite3.connect(CACHE)
    out, by_pid = {}, {}
    for idx, pid, ar, nm, al, pc, pd, na, nt in c.execute(
            "SELECT idx,pid,artist,name,album,played_count,played_date,"
            "n_artist,n_title FROM tracks"):
        t = {"idx": idx, "pid": pid, "artist": ar, "name": nm, "album": al,
             "played_count": pc or 0, "played_date": pd or ""}
        out.setdefault((na, nt), []).append(t)
        by_pid[pid] = t
    c.close()

    own = con is None
    con = con or sqlite3.connect(SYNC)
    try:
        ensure_schema(con)
        for na, nt, pid in con.execute(
                "SELECT n_artist, n_title, itunes_pid FROM itunes_link"):
            t = by_pid.get(pid)
            if t:
                out[(na, nt)] = [t]      # an explicit choice beats any guess
    except sqlite3.Error:
        pass
    finally:
        if own:
            con.close()
    return out


def search_library(text, limit=40):
    """Find iTunes tracks by a substring of artist or title."""
    text = (text or "").strip().lower()
    if not text:
        return []
    c = sqlite3.connect(CACHE)
    like = "%" + text.replace("%", "") + "%"
    rows = c.execute(
        "SELECT idx,pid,artist,name,album,played_count,played_date "
        "FROM tracks WHERE lower(artist) LIKE ? OR lower(name) LIKE ? "
        "ORDER BY artist, name LIMIT ?", (like, like, limit)).fetchall()
    c.close()
    return [{"idx": r[0], "pid": r[1], "artist": r[2], "name": r[3],
             "album": r[4], "played_count": r[5] or 0, "played_date": r[6] or ""}
            for r in rows]


def set_link(con, key, track):
    """Point a database key at a specific iTunes track."""
    ensure_schema(con)
    con.execute(
        "INSERT INTO itunes_link (n_artist, n_title, itunes_pid, itunes_artist, "
        "itunes_title, created_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(n_artist, n_title) DO UPDATE SET "
        "itunes_pid=excluded.itunes_pid, itunes_artist=excluded.itunes_artist, "
        "itunes_title=excluded.itunes_title, created_at=excluded.created_at",
        (key[0], key[1], track["pid"], track["artist"], track["name"],
         datetime.now().isoformat(timespec="seconds")))
    con.commit()


def clear_link(con, key):
    ensure_schema(con)
    con.execute("DELETE FROM itunes_link WHERE n_artist=? AND n_title=?", key)
    con.commit()


def get_link(con, key):
    ensure_schema(con)
    return con.execute(
        "SELECT itunes_pid, itunes_artist, itunes_title FROM itunes_link "
        "WHERE n_artist=? AND n_title=?", key).fetchone()


def choose(candidates):
    """Pick which iTunes copy gets the plays.

    Highest existing play count wins -- that's the copy actually being listened
    to. Ties break on most recently played, then on library order.
    """
    return sorted(candidates,
                  key=lambda t: (-t["played_count"], _neg_date(t["played_date"]),
                                 t["idx"]))[0]


def _neg_date(iso):
    return "" if not iso else "".join(chr(255 - ord(c)) for c in iso[:19])


# ------------------------------------------------------------- sync.db schema
def ensure_schema(con):
    cols = [r[1] for r in con.execute("PRAGMA table_info(spotify_tracks)")]
    if "spotify_plays" not in cols:
        con.execute("ALTER TABLE spotify_tracks ADD COLUMN spotify_plays INTEGER DEFAULT 0")
    if "spotify_last_played" not in cols:
        con.execute("ALTER TABLE spotify_tracks ADD COLUMN spotify_last_played TEXT")
    con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    con.execute("""CREATE TABLE IF NOT EXISTS itunes_undo(
        run_id TEXT, applied_at TEXT, pid TEXT, idx INTEGER,
        artist TEXT, name TEXT,
        old_count INTEGER, old_date TEXT, new_count INTEGER, new_date TEXT)""")
    # How many plays each song has already been credited from the export.
    # Without this a second history run adds all 10,008 plays over again: the
    # planner reads iTunes' current count and adds the export total to it.
    # Storing the running total (not a flag) also means a *newer* export later
    # credits only the difference.
    con.execute("""CREATE TABLE IF NOT EXISTS history_applied(
        n_artist TEXT, n_title TEXT,
        plays_credited INTEGER NOT NULL DEFAULT 0,
        last_credited TEXT, run_id TEXT, applied_at TEXT,
        credited_to TEXT,
        PRIMARY KEY (n_artist, n_title))""")
    led = [r[1] for r in con.execute("PRAGMA table_info(history_applied)")]
    if "credited_to" not in led:
        con.execute("ALTER TABLE history_applied ADD COLUMN credited_to TEXT")
    # Pre-run ledger state, so undoing a run also un-credits it. Without this an
    # undone run still counts as applied and a rebuild shows nothing to do.
    con.execute("""CREATE TABLE IF NOT EXISTS history_applied_undo(
        run_id TEXT, n_artist TEXT, n_title TEXT,
        prev_credited INTEGER, prev_last TEXT, existed INTEGER,
        prev_dest TEXT)""")
    u = [r[1] for r in con.execute("PRAGMA table_info(history_applied_undo)")]
    if "prev_dest" not in u:
        con.execute("ALTER TABLE history_applied_undo ADD COLUMN prev_dest TEXT")
    # Manual "this song is that iTunes track" overrides, for names too different
    # to match on their own -- an iTunes typo, or a differently-worded title.
    # One row per poller play already applied, so each play lands exactly once.
    # Per play rather than a cursor: a play with nowhere to go yet (song not in
    # iTunes or the database) stays pending until it has a home, instead of
    # being skipped forever once a cursor passes it.
    con.execute("""CREATE TABLE IF NOT EXISTS poller_applied(
        play_key TEXT PRIMARY KEY, run_id TEXT, applied_at TEXT, target TEXT,
        ref TEXT)""")
    pa = [r[1] for r in con.execute("PRAGMA table_info(poller_applied)")]
    if "ref" not in pa:
        # which iTunes track / database row got the play, so an undo that can't
        # restore one track releases only the plays it really took back
        con.execute("ALTER TABLE poller_applied ADD COLUMN ref TEXT")
    # A poller play's record before a run moved it from the database to iTunes,
    # so undoing that run returns it to the database instead of forgetting it.
    con.execute("""CREATE TABLE IF NOT EXISTS poller_applied_undo(
        run_id TEXT, play_key TEXT, prev_run_id TEXT, prev_applied_at TEXT,
        prev_target TEXT, prev_ref TEXT)""")
    # Previous database values, so undo covers database rows as well as iTunes.
    con.execute("""CREATE TABLE IF NOT EXISTS syncdb_undo(
        run_id TEXT, track_id TEXT, prev_plays INTEGER, prev_last TEXT,
        created INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS itunes_link(
        n_artist TEXT, n_title TEXT, itunes_pid TEXT,
        itunes_artist TEXT, itunes_title TEXT, created_at TEXT,
        PRIMARY KEY (n_artist, n_title))""")
    con.commit()


def get_meta(con, key, default=None):
    row = con.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(con, key, value):
    con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(value)))
    con.commit()


# ------------------------------------------------------------------ planning
def _source_tracks(source, con, min_ms=history.MIN_MS):
    """Normalised {(n_artist, n_title): {count, last, artist, name}}."""
    if source == "history":
        # Once a history import has run, the poller owns everything after its
        # watermark. A newer export overlaps plays the poller already applied,
        # so it is only counted up to the existing watermark.
        mark = get_meta(con, WATERMARK_KEY, "") or None
        agg = history.aggregate(min_ms=min_ms, until=mark)
        return agg["tracks"], agg
    # poller: only plays after the export's reach, or everything if no import yet
    mark = get_meta(con, WATERMARK_KEY, "")
    done = con.execute("SELECT play_key, target FROM poller_applied").fetchall()
    # Plays held in the database because the song wasn't in iTunes yet. Like the
    # history import's, they still owe iTunes, so build_plan moves them there
    # once the song arrives.
    staged = {k for k, tg in done if tg == TO_SYNCDB}
    applied = {k for k, tg in done} - staged
    data = plays_mod.load()
    tracks, counted = {}, 0
    raw = _poller_rows(data, mark, applied)
    for artist, name, ts, pkey, tid in raw:
        key = (norm_artist(artist.split(",")[0]), norm_title(name))
        t = tracks.setdefault(key, {"artist": artist, "name": name, "album": "",
                                    "track_id": tid, "count": 0, "last": "",
                                    "play_keys": [], "staged": 0,
                                    "staged_keys": [], "staged_last": ""})
        if pkey in staged:
            t["staged"] += 1
            t["staged_keys"].append(pkey)
            t["staged_last"] = max(t["staged_last"], ts)
            continue
        t["count"] += 1
        t["play_keys"].append(pkey)
        counted += 1
        if ts > t["last"]:
            t["last"] = ts
    return tracks, {"tracks": tracks, "plays": counted, "watermark": mark,
                    "entries": counted, "skipped": 0,
                    "already_applied_plays": len(done)}


def play_key(rec):
    """Stable identity for one logged play: when, plus which track."""
    tid = rec.get("track_id") or "{}::{}".format(rec.get("name", ""),
                                                 rec.get("artists", ""))
    return "{}|{}".format(rec.get("played_at", ""), tid)


def _poller_rows(data, mark, applied=frozenset()):
    """Individual plays after the watermark that haven't been applied yet."""
    import json
    seen = set()
    path = data.get("path") or plays_mod.path_from_config()
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            ts = rec.get("played_at", "")
            if mark and ts <= mark:
                continue          # already covered by the history import
            k = play_key(rec)
            if k in applied or k in seen:
                continue          # applied already, or a duplicated log line
            seen.add(k)
            out.append((rec.get("artists", ""), rec.get("name", ""), ts, k,
                        rec.get("track_id") or ""))
    return out


def build_plan(source="history", min_ms=history.MIN_MS, con=None):
    """Work out every change, touching nothing. Returns (rows, summary)."""
    own = con is None
    con = con or sqlite3.connect(SYNC)
    ensure_schema(con)
    tracks, agg = _source_tracks(source, con, min_ms)
    lib = load_snapshot(con)      # same connection, so links in it apply

    # Plays already credited from an earlier history run. Stored as a running
    # total per song, so re-running credits only the difference and a newer
    # export later picks up just its new plays.
    credited = {}
    if source == "history":
        for na, nt, n, last, dest in con.execute(
                "SELECT n_artist, n_title, plays_credited, last_credited, "
                "credited_to FROM history_applied"):
            credited[(na, nt)] = (n or 0, last or "", dest or "")

    sync_rows = {}
    for tid, ar, ti, st in con.execute(
            "SELECT track_id, artist, title, status FROM spotify_tracks"):
        sync_rows.setdefault((norm_artist(ar), norm_title(ti)),
                             {"track_id": tid, "artist": ar, "title": ti,
                              "status": st})

    rows, already = [], 0
    for key, src in tracks.items():
        prev_n, prev_last, prev_dest = credited.get(key, (0, "", ""))
        cands = lib.get(key)
        srow = sync_rows.get(key)
        if source == "poller":
            if cands and src.get("staged"):
                # held in the database until now; the song is in iTunes at last
                src = dict(src, count=src["count"] + src["staged"],
                           play_keys=src["play_keys"] + src["staged_keys"],
                           last=max(src["last"], src["staged_last"]))
            if not src["count"]:
                continue          # only plays already held in the database
        target_now = TO_ITUNES if cands else (TO_SYNCDB if srow else NO_TARGET)

        # Plays credited only to the database still owe iTunes. Recording them in
        # sync.db is a holding step for songs not yet imported, so once the song
        # does appear in iTunes those plays must still be applied there -- the
        # whole point of staging them.
        effective_prev = prev_n
        if target_now == TO_ITUNES and prev_dest and prev_dest != TO_ITUNES:
            effective_prev = 0

        delta = src["count"] - effective_prev
        newer = src["last"] > prev_last if prev_last else bool(src["last"])
        if delta <= 0 and not newer:
            already += 1          # fully credited already; nothing left to do
            continue
        delta = max(delta, 0)     # a date-only update still shows, with +0

        entry = {"key": key, "artist": src["artist"], "name": src["name"],
                 "spotify_plays": delta,
                 "spotify_total": src["count"],
                 "already_credited": effective_prev,
                 "spotify_last": src["last"],
                 # Keep Spotify's real id: if this song is liked later, classify()
                 # then finds this row instead of creating a second one.
                 "track_id": src.get("track_id") or "",
                 "play_keys": src.get("play_keys", []),
                 "ambiguous": len(cands) if cands else 0}
        if cands:
            best = choose(cands)
            new_date = _newer(best["played_date"], src["last"])
            entry.update({
                "target": TO_ITUNES, "idx": best["idx"], "pid": best["pid"],
                "album": best["album"],
                "old_count": best["played_count"], "old_date": best["played_date"],
                "new_count": best["played_count"] + delta,
                "new_date": new_date,
                "date_changes": new_date != (best["played_date"] or ""),
            })
        else:
            entry.update({
                # sync.db is an absolute assignment, not an increment, so the
                # full total is correct here.
                "target": target_now,
                "sync_track_id": srow["track_id"] if srow else None,
                "album": "", "old_count": prev_n, "old_date": prev_last,
                "new_count": src["count"], "new_date": src["last"],
                "date_changes": True,
            })
        rows.append(entry)

    rows.sort(key=lambda r: (-r["spotify_plays"], r["artist"].lower()))
    summary = {
        "source": source,
        # for the poller, every play in the plan -- including ones held in the
        # database that are moving to iTunes now
        "plays": (sum(r["spotify_plays"] for r in rows) if source == "poller"
                  else agg.get("plays", 0)),
        "entries": agg.get("entries", 0),
        "skipped": agg.get("skipped", 0),
        "watermark": agg.get("watermark", ""),
        "tracks": len(rows),
        "to_itunes": sum(1 for r in rows if r["target"] == TO_ITUNES),
        "to_syncdb": sum(1 for r in rows if r["target"] == TO_SYNCDB),
        "no_target": sum(1 for r in rows if r["target"] == NO_TARGET),
        "ambiguous": sum(1 for r in rows if r["ambiguous"] > 1),
        "already_applied": already,
        "min_ms": min_ms,
    }
    if own:
        con.close()
    return rows, summary


def _itunes_local(value):
    """An iTunes PlayedDate (datetime or iso string) as an aware local time.

    iTunes gives local wall-clock labelled +00:00; comparing it as UTC against
    Spotify's real UTC was off by the whole timezone offset.
    """
    if not value:
        return None
    if isinstance(value, str):
        value = _parse(value)
        if value is None:
            return None
    if value.year < 1971:
        # iTunes' placeholder for "never played" (a 1899/1601 date); Windows
        # also refuses to localise anything before 1970.
        return None
    naive = datetime(value.year, value.month, value.day,
                     value.hour, value.minute, value.second)
    try:
        return naive.astimezone()  # a naive datetime is read as local time
    except (OSError, OverflowError, ValueError):
        return None


def _newer(itunes_iso, spotify_iso):
    """Keep whichever last-played is later. Spotify's is UTC with a Z."""
    if not spotify_iso:
        return itunes_iso or ""
    if not itunes_iso:
        return spotify_iso
    a = _itunes_local(itunes_iso)
    b = _parse(spotify_iso)
    if a is None:
        return spotify_iso
    if b is None:
        return itunes_iso
    return spotify_iso if b > a else itunes_iso


def _parse(iso):
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _for_itunes(iso, from_utc):
    """Naive local wall-clock datetime to hand back to iTunes.

    iTunes reports PlayedDate as local wall-clock but pywin32 labels it +00:00.
    So a value that came *from* iTunes must have its tz stripped as-is, while a
    genuinely-UTC Spotify timestamp must be converted to local first. Treating
    an iTunes value as UTC shifts it by the whole offset -- it silently moved
    every restored date back four hours.
    """
    dt = _parse(iso)
    if dt is None:
        return None
    if from_utc:
        return dt.astimezone().replace(tzinfo=None)
    return dt.replace(tzinfo=None)


# ------------------------------------------------------------------ applying
LOGGED = "logged"   # played per history, never liked and not in iTunes.
                    # Deliberately NOT 'pending': that is download.py's work
                    # queue, and 1,295 of these would start downloading.


class Busy(RuntimeError):
    """Another apply or undo holds the lock."""


class _RunLock:
    """One apply/undo at a time across every process (GUI and scheduled job).

    An OS file lock rather than a marker file: Windows drops it when the process
    dies, so a crashed run can't leave everything locked out.
    """
    PATH = os.path.join(HERE, ".itunes_apply.lock")

    def __enter__(self):
        import msvcrt
        self.fh = open(self.PATH, "a+")
        try:
            msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.fh.close()
            raise Busy("another play-count run is in progress; try again shortly")
        return self

    def __exit__(self, *exc):
        import msvcrt
        try:
            self.fh.seek(0)
            msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self.fh.close()


def _credit(con, r, run_id, now):
    """History ledger: remember the export total credited to this song."""
    na, nt = r["key"]
    prev = con.execute("SELECT plays_credited, last_credited, credited_to "
                       "FROM history_applied WHERE n_artist=? AND n_title=?",
                       (na, nt)).fetchone()
    con.execute("INSERT INTO history_applied_undo VALUES(?,?,?,?,?,?,?)",
                (run_id, na, nt, prev[0] if prev else 0, prev[1] if prev else "",
                 1 if prev else 0, prev[2] if prev else ""))
    con.execute(
        "INSERT INTO history_applied (n_artist, n_title, plays_credited, "
        "last_credited, run_id, applied_at, credited_to) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(n_artist, n_title) DO UPDATE SET "
        "plays_credited=excluded.plays_credited, last_credited=excluded.last_credited, "
        "run_id=excluded.run_id, applied_at=excluded.applied_at, "
        "credited_to=excluded.credited_to",
        (na, nt, r.get("spotify_total", r["spotify_plays"]), r["spotify_last"],
         run_id, now, r["target"]))


def apply_plan(rows, summary, con=None, progress=None, write_itunes=True,
               create_missing=False, dry=False):
    """Apply an approved plan. Returns a result dict including the run id.

    Each play reaches iTunes at most once:
      * one run at a time (a process-wide lock shared with the GUI);
      * each row re-checks, at apply time, which of its plays are still owed,
        so an old preview or a second click can't re-apply them;
      * for iTunes, the "applied" record is committed *before* the track is
        changed. A crash between the two loses that one track's plays rather
        than ever counting them twice.

    dry=True runs this exact code but never assigns to iTunes; pass a throwaway
    copy of sync.db as `con` and nothing real changes. done["changes"] lists
    every change, dry or live.
    """
    with _RunLock():
        return _apply(rows, summary, con, progress, write_itunes,
                      create_missing, dry)


def _apply(rows, summary, con, progress, write_itunes, create_missing, dry):
    import win32com.client as w
    own = con is None
    con = con or sqlite3.connect(SYNC, timeout=30)
    ensure_schema(con)
    con.commit()
    source = summary.get("source")
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    now = datetime.now().isoformat(timespec="seconds")
    done = {"run_id": run_id, "itunes": 0, "syncdb": 0, "skipped": 0,
            "already": 0, "errors": [], "changes": [], "dry": dry}

    def owed(r):
        """Plays this row still owes, checked now inside the write lock.
        None means nothing to do. Also narrows r's play keys to those owed."""
        if source == "poller":
            keys = r.get("play_keys") or []
            if not keys:
                return None
            q = ",".join("?" * len(keys))
            held = dict(con.execute(
                "SELECT play_key, target FROM poller_applied WHERE play_key IN ("
                + q + ")", keys).fetchall())
            # A play held in the database is still owed to iTunes; any other
            # recorded play is done.
            r["_staged"] = {k for k, tg in held.items()
                            if tg == TO_SYNCDB and r["target"] == TO_ITUNES}
            r["play_keys"] = [k for k in keys if k not in held or k in r["_staged"]]
            return len(r["play_keys"]) or None
        if source == "history":
            na, nt = r["key"]
            prev = con.execute("SELECT plays_credited, last_credited, credited_to "
                               "FROM history_applied WHERE n_artist=? AND n_title=?",
                               (na, nt)).fetchone()
            credited, last, dest = (prev or (0, "", ""))
            if r["target"] == TO_ITUNES and dest and dest != TO_ITUNES:
                credited = 0          # staged in the database; iTunes is still owed
            delta = r.get("spotify_total", r["spotify_plays"]) - (credited or 0)
            if delta <= 0 and (r["spotify_last"] or "") <= (last or ""):
                return None
            return max(delta, 0)
        return r["spotify_plays"]

    def record(r, ref):
        if source == "poller":
            for k in r["play_keys"]:
                if k in r.get("_staged", ()):
                    # moving from the database to iTunes: keep the old record
                    # so undo can put it back rather than drop it
                    prev = con.execute("SELECT run_id, applied_at, target, ref FROM "
                                       "poller_applied WHERE play_key=?", (k,)).fetchone()
                    con.execute("INSERT INTO poller_applied_undo VALUES(?,?,?,?,?,?)",
                                (run_id, k) + tuple(prev))
                    con.execute("UPDATE poller_applied SET run_id=?, applied_at=?, "
                                "target=?, ref=? WHERE play_key=?",
                                (run_id, now, r["target"], ref, k))
                    continue
                con.execute("INSERT OR IGNORE INTO poller_applied VALUES(?,?,?,?,?)",
                            (k, run_id, now, r["target"], ref))
        elif source == "history":
            _credit(con, r, run_id, now)

    # ------------------------------------------------------------ iTunes rows
    itunes_rows = [r for r in rows if r["target"] == TO_ITUNES]
    if itunes_rows and write_itunes:
        _com_init()
        coll = w.Dispatch("iTunes.Application").LibraryPlaylist.Tracks
        for n, r in enumerate(itunes_rows, 1):
            if progress and n % 25 == 0:
                progress(n, len(itunes_rows))
            try:
                t = coll.Item(r["idx"])
                if str(t.TrackDatabaseID) != r["pid"]:
                    done["errors"].append((r["name"], "library changed; re-snapshot"))
                    done["skipped"] += 1
                    continue
            except Exception as e:
                done["errors"].append((r["name"], str(e)))
                done["skipped"] += 1
                continue

            con.execute("BEGIN IMMEDIATE")
            plays_owed = owed(r)
            if plays_owed is None:
                con.rollback()
                done["already"] += 1
                continue
            r["spotify_plays"] = plays_owed
            # Build on what iTunes holds now, never on the snapshot: the cache
            # can be hours old, and writing its count back erased plays.
            old_count, old_pd = t.PlayedCount or 0, t.PlayedDate
            old_date = old_pd.isoformat() if old_pd else ""
            new_count = old_count + plays_owed
            sp = _parse(r["spotify_last"])
            live = _itunes_local(old_pd)
            new_date = old_date
            set_date = bool(sp and (live is None or sp > live))
            if set_date:
                new_date = r["spotify_last"]
            con.execute("INSERT INTO itunes_undo VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (run_id, now, r["pid"], r["idx"], r["artist"], r["name"],
                         old_count, old_date, new_count, new_date))
            record(r, r["pid"])
            con.commit()              # recorded before iTunes is touched
            try:
                if not dry:
                    t.PlayedCount = new_count
                    if set_date:
                        t.PlayedDate = sp.astimezone().replace(tzinfo=None)
            except Exception as e:
                # iTunes refused: take the record back so the plays stay owed.
                con.execute("DELETE FROM itunes_undo WHERE run_id=? AND pid=?",
                            (run_id, r["pid"]))
                _release_poller(con, run_id, r["pid"])
                if source == "history":
                    _uncredit(con, run_id, [r["key"]])
                con.commit()
                done["errors"].append((r["name"], str(e)))
                done["skipped"] += 1
                continue
            r["new_count"], r["new_date"], r["_applied"] = new_count, new_date, True
            done["changes"].append({
                "where": "iTunes", "artist": r["artist"], "name": r["name"],
                "plays": plays_owed, "before": old_count, "after": new_count,
                "last_before": old_date, "last_after": new_date,
                "date_changed": new_date != old_date})
            done["itunes"] += 1

    # --------------------------------------------------------- database rows
    for r in rows:
        if r["target"] != TO_SYNCDB:
            continue
        con.execute("BEGIN IMMEDIATE")
        plays_owed = owed(r)
        if plays_owed is None:
            con.rollback()
            done["already"] += 1
            continue
        cur = con.execute("SELECT spotify_plays, spotify_last_played FROM "
                          "spotify_tracks WHERE track_id=?",
                          (r["sync_track_id"],)).fetchone() or (0, "")
        prev_plays, prev_last = cur[0] or 0, cur[1] or ""
        if source == "history":
            new_plays, new_last = r["new_count"], r["new_date"]
        else:
            # New plays add to the stored total; assigning replaced whole
            # history totals (7 plays became 1).
            new_plays = prev_plays + plays_owed
            new_last = max(prev_last, r["spotify_last"] or "")
        con.execute("INSERT INTO syncdb_undo VALUES(?,?,?,?,0)",
                    (run_id, r["sync_track_id"], prev_plays, prev_last))
        con.execute("UPDATE spotify_tracks SET spotify_plays=?, "
                    "spotify_last_played=? WHERE track_id=?",
                    (new_plays, new_last, r["sync_track_id"]))
        r["spotify_plays"] = plays_owed
        record(r, r["sync_track_id"])
        con.commit()
        r["_applied"] = True
        done["changes"].append({
            "where": "database", "artist": r["artist"], "name": r["name"],
            "plays": plays_owed, "before": prev_plays, "after": new_plays,
            "last_before": prev_last, "last_after": new_last,
            "date_changed": new_last != prev_last})
        done["syncdb"] += 1

    # ------------------------------------------ songs in neither, if asked for
    if create_missing:
        done["created"] = 0
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for r in rows:
            if r["target"] != NO_TARGET:
                continue
            con.execute("BEGIN IMMEDIATE")
            plays_owed = owed(r)
            if plays_owed is None:
                con.rollback()
                done["already"] += 1
                continue
            na, nt = r["key"]
            tid = r.get("track_id") or "hist:{}:{}".format(na, nt)[:190]
            cur = con.execute("SELECT 1 FROM spotify_tracks WHERE track_id=?",
                              (tid,)).fetchone()
            if cur:
                con.execute("UPDATE spotify_tracks SET spotify_plays="
                            "COALESCE(spotify_plays,0)+? WHERE track_id=?",
                            (plays_owed, tid))
            else:
                con.execute(
                    "INSERT INTO spotify_tracks (track_id, added_at, artist, "
                    "all_artists, title, album, duration_ms, track_number, year, "
                    "cover_url, spotify_url, n_artist, n_title, status, source, "
                    "spotify_plays, spotify_last_played, first_seen, updated_at) "
                    "VALUES (?,?,?,?,?,?,0,0,'','','',?,?,?,'history',?,?,?,?)",
                    (tid, r["spotify_last"], r["artist"], r["artist"], r["name"],
                     r.get("album") or "", na, nt, LOGGED,
                     plays_owed if source == "poller" else r["new_count"],
                     r["new_date"], now_iso, now_iso))
                con.execute("INSERT INTO syncdb_undo VALUES(?,?,0,'',1)",
                            (run_id, tid))
            record(r, tid)
            con.commit()
            r["_applied"] = True
            done["created"] += 1

    if source == "poller":
        done["plays_recorded"] = con.execute(
            "SELECT COUNT(*) FROM poller_applied WHERE run_id=?",
            (run_id,)).fetchone()[0]

    if source == "history" and summary.get("watermark"):
        mark = get_meta(con, WATERMARK_KEY, "") or ""
        if summary["watermark"] != mark:
            # Remember the previous mark so undo can put it back.
            set_meta(con, "watermark_before_" + run_id, mark)
            set_meta(con, WATERMARK_KEY, summary["watermark"])
    con.commit()
    if own:
        con.close()
    return done


def _release_poller(con, run_id, ref):
    """Take back the poller plays run_id gave to ref: a play it moved from the
    database returns to the database; one it recorded fresh is owed again."""
    n = 0
    for (k,) in con.execute("SELECT play_key FROM poller_applied WHERE run_id=? "
                            "AND ref IS ?", (run_id, ref)).fetchall():
        prev = con.execute("SELECT prev_run_id, prev_applied_at, prev_target, "
                           "prev_ref FROM poller_applied_undo WHERE run_id=? AND "
                           "play_key=?", (run_id, k)).fetchone()
        if prev:
            con.execute("UPDATE poller_applied SET run_id=?, applied_at=?, "
                        "target=?, ref=? WHERE play_key=?", tuple(prev) + (k,))
            con.execute("DELETE FROM poller_applied_undo WHERE run_id=? AND "
                        "play_key=?", (run_id, k))
        else:
            con.execute("DELETE FROM poller_applied WHERE play_key=?", (k,))
        n += 1
    return n


def _uncredit(con, run_id, keys):
    """Put history ledger entries for these songs back to before run_id."""
    for na, nt in keys:
        row = con.execute("SELECT prev_credited, prev_last, existed, prev_dest "
                          "FROM history_applied_undo WHERE run_id=? AND "
                          "n_artist=? AND n_title=?", (run_id, na, nt)).fetchone()
        if not row:
            continue
        prev_n, prev_last, existed, prev_dest = row
        if existed:
            con.execute("UPDATE history_applied SET plays_credited=?, "
                        "last_credited=?, credited_to=? WHERE n_artist=? AND "
                        "n_title=?", (prev_n, prev_last, prev_dest or "", na, nt))
        else:
            con.execute("DELETE FROM history_applied WHERE n_artist=? AND "
                        "n_title=?", (na, nt))
        con.execute("DELETE FROM history_applied_undo WHERE run_id=? AND "
                    "n_artist=? AND n_title=?", (run_id, na, nt))


def undo(run_id, con=None):
    """Reverse a run: iTunes counts/dates, database totals, and the records of
    which plays were applied.

    Plays are only released back to "owed" for tracks actually restored. A track
    undo can't reach keeps its record -- releasing it would re-apply those plays
    on top of the ones still sitting in iTunes. Its undo row is kept too, so the
    undo can be retried.
    """
    with _RunLock():
        return _undo(run_id, con)


def _undo(run_id, con):
    import win32com.client as w
    own = con is None
    con = con or sqlite3.connect(SYNC, timeout=30)
    ensure_schema(con)
    rows = con.execute(
        "SELECT pid, idx, artist, name, old_count, old_date FROM itunes_undo "
        "WHERE run_id=?", (run_id,)).fetchall()
    other = 0
    for tbl in ("history_applied_undo", "syncdb_undo", "poller_applied"):
        other += con.execute("SELECT COUNT(*) FROM " + tbl + " WHERE run_id=?",
                             (run_id,)).fetchone()[0]
    if not rows and not other:
        if own:
            con.close()
        return {"restored": 0, "errors": [("", "no such run: " + run_id)]}

    out = {"restored": 0, "errors": [], "kept": 0}
    failed_pids, failed_keys = set(), set()
    if rows:
        _com_init()
        coll = w.Dispatch("iTunes.Application").LibraryPlaylist.Tracks
    for pid, idx, artist, name, old_count, old_date in rows:
        try:
            t = coll.Item(idx)
            if str(t.TrackDatabaseID) != pid:
                raise RuntimeError("library changed; not restored")
            t.PlayedCount = old_count
            dt = _for_itunes(old_date, from_utc=False)
            if dt:
                t.PlayedDate = dt
            con.execute("DELETE FROM itunes_undo WHERE run_id=? AND pid=?",
                        (run_id, pid))
            out["restored"] += 1
        except Exception as e:
            failed_pids.add(pid)
            failed_keys.add((norm_artist(artist), norm_title(name)))
            out["errors"].append((name, str(e)))
            out["kept"] += 1

    led = [(na, nt) for na, nt in con.execute(
        "SELECT n_artist, n_title FROM history_applied_undo WHERE run_id=?",
        (run_id,)) if (na, nt) not in failed_keys]
    _uncredit(con, run_id, led)
    out["uncredited"] = len(led)

    db_rows = con.execute("SELECT track_id, prev_plays, prev_last, created "
                          "FROM syncdb_undo WHERE run_id=?", (run_id,)).fetchall()
    for tid, prev_plays, prev_last, created in db_rows:
        if created:
            con.execute("DELETE FROM spotify_tracks WHERE track_id=? AND status=?",
                        (tid, LOGGED))
        else:
            con.execute("UPDATE spotify_tracks SET spotify_plays=?, "
                        "spotify_last_played=? WHERE track_id=?",
                        (prev_plays, prev_last or None, tid))
    out["db_restored"] = len(db_rows)
    con.execute("DELETE FROM syncdb_undo WHERE run_id=?", (run_id,))

    released = 0
    for (ref,) in con.execute("SELECT DISTINCT ref FROM poller_applied WHERE "
                              "run_id=?", (run_id,)).fetchall():
        if ref in failed_pids:
            continue
        released += _release_poller(con, run_id, ref)
    out["plays_released"] = released

    prev_mark = get_meta(con, "watermark_before_" + run_id)
    if prev_mark is not None and not failed_pids:
        set_meta(con, WATERMARK_KEY, prev_mark)
        con.execute("DELETE FROM meta WHERE k=?", ("watermark_before_" + run_id,))
        out["watermark_restored"] = prev_mark or "(cleared)"
    con.commit()
    if own:
        con.close()
    return out


def runs(con=None):
    own = con is None
    con = con or sqlite3.connect(SYNC)
    ensure_schema(con)
    out = con.execute("SELECT run_id, applied_at, COUNT(*) FROM itunes_undo "
                      "GROUP BY run_id, applied_at ORDER BY run_id DESC").fetchall()
    if own:
        con.close()
    return out
