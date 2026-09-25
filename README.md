# spotify-sync

Finds Spotify Liked Songs that aren't already in `D:\Mp3`.

## Setup

The repository holds only the code. These programs aren't in it -- ffmpeg alone
is 214 MB, over GitHub's 100 MB file limit, and yt-dlp needs updating often
enough that a copy frozen in git would go stale -- so install them separately:

| Needs | Tested with | Where the code looks |
|---|---|---|
| Python + `pythonw.exe` | 3.13 | on PATH (the setup scripts find it there, or pass `-Python <path>`) |
| Python packages | spotipy 2.26, mutagen 1.47, pywin32 312 | `python -m pip install -r requirements.txt` |
| **yt-dlp.exe** | 2026.08.19 | `ytdlp_path` if set, else `tools\`, the repo folder, **its parent folder**, then PATH |
| **ffmpeg.exe** | 2026-04 gyan.dev full build | `ffmpeg_path` if set, else same as yt-dlp |
| Node.js | 24 | on PATH -- yt-dlp runs YouTube's player scripts with it |
| iTunes | 12.13 (Microsoft Store) | COM automation; must be installed |
| PowerShell | built into Windows | runs `yt2mp3.ps1` |

**Settings... > Download latest** (or `python tools_install.py yt-dlp|ffmpeg|all`)
fetches the current yt-dlp (GitHub releases, ~17 MB) or ffmpeg + ffprobe
(gyan.dev essentials build, ~110 MB), checks each against the SHA-256 its source
publishes, and installs it into `tools\` inside the repo (gitignored). The code
looks in `tools\` first, so the new copy is used from then on; copies elsewhere,
including any on PATH, are never touched. The previous copy is kept as `.old`,
and a failed or mismatched download leaves the installed one as it was.
`python tools_install.py --check` shows what's in use and what's latest.

Keep yt-dlp current: YouTube breaks old versions.
The parent-folder lookup exists because PATH on this machine resolves to an older
pip-installed yt-dlp.

Then:

1. Copy `config.example.json` to `config.json`. Fill in your Spotify app's
   client id and secret (redirect URI `http://127.0.0.1:8888/callback`), and set
   any of the settings below that differ on your machine.
2. `python spotify.py` once -- a browser opens to approve access; the token is
   saved to `.spotify_cache`.
