"""Every machine-specific setting, in one place.

Values come from config.json beside this file; anything not set there falls back
to the default below. The defaults are this project's original machine, written
with %USERPROFILE% where that makes them portable -- so an existing config.json
that sets none of these keeps behaving exactly as before.

    python settings.py      # print every setting and where it came from

Environment variables in values are expanded (%USERPROFILE%, %LOCALAPPDATA%...).
Forward or back slashes both work.
"""
import json, os, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")

# key: (default, what it is)
DEFAULTS = {
    "library_root": (r"D:\Mp3",
                     "your music library, organised <Artist>\\<Album>\\<file>.mp3"),
    "staging_root": (r"C:\temp\SpotifyDownloadOnTheSpot\Sorted",
                     "where new downloads land before 'Move to library'"),
    "temp_root": (r"C:\temp\SpotifyDownloadOnTheSpot\Tracks",
                  "scratch space for downloads in progress"),
    "plays_path": (r"%USERPROFILE%\SpotifyPoller\plays.jsonl",
                   "the SpotifyPoller play log"),
    "history_dir": (r"%USERPROFILE%\Downloads\my_spotify_data"
                    r"\Spotify Extended Streaming History",
                    "Spotify's Extended Streaming History export"),
    "ytdlp_path": ("", "yt-dlp.exe; blank = this folder, then its parent, then PATH"),
    "ffmpeg_path": ("", "ffmpeg.exe; blank = this folder, then its parent, then PATH"),
    "itunes_playlist": ("NewFromSpotify",
                        "playlist that 'Move to library' adds songs to"),
    "contact_email": ("", "sent to MusicBrainz with lookups made by yt2mp3.ps1"),
}
# keys holding a file or folder path
PATH_KEYS = {"library_root", "staging_root", "temp_root", "plays_path",
             "history_dir", "ytdlp_path", "ffmpeg_path"}

_cache = {"mtime": None, "cfg": {}}


def _config():
    """config.json, re-read only when it changes."""
    try:
        mtime = os.path.getmtime(CONFIG)
    except OSError:
        return {}
    if _cache["mtime"] != mtime:
        try:
            with open(CONFIG, encoding="utf-8") as f:
                _cache["cfg"] = json.load(f)
        except (ValueError, OSError):
            _cache["cfg"] = {}
        _cache["mtime"] = mtime
    return _cache["cfg"]


def _clean(key, value):
    value = os.path.expandvars(str(value)).strip()
    if key in PATH_KEYS and value:
        value = os.path.normpath(value)
    return value


def get(key):
    """The effective value of a setting (config.json, else the default)."""
    if key not in DEFAULTS:
        raise KeyError("unknown setting: " + key)
    raw = _config().get(key)
    if raw in (None, ""):
        raw = DEFAULTS[key][0]
    return _clean(key, raw)


def source(key):
    return "config.json" if _config().get(key) not in (None, "") else "default"


def tool(name, key):
    """Find yt-dlp.exe / ffmpeg.exe: the configured path, else next to these
    scripts, else their parent folder, else PATH.

    Parent before PATH on purpose: on the original machine PATH resolves to a
    pip-installed yt-dlp months older than the one in the parent folder, and
    YouTube breaks old versions. yt2mp3.ps1 searches in the same order.
    """
    configured = get(key)
    if configured:
        return configured if os.path.exists(configured) else None
    for folder in (HERE, os.path.dirname(HERE)):
        candidate = os.path.join(folder, name)
        if os.path.exists(candidate):
            return candidate
    return shutil.which(name)


def ytdlp():
    return tool("yt-dlp.exe", "ytdlp_path")


def ffmpeg():
    return tool("ffmpeg.exe", "ffmpeg_path")


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("settings from", CONFIG if os.path.exists(CONFIG) else "(no config.json)")
    for k, (default, what) in DEFAULTS.items():
        print("  {:<16} {:<11} {}".format(k, "[" + source(k) + "]", get(k) or "(blank)"))
        print("  {:<16} {:<11} {}".format("", "", what))
    print("  {:<16} {:<11} {}".format("-> yt-dlp", "", ytdlp() or "NOT FOUND"))
    print("  {:<16} {:<11} {}".format("-> ffmpeg", "", ffmpeg() or "NOT FOUND"))
