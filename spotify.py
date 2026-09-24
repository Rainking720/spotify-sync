"""Spotify auth + Liked Songs fetch.

Auth is the Authorization Code flow: a browser opens once, you approve, and the
refresh token is cached in .spotify_cache so every later run is unattended.
"""
import json, os, sys
import spotipy
from spotipy.oauth2 import SpotifyOAuth

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
CACHE = os.path.join(HERE, ".spotify_cache")
SCOPE = "user-library-read"


def load_config():
    cfg = {}
    if os.path.exists(CONFIG):
        with open(CONFIG) as f:
            cfg = json.load(f)
    # Env vars win, so the secret can stay out of the file if you'd rather.
    for key, env in (("client_id", "SPOTIFY_CLIENT_ID"),
                     ("client_secret", "SPOTIFY_CLIENT_SECRET"),
                     ("redirect_uri", "SPOTIFY_REDIRECT_URI")):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    missing = [k for k in ("client_id", "client_secret", "redirect_uri") if not cfg.get(k)]
    if missing:
        sys.exit(f"config.json is missing: {', '.join(missing)}")
    return cfg


def client(cfg=None):
    cfg = cfg or load_config()
    auth = SpotifyOAuth(
        client_id=cfg["client_id"],
        client_secret=cfg["client_secret"],
        redirect_uri=cfg["redirect_uri"],
        scope=SCOPE,
        cache_path=CACHE,
        open_browser=True,
    )
    return spotipy.Spotify(auth_manager=auth, requests_timeout=30, retries=5)


def _flatten(item):
    """Turn one /me/tracks item into a flat dict, or None if unusable."""
    tr = item.get("track") or {}
    if not tr or not tr.get("id") or tr.get("is_local"):
        return None
    artists = [a["name"] for a in tr.get("artists", []) if a.get("name")]
    album = tr.get("album") or {}
    images = album.get("images") or []
    return {
        "track_id": tr["id"],
        "added_at": item.get("added_at"),
        "artist": artists[0] if artists else "",
        "all_artists": ", ".join(artists),
        "title": tr.get("name", ""),
        "album": album.get("name", ""),
        "duration_ms": tr.get("duration_ms") or 0,
        "track_number": tr.get("track_number") or 0,
        "year": (album.get("release_date") or "")[:4],
        "cover_url": images[0]["url"] if images else "",
        "spotify_url": (tr.get("external_urls") or {}).get("spotify", ""),
    }


def fetch_liked(sp, known_ids=None, full=False, progress=True):
    """Page through Liked Songs, newest first.

    Likes are append-only at the top, so an incremental run stops once it sees a
    whole page it already knows. --full forces a complete walk.
    """
    known_ids = known_ids or set()
    out, offset, total = [], 0, None
    while True:
        page = sp.current_user_saved_tracks(limit=50, offset=offset)
        if total is None:
            total = page.get("total", 0)
            if progress:
                print(f"spotify: {total} liked songs")
        items = page.get("items") or []
        if not items:
            break
        flat = [f for f in (_flatten(i) for i in items) if f]
        out.extend(flat)
        if not full and flat and all(f["track_id"] in known_ids for f in flat):
            if progress:
                print(f"spotify: reached already-seen tracks at offset {offset}, stopping")
            break
        offset += len(items)
        if progress and offset % 500 == 0:
            print(f"  ...{offset}/{total}")
        if offset >= total:
            break
    return out


if __name__ == "__main__":
    sp = client()
    me = sp.current_user()
    print(f"authenticated as: {me.get('display_name') or me.get('id')}")
    page = sp.current_user_saved_tracks(limit=1)
    print(f"liked songs total: {page.get('total')}")
    for i in (page.get("items") or []):
        f = _flatten(i)
        if f:
            print(f"newest like: {f['artist']} - {f['title']} (added {f['added_at']})")
