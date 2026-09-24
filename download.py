"""Download pending tracks via yt2mp3.ps1 and record the outcome.

Spotify metadata is passed straight through (-Artist/-Title/-Album/-Track/-Year/
-CoverUrl) with -NoLookup, because Spotify already knows more than MusicBrainz can
infer from a YouTube title. -NonInteractive guarantees the script can never block
on a prompt during an unattended run.
"""
import json, os, subprocess, sys, tempfile

import procutil
from datetime import datetime, timezone

import ytpick

HERE = os.path.dirname(os.path.abspath(__file__))
PS1 = os.path.join(HERE, "yt2mp3.ps1")   # versioned with this project


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def invoke(track, url, ps1=PS1, music_root=None, timeout=600):
    """Run yt2mp3.ps1. Returns (ok, final_path_or_None, message)."""
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1,
           "-Url", url,
           "-Artist", track["artist"],
           "-Title", track["title"],
           "-Album", track["album"] or "Singles",
           "-NoLookup", "-NonInteractive"]
    if track.get("track_number"):
        cmd += ["-Track", str(track["track_number"])]
    if track.get("year"):
        cmd += ["-Year", str(track["year"])]
    if track.get("cover_url"):
        cmd += ["-CoverUrl", track["cover_url"]]
    # Always pass the folders and tools explicitly, so yt2mp3.ps1 downloads to the
    # same staging folder that "Move to library" later reads from. Before, it used
    # its own built-in default and ignored the staging_root setting.
    import settings
    cmd += ["-MusicRoot", music_root or settings.get("staging_root"),
            "-TempRoot", settings.get("temp_root")]
    for flag, found in (("-YtDlp", settings.ytdlp()), ("-Ffmpeg", settings.ffmpeg())):
        if found:
            cmd += [flag, found]
    if settings.get("contact_email"):
        cmd += ["-ContactEmail", settings.get("contact_email")]

    # Get the final path via a UTF-8 file rather than stdout: the console encoding
    # mangles non-ASCII names, which made correct downloads of "VOILA", "BLU EYES"
    # and similar look like failures.
    fd, pathout = tempfile.mkstemp(suffix=".path", text=False)
    os.close(fd)
    cmd += ["-PathOut", pathout]
    try:
        p = procutil.run(cmd, capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        os.unlink(pathout)
        return False, None, f"timed out after {timeout}s"

    final = None
    try:
        with open(pathout, encoding="utf-8") as fh:
            cand = fh.read().strip()
        if cand and os.path.exists(cand):
            final = cand
    except (OSError, UnicodeDecodeError):
        pass
    finally:
        try:
            os.unlink(pathout)
        except OSError:
            pass
    if not final:  # fall back to stdout for an un-patched script
        for line in reversed((p.stdout or "").strip().splitlines()):
            line = line.strip()
            if line.lower().endswith(".mp3") and os.path.exists(line):
                final = line
                break
    if p.returncode != 0:
        err = (p.stderr or "").strip().splitlines()
        return False, final, (err[-1] if err else f"exit {p.returncode}")[:300]
    if not final:
        return False, None, "script finished but no mp3 path found"
    return True, final, "ok"


def run(con, limit=None, music_root=None, dry=False, verbose=True):
    rows = con.execute("""SELECT track_id, artist, title, album, duration_ms,
                                 track_number, year, cover_url
                          FROM spotify_tracks WHERE status='pending'
                          ORDER BY added_at DESC""").fetchall()
    cols = ["track_id", "artist", "title", "album", "duration_ms",
            "track_number", "year", "cover_url"]
    tracks = [dict(zip(cols, r)) for r in rows]
    if limit:
        tracks = tracks[:limit]
    stats = {"downloaded": 0, "needs_review": 0, "failed": 0}

    for i, t in enumerate(tracks, 1):
        label = f"{t['artist']} - {t['title']}"
        if verbose:
            print(f"\n[{i}/{len(tracks)}] {label}")
        best, cands, reason = ytpick.pick(t["artist"], t["title"], t["duration_ms"])

        if not best:
            note = json.dumps({"reason": reason,
                               "candidates": [{"url": c["url"], "title": c["title"],
                                               "dur": c["duration"],
                                               "score": c.get("score")}
                                              for c in cands[:5]]})
            con.execute("""UPDATE spotify_tracks SET status='needs_review', note=?,
                           updated_at=? WHERE track_id=?""", (note, _now(), t["track_id"]))
            con.commit()
            stats["needs_review"] += 1
            if verbose:
                print(f"    REVIEW: {reason}")
            continue

        if verbose:
            print(f"    -> [{best['score']}] {best['duration']}s "
                  f"{best['channel']} | {best['title'][:50]}")
        if dry:
            continue

        ok, final, msg = invoke(t, best["url"], music_root=music_root)
        if ok:
            con.execute("""UPDATE spotify_tracks SET status='downloaded', yt_url=?,
                           matched_path=?, note=NULL, updated_at=?
                           WHERE track_id=?""",
                        (best["url"], final, _now(), t["track_id"]))
            stats["downloaded"] += 1
            if verbose:
                print(f"    OK: {os.path.basename(final)}")
        else:
            con.execute("""UPDATE spotify_tracks SET status='failed', yt_url=?,
                           note=?, updated_at=? WHERE track_id=?""",
                        (best["url"], msg, _now(), t["track_id"]))
            stats["failed"] += 1
            if verbose:
                print(f"    FAILED: {msg}")
        con.commit()  # commit per track so an interrupted run stays resumable
    return stats