3. `python check_setup.py` -- checks every item above and says what's missing.
4. Register the scheduled tasks: `register_task.ps1` (nightly sync) and
   `register_poll_task.ps1` (play counts every 2 hours); `make_shortcut.ps1`
   for the desktop shortcut. Each works from wherever the repo lives and finds
   `pythonw.exe` on PATH; add `-DryRun` to see what it would register first.
   Or open **Settings...** in the GUI: its *Scheduled tasks* section shows all
   three tasks (including SpotifyPoller's) and has a **Register** button for any
   that are missing.

### Settings

The easy way to change them is **Settings...** in the GUI toolbar: each box shows
the value in effect, with **Browse...** and **Default** buttons, and is checked as
you type (a missing music library or a mistyped yt-dlp path blocks saving unless
you confirm). Saving rewrites only these keys, keeps the Spotify credentials in
the file untouched, and leaves the previous file as `config.json.bak`. Changes
apply without restarting -- the scheduled jobs read the file on every run.

Everything machine-specific is a setting in `config.json`, read by `settings.py`
(the Python side) and by `yt2mp3.ps1` itself. Any key left out uses the default,
so a partial `config.json` is fine. `%USERPROFILE%`-style variables are expanded,
and forward or back slashes both work. `python settings.py` prints every setting
and whether it came from `config.json` or the default.

| Key | Default | What it is |
|---|---|---|
| `library_root` | `D:\Mp3` | your music library, `<Artist>\<Album>\<file>.mp3` |
| `staging_root` | `C:\temp\SpotifyDownloadOnTheSpot\Sorted` | where downloads land before **Move to library** |
| `temp_root` | `C:\temp\SpotifyDownloadOnTheSpot\Tracks` | scratch space for downloads in progress |
| `plays_path` | `%USERPROFILE%\SpotifyPoller\plays.jsonl` | the SpotifyPoller play log |
| `history_dir` | `%USERPROFILE%\Downloads\my_spotify_data\Spotify Extended Streaming History` | Spotify's streaming-history export |
| `ytdlp_path` | blank = `tools\`, repo folder, its parent, PATH | yt-dlp.exe |
| `ffmpeg_path` | blank = `tools\`, repo folder, its parent, PATH | ffmpeg.exe |
| `itunes_playlist` | `NewFromSpotify` | playlist **Move to library** adds to |
| `contact_email` | blank | sent to MusicBrainz with `yt2mp3.ps1` lookups; it asks for one |

The downloader passes `staging_root`, `temp_root` and the tool paths to
`yt2mp3.ps1` explicitly, so downloads always land where **Move to library** and
**Delete copy** look. They used to depend on the script's own built-in default.

`config.json`, `.spotify_cache`, every `*.db` and the logs are gitignored --
secrets and personal data stay on this machine.

## Usage

    python sync.py                          # classify only, no downloads
    python sync.py --download --limit 25    # fetch 25 missing tracks
    python sync.py --download --pick-only   # show chosen videos, download nothing
    python sync.py --full                   # re-check every liked song
    python sync.py --skip-fetch --skip-index --download --limit 10   # offline-ish batch

Writes `missing.csv` and records every track in `sync.db`. Commits after every
track, so an interrupted run resumes cleanly.

## Picking the right video

`yt-dlp ytsearch5:` with `--flat-playlist` (~2s/track). Spotify's exact duration
is the primary filter: anything more than `HARD_TOLERANCE` (7s) off is discarded
outright, which removes live cuts, extended mixes and "1 hour" uploads.

Survivors are scored -- duration closeness, `<Artist> - Topic` channel (YouTube's
official auto-generated audio), VEVO, channel/artist match with separators
stripped so `NewMedicineRock` matches `New Medicine`. **Title agreement is a gate,
not a bonus**: below 60% token overlap a candidate takes -5, because a
right-length video from the right channel can still be the wrong song.
**Channel bonuses require ownership.** A `- Topic` channel only earns +3 when it
is *this* artist's Topic channel; another artist's Topic channel takes -4. Without
that, "NVO - Topic" outscored Wither Away's own upload and downloaded a different
artist's recording of the same song.

**"cover" is only penalised off the artist's own channel**, since a band
labelling their own cover shouldn't be punished for being accurate.

Below `MIN_CONFIDENT` (3) nothing downloads -- it goes to `needs_review` with the
candidate URLs stored in `note`.

Spotify titles often carry a parenthetical subtitle YouTube omits ("Untitled (How
Could This Happen to Me?)" vs the band's own "Untitled"). A core-title match --
parentheticals stripped -- earns a smaller +1 so the official upload isn't
rejected, while a true full match still outranks it.

Note YouTube search results vary between runs, so re-picking an already-downloaded
track may return a different (equally valid) video, and a track that hit
`needs_review` may succeed on a later run. Don't treat either as a defect.

## reconcile.py

    python reconcile.py

Re-reads the output tree and promotes any `failed`/`pending` row whose file is
actually on disk. A download can succeed while the status write doesn't -- three
tracks were logged as failed purely because non-ASCII names ("VOILA", "BLU EYES")
came back mangled through the console encoding. `yt2mp3.ps1 -PathOut` now returns
the final path as UTF-8, and this repairs anything that still slips through.

Downloads call `yt2mp3.ps1` with Spotify's own metadata (`-Artist -Title -Album
-Track -Year -CoverUrl -NoLookup -NonInteractive`), since Spotify beats
MusicBrainz guessing from a YouTube title -- and its cover art is 640x640.

## Files

| File | Purpose | Rebuildable |
|---|---|---|
| `mp3_index.db` | `D:\Mp3` indexed by ID3 tags | yes, ~90s cold / ~17s warm |
| `sync.db` | per-track status + your decisions | **no** — backed up to `sync.db.bak` each run |
| `config.json` | Spotify credentials (gitignored) | — |

| Module | Does |
|---|---|
| `sync.py` | orchestrator: index, fetch likes, classify, download |
| `norm.py` | title/artist matching rules (**bump `NORM_VERSION` on any change**) |
| `index_mp3.py` | scans `D:\Mp3` into `mp3_index.db` |
| `spotify.py` | OAuth + Liked Songs paging |
| `ytpick.py` | YouTube search + candidate scoring |
| `download.py` | drives `yt2mp3.ps1`, records the outcome |
| `albums.py` | album tracklists and artist catalogues (manual use only) |
| `promote.py` | moves finished downloads into `D:\Mp3` |
| `itunes.py` | adds promoted files to iTunes + a playlist |
| `procutil.py` | runs children without console popups |
| `reconcile.py` | repairs statuses from what's on disk |
| `review.py` | command-line queue handling |
| `gui.py` | the desktop window |
| `run_sync.py` | nightly scheduled-task entry point |
| `history.py` | parses the Extended Streaming History export |
| `plays.py` | reads the SpotifyPoller play log |
| `itunes_sync.py` | play counts into iTunes and sync.db: plan, apply, undo |
| `poll_to_itunes.py` | 2-hourly scheduled-task entry point for play counts |
| `check_setup.py` | verifies the machine has everything in "Setup" |
| `yt2mp3.ps1` | downloads and tags one YouTube video (also usable by hand) |

## Matching

Only files with **both** an artist and title tag are indexed (~98.7%); filenames
in this library vary too much to parse. Normalization strips non-distinguishing
junk — `- Remastered 2011`, `(From The Vault)`, `feat. X`, case, punctuation,
`&` vs `and` — but keeps qualifiers that mean a different recording:
`(Acoustic)`, `(Live)`, `(Remix)`.

**If you change a rule in `norm.py`, bump `NORM_VERSION`.** The index only
re-reads files whose mtime changed, so without a bump most rows keep stale keys
and songs you own look missing. On a version bump the index re-keys all rows
from stored tags in ~17s, no disk read.

## Statuses in sync.db

`owned` · `pending` (missing, Phase 2 input) · `downloaded` · `needs_review`
· `skipped` · `failed`. The last four are sticky — a re-run never clobbers them.

## Scheduled runs

Registered as the task **"Spotify Liked Sync"**, daily at 03:00, running
`pythonw.exe run_sync.py` as you, only while logged on (so no stored password).
`StartWhenAvailable` catches up a run missed while the machine was off, so in
practice it fires shortly after your next logon rather than at 03:00.

    python run_sync.py          # same thing by hand
    logs\sync.log               # output, rotated at 5MB, 5 kept

Manage it from Task Scheduler, or:

    Get-ScheduledTaskInfo -TaskName 'Spotify Liked Sync'   # last run + result (0 = ok)
    Start-ScheduledTask     -TaskName 'Spotify Liked Sync' # run now
    Disable-ScheduledTask   -TaskName 'Spotify Liked Sync' # pause
    Unregister-ScheduledTask -TaskName 'Spotify Liked Sync'

`run_sync.py` must stay the entry point under `pythonw.exe`: there is no console,
so `sys.stdout` is `None` and any print before the log redirect would kill the run.
A lock file (`.sync.lock`, stale after 6h) stops runs overlapping.

To change the time, re-run `register_task.ps1` after editing the trigger, or edit
the task in Task Scheduler directly.

**Settings...** in the GUI lists the three tasks this setup relies on -- this one,
"Spotify Plays to iTunes" and SpotifyPoller's "SpotifyPlayTracker" -- with each
one's state, last run and result, and next run (`python tasks.py` prints the same).
A task that is missing gets a **Register** button. One that exists but runs a
different command from what its script would register now (say the folder
moved) is flagged in amber with **Re-register**. Both buttons ask first, then
run the task's own registration script, so the schedule is defined in one place.
SpotifyPoller is looked for in the folder holding the *SpotifyPoller play log*.
The two tasks from this repo are registered with the `pythonw.exe` of the Python
the GUI is running on (the one with this project's packages), not whichever
`pythonw.exe` comes first on PATH -- the Store's app alias or the Python install
manager can put others there.

## GUI

    python gui.py          (or double-click "Review Queue.cmd")

A tkinter window for everything still unresolved. Pick a track to see why it
stalled and the candidates that were stored when it was queued.

The **Downloaded** column shows when this tool fetched each song; click it
(like any header) to sort newest first. It's `downloaded_at` in `sync.db`, kept
by two SQLite triggers: stamped when a row becomes `downloaded` by any route
(nightly run, Retry, pasted URL, reconcile, Add song) or is re-downloaded from
a different URL, and left alone when **Move to library** changes its path. Rows
downloaded before the column existed were filled in from their file's modified
time; one whose file had gone stays blank and sorts last.

The **Show** dropdown picks which rows load (`Unresolved`, `Needs review`,
`Failed`, `Skipped`, `Downloaded`, `Unsorted`, `Owned`, `Everything`). The
**Find** box then narrows those as you type, matching artist, title or album.
Multiple words all have to match, though not as a phrase and not in the same
field, so `matchbox long` finds "Matchbox Twenty - Long Day". Matching is
substring rather than whole-word, so `matchb` works while you're still typing --
at the cost of the odd loose hit (`long day` also matches "Divided By **Frida**y",
since "Friday" contains "day"). Escape or **Clear** empties it. Filtering redraws
the rows already in memory, so there's no query per keystroke.

The list is multi-select (shift/ctrl-click). Controls are grouped by what they act
on: **Selected songs** under the list (Move to library, Delete copy) works on every
selected row; **This track** (Skip, Put back, Link to iTunes, Browse artist, Show
album) works on the one shown on the right; the candidate buttons sit inside the
**Candidates** box. Fixed-height panels are packed before the two lists, so a
small window shrinks the lists rather than hiding buttons.

* **Search terms** are pre-filled and editable -- fix a misspelled artist, drop a
  "- Acoustic" suffix, whatever helps -- then **Search YouTube** for fresh results.
  Results move on, so a re-search often finds videos the original run never saw.
* **Candidates** show score, length and the difference from Spotify's duration.
  Double-click to download, or **Open in browser** to check one first.
* **A specific YouTube video** box: paste a URL and click **Download for this
  track** to use a video the search can't find. It is saved under the selected
  track's name, so for a song that isn't in the list use **Add a new song by URL...**.
* **Skip** drops a track; **Put back** returns a skipped one to the queue.

Whatever is in the Artist/Title fields when you download is what gets written to
the tags, so corrections carry through to the file.

If nothing is within the duration tolerance, the candidate list falls back to
showing everything found -- check the Diff column before accepting one.

Searches and downloads run on worker threads, so the window stays responsive.
Worker threads never touch widgets; they hand results back through a queue.

## Pulling a whole album (manual only)

Nothing expands albums automatically. In the GUI, pick any track, click
**Show album...**, and you get its full tracklist with each track marked:

| State | Meaning |
|---|---|
| `new` | not known anywhere -- this is what a download would fetch |
| `owned` | already in D:\Mp3 (the matching file is shown) |
| `downloaded` | already pulled by this tool |
| `queued` / `skipped` | already has a row in sync.db |

Only `new` tracks are preselected. Ctrl-click to adjust, then **Download
selected**; you get a confirmation with the count, and an explicit warning if the
selection includes anything you already have. **Stop** halts after the current
track, and everything commits per track so a stopped run is resumable.

Album tracks are stored with `source='album'`, never `'liked'`. This matters: the
incremental Liked Songs fetch stops at the first page of already-known ids, so an
album track you later like would otherwise halt the scan early and hide other new
likes.

### Number words (normalizer v3)

Spotify writes "Matchbox Twenty"; this library has "Matchbox 20" across 134
tracks. Those normalised to different keys, so every Matchbox Twenty like looked
missing and was re-downloaded. Number words now fold to digits, which also covers
"3 Doors Down" / "Three Doors Down" and similar. Bumping `NORM_VERSION` to 3
re-keyed both databases automatically.

This found 7 already-downloaded Matchbox Twenty tracks that exist in D:\Mp3 --
though 4 of those are only there as *live* recordings, so the studio versions may
be worth keeping. Album context is not part of the match key, so that call is
yours.

## No console popups

Under `pythonw.exe` the parent has no console, so every console child gets its own
window -- a twelve-track album produced a stream of PowerShell, yt-dlp and ffmpeg
popups. `procutil.run()` wraps `subprocess.run` with `CREATE_NO_WINDOW` plus a
hidden `STARTUPINFO`; grandchildren inherit the hidden console, so suppressing the
PowerShell call also covers yt-dlp and ffmpeg beneath it.

**Any new subprocess call in this project should go through `procutil.run`**, not
`subprocess.run` directly, or the popups come back. This affects the 3am scheduled
task too, which runs under `pythonw` for the same reason.

## Browsing an artist's catalogue

**Browse artist...** in the GUI. Type any artist (pre-filled from the selected
track, but you can type anything - no selection needed), pick from the matches,
and you get their whole catalogue: albums, singles, compilations, filterable by
release group. Double-click a release to open it in the album view, where the
usual owned/downloaded/new marking applies.

This is the answer to "the tag says Singles but it must be on a real album
somewhere" - browse the artist and look.

Releases are de-duplicated by (name, type): Spotify lists the same album once per
market, so the raw feed repeats heavily.

### Spotify's limit=10 cap

`/search` and `/artists/{id}/albums` reject `limit` above **10** with
"Invalid limit", while `/me/tracks` and `/albums/{id}/tracks` still accept 50.
Both boundaries were probed directly. `albums.PAGE_LIMIT` exists for this - use it
for those two endpoints, and don't "optimise" it upward.

### Coverage

Only what Spotify knows. Obscure and unreleased artists may be missing entirely
(MusicBrainz didn't have them either, in at least one case). For those, paste a
YouTube URL in the review queue instead.

## Moving finished downloads into the library

Downloads land in the staging tree (`yt2mp3.ps1`'s output folder), not in
`D:\Mp3`. The **Unsorted** filter lists downloaded tracks whose file is still
outside the library root. Select any number of them and click **Move to library**.

Promotion relocates each file *relative to the staging root* rather than
rebuilding `<Artist>\<Album>` from the tags. The staging tree already uses the
library's layout, so this reuses the sanitising `yt2mp3.ps1` already did instead
of applying a second, subtly different one. Files from anywhere else fall back to
rebuilding the path from the tags.

You get a confirmation with a breakdown first. Each file is one of:

| State | Meaning |
|---|---|
| `ok` | will be moved |
| `already sorted` | already under the library root |
| `already in library` | a file of that name is already at the destination |
| `file missing` | `sync.db` points at a file that isn't there |

**Nothing in the library is ever overwritten.** A name clash is reported and
skipped, and the staging copy is left alone, so neither side is lost. Empty
staging folders are pruned after a successful move, and `sync.db` paths are
updated as each file lands.

After moving, `mp3_index.db` is stale until it rescans — the next sync run (or
`python index_mp3.py`, ~16s) picks them up, after which they classify as `owned`.

### Case-only vs real name differences

Windows folds case-only folder differences together, so `NOTHING MORE` lands in
an existing `Nothing More\`. Genuinely different names do not: promoting
Matchbox Twenty creates `D:\Mp3\Matchbox Twenty\` beside the existing
`Matchbox 20\`, splitting that artist across two folders. Move those by hand if
it bothers you.

## iTunes

The **also add to iTunes "NewFromSpotify"** checkbox beside **Move to library** adds each
promoted file to the iTunes library and that playlist. Files move first; only the
ones that actually landed are added, so conflicts and skips never get added.

If iTunes is unreachable the move still succeeds and the failure is logged — an
iTunes problem must never undo a completed move.

Needs `pywin32`. The playlist is created if missing; a *smart* playlist of that
name is rejected, since `AddFile` can't add to one.

### Things that will catch you out

**The Store (UWP) build still supports COM.** It registers through *packaged COM*,
so `HKLM\SOFTWARE\Classes\iTunes.Application` does not exist even though
`Dispatch("iTunes.Application")` works. Don't read a missing registry key as proof
it's unavailable — test it.

**`Location` is on `IITFileOrCDTrack`, not `IITTrack`.** Collections hand back the
base interface, so reading `.Location` raises `AttributeError` and, if you swallow
it, silently looks like an empty path. `CastTo` first.

**Deleting a playlist track doesn't remove it from the library.** `track.Delete()`
on a playlist entry removes only that entry; the library row survives. Find the
library track to remove it properly.

**COM needs `CoInitialize` per thread**, which `itunes.init_thread()` does.
`uninit_thread()` is deliberately a no-op: calling `CoUninitialize()` tore down COM
for the whole calling thread even when this module didn't own it, breaking the
caller's next COM call.

**Copy-on-add** is off on this machine (verified: an added track's `Location`
pointed back at `D:\Mp3`), so there's one copy on disk. If "Copy files to iTunes
Media folder when adding to library" is ever turned on in Preferences > Advanced,
files get duplicated into the iTunes media folder — the code detects this by
comparing the reported location against the source and warns in the log.

## Discarding a copy you already own

Some downloads duplicate songs already in `D:\Mp3` -- usually because the matcher
couldn't connect the two names. Select them in the list and press **Delete** (or
the **Delete copy (already owned)** button). After a confirmation listing exactly
what will go, this:

* deletes the staging copy,
* prunes the album folder, then the artist folder, if the delete emptied them,
* marks the row `owned` and repoints `matched_path` at the library file when the
  index knows it.

**Files under the library root are never deleted by this.** If a selected track
has already been promoted to `D:\Mp3`, it is reported and left completely alone --
the row isn't even marked, since nothing was discarded. Only files inside the
staging tree can be removed.

### Why manual ownership has to be sticky

These are precisely the tracks the matcher *cannot* find -- that's why they were
downloaded. Marking one `owned` would therefore be undone by the next sync, which
would see no library match, set it back to `pending`, and download it all over
again.

So `discard()` writes `note = 'confirmed owned manually'`, and `classify()` treats
an `owned` row carrying that note as sticky, in both the Python branch and the
`ON CONFLICT` clause. An ordinary `owned` row (no such note) still floats: if its
library file disappears it correctly returns to `pending`.

If you ever want such a track downloaded again, clear its note or set the status
back to `pending`.

### Why Delete needs care in the UI

Three separate things made a second Delete press look like it did nothing:

* `reload()` rebuilds the list and **drops the selection**, so the next press had
  nothing to act on. The row position is now restored afterwards (like a file
  manager) and keyboard focus goes back to the list, so you can keep pressing it.
* The key was bound **on the tree**, so once the confirm dialog took focus away
  the binding stopped firing at all. It's bound on the window now, with
  `_on_delete_key` ignoring the press when a text widget has focus -- otherwise
  Delete while typing in **Find** would delete files.
* `if self.busy: return` was a **silent** no-op. It logs now.

The guard reads `event.widget` rather than `focus_get()`, which returns `None`
when the window isn't mapped or focused and would let the press through.

## Play counts

A separate backend at `C:\Users\MurphyG\SpotifyPoller` polls Spotify's
recently-played endpoint every 2 hours (scheduled task `SpotifyPlayTracker`) and
appends each play to `plays.jsonl`. Spotify only exposes the last ~50 plays at any
moment, so that log is the only lasting history. **This project only reads it** --
the poller is left alone. Its `HANDOFF.md` documents the backend.

`plays.py` aggregates the log exactly as that folder's `report.js` does: group by
`track_id`, count plays, keep the newest `played_at`, falling back to
`name::artists` when `track_id` is missing. Results are cached and re-read only
when the file's mtime changes.

    python plays.py        # console summary, like report.js

The list gains **Plays** and **Last played** columns on every filter, and a
**Played** filter showing only tracks with plays, most-played first (most recent
first within a count). Path override: `plays_path` in `config.json`.

### Joining plays to sync.db

Two passes, and the order matters:

1. **Exact `track_id`.** Each play row that gets used is marked as claimed.
2. **Normalised artist+title**, for rows the first pass missed -- the same song
   carries a different `track_id` when played from another release. A play
   already claimed is **never** counted again.

Without that claim check, a song liked twice (once on its album, once as a
single) showed the single play on *both* rows. `unmatched()` reads the same
claimed set rather than re-deriving the match, because GUI rows carry no
`n_artist`/`n_title` and a fallback-matched track otherwise counted on both sides
of the total. Attributed + unattributed now always equals the number of plays
logged -- worth asserting if this is ever changed.

### Played but not liked

Currently 17 of 55 played tracks have no `sync.db` row at all: you played them
without liking them, so they appear nowhere in the list, which is driven by
`sync.db`. `plays.unmatched(items)` returns them. Surfacing them in the UI would
mean synthesising rows with no track actions behind them, so it hasn't been done.

## Spotify play counts -> iTunes

**Play counts -> iTunes...** in the GUI toolbar. Two sources feed one pipeline:

* **history** -- the one-time Extended Streaming History export (`history.py`).
* **poller** -- the ongoing 2-hourly recently-played log (`plays.py`).

Nothing is written until **Apply**. The preview is sortable by plays, last
played, resulting count or artist.

### A play means 60 seconds

`history.MIN_MS` is 60000. Of 18,785 exported entries only 10,008 qualify:
27.6% are under 5 seconds and 41.6% under 30. Counting every row would have
added 18,785 plays and filled the most-played rankings with skips.

### The watermark, and why it matters

The export runs to 2026-09-20; the poller started 2026-09-19. **50 of the
poller's first 58 plays are already in the export.** Applying the history import
stores its last timestamp as `history_watermark` in sync.db's `meta` table, and
the poller source then only ever considers plays after it -- verified as 58 plays
dropping to 8. Clearing that key would double-count.

### Where each play lands

| Bucket | Meaning | Count |
|---|---|---|
| iTunes | matched a library track | 1,071 |
| database | no iTunes track, but a sync.db row | 139 |
| neither | streamed, never liked, not owned | 1,295 |

The third bucket is only recorded if you tick **also record songs that are in
neither**. Those rows get status **`logged`** -- deliberately *not* `pending`,
which is `download.py`'s work queue and would have started 1,295 downloads.
They keep Spotify's real track id, so liking one later updates that row rather
than creating a second.

Where one song matches several iTunes tracks (259 cases: album, compilation,
live), the copy with the **highest existing play count** takes the plays; ties
break on most recently played, then library order.

### Never crediting the same history twice

`history_applied` stores, per song, how many plays the export has already been
credited for. The planner subtracts that from the export's total and only applies
the difference, so:

* re-running the import shows those songs as **already credited** and does nothing;
* a **newer export** later credits only its new plays, rather than everything again.

This matters because the planner reads iTunes' *current* count and adds to it --
without the ledger, a second run would add all 10,008 plays over again. The
watermark does not help here: it only stops the *poller* re-applying plays the
export already covered.

It's a running total rather than a flag, which is what makes a fresh export work
incrementally. A song credited 89 plays that shows 95 in a newer export applies 6.
For the same reason the iTunes branch adds the **delta**, not the export total --
adding the total would be right only on a first run.

**`credited_to` records where the plays went, and that matters.** Songs staged in
sync.db (not yet in iTunes) are credited as `syncdb`. That is a holding step, not
a destination: if the ledger treated it as done, importing the song into iTunes
later could never receive those plays, which defeats the point of staging them.
So a song credited only to the database is treated as **owing iTunes its full
total** the moment it appears there, and is re-credited as `itunes` once applied.

Verified: stage 45 plays in the database -> rebuild lists nothing -> the song
appears in iTunes -> all 45 are offered and applied -> rebuild lists nothing.

**Undo un-credits it too.** `history_applied_undo` holds the pre-run ledger state
and the previous watermark, so undoing a run restores counts, dates, the ledger
*and* the watermark. Without that the songs would stay marked as credited, the
poller would keep suppressing plays that were never applied, and a rebuild would
show nothing to do.

Verified end to end: apply 3 tracks -> rebuild shows 0 of them and
`already_applied=3` -> undo restores counts and dates exactly, empties the ledger,
clears the watermark, and they become actionable again.

### Undo, and the timezone trap

Every write records the track's previous `PlayedCount` and `PlayedDate` in
`itunes_undo`, keyed by run id. **Undo a run...** restores them. iTunes has no
undo of its own for play counts.

**iTunes reports `PlayedDate` as local wall-clock time but pywin32 labels it
`+00:00`.** Treating that as real UTC and converting shifts it by the whole
offset -- the first undo silently moved every restored date back four hours. So:

* a Spotify timestamp (genuinely UTC) is **converted** to local before writing;
* a value that came from iTunes is written back with its tz **stripped, never
  converted**.

That's what `_for_itunes(iso, from_utc)` encodes. Verified by an exact
apply-then-undo round trip on two tracks.

### The iTunes snapshot

`itunes_cache.db` holds the library (~29,138 tracks, ~142s to read at 2.5ms each)
so previews are instant. The snapshot is built into a temp file and swapped in
atomically -- writing in place meant a plan built *during* a re-read saw an empty
library and marked every track unmatched, which is a silently wrong preview rather
than an error. Re-read it with **Re-read iTunes** if you've played
anything in iTunes since. Apply re-checks each track's `TrackDatabaseID` against
the cache and skips any that moved, rather than writing to the wrong track.

## Linking a song to its iTunes track by hand

Some songs are in iTunes under a name too different to match automatically --
an iTunes-side typo ("high drive heart" for "High Dive Heart"), or a reworded
title. Select the track and click **Link to iTunes...**: it searches the iTunes
snapshot by artist or title, and you pick the right track. If the database's
artist finds nothing (the usual sign of a misspelt iTunes artist), it falls back
to searching by title automatically.

Links live in sync.db's `itunes_link` table, keyed on the normalised
artist+title and pointing at the iTunes track's persistent `TrackDatabaseID`, so
they survive re-reading the library. `load_snapshot()` folds them in and an
explicit link beats any name match, which means history and poller play counts
follow the link: linking High Dive Heart moved its 2 history plays from the
database bucket to iTunes.

**Remove link** undoes it. Links affect iTunes play-count matching only; they
don't move files or change how `D:\Mp3` ownership is detected. Like the ledger,
they key on the normalised name, so a `norm.py` rule change can orphan one.

## Poller plays reach iTunes exactly once

`poll_to_itunes.py` (every 2 hours; `--dry-run` to preview) applies each logged
play once and never again. Each play has a key -- `played_at|track_id` -- and
`poller_applied` records it when applied. What enforces "never twice":

| Risk | Guard |
|---|---|
| re-running on the same log | applied keys are skipped when the plan is built |
| an old preview, or a second click | every row re-checks at apply time which of its plays are still owed |
| two runs at once (GUI + scheduled job) | an OS file lock on `.itunes_apply.lock`; Windows releases it if a run dies |
| a crash mid-run | the "applied" record is committed **before** the iTunes track changes, so a crash can lose one track's play but never double it |
| iTunes refusing a write | the record is taken back, so those plays stay owed |
| an undo that can't reach a track | only plays for tracks actually restored are released; the rest stay applied and the undo can be retried |
| a duplicated log line | counted once |
| a newer history export later | counted only up to the existing watermark -- after it, the poller owns the plays |

Verified by `test_once.py`-style checks against a sandbox database, including a
subprocess killed between the record and the iTunes write.

Two things outside the code's control: restoring an **old copy of sync.db**
rolls back the applied-play records, so those plays would be applied again; and
the key depends on Spotify's `played_at` for a play staying the same (it has, in
every line logged so far).

### The 2-hourly task

Registered as **"Spotify Plays to iTunes"** (`register_poll_task.ps1`), running
`pythonw.exe poll_to_itunes.py` every 2 hours at :03 on odd hours -- 15 minutes
after SpotifyPlayTracker's :48 on even hours, so each run picks up the plays
that poll just logged. Same shape as the poller task: runs as you while logged on,
catches up missed runs, never overlaps itself. Output goes to
`logs\poll_itunes.log`; each run re-reads iTunes first (~3-4 minutes).

    Get-ScheduledTaskInfo -TaskName 'Spotify Plays to iTunes'
    Disable-ScheduledTask -TaskName 'Spotify Plays to iTunes'   # pause
