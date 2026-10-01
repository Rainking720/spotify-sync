"""Pick the right YouTube video for a Spotify track.

Spotify gives exact duration, which is the strongest available signal: it rules
out live cuts, extended mixes, full-album rips and '1 hour loop' uploads that a
title match alone would happily accept.
"""
import os, re, shutil, subprocess, sys

import procutil

HERE = os.path.dirname(os.path.abspath(__file__))
SEP = "|||"

# A candidate outside this many seconds of Spotify's duration is never used.
HARD_TOLERANCE = 7
# Best candidate must reach this score to download unattended.
MIN_CONFIDENT = 3
# Bonus for YouTube's top result when searching the song's ISRC. That is almost
# always the auto-generated official audio -- the album recording itself -- which
# the channel checks below rarely recognise (YouTube now lists it under the
# artist's own channel name rather than "<Artist> - Topic"). Big enough to beat
# a music or lyric video of the same song (~9), not so big that it overrides the
# duration window or the title gate.
ISRC_BONUS = 4

# Words that signal a different recording. Only penalised when Spotify's own
# title does NOT contain them -- a liked track called "... - Live" should match
# a live video.
JUNK_WORDS = ["live", "remix", "reaction", "karaoke", "instrumental",
              "nightcore", "sped up", "slowed", "8d", "loop", "tutorial",
              "behind the scenes", "interview", "teaser", "trailer", "mashup",
              "full album", "greatest hits", "medley"]

# Usually fan re-uploads: fine audio, but prefer the official source.
SOFT_WORDS = ["lyrics", "lyric video", "audio only", "hq", "hd audio"]


def ytdlp_path():
    """yt-dlp.exe: the ytdlp_path setting, else this folder, its parent, PATH."""
    import settings
    found = settings.ytdlp()
    if found:
        return found
    raise FileNotFoundError("yt-dlp.exe not found -- set ytdlp_path in config.json")


def search(artist, title, n=5, timeout=90):
    """Return candidate dicts from a YouTube search. Raw Spotify strings in."""
    return _search(f"{artist} {title}".strip(), n, timeout)


def search_isrc(isrc, timeout=90):
    """YouTube's top result for an ISRC, flagged isrc_hit -- or [] when there is
    no ISRC, no result, or the search fails. Only the top result: below it,
    YouTube pads with unrelated videos."""
    if not isrc:
        return []
    try:
        hits = _search(isrc, 1, timeout)
    except Exception:
        return []          # extra candidates only; never the reason a search fails
    for c in hits:
        c["isrc_hit"] = True
    return hits


def candidates(artist, title, isrc=None, n=5):
    """The ISRC hit (if any) plus the usual artist/title search, without
    duplicates. The artist/title search always runs, so a song with no ISRC, or
    one YouTube doesn't know, is matched exactly as before."""
    hits = search_isrc(isrc)
    seen = {c["id"] for c in hits}
    return hits + [c for c in search(artist, title, n=n) if c["id"] not in seen]


