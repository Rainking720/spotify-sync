"""Check that everything spotify-sync needs is installed and where it's expected.

    python check_setup.py            # full check
    python check_setup.py --no-itunes  # skip iTunes (it launches iTunes if closed)

Prints OK / MISSING / WARN per item and exits non-zero if anything required is
missing. None of these programs live in the repository -- see README "Setup".
"""
import argparse, importlib, json, os, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
problems = []


def line(state, what, detail=""):
    print("  {:<8} {:<30} {}".format(state, what, detail))
    if state == "MISSING":
        problems.append(what)


def run(cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                           encoding="utf-8", errors="replace")
        return (p.stdout or p.stderr or "").strip().splitlines()[0]
    except Exception as e:
        return "error: " + str(e)


def find_tool(name, key):
    """Resolve exactly as the code does (settings.tool), and say how it was found."""
    import settings
    p = settings.tool(name, key)
    if not p:
        return None, ""
    if settings.get(key):
        return p, "set by " + key
    d = os.path.dirname(os.path.normcase(p))
    if d == os.path.normcase(PARENT):
        return p, "found next to the repo"
    if d == os.path.normcase(HERE):
        return p, "found in the repo folder"
    return p, "found on PATH only"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-itunes", action="store_true")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("Python")
    v = sys.version_info
    line("OK" if v >= (3, 10) else "MISSING", "Python " + ".".join(map(str, v[:3])),
         sys.executable)
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    line("OK" if os.path.exists(pyw) else "MISSING", "pythonw.exe",
         "used by the scheduled tasks and the GUI shortcut")
    for mod, pkg in (("spotipy", "spotipy"), ("mutagen", "mutagen"),
                     ("win32com.client", "pywin32"), ("tkinter", None)):
        try:
            importlib.import_module(mod)
            ver = ""
            if pkg:
                import importlib.metadata as md
                ver = md.version(pkg)
            line("OK", pkg or mod, ver)
        except Exception:
            line("MISSING", pkg or mod,
                 "python -m pip install -r requirements.txt" if pkg else
                 "reinstall Python with Tcl/Tk")

    print("\nPrograms")
    yt, where = find_tool("yt-dlp.exe", "ytdlp_path")
    if yt:
        state = "WARN" if where == "found on PATH only" else "OK"
        line(state, "yt-dlp " + run([yt, "--version"]), yt + "  (" + where + ")")
        if state == "WARN":
            print("           yt2mp3.ps1 prefers a copy in " + PARENT +
                  "; a PATH copy may be older")
    else:
        line("MISSING", "yt-dlp.exe", "put it in " + PARENT + " or set ytdlp_path")
    ff, where = find_tool("ffmpeg.exe", "ffmpeg_path")
    line("OK" if ff else "MISSING", "ffmpeg",
         (ff + "  (" + where + ")") if ff else "put it in " + PARENT + " or set ffmpeg_path")
    node = shutil.which("node")
    line("OK" if node else "MISSING", "Node.js " + (run([node, "--version"]) if node else ""),
         (node or "") + "  (yt-dlp uses it for YouTube's scripts)")
    ps1 = os.path.join(HERE, "yt2mp3.ps1")
    line("OK" if os.path.exists(ps1) else "MISSING", "yt2mp3.ps1", ps1)

    print("\nConfiguration")
    cfg_path = os.path.join(HERE, "config.json")
    cfg = {}
    if os.path.exists(cfg_path):
        try:
            cfg = json.load(open(cfg_path, encoding="utf-8"))
        except ValueError:
            line("MISSING", "config.json", "not valid JSON")
    if cfg:
        missing = [k for k in ("client_id", "client_secret", "redirect_uri") if not cfg.get(k)]
        line("MISSING" if missing else "OK", "config.json",
             "missing: " + ", ".join(missing) if missing else "Spotify credentials present")
    elif not os.path.exists(cfg_path):
        line("MISSING", "config.json", "copy config.example.json and fill it in")
    line("OK" if os.path.exists(os.path.join(HERE, ".spotify_cache")) else "WARN",
         "Spotify sign-in", "saved" if os.path.exists(os.path.join(HERE, ".spotify_cache"))
         else "run: python spotify.py   (opens a browser once)")
    import settings
    for key, required in (("library_root", True), ("staging_root", False),
                          ("temp_root", False), ("plays_path", False),
                          ("history_dir", False)):
        p = settings.get(key)
        ok = os.path.exists(p)
        line("OK" if ok else ("MISSING" if required else "WARN"), key,
             p + "  [" + settings.source(key) + "]")
    line("OK", "itunes_playlist", settings.get("itunes_playlist") +
         "  [" + settings.source("itunes_playlist") + "]")
    line("OK" if settings.get("contact_email") else "WARN", "contact_email",
         (settings.get("contact_email") or "not set -- MusicBrainz asks for one")
         + "  [" + settings.source("contact_email") + "]")

    print("\niTunes")
    if args.no_itunes:
        line("WARN", "iTunes", "skipped (--no-itunes)")
    else:
        try:
            import pythoncom, win32com.client as w
            pythoncom.CoInitialize()
            app = w.Dispatch("iTunes.Application")
            line("OK", "iTunes " + str(app.Version),
                 "{} tracks".format(app.LibraryPlaylist.Tracks.Count))
        except Exception as e:
            line("MISSING", "iTunes", "COM not reachable: " + str(e)[:60])

    print("\nScheduled tasks")
    for name in ("Spotify Liked Sync", "Spotify Plays to iTunes", "SpotifyPlayTracker"):
        out = run(["powershell", "-NoProfile", "-Command",
                   "(Get-ScheduledTask -TaskName '{}' -ErrorAction SilentlyContinue).State"
                   .format(name)])
        line("OK" if out in ("Ready", "Running") else "WARN", name,
             out or "not registered (see README)")

    print("\n" + ("All required pieces are present." if not problems else
                  "Missing: " + ", ".join(problems)))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
