"""Download the latest yt-dlp.exe and ffmpeg.exe into this repo's tools folder.

    python tools_install.py yt-dlp      # or: ffmpeg, all
    python tools_install.py --check     # show what's installed and what's latest

Sources are fixed, official ones -- never taken from a web page:
  yt-dlp  the yt-dlp project's GitHub releases (checked against SHA2-256SUMS)
  ffmpeg  gyan.dev's "release essentials" build, the Windows build ffmpeg.org
          links to (checked against its .sha256)

Everything is verified against the published SHA-256 before it replaces
anything, and swapped in with an atomic rename, so a failed or tampered download
never leaves a broken tool behind.

It only ever writes to <repo>\\tools, which settings.tool() searches first.
Copies elsewhere -- such as C:\\common, which other programs may use from PATH --
are left alone.
"""
import hashlib, os, shutil, subprocess, sys, tempfile, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, "tools")

YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
YTDLP_SUMS = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/SHA2-256SUMS"
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
FFMPEG_SUM = FFMPEG_URL + ".sha256"
FFMPEG_VERSION = "https://www.gyan.dev/ffmpeg/builds/release-version"
USER_AGENT = "spotify-sync tools installer"


class InstallError(RuntimeError):
    pass


def _get(url, timeout=60, **kw):
    import requests
    r = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT},
                     allow_redirects=True, **kw)
    if r.status_code != 200:
        raise InstallError("{} returned HTTP {}".format(url, r.status_code))
    return r


def _download(url, dest, progress=None):
    """Stream url to dest, returning its SHA-256."""
    h = hashlib.sha256()
    with _get(url, timeout=120, stream=True) as r:
        total = int(r.headers.get("content-length") or 0)
        done = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    return h.hexdigest()


def _version(exe, flag):
    try:
        p = subprocess.run([exe, flag], capture_output=True, text=True, timeout=60,
                           encoding="utf-8", errors="replace",
                           creationflags=0x08000000)           # no console window
        return (p.stdout or p.stderr).strip().splitlines()[0]
    except Exception as e:
        return "error: " + str(e)


def _swap_in(tmp_file, final):
    """Replace final with tmp_file atomically; keep the old copy as .old."""
    os.makedirs(os.path.dirname(final), exist_ok=True)
    if os.path.exists(final):
        old = final + ".old"
        if os.path.exists(old):
            os.remove(old)
        os.replace(final, old)
    os.replace(tmp_file, final)


def install_ytdlp(progress=None):
    """Latest yt-dlp.exe -> tools\\yt-dlp.exe. Returns (path, version)."""
    sums = _get(YTDLP_SUMS).text
    expected = next((l.split()[0].lower() for l in sums.splitlines()
                     if l.strip().endswith(" yt-dlp.exe")), None)
    if not expected:
        raise InstallError("yt-dlp checksum list has no entry for yt-dlp.exe")
    os.makedirs(TOOLS, exist_ok=True)
    tmp = os.path.join(TOOLS, "yt-dlp.exe.download")
    try:
        got = _download(YTDLP_URL, tmp, progress)
        if got != expected:
            raise InstallError("yt-dlp.exe checksum mismatch - download discarded")
        version = _version(tmp, "--version")
        if version.startswith("error"):
            raise InstallError("downloaded yt-dlp.exe won't run: " + version)
        final = os.path.join(TOOLS, "yt-dlp.exe")
        _swap_in(tmp, final)
        return final, version
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def install_ffmpeg(progress=None):
    """Latest ffmpeg.exe and ffprobe.exe -> tools\\. Returns (path, version).

    ffprobe comes too: yt-dlp uses it to inspect audio before converting, and it
    looks for it next to ffmpeg.
    """
    expected = _get(FFMPEG_SUM).text.split()[0].lower()
    if len(expected) != 64:
        raise InstallError("ffmpeg checksum file isn't a SHA-256")
    os.makedirs(TOOLS, exist_ok=True)
    work = tempfile.mkdtemp(prefix="ffmpeg_", dir=TOOLS)
    try:
        zpath = os.path.join(work, "ffmpeg.zip")
        got = _download(FFMPEG_URL, zpath, progress)
        if got != expected:
            raise InstallError("ffmpeg zip checksum mismatch - download discarded")
        staged = {}
        with zipfile.ZipFile(zpath) as z:
            for name in ("ffmpeg.exe", "ffprobe.exe"):
                member = next((m for m in z.namelist()
                               if m.replace("\\", "/").endswith("/bin/" + name)), None)
                if not member:
                    raise InstallError(name + " not found in the ffmpeg zip")
                out = os.path.join(work, name)
                with z.open(member) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                staged[name] = out
        version = _version(staged["ffmpeg.exe"], "-version")
        if version.startswith("error"):
            raise InstallError("downloaded ffmpeg.exe won't run: " + version)
        for name, path in staged.items():
            _swap_in(path, os.path.join(TOOLS, name))
        return os.path.join(TOOLS, "ffmpeg.exe"), version
    finally:
        shutil.rmtree(work, ignore_errors=True)


def latest():
    """What each source currently offers, without downloading the tools."""
    out = {}
    try:
        r = _get(YTDLP_URL, stream=True)
        out["yt-dlp"] = r.url.split("/download/")[1].split("/")[0] if "/download/" in r.url \
            else "latest"
        r.close()
    except Exception as e:
        out["yt-dlp"] = "unavailable: " + str(e)[:60]
    try:
        out["ffmpeg"] = _get(FFMPEG_VERSION).text.strip()
    except Exception as e:
        out["ffmpeg"] = "unavailable: " + str(e)[:60]
    return out


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    what = sys.argv[1] if len(sys.argv) > 1 else "--check"
    if what == "--check":
        import settings
        for name, key, flag in (("yt-dlp.exe", "ytdlp_path", "--version"),
                                ("ffmpeg.exe", "ffmpeg_path", "-version")):
            p = settings.tool(name, key)
            print("{:<11} {}  {}".format(name, p or "NOT FOUND",
                                         _version(p, flag)[:60] if p else ""))
        print("latest:", latest())
        sys.exit(0)

    def bar(done, total):
        if total:
            print("\r  {:5.1f} / {:.1f} MB".format(done / 1048576, total / 1048576),
                  end="", flush=True)
    for tool, fn in (("yt-dlp", install_ytdlp), ("ffmpeg", install_ffmpeg)):
        if what in (tool, "all"):
            print("installing", tool)
            path, version = fn(bar)
            print("\n  ->", path, "|", version)