def _search(query, n, timeout):
    fmt = SEP.join(["%(id)s", "%(title)s", "%(duration)s", "%(channel)s"])
    cmd = [ytdlp_path(), "--js-runtimes", "node", "--no-warnings", "--quiet",
           "--skip-download", "--flat-playlist", "--print", fmt, f"ytsearch{n}:{query}"]
    try:
        p = procutil.run(cmd, capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return []
    out = []
    for line in (p.stdout or "").splitlines():
        parts = line.split(SEP)
        if len(parts) < 4:
            continue
        vid, vtitle, dur, chan = parts[0], parts[1], parts[2], parts[3]
        try:
            dur = int(float(dur))
        except (ValueError, TypeError):
            continue
        out.append({"id": vid, "title": vtitle, "duration": dur, "channel": chan,
                    "url": f"https://www.youtube.com/watch?v={vid}"})
    return out


def probe(url, timeout=60):
    """Duration/title/channel for one URL, without downloading it.

    Used to check a pasted URL really is the selected song before its audio gets
    written out under that song's name.
    """
    fmt = SEP.join(["%(id)s", "%(title)s", "%(duration)s", "%(channel)s"])
    cmd = [ytdlp_path(), "--js-runtimes", "node", "--no-warnings", "--quiet",
           "--skip-download", "--print", fmt, url]
    try:
        p = procutil.run(cmd, capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    for line in (p.stdout or "").splitlines():
        parts = line.split(SEP)
        if len(parts) < 4:
            continue
        try:
            dur = int(float(parts[2]))
        except (ValueError, TypeError):
            continue
        return {"id": parts[0], "title": parts[1], "duration": dur,
                "channel": parts[3], "url": url}
    return None


def _toks(s):
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _flat(s):
    """Strip all separators so 'NewMedicineRock' contains 'newmedicine'."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def score(cand, artist, title, want_sec):
    """Higher is better. Returns (score, reasons)."""
    s, why = 0, []
    ct, chan = cand["title"].lower(), (cand["channel"] or "").lower()
    sp_title = (title or "").lower()

    delta = abs(cand["duration"] - want_sec)
    if delta <= 2:
        s += 3; why.append(f"duration exact ({delta}s)")
    elif delta <= 4:
        s += 2; why.append(f"duration close ({delta}s)")
    else:
        s += 1; why.append(f"duration ok ({delta}s)")

    # Title agreement is a gate, not a bonus: a right-length video from the right
    # channel is still the wrong song if the title doesn't match ("More Than Me"
    # scored 6 against "Made Me A Man" before this).
    # Spotify often carries a parenthetical subtitle that YouTube drops, e.g.
    # "Untitled (How Could This Happen to Me?)" vs the band's own "Untitled".
    # Match on the full title first; fall back to the core with a smaller bonus
    # so a real full match still outranks it and qualifiers keep their weight.
    stop = {"a", "the", "and", "of", "to", "in", "my", "me", "it", "is"}
    ct_toks = _toks(ct)

    def _ov(ref):
        r = _toks(ref) - stop
        if not r:
            r = _toks(ref)
        return (len(r & ct_toks) / len(r)) if r else 0.0

    full = _ov(title)
    core = _ov(re.sub(r"[\(\[].*?[\)\]]", " ", title or ""))
    if full >= 0.8:
        s += 3; why.append("title match")
    elif core >= 0.8:
        s += 1; why.append("core title match (subtitle dropped)")
    elif full >= 0.6:
        s += 1; why.append(f"partial title ({full:.0%})")
    else:
        s -= 5; why.append(f"TITLE MISMATCH ({full:.0%})")

    # Compare with separators removed so "NewMedicineRock" matches "New Medicine".
    a_flat = _flat(artist)
    chan_is_artist = bool(a_flat) and a_flat in _flat(chan)

    # "<Artist> - Topic" is YouTube's auto-generated official audio -- but only
    # when it is THIS artist's Topic channel. Without the ownership check,
    # "NVO - Topic" outscored Wither Away's own upload of the same song.
    if chan.endswith(" - topic"):
        if chan_is_artist:
            s += 3; why.append("Topic channel")
        elif cand.get("isrc_hit"):
            # A label's Topic channel ("Thriller Records - Topic") carrying the
            # official audio. The ISRC already proves the recording, so the
            # different-name penalty below -- meant for another artist's version
            # of the same song -- doesn't apply.
            why.append("label's Topic channel")
        else:
            s -= 4; why.append("Topic channel of DIFFERENT artist")
    elif "vevo" in chan and chan_is_artist:
        s += 2; why.append("VEVO")
    if chan_is_artist:
        s += 2; why.append("channel is artist")
    if a_flat and a_flat in _flat(ct):
        s += 1; why.append("artist in title")

    for w in JUNK_WORDS:
        if w in ct and w not in sp_title:
            s -= 3; why.append(f"penalty:{w}")
    # "cover" says whose song it is, not which recording. On the artist's own
    # channel it is just them labelling their own cover -- don't punish that.
    if "cover" in ct and "cover" not in sp_title and not chan_is_artist:
        s -= 3; why.append("penalty:cover")
    for w in SOFT_WORDS:
        if w in ct and w not in sp_title:
            s -= 1; why.append(f"soft:{w}")
    if cand.get("isrc_hit"):
        s += ISRC_BONUS; why.append("found by ISRC")
    return s, why


def pick(artist, title, duration_ms, n=5, isrc=None):
    """Return (best, candidates, reason). best is None when unsure."""
    want = round((duration_ms or 0) / 1000)
    cands = candidates(artist, title, isrc, n=n)
    if not cands:
        return None, [], "no search results"
    if not want:
        return None, cands, "no Spotify duration to verify against"

    viable = [c for c in cands if abs(c["duration"] - want) <= HARD_TOLERANCE]
    if not viable:
        closest = min(cands, key=lambda c: abs(c["duration"] - want))
        return None, cands, (f"no candidate within {HARD_TOLERANCE}s of {want}s "
                             f"(closest {closest['duration']}s)")
    for c in viable:
        c["score"], c["why"] = score(c, artist, title, want)
    viable.sort(key=lambda c: (-c["score"], abs(c["duration"] - want)))
    best = viable[0]
    if best["score"] < MIN_CONFIDENT:
        return None, viable, f"low confidence (score {best['score']})"
    return best, viable, "ok"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    a, t, ms = sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 0
    isrc = sys.argv[4] if len(sys.argv) > 4 else None
    best, cands, reason = pick(a, t, ms, isrc=isrc)
    print(f"query: {a} - {t}  (want {round(ms/1000)}s)\nreason: {reason}")
    for c in cands:
        mark = "->" if best and c["id"] == best["id"] else "  "
        mark += "*" if c.get("isrc_hit") else " "
        print(f" {mark} [{c.get('score','?'):>3}] {c['duration']:>4}s {c['channel'][:28]:28} "
              f"{c['title'][:52]}")
        if c.get("why"):
            print(f"        {', '.join(c['why'])}")
