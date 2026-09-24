"""Move downloaded files out of the staging folder into D:\\Mp3.

Downloads land in yt2mp3.ps1's output tree, which already uses the library's
layout (<Artist>\\<Album>\\<Artist> - NN - <Title>.mp3). Promotion therefore just
relocates the path relative to the staging root, preserving the sanitising the
script already did instead of re-deriving folder names from tags and risking a
mismatch.

Nothing is ever overwritten: if a file of that name already exists in the library
the move is reported as a conflict and skipped, because the library copy could be
something you care about.
"""
import json, os, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STAGING = r"C:\temp\SpotifyDownloadOnTheSpot\Sorted"
DEFAULT_LIBRARY = r"D:\Mp3"

OK = "ok"                    # ready to move
ALREADY = "already sorted"   # lives under the library root already
MISSING = "file missing"
CONFLICT = "already in library"
OUTSIDE = "outside staging"  # unknown location; fall back to artist/album tags


def roots():
    cfg = {}
    path = os.path.join(HERE, "config.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
        except (ValueError, OSError):
            cfg = {}
    lib = os.path.normpath(cfg.get("library_root") or DEFAULT_LIBRARY)
    stage = os.path.normpath(cfg.get("staging_root") or DEFAULT_STAGING)
    return lib, stage


def _sanitize(s):
    out = "".join("_" if ch in '<>:"/\\|?*' else ch for ch in (s or ""))
    out = out.strip().rstrip(".")
    return out or "_"


def under(path, root):
    if not path:
        return False
    try:
        return os.path.commonpath([os.path.normpath(path),
                                   os.path.normpath(root)]) == os.path.normpath(root)
    except ValueError:      # different drives
        return False


def destination(track, src, lib, stage):
    """Where this file should live in the library."""
    if under(src, stage):
        return os.path.join(lib, os.path.relpath(src, stage))
    # Unknown origin: rebuild <Artist>\<Album>\<filename> from the tags we hold.
    return os.path.join(lib, _sanitize(track.get("artist")),
                        _sanitize(track.get("album") or "Singles"),
                        os.path.basename(src))


def plan(tracks, lib=None, stage=None):
    """Work out what would happen. Pure inspection: nothing is moved."""
    if lib is None or stage is None:
        lib, stage = roots()
    out = []
    for t in tracks:
        src = t.get("matched_path")
        if not src:
            out.append({"track": t, "src": None, "dst": None, "state": MISSING})
            continue
        src = os.path.normpath(src)
        if under(src, lib):
            out.append({"track": t, "src": src, "dst": src, "state": ALREADY})
            continue
        if not os.path.exists(src):
            out.append({"track": t, "src": src, "dst": None, "state": MISSING})
            continue
        dst = os.path.normpath(destination(t, src, lib, stage))
        state = CONFLICT if os.path.exists(dst) else OK
        if not under(src, stage) and state == OK:
            state = OUTSIDE       # still movable, just flag where it came from
        out.append({"track": t, "src": src, "dst": dst, "state": state})
    return out


def apply(entries, con=None, prune=True):
    """Move everything in OK/OUTSIDE state. Returns (moved, [(entry, error)])."""
    moved, errors = 0, []
    for e in entries:
        if e["state"] not in (OK, OUTSIDE):
            continue
        src, dst = e["src"], e["dst"]
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.exists(dst):          # re-check: the plan may be stale
                e["state"] = CONFLICT
                errors.append((e, "destination appeared before the move"))
                continue
            shutil.move(src, dst)            # handles the C: -> D: copy+delete
            moved += 1
            e["state"] = ALREADY
            e["moved"] = True                # distinguishes "we moved it just now"
                                             # from "it was already sorted"
            if con is not None:
                con.execute("UPDATE spotify_tracks SET matched_path=?, "
                            "updated_at=datetime('now') WHERE track_id=?",
                            (dst, e["track"]["track_id"]))
            if prune:
                _prune(os.path.dirname(src))
        except Exception as ex:
            errors.append((e, str(ex)))
    if con is not None:
        con.commit()
    return moved, errors


def _prune(folder, stop_after=2):
    """Remove folders the move just emptied, walking up a couple of levels."""
    for _ in range(stop_after):
        try:
            if not os.path.isdir(folder) or os.listdir(folder):
                return
            os.rmdir(folder)
            folder = os.path.dirname(folder)
        except OSError:
            return


def summarize(entries):
    out = {}
    for e in entries:
        out[e["state"]] = out.get(e["state"], 0) + 1
    return out


# --- discarding a redundant staging copy -------------------------------------
#
# For a track you already own: throw away the copy we downloaded and record the
# row as owned. Only files inside the staging tree are ever deleted.

DELETE = "delete"          # a staging copy we may remove
PROTECTED = "in library"   # lives under D:\Mp3 -- never deleted here
MANUAL_OWNED = "confirmed owned manually"


def discard_plan(tracks, lib=None, stage=None):
    """What a discard would do. Nothing is touched."""
    if lib is None or stage is None:
        lib, stage = roots()
    out = []
    for t in tracks:
        src = t.get("matched_path")
        src = os.path.normpath(src) if src else None
        if src and under(src, lib):
            # Deleting here would destroy the library copy itself.
            state = PROTECTED
        elif not src or not os.path.exists(src):
            state = MISSING
        elif under(src, stage):
            state = DELETE
        else:
            state = OUTSIDE     # somewhere unexpected; don't delete it
        out.append({"track": t, "src": src, "dst": None, "state": state})
    return out


def discard(entries, con=None, lib_keys=None, prune=True):
    """Delete staging copies and mark the rows owned.

    Rows are marked owned whatever their file state: the point is that you have
    the song already. matched_path is repointed at the library file when the
    index knows it, so the row still names something real.
    Returns (deleted, marked, [(entry, error)]).
    """
    deleted = marked = 0
    errors = []
    for e in entries:
        t = e["track"]
        if e["state"] == DELETE:
            try:
                os.remove(e["src"])
                deleted += 1
                if prune:
                    _prune(os.path.dirname(e["src"]))
            except Exception as ex:
                errors.append((e, str(ex)))
                continue
        elif e["state"] == PROTECTED:
            errors.append((e, "file is in the library; left alone"))
            continue
        if con is not None:
            path = None
            if lib_keys:
                path = lib_keys.get((t.get("n_artist"), t.get("n_title")))
            con.execute("UPDATE spotify_tracks SET status='owned', matched_path=?, "
                        "note=?, updated_at=datetime('now') WHERE track_id=?",
                        (path, MANUAL_OWNED, t["track_id"]))
        marked += 1
    if con is not None:
        con.commit()
    return deleted, marked, errors
