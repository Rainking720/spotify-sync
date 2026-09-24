"""Parse Spotify's Extended Streaming History export.

The export is a one-time download from Spotify's privacy page: a folder of
Streaming_History_Audio_<year>.json files, each an array of play events.

A "play" here means ms_played >= MIN_MS. This matters more than it looks: 41.6%
of the raw entries in this export are under 30 seconds and 27.6% are under five,
so counting every row would inflate the totals by about 71% and fill the
most-played rankings with things that were skipped.
"""
import glob, json, os
from datetime import datetime, timezone

from norm import norm_artist, norm_title

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DIR = (r"C:\Users\MurphyG\Downloads\my_spotify_data"
               r"\Spotify Extended Streaming History")
MIN_MS = 60_000          # only count a play once a minute has actually been heard


def dir_from_config():
    cfg = {}
    p = os.path.join(HERE, "config.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                cfg = json.load(f)
        except (ValueError, OSError):
            cfg = {}
    return cfg.get("history_dir") or DEFAULT_DIR


def files(directory=None):
    directory = directory or dir_from_config()
    return sorted(glob.glob(os.path.join(directory,
                                         "Streaming_History_Audio_*.json")))


def aggregate(directory=None, min_ms=MIN_MS, until=None):
    """Collapse the export into one row per song.

    Returns {"tracks": {key: row}, "plays": n, "entries": n, "skipped": n,
             "watermark": iso, "files": [...]}

    key is the normalised (artist, title): the same song appears under several
    spotify_track_uris across releases, and iTunes has no notion of those ids
    anyway, so the name is the only thing that can join the two libraries.
    """
    paths = files(directory)
    tracks, entries, counted, watermark = {}, 0, 0, ""
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (ValueError, OSError):
            continue
        for rec in data:
            ts = rec.get("ts") or ""
            if until and ts > until:
                # Past the point the poller took over: counting it again would
                # re-apply plays the poller has already applied.
                continue
            entries += 1
            if ts > watermark:
                # The watermark covers every entry, not just counted ones: it
                # marks how far the export reaches, which is what stops the
                # 2-hourly poller re-applying plays this import already has.
                watermark = ts
            name = rec.get("master_metadata_track_name")
            if not name:
                continue                       # podcast, audiobook or local file
            if (rec.get("ms_played") or 0) < min_ms:
                continue
            artist = rec.get("master_metadata_album_artist_name") or ""
            key = (norm_artist(artist), norm_title(name))
            row = tracks.get(key)
            if row is None:
                uri = rec.get("spotify_track_uri") or ""
                row = tracks[key] = {
                    "artist": artist, "name": name,
                    "album": rec.get("master_metadata_album_album_name") or "",
                    "track_id": uri.split(":")[-1] if uri else "",
                    "count": 0, "last": "",
                }
            row["count"] += 1
            counted += 1
            if ts > row["last"]:
                row["last"] = ts
    return {"tracks": tracks, "plays": counted, "entries": entries,
            "skipped": entries - counted, "watermark": watermark,
            "files": paths, "min_ms": min_ms}


def local_time(iso):
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso[:16].replace("T", " ")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    d = aggregate()
    print("files: {}".format(len(d["files"])))
    print("{} entries -> {} plays of at least {}s across {} tracks".format(
        d["entries"], d["plays"], d["min_ms"] // 1000, len(d["tracks"])))
    print("not counted (too short / not music): {}".format(d["skipped"]))
    print("watermark: {}\n".format(d["watermark"]))
    top = sorted(d["tracks"].values(), key=lambda r: -r["count"])[:15]
    print("{:>5}  {:<30} {:<26} {}".format("Plays", "Track", "Artist", "Last played"))
    for r in top:
        print("{:>5}  {:<30} {:<26} {}".format(
            r["count"], r["name"][:30], r["artist"][:26], local_time(r["last"])))
