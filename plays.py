"""Play counts from the SpotifyPoller log.

A separate backend (C:\\Users\\MurphyG\\SpotifyPoller) polls Spotify's
recently-played endpoint every 2 hours and appends each play to plays.jsonl.
That file is the only thing read here -- the poller is left alone.

Aggregation mirrors report.js in that folder: group by track_id, count plays,
keep the newest played_at, falling back to "name::artists" when track_id is
missing.
"""
import json, os
from datetime import datetime, timezone

from norm import norm_artist, norm_title

HERE = os.path.dirname(os.path.abspath(__file__))

_cache = {"path": None, "mtime": None, "data": None}


def path_from_config():
    """The poller's play log: the plays_path setting (see settings.py)."""
    import settings
    return settings.get("plays_path")


def aggregate(path=None):
    """Return {"by_id": {...}, "by_key": {...}, "total": n, "tracks": n}.

    by_id  -- track_id      -> {name, artists, count, last}
    by_key -- (nartist, ntitle) -> same dict, for plays whose track_id doesn't
              appear in sync.db because the song was logged from another release.
    """
    path = path or path_from_config()
    if not os.path.exists(path):
        return {"by_id": {}, "by_key": {}, "total": 0, "tracks": 0, "path": path}

    by_id, total = {}, 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue          # a partially written last line; skip it
            key = rec.get("track_id") or "{}::{}".format(
                rec.get("name", ""), rec.get("artists", ""))
            row = by_id.get(key)
            played = rec.get("played_at", "")
            if row:
                row["count"] += 1
                if played > row["last"]:
                    row["last"] = played
            else:
                by_id[key] = {"name": rec.get("name", ""),
                              "artists": rec.get("artists", ""),
                              "count": 1, "last": played}
            total += 1

    by_key = {}
    for row in by_id.values():
        primary = (row["artists"].split(",")[0]).strip()
        by_key.setdefault((norm_artist(primary), norm_title(row["name"])), row)
    return {"by_id": by_id, "by_key": by_key, "total": total,
            "tracks": len(by_id), "path": path}


def load(path=None):
    """Cached aggregate; re-reads only when the log has changed."""
    path = path or path_from_config()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    if _cache["path"] == path and _cache["mtime"] == mtime and _cache["data"]:
        return _cache["data"]
    data = aggregate(path)
    _cache.update({"path": path, "mtime": mtime, "data": data})
    return data


def annotate(items, data=None):
    """Attach play_count / last_played to sync.db rows, in place.

    Matches on track_id first, then normalised artist+title -- the same song can
    carry a different track_id when it was played from another release.
    """
    data = data or load()
    by_id, by_key = data["by_id"], data["by_key"]
    matched, claimed = 0, set()

    # Pass 1 -- exact track_id. Record which play rows get used.
    for t in items:
        row = by_id.get(t.get("track_id"))
        if row is not None:
            t["play_count"], t["last_played"] = row["count"], row["last"]
            claimed.add(id(row))
            matched += 1
        else:
            t["play_count"], t["last_played"] = 0, ""

    # Pass 2 -- artist+title, for rows the exact pass missed. A play already
    # claimed is never counted again: the same song can be liked twice (album
    # and single), and without this both rows would each show that one play.
    for t in items:
        if t["play_count"]:
            continue
        key = (t.get("n_artist"), t.get("n_title"))
        if key == (None, None):
            key = (norm_artist(t.get("artist")), norm_title(t.get("title")))
        row = by_key.get(key)
        if row is not None and id(row) not in claimed:
            t["play_count"], t["last_played"] = row["count"], row["last"]
            claimed.add(id(row))
            matched += 1
    # unmatched() reads this instead of re-deriving the match, which it can't do
    # reliably: GUI rows carry no n_artist/n_title, so a fallback-matched track
    # looked unmatched there and got counted on both sides of the total.
    data["_claimed"] = claimed
    return matched


def unmatched(items, data=None):
    """Played tracks that no sync.db row accounts for (played but not liked).

    Call annotate(items) first: this reports exactly what annotate didn't claim,
    so attributed + unattributed always equals the number of plays logged.
    """
    data = data or load()
    claimed = data.get("_claimed")
    if claimed is None:
        annotate(items, data)
        claimed = data.get("_claimed", set())
    out = [row for row in data["by_id"].values() if id(row) not in claimed]
    out.sort(key=lambda r: (-r["count"], r["name"]))
    return out


def local_time(iso):
    """'2026-09-22T15:46:07.534Z' -> a short local timestamp for display."""
    if not iso:
        return ""
    try:
        s = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso[:16].replace("T", " ")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    d = load()
    print("log: {}".format(d["path"]))
    print("{} plays across {} distinct tracks\n".format(d["total"], d["tracks"]))
    rows = sorted(d["by_id"].values(), key=lambda r: (-r["count"], r["name"]))
    print("{:>5}  {:<36} {:<24} {}".format("Plays", "Track", "Artist", "Last played"))
    for r in rows[:25]:
        print("{:>5}  {:<36} {:<24} {}".format(
            r["count"], r["name"][:36], r["artists"][:24], local_time(r["last"])))
