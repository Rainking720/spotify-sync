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


# Settings a missing path breaks outright, versus ones that are optional or get
# created on first use.
REQUIRED = {"library_root"}
CREATED_ON_USE = {"staging_root", "temp_root"}
FILE_KEYS = {"plays_path", "ytdlp_path", "ffmpeg_path"}


def is_default(key, value):
    """True when value is blank or just the default written out."""
    if value in (None, ""):
        return True
    return (os.path.normcase(_clean(key, value)) ==
            os.path.normcase(_clean(key, DEFAULTS[key][0])))


def validate(key, value):
    """('ok' | 'warn' | 'error', message) for a value about to be saved."""
    v = _clean(key, value) if value else _clean(key, DEFAULTS[key][0])
    if key == "itunes_playlist":
        return "ok", "" if value else "uses the default"
    if key == "contact_email":
        if v and "@" not in v:
            return "warn", "doesn't look like an email address"
        return ("warn", "blank - MusicBrainz asks for a contact") if not v else ("ok", "")
    if key in ("ytdlp_path", "ffmpeg_path") and not v:
        found = tool("yt-dlp.exe" if key == "ytdlp_path" else "ffmpeg.exe", key)
        return ("ok", "found automatically: " + found) if found else \
               ("error", "not found automatically - browse to it")
    if key in PATH_KEYS and not os.path.exists(v):
        if key in REQUIRED or key in ("ytdlp_path", "ffmpeg_path"):
            return "error", "doesn't exist"
        if key in CREATED_ON_USE:
            return "warn", "doesn't exist yet - it will be created on first use"
        return "warn", "doesn't exist - that feature won't find anything"
    return "ok", ""


def save(values):
    """Write settings into config.json; returns the keys that changed.

    values: {key: text}. Blank, or equal to the default, removes the key so the
    default applies (and keeps adapting, e.g. %USERPROFILE%). Every other key in
    the file -- the Spotify credentials in particular -- is kept exactly as it
    was. Written to a temp file and swapped in, with the previous version kept as
    config.json.bak, so a failed write can't leave a half-written config.
    """
    cfg = {}
    if os.path.exists(CONFIG):
        with open(CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)          # let a broken file fail loudly, not be wiped
    changed = []
    for key, value in values.items():
        if key not in DEFAULTS:
            raise KeyError("unknown setting: " + key)
        value = (value or "").strip()
        effective = _clean(key, value) if value else _clean(key, DEFAULTS[key][0])
        if os.path.normcase(effective) == os.path.normcase(get(key)):
            continue        # untouched: leave the file's entry (or its absence) alone
        if is_default(key, value):
            if key in cfg:
                del cfg[key]
                changed.append(key)
        elif cfg.get(key) != value:
            cfg[key] = value
            changed.append(key)
    if not changed:
        return []
    tmp = CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    if os.path.exists(CONFIG):
        shutil.copy2(CONFIG, CONFIG + ".bak")
    os.replace(tmp, CONFIG)
    _cache["mtime"] = None            # re-read on next get()
    return changed


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
