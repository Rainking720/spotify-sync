"""Add promoted files to iTunes and a playlist.

This drives iTunes' COM automation interface. The Store (UWP) build registers it
through packaged COM, so the class does not appear under the classic
HKLM\\Software\\Classes\\iTunes.Application key even though Dispatch works -- don't
take a missing registry key as proof it's unavailable.

Everything here is best-effort: if iTunes isn't reachable, the caller's file move
has already succeeded and must not be treated as failed.
"""
import os

import settings as _settings
PLAYLIST = _settings.get("itunes_playlist")   # see settings.py
PROG_ID = "iTunes.Application"
ADD_TIMEOUT = 30.0


class ITunesError(RuntimeError):
    pass


def _com():
    try:
        import pythoncom
        import win32com.client as client
    except ImportError as e:
        raise ITunesError("pywin32 is not installed (" + str(e) + ")")
    return pythoncom, client


def init_thread():
    """COM must be initialised per thread; the GUI does its moves on a worker."""
    pythoncom, _ = _com()
    try:
        pythoncom.CoInitialize()
    except Exception:
        pass


def uninit_thread():
    """Deliberately a no-op.

    Calling CoUninitialize() here tore down COM for the whole calling thread,
    even when this module didn't own it -- a caller that used COM before us was
    left with "CoInitialize has not been called". Leaving COM initialised for the
    life of the thread is harmless, and the GUI's worker threads are short-lived.
    """
    return


def connect():
    """Attach to iTunes. Launches it if it isn't running."""
    _, client = _com()
    try:
        return client.Dispatch(PROG_ID)
    except Exception as e:
        raise ITunesError("could not reach iTunes: " + str(e))


def available():
    try:
        app = connect()
        return True, str(app.Version)
    except Exception as e:
        return False, str(e)


def get_playlist(app, name=PLAYLIST, create=True):
    """Find a user playlist by name, creating it if needed.

    Smart playlists are rejected: their contents come from a rule, so AddFile
    would fail and silently look like a no-op.
    """
    _, client = _com()
    src = app.LibrarySource
    for i in range(1, src.Playlists.Count + 1):
        pl = src.Playlists.Item(i)
        if pl.Name.lower() != name.lower():
            continue
        try:
            up = client.CastTo(pl, "IITUserPlaylist")
        except Exception:
            up = pl
        if getattr(up, "Smart", False):
            raise ITunesError("playlist '" + name + "' is a smart playlist; "
                              "tracks cannot be added to it directly")
        return up
    if not create:
        raise ITunesError("playlist '" + name + "' not found")
    pl = app.CreatePlaylist(name)
    try:
        return client.CastTo(pl, "IITUserPlaylist")
    except Exception:
        return pl


def add_file(playlist, path, timeout=ADD_TIMEOUT):
    """Add one file to the library and the playlist.

    Returns (track_or_None, location). `location` is where iTunes says the track
    lives: if that differs from `path`, iTunes copied the file into its own media
    folder ("Copy files to iTunes Media folder when adding to library"), which
    means a second copy on disk.
    """
    import time
    if not os.path.exists(path):
        raise ITunesError("file not found: " + path)
    status = playlist.AddFile(path)
    if status is None:
        raise ITunesError("iTunes rejected the file (unsupported or unreadable)")
    deadline = time.time() + timeout
    while getattr(status, "InProgress", False) and time.time() < deadline:
        time.sleep(0.1)
    try:
        tracks = status.Tracks
        if tracks is None or tracks.Count < 1:
            raise ITunesError("iTunes reported no track added")
        track = tracks.Item(1)
    except ITunesError:
        raise
    except Exception as e:
        raise ITunesError("could not read the added track: " + str(e))
    # Location lives on IITFileOrCDTrack, not the base IITTrack the collection
    # hands back; without this cast every read raises AttributeError and the
    # path silently comes back empty.
    location = ""
    try:
        _, client = _com()
        location = client.CastTo(track, "IITFileOrCDTrack").Location or ""
    except Exception:
        pass
    return track, location


def add_many(paths, playlist_name=PLAYLIST, progress=None):
    """Add several files. Returns (added, [(path, error)], copied_elsewhere)."""
    init_thread()
    added, errors, copied = 0, [], []
    try:
        app = connect()
        pl = get_playlist(app, playlist_name)
        for i, p in enumerate(paths, 1):
            if progress:
                progress(i, len(paths), p)
            try:
                _, loc = add_file(pl, p)
                added += 1
                if loc and os.path.normcase(os.path.normpath(loc)) != \
                        os.path.normcase(os.path.normpath(p)):
                    copied.append((p, loc))
            except Exception as e:
                errors.append((p, str(e)))
    finally:
        uninit_thread()
    return added, errors, copied
