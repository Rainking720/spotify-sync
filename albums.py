"""Expand one track into its full album, on explicit request only.

Nothing here runs during a normal sync. It exists so you can pick a track in the
GUI and deliberately pull the rest of its album.

Album tracks are stored with source='album' so they can never short-circuit the
incremental Liked Songs fetch, which stops at the first page of already-known ids.
"""
import os
from datetime import datetime, timezone

from norm import norm_artist, norm_title

# What we know about each album track before downloading anything.
OWNED = "owned"            # already in D:\Mp3
HAVE = "downloaded"        # already pulled by this tool
QUEUED = "queued"          # already pending/needs_review/failed in sync.db
NEW = "new"                # nothing known: this is what a download would fetch
SKIPPED = "skipped"


# Spotify rejects limit > 10 on /search and /artists/{id}/albums with
# "Invalid limit", while /me/tracks and /albums/{id}/tracks still accept 50.
# Verified by probing each boundary; page these two in tens.
PAGE_LIMIT = 10


def album_of(sp, track_id):
    """Full album object for a track we already hold, including art and date."""
    return sp.track(track_id)["album"]


def search_artists(sp, name, limit=PAGE_LIMIT):
    """Candidate artists for a typed name. Exact matches are floated to the top."""
    items = sp.search(q=name, type="artist", limit=min(limit, PAGE_LIMIT))["artists"]["items"]
    want = (name or "").strip().lower()
    items.sort(key=lambda a: (a["name"].strip().lower() != want,
                              -(a.get("popularity") or 0)))
    return items


def artist_releases(sp, artist_id, groups="album,single,compilation"):
    """Every release for an artist, de-duplicated by (name, type).

    Spotify lists the same album once per market, so the raw feed repeats.
    """
    out, offset = {}, 0
    while True:
        page = sp.artist_albums(artist_id, include_groups=groups,
                                limit=PAGE_LIMIT, offset=offset)
        items = page.get("items") or []
        if not items:
            break
        for al in items:
            out.setdefault((al["name"].strip().lower(), al.get("album_type")), al)
        offset += len(items)
        if offset >= page.get("total", 0):
            break
    releases = list(out.values())
    releases.sort(key=lambda a: (a.get("release_date") or ""), reverse=True)
    return releases


def album_tracks(sp, album):
    """Flatten an album's tracklist into the shape download.invoke expects.

    album_tracks() returns *simplified* track objects with no album or images,
    so those fields come from the parent album.
    """
    images = album.get("images") or []
    cover = images[0]["url"] if images else ""
    year = (album.get("release_date") or "")[:4]
    out, offset = [], 0
    while True:
        page = sp.album_tracks(album["id"], limit=50, offset=offset)
        items = page.get("items") or []
        if not items:
            break
        for it in items:
            if not it.get("id"):
                continue
            artists = [a["name"] for a in it.get("artists", []) if a.get("name")]
            out.append({
                "track_id": it["id"],
                "artist": artists[0] if artists else "",
                "all_artists": ", ".join(artists),
                "title": it.get("name", ""),
                "album": album.get("name", ""),
                "duration_ms": it.get("duration_ms") or 0,
                "track_number": it.get("track_number") or 0,
                "year": year,
                "cover_url": cover,
                "spotify_url": (it.get("external_urls") or {}).get("spotify", ""),
                "added_at": None,
            })
        offset += len(items)
        if offset >= page.get("total", 0):
            break
    return out


def classify(con, tracks, lib_keys):
    """Annotate each track with state + why, without writing anything."""
    known = {tid: st for tid, st in
             con.execute("SELECT track_id, status FROM spotify_tracks")}
    for t in tracks:
        na, nt = norm_artist(t["artist"]), norm_title(t["title"])
        t["n_artist"], t["n_title"] = na, nt
        st = known.get(t["track_id"])
        hit = lib_keys.get((na, nt))
        if st == "downloaded":
            t["state"], t["detail"] = HAVE, "already downloaded"
        elif st == "skipped":
            t["state"], t["detail"] = SKIPPED, "previously skipped"
        elif st in ("pending", "needs_review", "failed"):
            t["state"], t["detail"] = QUEUED, "already queued (" + st + ")"
        elif hit:
            t["state"], t["detail"] = OWNED, os.path.basename(hit)
        else:
            t["state"], t["detail"] = NEW, ""
    return tracks


def summarize(tracks):
    out = {}
    for t in tracks:
        out[t["state"]] = out.get(t["state"], 0) + 1
    return out


def queue(con, tracks):
    """Insert the given album tracks as pending. Returns how many were added.

    Existing rows are left alone: a liked track keeps source='liked' and whatever
    status it already earned.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    existing = {tid for (tid,) in con.execute("SELECT track_id FROM spotify_tracks")}
    added = 0
    for t in tracks:
        if t["track_id"] in existing:
            continue
        con.execute(
            "INSERT INTO spotify_tracks (track_id, added_at, artist, all_artists, "
            "title, album, duration_ms, track_number, year, cover_url, spotify_url, "
            "n_artist, n_title, status, source, first_seen, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'pending','album',?,?)",
            (t["track_id"], now, t["artist"], t["all_artists"], t["title"],
             t["album"], t["duration_ms"], t["track_number"], t["year"],
             t["cover_url"], t["spotify_url"], t["n_artist"], t["n_title"],
             now, now))
        added += 1
    con.commit()
    return added
