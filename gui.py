"""Desktop queue manager for tracks that failed or need review.

    python gui.py

Shows every unresolved track, lets you re-search (optionally with a corrected
artist/title), pick from the candidates, paste a URL yourself, or skip.
Searches and downloads run on worker threads so the window stays responsive.
"""
import json, os, queue, sqlite3, sys, threading, webbrowser
import tkinter as tk
from tkinter import ttk, messagebox

import download, plays, ytpick

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "sync.db")
import settings
def itunes_playlist():
    """Read on use, so a change made in Settings applies without a restart."""
    return settings.get("itunes_playlist")
COLS = ["track_id", "artist", "title", "album", "duration_ms",
        "track_number", "year", "cover_url"]
FILTERS = {
    "Unresolved": ("needs_review", "failed"),
    "Needs review": ("needs_review",),
    "Failed": ("failed",),
    "Skipped": ("skipped",),
    "All open": ("needs_review", "failed", "skipped"),
    "Downloaded": ("downloaded",),
    "Unsorted": ("downloaded",),      # post-filtered to files still in staging
    "Played": ("needs_review", "failed", "skipped", "downloaded", "owned"),
    "Owned": ("owned",),
    "Everything": ("needs_review", "failed", "skipped", "downloaded", "owned"),
}


def db():
    return sqlite3.connect(DB, timeout=15)


def fetch(statuses):
    qs = ",".join("?" * len(statuses))
    con = db()
    extra = ["status", "note", "matched_path"]
    rows = con.execute(
        "SELECT " + ",".join(COLS + extra) + " FROM spotify_tracks "
        "WHERE status IN (" + qs + ") ORDER BY artist,title", statuses).fetchall()
    con.close()
    return [dict(zip(COLS + extra, r)) for r in rows]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Spotify Sync - Review Queue")
        # Big enough for the full song list beside the track panel, but never
        # larger than the screen.
        w = min(1400, self.winfo_screenwidth() - 60)
        h = min(860, self.winfo_screenheight() - 80)
        self.geometry("{}x{}".format(w, h))
        self.minsize(min(1080, w), min(620, h))   # never above the screen size
        self.items, self.cands, self.busy = [], [], False
        # The selection the user actually made, kept separately from the tree so
        # it survives redraws and rows being hidden by the Find filter.
        self._sel_ids = set()
        # (column, descending) chosen by clicking a header; None = natural order.
        # Kept across filter changes and refreshes.
        self._sort = None
        self._sort_changed = False
        self.msgq = queue.Queue()
        self._build()
        self.refresh_labels()
        self.reload()
        self.after(100, self._drain)

    # ---------- layout ----------
    def _build(self):
        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(fill="x")
        ttk.Label(bar, text="Show:").pack(side="left")
        self.filter = tk.StringVar(value="Unresolved")
        cb = ttk.Combobox(bar, textvariable=self.filter, values=list(FILTERS),
                          state="readonly", width=14)
        cb.pack(side="left", padx=(4, 10))
        cb.bind("<<ComboboxSelected>>", lambda e: self.reload())
        ttk.Button(bar, text="Refresh", command=self.reload).pack(side="left")

        ttk.Label(bar, text="Find:").pack(side="left", padx=(14, 0))
        self.q = tk.StringVar()
        qe = ttk.Entry(bar, textvariable=self.q, width=28)
        qe.pack(side="left", padx=4)
        qe.bind("<Escape>", lambda e: self.q.set(""))
        ttk.Button(bar, text="Clear", command=lambda: self.q.set("")).pack(side="left")
        # Filters the loaded rows in memory; no database round-trip per keystroke.
        self.q.trace_add("write", lambda *a: self.render())

        ttk.Button(bar, text="Settings...",
                   command=self.open_settings).pack(side="right", padx=(0, 12))
        ttk.Button(bar, text="Play counts -> iTunes...",
                   command=self.open_catchup).pack(side="left", padx=(14, 0))

        self.count = ttk.Label(bar, text="")
        self.count.pack(side="right")

        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=8, pady=(0, 6))

        # ---------------- left: the song list, and actions on its selection
        left = ttk.Frame(pane)
        lf = ttk.Frame(left)
        lf.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            lf, columns=("status", "where", "artist", "title", "plays", "last"),
            show="headings", selectmode="extended")
        self._head_text = {}
        # Short fixed-width columns don't stretch, so Plays and Last played stay
        # visible; Artist and Title take whatever width is left.
        for c, w, t, grow in (("status", 80, "Status", False),
                              ("where", 64, "File", False),
                              ("artist", 130, "Artist", True),
                              ("title", 170, "Title", True),
                              ("plays", 46, "Plays", False),
                              ("last", 108, "Last played", False)):
            self._head_text[c] = t
            self.tree.heading(c, text=t, command=lambda c=c: self.sort_by(c))
            self.tree.column(c, width=w, minwidth=40 if not grow else 80,
                             stretch=grow, anchor="w")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(lf, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=sb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        lf.rowconfigure(0, weight=1)
        lf.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        # Snapshot only on real user input: <<TreeviewSelect>> also fires when a
        # redraw re-applies the selection, which would make it forget itself.
        self.tree.bind("<ButtonRelease-1>", self._remember_selection)
        self.tree.bind("<KeyRelease>", self._remember_selection)

        # These act on every song selected in the list, so they sit under it.
        sel = ttk.LabelFrame(left, text="Selected songs  (Ctrl/Shift-click for several)",
                             padding=6)
        sel.pack(fill="x", side="bottom", pady=(6, 0), before=lf)
        ttk.Button(sel, text="Move to library",
                   command=self.move_to_library).grid(row=0, column=0, sticky="w")
        self.to_itunes = tk.BooleanVar(value=True)
        self.itunes_label = tk.StringVar()
        ttk.Checkbutton(sel, textvariable=self.itunes_label,
                        variable=self.to_itunes).grid(row=0, column=1, sticky="w",
                                                      padx=(8, 0))
        ttk.Button(sel, text="Delete copy (already owned)",
                   command=self.discard_copies).grid(row=1, column=0, sticky="w",
                                                     pady=(4, 0))
        ttk.Label(sel, text="the Delete key does the same", foreground="#666666"
                  ).grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(4, 0))
        pane.add(left, weight=1)

        # ---------------- right: the track in focus
        right = ttk.Frame(pane)
        pane.add(right, weight=1)

        self.detail = ttk.Label(right, text="Select a track", justify="left",
                                anchor="w", font=("Segoe UI", 10, "bold"))
        self.detail.pack(fill="x", pady=(0, 2))
        self.why = ttk.Label(right, text="", justify="left", anchor="w",
                             foreground="#aa3333", wraplength=640)
        self.why.pack(fill="x", pady=(0, 6))

        tf = ttk.LabelFrame(right, text="This track", padding=6)
        tf.pack(fill="x")
        for col, (text, cmd) in enumerate((
                ("Skip", self.do_skip), ("Put back", self.do_requeue),
                ("Link to iTunes...", self.link_itunes),
                ("Browse artist...", self.browse_artist),
                ("Show album...", self.show_album))):
            ttk.Button(tf, text=text, command=cmd).grid(
                row=0, column=col, padx=(0 if col == 0 else 4, 0), sticky="w")

        box = ttk.LabelFrame(
            right, text="Search terms (correct these if the search looks wrong)",
            padding=6)
        box.pack(fill="x", pady=(6, 0))
        self.f_artist, self.f_title = tk.StringVar(), tk.StringVar()
        ttk.Label(box, text="Artist").grid(row=0, column=0, sticky="w")
        ttk.Entry(box, textvariable=self.f_artist, width=24).grid(
            row=0, column=1, padx=4, sticky="we")
        ttk.Label(box, text="Title").grid(row=0, column=2, sticky="w")
        ttk.Entry(box, textvariable=self.f_title, width=30).grid(
            row=0, column=3, padx=4, sticky="we")
        self.b_search = ttk.Button(box, text="Search YouTube", command=self.do_search)
        self.b_search.grid(row=0, column=4, padx=(4, 0))
        box.columnconfigure(1, weight=1)
        box.columnconfigure(3, weight=2)
        self.ignore_dur = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="show candidates outside the duration filter",
                        variable=self.ignore_dur).grid(row=1, column=1, columnspan=3,
                                                       sticky="w", pady=(4, 0))

        cf = ttk.LabelFrame(right, text="Candidates", padding=6)
        cf.pack(fill="both", expand=True, pady=6)
        ctf = ttk.Frame(cf)
        ctf.pack(fill="both", expand=True)
        self.ctree = ttk.Treeview(
            ctf, columns=("score", "dur", "delta", "channel", "vtitle"),
            show="headings", selectmode="browse")
        for c, w, t, grow in (("score", 50, "Score", False), ("dur", 54, "Len", False),
                              ("delta", 54, "Diff", False),
                              ("channel", 150, "Channel", True),
                              ("vtitle", 300, "Video title", True)):
            self.ctree.heading(c, text=t)
            self.ctree.column(c, width=w, minwidth=40, stretch=grow, anchor="w")
        cs = ttk.Scrollbar(ctf, orient="vertical", command=self.ctree.yview)
        self.ctree.configure(yscrollcommand=cs.set)
        self.ctree.pack(side="left", fill="both", expand=True)
        cs.pack(side="right", fill="y")
        self.ctree.bind("<Double-1>", lambda e: self.download_selected())
        cb2 = ttk.Frame(cf)
        cb2.pack(fill="x", side="bottom", pady=(6, 0), before=ctf)
        ttk.Button(cb2, text="Download selected",
                   command=self.download_selected).pack(side="left")
        ttk.Button(cb2, text="Open in browser",
                   command=self.open_selected).pack(side="left", padx=4)
        ttk.Label(cb2, text="or double-click a candidate", foreground="#666666"
                  ).pack(side="left", padx=(8, 0))

        uf = ttk.LabelFrame(right, text="A specific YouTube video", padding=6)
        uf.pack(fill="x", side="bottom", before=cf)
        self.f_url = tk.StringVar()
        ttk.Label(uf, text="URL").grid(row=0, column=0, sticky="w")
        ttk.Entry(uf, textvariable=self.f_url).grid(row=0, column=1, sticky="we",
                                                    padx=4)
        # "for this track": it is saved under the selected track's name, which is
        # what mislabelled two Forest Blakk songs when this read "Download this URL".
        ttk.Button(uf, text="Download for this track",
                   command=self.download_url).grid(row=0, column=2)
        ttk.Label(uf, text="Not in the list at all?", foreground="#666666"
                  ).grid(row=1, column=1, sticky="e", padx=4, pady=(4, 0))
        ttk.Button(uf, text="Add a new song by URL...",
                   command=self.add_song_by_url).grid(row=1, column=2, sticky="we",
                                                       pady=(4, 0))
        uf.columnconfigure(1, weight=1)

        # Start with the list wide enough to show every column.
        self.after(60, lambda: pane.sashpos(0, 640))

        self.logbox = tk.Text(self, height=6, wrap="word", state="disabled",
                              font=("Consolas", 9))
        # Reserved before the panes: otherwise a short window pushes the log off.
        self.logbox.pack(fill="x", side="bottom", padx=8, pady=(0, 8), before=pane)

        # Bound on the window, not the list: after the confirm dialog closes,
        # focus no longer sits on the tree, so a tree-only binding silently
        # stopped working after the first delete.
        self.bind("<Delete>", self._on_delete_key)

    # ---------- helpers ----------
    def log(self, msg):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", str(msg) + "\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def _drain(self):
        """Worker threads never touch widgets; they queue calls for this loop."""
        while True:
            try:
                fn, args = self.msgq.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except Exception as e:
                self.log("ui error: " + str(e))
        self.after(100, self._drain)

    def post(self, fn, *args):
        self.msgq.put((fn, args))

    def set_busy(self, on, what=""):
        self.busy = on
        self.b_search.configure(state="disabled" if on else "normal")
        self.configure(cursor="watch" if on else "")
        if what:
            self.log(what)

    def current(self):
        """First selected track: the single-track actions operate on this."""
        sel = self.tree.selection()
        if not sel:
            return None
        return self.items[int(sel[0])]

    def selected(self):
        """Every selected track, in the order shown."""
        return [self.items[int(i)] for i in self.tree.selection()]

    def want_secs(self, t):
        return round((t["duration_ms"] or 0) / 1000)

    # ---------- data ----------
    def summary(self):
        con = db()
        rows = dict(con.execute(
            "SELECT status, COUNT(*) FROM spotify_tracks GROUP BY status"))
        con.close()
        return rows

    def _selected_ids(self):
        """Track ids of the current selection, safe against stale row ids."""
        out = set()
        for i in self.tree.selection():
            try:
                out.add(self.items[int(i)]["track_id"])
            except (IndexError, ValueError, KeyError, TypeError):
                pass
        return out

    def _remember_selection(self, _event=None):
        """Record what the user picked, so a redraw can put it back."""
        self._sel_ids = self._selected_ids()

    # Numbers and dates read most usefully biggest/newest first.
    DESC_FIRST = {"plays", "last"}

    def sort_by(self, col):
        """Header click: sort by that column, or reverse it if already sorted."""
        if self._sort and self._sort[0] == col:
            self._sort = (col, not self._sort[1])
        else:
            self._sort = (col, col in self.DESC_FIRST)
        for c, text in self._head_text.items():
            arrow = ""
            if self._sort[0] == c:
                arrow = " ▼" if self._sort[1] else " ▲"
            self.tree.heading(c, text=text + arrow)
        self._sort_changed = True
        self.render()

    def _sort_key(self, col, t, where):
        if col == "plays":
            return t.get("play_count") or 0
        if col == "last":
            return t.get("last_played") or ""
        if col == "where":
            return where
        return (t.get(col) or "").lower()

    def _ordered(self, lib, stage):
        """(index, track, where) in display order. Indexes are untouched, so
        row ids still point back into self.items."""
        rows = [(i, t, self._where(t, lib, stage)) for i, t in enumerate(self.items)]
        if not self._sort:
            return rows
        col, desc = self._sort
        blank = lambda r: self._sort_key(col, r[1], r[2]) in ("", 0)
        # Blanks (never played, no file) go last in either direction rather than
        # filling the top of the list on a descending sort.
        filled = [r for r in rows if not blank(r)]
        empty = [r for r in rows if blank(r)]
        filled.sort(key=lambda r: (self._sort_key(col, r[1], r[2]),
                                   (r[1].get("artist") or "").lower(),
                                   (r[1].get("title") or "").lower()),
                    reverse=desc)
        empty.sort(key=lambda r: ((r[1].get("artist") or "").lower(),
                                  (r[1].get("title") or "").lower()))
        return filled + empty

    @staticmethod
    def _where(t, lib, stage):
        """Where this track's file actually is.

        'downloaded' is provenance -- this tool fetched it -- and stays set after
        the file is filed into the library. Without this column, moving a track
        looks like it did nothing, because the row stays in the Downloaded list.
        """
        import promote
        p = t.get("matched_path")
        if not p:
            return ""
        if promote.under(p, lib):
            return "library"
        if promote.under(p, stage):
            return "staging"
        return "elsewhere"

    def reload(self):
        """Re-read from the database, then draw."""
        keep = self._selected_ids() or set(self._sel_ids)
        name = self.filter.get()
        self.items = fetch(FILTERS[name])
        if name == "Unsorted":
            # "Downloaded" rows whose file is still outside the library root.
            import promote
            lib, _ = promote.roots()
            self.items = [t for t in self.items if t.get("matched_path")
                          and not promote.under(t["matched_path"], lib)]

        # Play counts come from the SpotifyPoller log, joined on track_id with a
        # normalised artist+title fallback (the same song can carry a different
        # track_id when it was played from another release).
        self.play_data = plays.load()
        plays.annotate(self.items, self.play_data)
        if name == "Played":
            self.items = [t for t in self.items if t.get("play_count")]
            # Most played first; within a count, most recently played first.
            self.items.sort(key=lambda t: t.get("last_played") or "", reverse=True)
            self.items.sort(key=lambda t: -t["play_count"])
        self.render(keep)

    def render(self, keep=None):
        """Draw self.items through the Find box. Row ids stay indexes into
        self.items, so current() resolves correctly however it is filtered.

        Redrawing empties the tree, which drops the selection -- so selected
        track ids are carried across and re-applied. Without this, every
        keystroke in Find and every refresh silently cleared a multi-selection.
        """
        if keep is None:
            # Prefer what's visibly selected; fall back to the remembered set so
            # a selection hidden by the filter comes back when it's cleared.
            keep = self._selected_ids() or set(self._sel_ids)
        # Emptying the tree resets the scroll to the top, so where you were
        # reading is lost on every redraw. Put it back afterwards.
        try:
            first_visible = self.tree.yview()[0]
        except Exception:
            first_visible = 0.0
        tokens = self.q.get().strip().lower().split()
        import promote
        lib, stage = promote.roots()
        self.tree.delete(*self.tree.get_children())
        shown, restore = 0, []
        if self._sort_changed:
            # A new sort order makes the old scroll offset meaningless.
            first_visible, self._sort_changed = 0.0, False
        for i, t, where in self._ordered(lib, stage):
            hay = " ".join((t.get("artist") or "", t.get("title") or "",
                            t.get("album") or "")).lower()
            if tokens and not all(tok in hay for tok in tokens):
                continue
            self.tree.insert("", "end", iid=str(i), values=(
                t["status"], where, t["artist"], t["title"],
                t.get("play_count") or "",
                plays.local_time(t.get("last_played"))))
            if t.get("track_id") in keep:
                restore.append(str(i))
            shown += 1
        if restore:
            self.tree.selection_set(restore)
        # Restore the reading position rather than jumping to the selection:
        # see() dragged the view to the first selected row on every redraw.
        if first_visible:
            try:
                self.tree.yview_moveto(first_visible)
            except Exception:
                pass
        self.count.configure(
            text="{} of {} track(s)".format(shown, len(self.items))
            if tokens else "{} track(s)".format(len(self.items)))
        self.ctree.delete(*self.ctree.get_children())
        self.cands = []
        if shown:
            self.detail.configure(text="Select a track")
            self.why.configure(text="")
        elif self.items:
            self.detail.configure(text='No match for "' + self.q.get().strip() + '"')
            self.why.configure(
                foreground="#555555",
                text="{} track(s) in this view. Clear the Find box, or press "
                     "Escape in it, to see them all.".format(len(self.items)))
        else:
            # An empty list with no explanation reads like a broken window.
            s = self.summary()
            total = sum(s.values())
            parts = ", ".join("{} {}".format(v, k) for k, v in
                              sorted(s.items(), key=lambda kv: -kv[1]))
            self.detail.configure(
                text='Nothing to handle in "{}".'.format(self.filter.get()))
            self.why.configure(
                foreground="#2a7a2a",
                text="{} liked songs tracked: {}.\n"
                     "If you expected something here, try another filter above."
                     .format(total, parts))

    def on_select(self, _evt=None):
        t = self.current()
        if not t:
            return
        want = self.want_secs(t)
        self.detail.configure(
            text="{} - {}\nalbum: {}   |   Spotify length: {}s   |   status: {}".format(
                t["artist"], t["title"], t["album"], want, t["status"]))
        try:
            d = json.loads(t["note"] or "{}")
        except (ValueError, TypeError):
            d = {}
        self.why.configure(text=d.get("reason") or (t["note"] or ""),
                           foreground="#aa3333")  # reset: the empty state turns it green
        # Re-selection after a redraw fires this again for the same track; don't
        # clobber search terms or a pasted URL the user has edited in the meantime.
        if getattr(self, "_shown_id", None) == t.get("track_id"):
            return
        self._shown_id = t.get("track_id")
        self.f_artist.set(t["artist"])
        self.f_title.set(t["title"])
        self.f_url.set("")
        # Candidates stored when the track was queued; re-search for fresh ones.
        self.cands = [{"url": c.get("url"), "title": c.get("title"),
                       "duration": c.get("dur") or 0, "channel": "",
                       "score": c.get("score")}
                      for c in (d.get("candidates") or [])]
        self._fill_cands(want)

    def _fill_cands(self, want):
        self.ctree.delete(*self.ctree.get_children())
        for i, c in enumerate(self.cands):
            delta = (c.get("duration") or 0) - want
            score = c.get("score")
            self.ctree.insert(
                "", "end", iid=str(i),
                values=("-" if score is None else score,
                        str(c.get("duration")) + "s",
                        "{:+d}s".format(delta),
                        (c.get("channel") or "")[:32],
                        (c.get("title") or "")[:80]))

    # ---------- actions ----------
    def do_search(self):
        t = self.current()
        if not t or self.busy:
            return
        artist = self.f_artist.get().strip()
        title = self.f_title.get().strip()
        if not artist and not title:
            messagebox.showinfo("Nothing to search", "Enter an artist or title.")
            return
        want = self.want_secs(t)
        loose = self.ignore_dur.get()
        self.set_busy(True, "searching: " + artist + " - " + title + " ...")

        def work():
            try:
                found = ytpick.search(artist, title, n=8)
                for c in found:
                    c["score"], c["why"] = ytpick.score(c, artist, title, want)
                if not loose:
                    tight = [c for c in found
                             if abs(c["duration"] - want) <= ytpick.HARD_TOLERANCE]
                    found = tight or found
                found.sort(key=lambda c: (-c["score"], abs(c["duration"] - want)))
                self.post(self._searched, found, want)
            except Exception as e:
                self.post(self._failed, "search failed: " + str(e))

        threading.Thread(target=work, daemon=True).start()

    def _searched(self, found, want):
        self.cands = found
        self._fill_cands(want)
        self.set_busy(False, "found " + str(len(found)) + " candidate(s)")

    def _failed(self, msg):
        self.set_busy(False, msg)

    def _selected_cand(self):
        sel = self.ctree.selection()
        if not sel:
            messagebox.showinfo("Pick one", "Select a candidate first.")
            return None
        return self.cands[int(sel[0])]

    def open_selected(self):
        c = self._selected_cand()
        if c and c.get("url"):
            webbrowser.open(c["url"])

    def download_selected(self):
        c = self._selected_cand()
        if c and c.get("url"):
            self._download(c["url"])

    def download_url(self):
        """Download a pasted URL against the selected track.

        The audio is tagged and filed under the *selected* track, so a URL for a
        different song silently produces a mislabelled file and overwrites that
        row. Check the video's length against the track before committing.
        """
        url = self.f_url.get().strip()
        if not url:
            messagebox.showinfo("No URL", "Paste a YouTube URL first.")
            return
        t = self.current()
        if not t:
            messagebox.showinfo(
                "No track selected",
                "A pasted URL is saved against the selected track, so pick the "
                "song it belongs to first.\n\nIf the song isn't in the list at "
                "all, use \"Add song by URL...\" instead.")
            return
        want = round((t.get("duration_ms") or 0) / 1000)
        info = ytpick.probe(url)
        if info and want:
            delta = abs(info["duration"] - want)
            if delta > ytpick.HARD_TOLERANCE:
                if not messagebox.askyesno("That looks like a different song", (
                        "The video is {}s, but \"{} - {}\" is {}s ({}s apart).\n\n"
                        "The download is tagged and filed as the selected track, "
                        "so if this URL is a different song it will be saved "
                        "under the wrong name.\n\nVideo: {}\n\nDownload anyway?"
                    ).format(info["duration"], t["artist"], t["title"], want,
                             delta, info["title"][:70])):
                    return
        self._download(url)

    def _download(self, url):
        t = self.current()
        if not t or self.busy:
            return
        # Whatever is in the fields wins: a correction belongs in the tags too.
        trk = dict(t)
        trk["artist"] = self.f_artist.get().strip() or t["artist"]
        trk["title"] = self.f_title.get().strip() or t["title"]
        self.set_busy(True, "downloading " + trk["artist"] + " - " + trk["title"] + " ...")

        def work():
            try:
                ok, final, msg = download.invoke(trk, url)
                if ok:
                    con = db()
                    con.execute(
                        "UPDATE spotify_tracks SET status='downloaded', yt_url=?, "
                        "matched_path=?, note='resolved in GUI', "
                        "updated_at=datetime('now') WHERE track_id=?",
                        (url, final, trk["track_id"]))
                    con.commit()
                    con.close()
                self.post(self._downloaded, ok, final, msg, trk)
            except Exception as e:
                self.post(self._failed, "download error: " + str(e))

        threading.Thread(target=work, daemon=True).start()

    def _downloaded(self, ok, final, msg, trk):
        self.set_busy(False)
        if not ok:
            self.log("FAILED: " + str(msg))
            messagebox.showerror("Download failed", str(msg))
            return
        self.log("OK: " + str(final))
        try:
            from mutagen.mp3 import MP3
            got = MP3(final).info.length
            want = (trk["duration_ms"] or 0) / 1000
            flag = "" if abs(got - want) <= 7 else "   <-- differs from Spotify"
            self.log("    {:.1f}s vs Spotify {:.1f}s{}".format(got, want, flag))
        except Exception:
            pass
        self.reload()

    def _set_status(self, status, label):
        t = self.current()
        if not t:
            return
        con = db()
        con.execute("UPDATE spotify_tracks SET status=?, updated_at=datetime('now') "
                    "WHERE track_id=?", (status, t["track_id"]))
        con.commit()
        con.close()
        self.log(label + ": " + t["artist"] + " - " + t["title"])
        self.reload()

    def do_skip(self):
        self._set_status("skipped", "skipped")

    def do_requeue(self):
        self._set_status("needs_review", "put back")

    def show_album(self):
        t = self.current()
        if not t or self.busy:
            return
        AlbumDialog(self, seed=t)

    def browse_artist(self):
        """Browse any artist's catalogue -- no selection needed, type a name."""
        t = self.current()
        ArtistDialog(self, (t or {}).get("artist", ""))

    def open_catchup(self):
        CatchUpDialog(self)

    def open_settings(self):
        SettingsDialog(self)

    def refresh_labels(self):
        """Re-read settings shown in the window's own labels."""
        self.itunes_label.set('also add to iTunes "' + itunes_playlist() + '"')

    def link_itunes(self):
        t = self.current()
        if not t:
            messagebox.showinfo("Nothing selected",
                                "Select the track to link first.")
            return
        LinkDialog(self, t)

    def add_song_by_url(self):
        """Download a song that has no row at all, creating one for it.

        Without this the only way to fetch an unlisted song was to select some
        other track and paste a URL, which filed the audio under that track's
        name and overwrote its row.
        """
        if self.busy:
            self.log("busy - wait for the current job to finish")
            return
        AddSongDialog(self)

    def move_to_library(self):
        """Move the selected downloads out of staging into the library."""
        import promote
        picked = self.selected()
        if not picked:
            messagebox.showinfo("Nothing selected",
                                "Select one or more tracks in the list on the left.")
            return
        if self.busy:
            return
        lib, stage = promote.roots()
        entries = promote.plan(picked, lib, stage)
        counts = promote.summarize(entries)
        movable = [e for e in entries
                   if e["state"] in (promote.OK, promote.OUTSIDE)]
        if not movable:
            messagebox.showinfo(
                "Nothing to move",
                "None of the {} selected track(s) can be moved.\n\n{}".format(
                    len(picked),
                    "\n".join("{}: {}".format(k, v) for k, v in counts.items())))
            return
        detail = "\n".join("{}: {}".format(k, v) for k, v in counts.items())
        if not messagebox.askyesno(
                "Move to library",
                "Move {} file(s) into {}?\n\n{}\n\nConflicts and missing files are "
                "skipped; nothing in the library is overwritten.".format(
                    len(movable), lib, detail)):
            return

        self.set_busy(True, "moving {} file(s) to {} ...".format(len(movable), lib))

        want_itunes = self.to_itunes.get()

        def work():
            con = db()
            try:
                moved, errors = promote.apply(entries, con)
            except Exception as e:
                con.close()
                self.post(self._failed, "move failed: " + str(e))
                return
            con.close()

            itunes_result = None
            if want_itunes and moved:
                # Only files that actually landed in the library.
                paths = [e["dst"] for e in entries if e.get("moved")
                         and e["dst"] and os.path.exists(e["dst"])]
                try:
                    import itunes
                    self.post(self._progress_msg,
                              "adding {} file(s) to iTunes...".format(len(paths)))
                    itunes_result = itunes.add_many(paths, itunes_playlist())
                except Exception as e:
                    # The move succeeded; an iTunes problem must not undo that.
                    itunes_result = (0, [("", str(e))], [])
            self.post(self._moved, moved, errors, counts, itunes_result)

        threading.Thread(target=work, daemon=True).start()

    # Text-entry classes where Delete means "delete a character", not "delete
    # the selected tracks". ttk.Combobox subclasses ttk.Entry, so it's covered.
    TEXT_CLASSES = ("TEntry", "Entry", "Text", "TCombobox", "Spinbox", "TSpinbox")

    def _on_delete_key(self, event=None):
        """Delete acts on the list, except while typing in a text field.

        Prefers event.widget: focus_get() returns None when the window isn't
        mapped or focused, which would let the guard fall through.
        """
        w = getattr(event, "widget", None) or self.focus_get()
        try:
            if w is not None and w.winfo_class() in self.TEXT_CLASSES:
                return
        except Exception:
            pass
        self.discard_copies()

    def discard_copies(self):
        """Throw away staging copies of songs already in the library."""
        import promote
        if self.busy:
            self.log("busy - wait for the current job to finish")
            return
        picked = self.selected()
        if not picked:
            messagebox.showinfo(
                "Nothing selected",
                "Select one or more tracks in the list on the left.\n\n"
                "(The selection is cleared after each delete, so pick the next "
                "one before pressing Delete again.)")
            return
        lib, stage = promote.roots()
        entries = promote.discard_plan(picked, lib, stage)
        deletable = [e for e in entries if e["state"] == promote.DELETE]
        protected = [e for e in entries if e["state"] == promote.PROTECTED]
        other = [e for e in entries
                 if e["state"] not in (promote.DELETE, promote.PROTECTED)]

        lines = ["Mark {} track(s) as owned.".format(len(entries)), ""]
        if deletable:
            lines.append("DELETE {} file(s) from the staging folder:".format(
                len(deletable)))
            for e in deletable[:8]:
                lines.append("   " + os.path.basename(e["src"]))
            if len(deletable) > 8:
                lines.append("   ...and {} more".format(len(deletable) - 8))
            lines.append("")
            lines.append("Empty album/artist folders are removed too.")
        if protected:
            lines.append("")
            lines.append("{} file(s) are already in {} and will NOT be "
                         "deleted.".format(len(protected), lib))
        if other:
            lines.append("")
            lines.append("{} have no staging file; they are only marked "
                         "owned.".format(len(other)))
        lines.append("")
        lines.append("This cannot be undone. Continue?")

        if not messagebox.askyesno("Delete downloaded copies", "\n".join(lines)):
            return

        # Remember where we were so the next row can be selected afterwards.
        sel = self.tree.selection()
        self._last_row = self.tree.index(sel[0]) if sel else 0

        self.set_busy(True, "deleting {} staging file(s)...".format(len(deletable)))
        keys = self.lib_keys()

        def work():
            con = db()
            try:
                deleted, marked, errors = promote.discard(entries, con, keys)
            except Exception as e:
                con.close()
                self.post(self._failed, "delete failed: " + str(e))
                return
            con.close()
            self.post(self._discarded, deleted, marked, errors)

        threading.Thread(target=work, daemon=True).start()

    def _discarded(self, deleted, marked, errors):
        self.set_busy(False)
        self.log("deleted {} staging file(s); marked {} owned".format(deleted, marked))
        for e, err in errors[:8]:
            self.log("  {}: {}".format(e["track"].get("title"), err))
        self.reload()
        self._reselect(getattr(self, "_last_row", 0))

    def _reselect(self, row):
        """Put the selection back after a reload, so Delete can be used again.

        reload() rebuilds the tree and drops the selection; without this the next
        Delete finds nothing selected and appears to do nothing.
        """
        rows = self.tree.get_children()
        if not rows:
            return
        row = max(0, min(row, len(rows) - 1))
        iid = rows[row]
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self.tree.see(iid)
        self.tree.focus_set()       # keyboard focus back on the list
        self._remember_selection()  # the old ids are gone; this row is current
        self.on_select()

    def _progress_msg(self, text):
        self.log(text)

    def _moved(self, moved, errors, counts, itunes_result=None):
        self.set_busy(False)
        self.log("moved {} file(s) into the library".format(moved))
        for k, v in counts.items():
            if k != "ok" and v:
                self.log("  {}: {}".format(k, v))
        for e, err in errors[:10]:
            self.log("  ERROR {}: {}".format(e["track"].get("title"), err))
        if itunes_result is not None:
            added, ierrors, copied = itunes_result
            self.log('  iTunes: added {} to "{}"'.format(added, itunes_playlist()))
            for p, err in ierrors[:6]:
                self.log("    iTunes ERROR {}: {}".format(os.path.basename(p or ""), err))
            if copied:
                self.log("    NOTE: iTunes copied {} file(s) into its own media "
                         "folder, so they now exist twice. Turn off "
                         '"Copy files to iTunes Media folder" in iTunes '
                         "Preferences > Advanced.".format(len(copied)))
        if moved:
            # Those files are in D:\Mp3 now; the cached library index is stale.
            self._lib = None
            self.log("  (re-run the sync, or Refresh, to re-index the library)")
        self.reload()

    def lib_keys(self):
        """Cached library index: ~28k rows, so load it at most once per session."""
        if getattr(self, "_lib", None) is None:
            import index_mp3
            self._lib = index_mp3.load_keys()
        return self._lib


class AlbumDialog(tk.Toplevel):
    """Explicitly pull the rest of an album. Nothing downloads without a click."""

    STATE_ORDER = {"new": 0, "queued": 1, "skipped": 2, "downloaded": 3, "owned": 4}

    def __init__(self, app, seed=None, album=None):
        """Open from a track we hold (seed), or from a browsed album object."""
        super().__init__(app)
        self.app, self.seed, self.album = app, seed or {}, album
        self.tracks, self.stop, self.busy = [], False, False
        name = (album or {}).get("name") or self.seed.get("album") or ""
        self.title("Album - " + name)
        self.geometry("880x560")
        self.transient(app)

        head = ttk.Frame(self, padding=(10, 8))
        head.pack(fill="x")
        self.head = ttk.Label(head, text="Loading album...", justify="left",
                              anchor="w", font=("Segoe UI", 10, "bold"))
        self.head.pack(fill="x")
        self.sub = ttk.Label(head, text="", justify="left", anchor="w")
        self.sub.pack(fill="x")

        mid = ttk.Frame(self, padding=(10, 0))
        mid.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(mid, columns=("n", "state", "title", "len", "detail"),
                                 show="headings", selectmode="extended")
        for c, w, t in (("n", 40, "#"), ("state", 92, "State"), ("title", 300, "Title"),
                        ("len", 56, "Len"), ("detail", 280, "Notes")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        foot = ttk.Frame(self, padding=(10, 8))
        foot.pack(fill="x")
        self.status = ttk.Label(foot, text="")
        self.status.pack(side="left")
        self.b_close = ttk.Button(foot, text="Close", command=self.destroy)
        self.b_close.pack(side="right")
        self.b_go = ttk.Button(foot, text="Download selected",
                               command=self.download, state="disabled")
        self.b_go.pack(side="right", padx=6)
        self.b_stop = ttk.Button(foot, text="Stop", command=self.request_stop,
                                 state="disabled")
        self.b_stop.pack(side="right")
        ttk.Button(foot, text="Select all new",
                   command=self.select_new).pack(side="left", padx=(16, 0))

        self.load()

    # ---------- loading ----------
    def load(self):
        self.status.configure(text="fetching tracklist...")

        def work():
            try:
                import albums, spotify
                sp = spotify.client()
                alb = self.album or albums.album_of(sp, self.seed["track_id"])
                tr = albums.album_tracks(sp, alb)
                con = db()
                albums.classify(con, tr, self.app.lib_keys())
                con.close()
                self.app.post(self._loaded, alb, tr)
            except Exception as e:
                self.app.post(self._err, "could not load album: " + str(e))

        threading.Thread(target=work, daemon=True).start()

    def _loaded(self, alb, tracks):
        import albums
        tracks.sort(key=lambda x: x.get("track_number") or 0)
        self.tracks = tracks
        who = self.seed.get("artist") or ", ".join(
            a["name"] for a in (alb.get("artists") or []) if a.get("name"))
        self.head.configure(text="{}  -  {}  ({})".format(
            who, alb.get("name", ""), (alb.get("release_date") or "")[:4]))
        s = albums.summarize(tracks)
        self.sub.configure(text="{} tracks:  {} new,  {} already owned,  "
                                "{} already downloaded,  {} queued/skipped".format(
            len(tracks), s.get("new", 0), s.get("owned", 0),
            s.get("downloaded", 0), s.get("queued", 0) + s.get("skipped", 0)))
        self.tree.delete(*self.tree.get_children())
        for i, t in enumerate(tracks):
            self.tree.insert("", "end", iid=str(i), values=(
                t.get("track_number") or "", t["state"], t["title"],
                str(round((t["duration_ms"] or 0) / 1000)) + "s", t.get("detail", "")))
        self.select_new()
        self.b_go.configure(state="normal")
        self.status.configure(text="")

    def _err(self, msg):
        self.status.configure(text=msg)
        self.app.log(msg)

    def select_new(self):
        new = [str(i) for i, t in enumerate(self.tracks) if t["state"] == "new"]
        self.tree.selection_set(new)
        if new:
            self.tree.see(new[0])

    def request_stop(self):
        self.stop = True
        self.status.configure(text="stopping after this track...")

    # ---------- downloading ----------
    def download(self):
        if self.busy:
            return
        sel = [self.tracks[int(i)] for i in self.tree.selection()]
        already = [t for t in sel if t["state"] in ("owned", "downloaded")]
        if not sel:
            messagebox.showinfo("Nothing selected", "Select at least one track.")
            return
        msg = "Download {} track(s) from this album?".format(len(sel))
        if already:
            msg += ("\n\n{} of them you already have; they will be downloaded "
                    "again as duplicates.".format(len(already)))
        if not messagebox.askyesno("Confirm", msg):
            return

        self.busy, self.stop = True, False
        self.b_go.configure(state="disabled")
        self.b_stop.configure(state="normal")
        self.b_close.configure(state="disabled")

        def work():
            import albums, download, ytpick
            con = db()
            albums.queue(con, sel)          # track them before fetching anything
            ok = fail = review = 0
            for n, t in enumerate(sel, 1):
                if self.stop:
                    self.app.post(self._progress,
                                  "stopped at {}/{}".format(n - 1, len(sel)))
                    break
                self.app.post(self._progress, "{}/{}  {}".format(n, len(sel), t["title"]))
                try:
                    best, cands, reason = ytpick.pick(
                        t["artist"], t["title"], t["duration_ms"])
                    if not best:
                        con.execute("UPDATE spotify_tracks SET status='needs_review',"
                                    "note=?,updated_at=datetime('now') WHERE track_id=?",
                                    (json.dumps({"reason": reason, "candidates": [
                                        {"url": c["url"], "title": c["title"],
                                         "dur": c["duration"], "score": c.get("score")}
                                        for c in cands[:5]]}), t["track_id"]))
                        con.commit()
                        review += 1
                        continue
                    good, final, msg2 = download.invoke(t, best["url"])
                    if good:
                        con.execute("UPDATE spotify_tracks SET status='downloaded',"
                                    "yt_url=?,matched_path=?,updated_at=datetime('now') "
                                    "WHERE track_id=?", (best["url"], final, t["track_id"]))
                        ok += 1
                        self.app.post(self.app.log, "OK: " + os.path.basename(final))
                    else:
                        con.execute("UPDATE spotify_tracks SET status='failed',note=?,"
                                    "updated_at=datetime('now') WHERE track_id=?",
                                    (msg2, t["track_id"]))
                        fail += 1
                        self.app.post(self.app.log, "FAILED: " + t["title"] + " - " + str(msg2))
                    con.commit()
                except Exception as e:
                    fail += 1
                    self.app.post(self.app.log, "ERROR: " + t["title"] + " - " + str(e))
            con.close()
            self.app.post(self._done, ok, review, fail)

        threading.Thread(target=work, daemon=True).start()

    def _progress(self, text):
        self.status.configure(text=text)

    def _done(self, ok, review, fail):
        self.busy = False
        self.b_go.configure(state="normal")
        self.b_stop.configure(state="disabled")
        self.b_close.configure(state="normal")
        self.status.configure(text="done: {} downloaded, {} need review, {} failed"
                              .format(ok, review, fail))
        self.app.log("album: {} downloaded, {} need review, {} failed"
                     .format(ok, review, fail))
        self.app._lib = None          # new files on disk; rebuild the cache lazily
        self.load()
        self.app.reload()


class LinkDialog(tk.Toplevel):
    """Point a database song at the iTunes track it really is.

    For names too different to match automatically -- an iTunes typo like
    "high drive heart" for "High Dive Heart", or a reworded title. The link is
    keyed on the normalised name, so play counts from history and the poller
    follow it too.
    """

    def __init__(self, app, track):
        super().__init__(app)
        from norm import norm_artist, norm_title
        self.app, self.track = app, track
        self.key = (norm_artist(track["artist"]), norm_title(track["title"]))
        self.results = []
        self.title("Link to iTunes")
        self.geometry("820x480")
        self.transient(app)

        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="{} - {}".format(track["artist"], track["title"]),
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.current_lbl = ttk.Label(top, text="", foreground="#555555")
        self.current_lbl.pack(anchor="w", pady=(2, 6))

        sf = ttk.Frame(top)
        sf.pack(fill="x")
        ttk.Label(sf, text="Search iTunes (artist or title):").pack(side="left")
        self.q = tk.StringVar(value=track["artist"])
        e = ttk.Entry(sf, textvariable=self.q, width=40)
        e.pack(side="left", padx=4)
        e.bind("<Return>", lambda ev: self.search())
        ttk.Button(sf, text="Search", command=self.search).pack(side="left")

        mid = ttk.Frame(self, padding=(10, 0))
        mid.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(mid, columns=("artist", "name", "album", "plays"),
                                 show="headings", selectmode="browse")
        for c, w, t in (("artist", 180, "Artist"), ("name", 260, "Title"),
                        ("album", 240, "Album"), ("plays", 56, "Plays")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda ev: self.link())

        foot = ttk.Frame(self, padding=(10, 8))
        foot.pack(fill="x")
        self.status = ttk.Label(foot, text="")
        self.status.pack(side="left")
        ttk.Button(foot, text="Close", command=self.destroy).pack(side="right")
        ttk.Button(foot, text="Link selected", command=self.link).pack(side="right", padx=6)
        self.b_clear = ttk.Button(foot, text="Remove link", command=self.unlink)
        self.b_clear.pack(side="right")

        self.show_current()
        self.search()

    def show_current(self):
        import itunes_sync as isync
        con = db()
        cur = isync.get_link(con, self.key)
        con.close()
        if cur:
            self.current_lbl.configure(
                text="Currently linked to: {} - {}".format(cur[1], cur[2]))
            self.b_clear.configure(state="normal")
        else:
            self.current_lbl.configure(
                text="Not linked -- matched by name only, if at all.")
            self.b_clear.configure(state="disabled")

    def search(self):
        import itunes_sync as isync
        n, _ = isync.snapshot_age()
        if not n:
            self.status.configure(
                text="No iTunes snapshot yet -- use Re-read iTunes in the "
                     "play-count window first.")
            return
        self.results = isync.search_library(self.q.get())
        fell_back = False
        if not self.results and self.q.get().strip() == self.track["artist"]:
            # The usual reason to be here is a misspelt iTunes artist, so the
            # artist finds nothing -- the title usually still does.
            self.results = isync.search_library(self.track["title"])
            fell_back = bool(self.results)
        self.tree.delete(*self.tree.get_children())
        for i, r in enumerate(self.results):
            self.tree.insert("", "end", iid=str(i), values=(
                r["artist"], r["name"], r["album"], r["played_count"] or ""))
        self.status.configure(text="{} match(es){}{}".format(
            len(self.results), " (first 40 shown)" if len(self.results) >= 40 else "",
            " -- nothing under that artist, so searched the title" if fell_back else ""))

    def link(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Pick one", "Select the iTunes track first.",
                                parent=self)
            return
        import itunes_sync as isync
        r = self.results[int(sel[0])]
        con = db()
        isync.set_link(con, self.key, r)
        con.close()
        self.app.log("linked {} - {}  ->  iTunes {} - {}".format(
            self.track["artist"], self.track["title"], r["artist"], r["name"]))
        self.show_current()
        self.status.configure(text="linked")

    def unlink(self):
        import itunes_sync as isync
        con = db()
        isync.clear_link(con, self.key)
        con.close()
        self.app.log("removed iTunes link for {} - {}".format(
            self.track["artist"], self.track["title"]))
        self.show_current()
        self.status.configure(text="link removed")


class SettingsDialog(tk.Toplevel):
    """Edit the machine-specific settings stored in config.json.

    The Spotify credentials in the same file are never shown or touched here.
    Values are checked as you type; problems that would break a run block saving
    unless you confirm.
    """

    # key: (label, browse kind, file types)
    FIELDS = [
        ("library_root", "Music library", "dir", None),
        ("staging_root", "Download folder", "dir", None),
        ("temp_root", "Temporary folder", "dir", None),
        ("plays_path", "SpotifyPoller play log", "file",
         [("Play log", "*.jsonl"), ("All files", "*.*")]),
        ("history_dir", "Streaming history export", "dir", None),
        ("ytdlp_path", "yt-dlp.exe", "file", [("yt-dlp", "yt-dlp*.exe"), ("Programs", "*.exe")]),
        ("ffmpeg_path", "ffmpeg.exe", "file", [("ffmpeg", "ffmpeg*.exe"), ("Programs", "*.exe")]),
        ("itunes_playlist", "iTunes playlist", None, None),
        ("contact_email", "Contact email", None, None),
    ]
    COLOURS = {"ok": "#2a7a2a", "warn": "#a86a00", "error": "#aa3333"}
    # key: (display name, approximate download size, source shown to the user)
    INSTALLABLE = {
        "ytdlp_path": ("yt-dlp.exe", "about 17 MB", "the yt-dlp project's GitHub releases"),
        "ffmpeg_path": ("ffmpeg.exe and ffprobe.exe", "about 110 MB",
                        "gyan.dev, the Windows build ffmpeg.org links to"),
    }

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Settings")
        self.transient(app)
        self.resizable(True, False)
        self.minsize(780, 0)
        self.vars, self.status, self.install_buttons = {}, {}, {}

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Paths and options for this computer. Leave a box as its "
                  "default unless something lives elsewhere on this machine.",
                  foreground="#555555").grid(row=0, column=0, columnspan=4, sticky="w",
                                             pady=(0, 10))
        row = 1
        for key, label, kind, types in self.FIELDS:
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8))
            var = tk.StringVar(value=settings.get(key))
            self.vars[key] = var
            ttk.Entry(body, textvariable=var, width=70).grid(row=row, column=1, sticky="we")
            if kind:
                ttk.Button(body, text="Browse...",
                           command=lambda k=key, kd=kind, ty=types: self.browse(k, kd, ty)
                           ).grid(row=row, column=2, padx=(6, 0))
            ttk.Button(body, text="Default", command=lambda k=key: self.reset(k)
                       ).grid(row=row, column=3, padx=(4, 0))
            if key in self.INSTALLABLE:
                b = ttk.Button(body, text="Download latest",
                               command=lambda k=key: self.install(k))
                b.grid(row=row, column=4, padx=(4, 0))
                self.install_buttons[key] = b
            st = ttk.Label(body, text="", justify="left", anchor="w")
            st.grid(row=row + 1, column=1, columnspan=3, sticky="w", pady=(0, 8))
            self.status[key] = st
            var.trace_add("write", lambda *a, k=key: self.check(k))
            self.check(key)
            row += 2
        body.columnconfigure(1, weight=1)

        # ---- scheduled tasks: status, and registration where missing or stale
        self.tasks_frame = ttk.LabelFrame(self, text="Scheduled tasks", padding=8)
        self.tasks_frame.pack(fill="x", padx=12, pady=(0, 10))
        self.tasks_frame.columnconfigure(1, weight=1)
        self.task_note = ttk.Label(self.tasks_frame, text="checking Task Scheduler...",
                                   foreground="#555555")
        self.task_note.grid(row=0, column=0, columnspan=2, sticky="w")
        self.b_tasks = ttk.Button(self.tasks_frame, text="Refresh",
                                  command=self.load_tasks)
        self.b_tasks.grid(row=0, column=2, sticky="e")
        self.task_rows = []
        self.load_tasks()

        foot = ttk.Frame(self, padding=(12, 0, 12, 12))
        foot.pack(fill="x")
        ttk.Label(foot, text="Spotify credentials live in config.json and aren't "
                  "edited here.", foreground="#555555").pack(side="left")
        ttk.Button(foot, text="Open folder",
                   command=lambda: os.startfile(settings.HERE)).pack(side="left", padx=6)
        ttk.Button(foot, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(foot, text="Save", command=self.save).pack(side="right", padx=6)
        self.grab_set()

    # ------------------------------------------------------------ tasks
    def load_tasks(self):
        import tasks
        self.b_tasks.configure(state="disabled")
        self.task_note.configure(text="checking Task Scheduler...")
        post = self.app.post

        def work():
            try:
                post(self._tasks_loaded, tasks.status_all(), None)
            except Exception as e:
                post(self._tasks_loaded, [], str(e))

        threading.Thread(target=work, daemon=True).start()

    def _tasks_loaded(self, rows, error):
        if not self.winfo_exists():
            return
        self.b_tasks.configure(state="normal")
        for w in self.task_rows:
            w.destroy()
        self.task_rows = []
        if error:
            self.task_note.configure(text="couldn't read Task Scheduler: " + error)
            return
        bad = sum(1 for r in rows if r["level"] != "ok")
        self.task_note.configure(
            text="All three are registered and up to date." if not bad else
            "{} of {} need attention.".format(bad, len(rows)))
        for i, r in enumerate(rows, start=1):
            name = ttk.Label(self.tasks_frame, text=r["name"], font=("Segoe UI", 9, "bold"))
            name.grid(row=i * 2 - 1, column=0, sticky="nw", pady=(6, 0), padx=(0, 12))
            what = ttk.Label(self.tasks_frame, text=r["what"], foreground="#666666")
            what.grid(row=i * 2, column=0, sticky="nw", padx=(0, 12))
            summary = r["summary"]
            if not r["script_found"]:
                summary += "\nregistration script not found: " + r["script"]
            st = ttk.Label(self.tasks_frame, text=summary, justify="left",
                           foreground=self.COLOURS[r["level"]], wraplength=560)
            st.grid(row=i * 2 - 1, column=1, rowspan=2, sticky="w", pady=(6, 0))
            self.task_rows += [name, what, st]
            if r["script_found"] and (not r["registered"] or not r["current"]):
                b = ttk.Button(self.tasks_frame,
                               text="Register" if not r["registered"] else "Re-register",
                               command=lambda row=r: self.register_task(row))
                b.grid(row=i * 2 - 1, column=2, rowspan=2, sticky="e", pady=(6, 0))
                self.task_rows.append(b)

    def register_task(self, row):
        import tasks
        verb = "Register" if not row["registered"] else "Re-register"
        exp = row.get("expected") or {}
        if not messagebox.askyesno(verb + " task", (
                "{} the scheduled task \"{}\"?\n\nIt {}, running as you while "
                "you're logged in, with no window.\n\nRuns: {} {}\nIn: {}{}").format(
                    verb, row["name"], row["what"], exp.get("execute", ""),
                    exp.get("arguments", ""), exp.get("working dir", ""),
                    "\n\nThis replaces the existing task of that name."
                    if row["registered"] else ""), parent=self):
            return
        self.b_tasks.configure(state="disabled")
        post = self.app.post

        def work():
            try:
                ok, msg = tasks.register(row)
            except Exception as e:
                ok, msg = False, str(e)
            post(self._task_registered, row, ok, msg)

        threading.Thread(target=work, daemon=True).start()

    def _task_registered(self, row, ok, msg):
        if not self.winfo_exists():
            return
        self.app.log(("registered " if ok else "couldn't register ") + row["name"] +
                     (": " + msg.splitlines()[-1] if msg else ""))
        if not ok:
            messagebox.showerror("Couldn't register", msg or "unknown error", parent=self)
        self.load_tasks()

    def describe(self, key):
        default = settings.DEFAULTS[key][0]
        what = settings.DEFAULTS[key][1]
        return what + ("  (default: " + default + ")" if default else "  (default: blank)")

    def check(self, key):
        value = self.vars[key].get().strip()
        level, msg = settings.validate(key, value)
        mark = {"ok": "OK", "warn": "Check", "error": "Problem"}[level]
        text = self.describe(key)
        if msg:
            text = mark + ": " + msg + "\n" + text
        self.status[key].configure(text=text, foreground=self.COLOURS[level]
                                   if msg else "#666666")
        return level, msg

    def browse(self, key, kind, types):
        from tkinter import filedialog
        current = self.vars[key].get().strip()
        start = current if os.path.isdir(current) else os.path.dirname(current)
        if kind == "dir":
            chosen = filedialog.askdirectory(parent=self, initialdir=start or None,
                                             title="Choose the " + key.replace("_", " "))
        else:
            chosen = filedialog.askopenfilename(parent=self, initialdir=start or None,
                                                filetypes=types)
        if chosen:
            self.vars[key].set(os.path.normpath(chosen))

    def install(self, key):
        """Download the latest copy into the repo's tools folder, in the background."""
        import tools_install
        name, size, source = self.INSTALLABLE[key]
        if not messagebox.askyesno("Download latest", (
                "Download {} ({}) from {} and install it into:\n\n  {}\n\n"
                "It is checked against the SHA-256 the source publishes before it "
                "replaces anything. Copies elsewhere (such as on your PATH) are not "
                "changed.\n\nDownload now?").format(name, size, source,
                                                    tools_install.TOOLS), parent=self):
            return
        btn = self.install_buttons[key]
        btn.configure(state="disabled")
        post = self.app.post

        def progress(done, total):
            if total:
                post(self._install_progress, key, done, total)

        def work():
            try:
                fn = (tools_install.install_ytdlp if key == "ytdlp_path"
                      else tools_install.install_ffmpeg)
                path, version = fn(progress)
                post(self._installed, key, path, version, None)
            except Exception as e:
                post(self._installed, key, None, None, str(e))

        threading.Thread(target=work, daemon=True).start()

    def _install_progress(self, key, done, total):
        if self.winfo_exists():
            self.status[key].configure(
                foreground="#555555",
                text="downloading {:.1f} / {:.1f} MB ...".format(done / 1048576,
                                                                  total / 1048576))

    def _installed(self, key, path, version, error):
        if not self.winfo_exists():
            return
        self.install_buttons[key].configure(state="normal")
        name = self.INSTALLABLE[key][0]
        if error:
            self.check(key)
            self.app.log("download of {} failed: {}".format(name, error))
            messagebox.showerror("Download failed", error, parent=self)
            return
        self.app.log("installed {}: {}  ({})".format(name, path, version))
        current = self.vars[key].get().strip()
        if current and os.path.normcase(os.path.normpath(current)) != os.path.normcase(path):
            # A path set by hand beats the tools folder, so offer to switch to it.
            if messagebox.askyesno("Use the new copy?", (
                    "{} is set to:\n  {}\n\nSwitch it to the copy just installed?\n  {}"
                    ).format(name, current, path), parent=self):
                self.vars[key].set("")      # blank = found automatically, tools first
        self.check(key)
        messagebox.showinfo("Installed", "{}\n{}\n\n{}".format(
            name, version, path), parent=self)

    def reset(self, key):
        default = settings.DEFAULTS[key][0]
        self.vars[key].set(os.path.normpath(os.path.expandvars(default))
                           if default and key in settings.PATH_KEYS else default)

    def save(self):
        problems = []
        for key, label, _k, _t in self.FIELDS:
            level, msg = self.check(key)
            if level == "error":
                problems.append("{}: {}".format(label, msg))
        if problems and not messagebox.askyesno(
                "Save with problems?",
                "These would stop parts of the program working:\n\n  " +
                "\n  ".join(problems) + "\n\nSave anyway?", parent=self):
            return
        try:
            changed = settings.save({k: v.get() for k, v in self.vars.items()})
        except Exception as e:
            messagebox.showerror("Couldn't save", str(e), parent=self)
            return
        if changed:
            self.app.log("settings saved: " + ", ".join(changed) +
                         "   (previous config.json kept as config.json.bak)")
            self.app.refresh_labels()
            self.app.reload()
        else:
            self.app.log("settings: nothing changed")
        self.destroy()


class AddSongDialog(tk.Toplevel):
    """Fetch a song that isn't in the list, giving it its own row."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.info = None
        self.title("Add a song by URL")
        self.geometry("620x270")
        self.transient(app)
        self.resizable(False, False)

        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="YouTube URL").grid(row=0, column=0, sticky="w")
        self.f_url = tk.StringVar()
        e = ttk.Entry(f, textvariable=self.f_url, width=54)
        e.grid(row=0, column=1, columnspan=2, sticky="we", padx=4, pady=2)
        e.bind("<Return>", lambda ev: self.check())
        ttk.Button(f, text="Check", command=self.check).grid(row=0, column=3, padx=4)

        ttk.Label(f, text="Artist").grid(row=1, column=0, sticky="w")
        self.f_artist = tk.StringVar()
        ttk.Entry(f, textvariable=self.f_artist, width=54).grid(
            row=1, column=1, columnspan=2, sticky="we", padx=4, pady=2)
        ttk.Label(f, text="Title").grid(row=2, column=0, sticky="w")
        self.f_title = tk.StringVar()
        ttk.Entry(f, textvariable=self.f_title, width=54).grid(
            row=2, column=1, columnspan=2, sticky="we", padx=4, pady=2)
        ttk.Label(f, text="Album").grid(row=3, column=0, sticky="w")
        self.f_album = tk.StringVar(value="Singles")
        ttk.Entry(f, textvariable=self.f_album, width=54).grid(
            row=3, column=1, columnspan=2, sticky="we", padx=4, pady=2)

        self.detail = ttk.Label(f, text="Paste a URL and press Check.",
                                justify="left", anchor="w", wraplength=560,
                                foreground="#555555")
        self.detail.grid(row=4, column=0, columnspan=4, sticky="we", pady=(10, 6))
        self.status = ttk.Label(f, text="", anchor="w")
        self.status.grid(row=5, column=0, columnspan=2, sticky="w")
        self.b_go = ttk.Button(f, text="Download", command=self.go, state="disabled")
        self.b_go.grid(row=5, column=2, sticky="e", padx=4)
        ttk.Button(f, text="Close", command=self.destroy).grid(row=5, column=3, sticky="e")
        f.columnconfigure(1, weight=1)

    def check(self):
        url = self.f_url.get().strip()
        if not url:
            return
        self.status.configure(text="checking...")

        def work():
            try:
                info = ytpick.probe(url)
                self.app.post(self._checked, info)
            except Exception as e:
                self.app.post(self._failed, str(e))
        threading.Thread(target=work, daemon=True).start()

    def _checked(self, info):
        self.status.configure(text="")
        if not info:
            self.detail.configure(text="Could not read that URL.", foreground="#aa3333")
            self.b_go.configure(state="disabled")
            return
        self.info = info
        self.detail.configure(
            foreground="#555555",
            text="{}\nchannel: {}   length: {}s".format(
                info["title"], info["channel"], info["duration"]))
        # Seed artist/title from "Artist - Title" when the uploader used it.
        if not self.f_artist.get() and not self.f_title.get():
            parts = info["title"].split(" - ", 1)
            if len(parts) == 2:
                self.f_artist.set(parts[0].strip())
                self.f_title.set(self._clean(parts[1]))
            else:
                self.f_title.set(self._clean(info["title"]))
            if info["channel"].lower().endswith(" - topic"):
                self.f_artist.set(info["channel"][:-8].strip())
        self.b_go.configure(state="normal")

    @staticmethod
    def _clean(title):
        """Drop uploader promo suffixes so the tag isn't
        'Love Somebody Again (Official Lyric Video)'."""
        import re
        pat = (r"\s*[\(\[]\s*(official\s+)?(music\s+|lyric\s+|lyrics\s+|audio\s*)?"
               r"(video|audio|visualizer|visualiser|lyrics?|hd|hq|4k)\s*[\)\]]")
        out = re.sub(pat, "", title, flags=re.I)
        return re.sub(r"\s+", " ", out).strip(" -")

    def _failed(self, msg):
        self.status.configure(text=msg)

    def go(self):
        artist = self.f_artist.get().strip()
        title = self.f_title.get().strip()
        url = self.f_url.get().strip()
        if not (artist and title and url):
            messagebox.showinfo("Missing details",
                                "Artist, title and URL are all needed.")
            return
        info = self.info or {}
        trk = {"track_id": "url:" + (info.get("id") or url)[-32:],
               "artist": artist, "all_artists": artist, "title": title,
               "album": self.f_album.get().strip() or "Singles",
               "duration_ms": (info.get("duration") or 0) * 1000,
               "track_number": 0, "year": "", "cover_url": "", "spotify_url": ""}
        self.b_go.configure(state="disabled")
        self.status.configure(text="downloading...")

        def work():
            try:
                ok, final, msg = download.invoke(trk, url)
                self.app.post(self._done, ok, final, msg, trk, url)
            except Exception as e:
                self.app.post(self._failed, "download error: " + str(e))
        threading.Thread(target=work, daemon=True).start()

    def _done(self, ok, final, msg, trk, url):
        self.b_go.configure(state="normal")
        if not ok:
            self.status.configure(text="failed")
            messagebox.showerror("Download failed", str(msg))
            return
        from norm import norm_artist, norm_title
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        con = db()
        con.execute(
            "INSERT OR REPLACE INTO spotify_tracks (track_id, added_at, artist, "
            "all_artists, title, album, duration_ms, track_number, year, "
            "cover_url, spotify_url, n_artist, n_title, status, source, yt_url, "
            "matched_path, first_seen, updated_at) VALUES "
            "(?,?,?,?,?,?,?,0,'','','',?,?,'downloaded','manual',?,?,?,?)",
            (trk["track_id"], now, trk["artist"], trk["artist"], trk["title"],
             trk["album"], trk["duration_ms"],
             norm_artist(trk["artist"]), norm_title(trk["title"]),
             url, final, now, now))
        con.commit()
        con.close()
        self.status.configure(text="done")
        self.app.log("added by URL: {} - {}".format(trk["artist"], trk["title"]))
        self.app.log("   " + str(final))
        self.app.reload()


class CatchUpDialog(tk.Toplevel):
    """Preview and apply Spotify play counts to iTunes and sync.db.

    Nothing is written until Apply is pressed, and every write is recorded so the
    run can be undone -- iTunes has no undo for play counts.
    """

    SORTS = {
        "Plays (most first)": lambda r: (-r["spotify_plays"], r["artist"].lower()),
        "Last played (newest first)": lambda r: (r["new_date"] or "", ""),
        "New count (highest first)": lambda r: (-r["new_count"], r["artist"].lower()),
        "Artist": lambda r: (r["artist"].lower(), r["name"].lower()),
    }
    REVERSED = {"Last played (newest first)"}

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.rows, self.summary, self.busy = [], {}, False
        self.title("Spotify play counts -> iTunes")
        self.geometry("1120x680")
        self.transient(app)

        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="Source:").pack(side="left")
        self.source = tk.StringVar(value="history")
        src = ttk.Combobox(top, textvariable=self.source, state="readonly", width=32,
                           values=["history", "poller"])
        src.pack(side="left", padx=4)
        src.bind("<<ComboboxSelected>>", lambda e: self.build())
        self.b_build = ttk.Button(top, text="Build preview", command=self.build)
        self.b_build.pack(side="left", padx=6)
        ttk.Label(top, text="Sort:").pack(side="left", padx=(14, 0))
        self.sort = tk.StringVar(value="Plays (most first)")
        so = ttk.Combobox(top, textvariable=self.sort, state="readonly", width=26,
                          values=list(self.SORTS))
        so.pack(side="left", padx=4)
        so.bind("<<ComboboxSelected>>", lambda e: self.fill())
        self.b_snap = ttk.Button(top, text="Re-read iTunes", command=self.resnapshot)
        self.b_snap.pack(side="right")

        self.head = ttk.Label(self, text="", justify="left", anchor="w",
                              padding=(10, 0))
        self.head.pack(fill="x")
        self.snap = ttk.Label(self, text="", justify="left", anchor="w",
                              foreground="#555555", padding=(10, 0))
        self.snap.pack(fill="x", pady=(0, 6))

        mid = ttk.Frame(self, padding=(10, 0))
        mid.pack(fill="both", expand=True)
        cols = ("target", "sp", "was", "becomes", "artist", "name", "last", "amb")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="browse")
        for c, w, t in (("target", 74, "Where"), ("sp", 46, "Spotify"),
                        ("was", 46, "Was"), ("becomes", 60, "Becomes"),
                        ("artist", 168, "Artist"), ("name", 250, "Track"),
                        ("last", 120, "New last played"), ("amb", 46, "Dup")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        foot = ttk.Frame(self, padding=(10, 8))
        foot.pack(fill="x")
        self.status = ttk.Label(foot, text="")
        self.status.pack(side="left")
        ttk.Button(foot, text="Close", command=self.destroy).pack(side="right")
        self.b_apply = ttk.Button(foot, text="Apply", command=self.apply,
                                  state="disabled")
        self.b_apply.pack(side="right", padx=6)
        self.b_undo = ttk.Button(foot, text="Undo a run...", command=self.undo)
        self.b_undo.pack(side="right", padx=(0, 12))
        self.create_missing = tk.BooleanVar(value=False)
        self.cm = ttk.Checkbutton(
            foot, text="also record songs that are in neither",
            variable=self.create_missing)
        self.cm.pack(side="left", padx=(16, 0))

        self.show_snapshot()
        self.build()

    # ---------- helpers ----------
    def show_snapshot(self):
        import itunes_sync as isync
        n, taken = isync.snapshot_age()
        self.snap.configure(
            text="iTunes snapshot: {} tracks{}".format(
                n, ", taken " + taken if taken else " (never taken)")
            + "  -  re-read it if you've played anything in iTunes since.")

    def set_busy(self, on, msg=""):
        self.busy = on
        for b in (self.b_build, self.b_apply, self.b_snap, self.b_undo):
            b.configure(state="disabled" if on else "normal")
        if not on and not self.rows:
            self.b_apply.configure(state="disabled")
        if msg:
            self.status.configure(text=msg)

    # ---------- build ----------
    def resnapshot(self):
        if self.busy:
            return
        self.set_busy(True, "re-reading the iTunes library (about 2 minutes)...")

        def work():
            try:
                import itunes_sync as isync
                n = isync.snapshot(progress=lambda i, t: self.app.post(
                    self.status.configure, {"text": "reading iTunes {}/{}".format(i, t)}))
                self.app.post(self._snapped, n)
            except Exception as e:
                self.app.post(self._err, "snapshot failed: " + str(e))

        threading.Thread(target=work, daemon=True).start()

    def _snapped(self, n):
        self.set_busy(False, "snapshot: {} tracks".format(n))
        self.show_snapshot()
        self.build()

    def build(self):
        if self.busy:
            return
        self.set_busy(True, "building preview...")
        source = self.source.get()

        def work():
            try:
                import itunes_sync as isync
                rows, summary = isync.build_plan(source)
                self.app.post(self._built, rows, summary)
            except Exception as e:
                self.app.post(self._err, "could not build the preview: " + str(e))

        threading.Thread(target=work, daemon=True).start()

    def _built(self, rows, summary):
        self.rows, self.summary = rows, summary
        src = summary["source"]
        if src == "history":
            self.head.configure(text=(
                "{entries} entries -> {plays} plays of at least {secs}s "
                "({skipped} too short or not music), {tracks} tracks.\n"
                "{to_itunes} update iTunes, {to_syncdb} update the database, "
                "{no_target} match neither. {ambiguous} songs exist on more than "
                "one iTunes track -- the most-played copy gets them.\n"
                "{already_applied} songs were credited by an earlier run and are "
                "not listed again.").format(
                    secs=summary["min_ms"] // 1000, **summary))
        else:
            self.head.configure(text=(
                "{plays} plays since the history import's watermark, {tracks} tracks.\n"
                "{to_itunes} update iTunes, {to_syncdb} update the database, "
                "{no_target} match neither.").format(**summary))
        self.fill()
        self.set_busy(False, "")
        self.b_apply.configure(state="normal" if self.rows else "disabled")

    def fill(self):
        key = self.SORTS[self.sort.get()]
        rows = sorted(self.rows, key=key, reverse=self.sort.get() in self.REVERSED)
        self.tree.delete(*self.tree.get_children())
        label = {"itunes": "iTunes", "syncdb": "database", "none": "neither"}
        for i, r in enumerate(rows):
            self.tree.insert("", "end", iid=str(i), values=(
                label.get(r["target"], r["target"]), r["spotify_plays"],
                r["old_count"] or "", r["new_count"], r["artist"], r["name"],
                (r["new_date"] or "")[:16].replace("T", " "),
                r["ambiguous"] if r["ambiguous"] > 1 else ""))
        self.status.configure(text="{} rows".format(len(rows)))

    def _err(self, msg):
        self.set_busy(False, msg)
        self.app.log(msg)

    # ---------- apply ----------
    def apply(self):
        if self.busy or not self.rows:
            return
        s = self.summary
        extra = ""
        if self.create_missing.get():
            extra = ("\n\nAlso creating {} rows for songs in neither, marked "
                     "'logged' so nothing tries to download them.".format(s["no_target"]))
        if not messagebox.askyesno("Apply play counts", (
                "Update {} iTunes tracks and {} database rows?{}\n\n"
                "iTunes play counts have no undo of their own, so each track's "
                "previous count and date are recorded first -- use 'Undo a run' "
                "to reverse this.\n\nContinue?").format(
                    s["to_itunes"], s["to_syncdb"], extra)):
            return
        self.set_busy(True, "applying...")
        rows, summary = list(self.rows), dict(self.summary)
        create = self.create_missing.get()

        def work():
            try:
                import itunes_sync as isync
                res = isync.apply_plan(
                    rows, summary,
                    progress=lambda n, t: self.app.post(
                        self.status.configure, {"text": "iTunes {}/{}".format(n, t)}),
                    create_missing=create)
                self.app.post(self._applied, res)
            except Exception as e:
                self.app.post(self._err, "apply failed: " + str(e))

        threading.Thread(target=work, daemon=True).start()

    def _applied(self, res):
        self.set_busy(False, "run {}: {} iTunes, {} database{}, {} skipped".format(
            res["run_id"], res["itunes"], res["syncdb"],
            ", {} created".format(res["created"]) if res.get("created") else "",
            res["skipped"]))
        self.app.log("play counts applied - run {}: {} iTunes, {} database".format(
            res["run_id"], res["itunes"], res["syncdb"]))
        for name, err in res["errors"][:8]:
            self.app.log("   {}: {}".format(name, err))
        self.build()
        self.app.reload()

    def undo(self):
        import itunes_sync as isync
        runs = isync.runs()
        if not runs:
            messagebox.showinfo("Nothing to undo", "No runs have been applied.")
            return
        run_id, applied_at, n = runs[0]
        if not messagebox.askyesno("Undo", (
                "Undo run {}?\n\nApplied {}\n{} iTunes tracks would be restored "
                "to their previous play count and last-played date.").format(
                    run_id, applied_at, n)):
            return
        self.set_busy(True, "undoing " + run_id + "...")

        def work():
            try:
                res = isync.undo(run_id)
                self.app.post(self._undone, run_id, res)
            except Exception as e:
                self.app.post(self._err, "undo failed: " + str(e))

        threading.Thread(target=work, daemon=True).start()

    def _undone(self, run_id, res):
        self.set_busy(False, "undid {}: {} restored".format(run_id, res["restored"]))
        self.app.log("undid run {}: {} tracks restored".format(run_id, res["restored"]))
        for name, err in res["errors"][:8]:
            self.app.log("   {}: {}".format(name, err))
        self.build()


class ArtistDialog(tk.Toplevel):
    """Find an artist, list their whole catalogue, open any release.

    Useful when a file's album tag is wrong or says "Singles" -- the track is
    usually on a real release somewhere.
    """

    def __init__(self, app, artist=""):
        super().__init__(app)
        self.app = app
        self.artists, self.releases = [], []
        self.title("Browse artist")
        self.geometry("940x560")
        self.transient(app)

        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="Artist:").pack(side="left")
        self.f_name = tk.StringVar(value=artist)
        e = ttk.Entry(top, textvariable=self.f_name, width=36)
        e.pack(side="left", padx=4)
        e.bind("<Return>", lambda ev: self.search())
        self.b_search = ttk.Button(top, text="Search", command=self.search)
        self.b_search.pack(side="left")
        self.groups = tk.StringVar(value="album,single,compilation")
        ttk.Combobox(top, textvariable=self.groups, state="readonly", width=24,
                     values=["album,single,compilation", "album", "album,single",
                             "single", "compilation", "appears_on"]
                     ).pack(side="left", padx=(12, 0))

        mid = ttk.PanedWindow(self, orient="horizontal")
        mid.pack(fill="both", expand=True, padx=10)

        lf = ttk.LabelFrame(mid, text="Artists", padding=4)
        self.atree = ttk.Treeview(lf, columns=("name",), show="headings",
                                  selectmode="browse")
        self.atree.heading("name", text="Name")
        self.atree.column("name", width=210, anchor="w")
        self.atree.pack(fill="both", expand=True)
        self.atree.bind("<<TreeviewSelect>>", lambda ev: self.load_releases())
        mid.add(lf, weight=1)

        rf = ttk.LabelFrame(mid, text="Releases (double-click to open)", padding=4)
        self.rtree = ttk.Treeview(rf, columns=("type", "year", "name", "n"),
                                  show="headings", selectmode="browse")
        for c, w, t in (("type", 92, "Type"), ("year", 54, "Year"),
                        ("name", 320, "Release"), ("n", 54, "Tracks")):
            self.rtree.heading(c, text=t)
            self.rtree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(rf, orient="vertical", command=self.rtree.yview)
        self.rtree.configure(yscrollcommand=sb.set)
        self.rtree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.rtree.bind("<Double-1>", lambda ev: self.open_release())
        mid.add(rf, weight=3)

        foot = ttk.Frame(self, padding=(10, 8))
        foot.pack(fill="x")
        self.status = ttk.Label(foot, text="")
        self.status.pack(side="left")
        ttk.Button(foot, text="Close", command=self.destroy).pack(side="right")
        ttk.Button(foot, text="Open album",
                   command=self.open_release).pack(side="right", padx=6)

        if artist:
            self.search()

    def search(self):
        name = self.f_name.get().strip()
        if not name:
            return
        self.status.configure(text="searching for " + name + " ...")
        self.b_search.configure(state="disabled")

        def work():
            try:
                import albums, spotify
                found = albums.search_artists(spotify.client(), name)
                self.app.post(self._found, found, name)
            except Exception as e:
                self.app.post(self._err, "artist search failed: " + str(e))

        threading.Thread(target=work, daemon=True).start()

    def _found(self, found, name):
        self.b_search.configure(state="normal")
        self.artists = found
        self.atree.delete(*self.atree.get_children())
        self.rtree.delete(*self.rtree.get_children())
        for i, a in enumerate(found):
            self.atree.insert("", "end", iid=str(i), values=(a["name"],))
        if not found:
            self.status.configure(
                text='Spotify has no artist matching "' + name + '".')
            return
        self.status.configure(text=str(len(found)) + " artist(s)")
        self.atree.selection_set("0")

    def _err(self, msg):
        self.b_search.configure(state="normal")
        self.status.configure(text=msg)
        self.app.log(msg)

    def load_releases(self):
        sel = self.atree.selection()
        if not sel:
            return
        art = self.artists[int(sel[0])]
        self.status.configure(text="loading releases for " + art["name"] + " ...")
        groups = self.groups.get()

        def work():
            try:
                import albums, spotify
                rel = albums.artist_releases(spotify.client(), art["id"], groups)
                self.app.post(self._releases, rel, art)
            except Exception as e:
                self.app.post(self._err, "could not list releases: " + str(e))

        threading.Thread(target=work, daemon=True).start()

    def _releases(self, rel, art):
        self.releases = rel
        self.rtree.delete(*self.rtree.get_children())
        for i, al in enumerate(rel):
            self.rtree.insert("", "end", iid=str(i), values=(
                al.get("album_type", ""), (al.get("release_date") or "")[:4],
                al.get("name", ""), al.get("total_tracks", "")))
        self.status.configure(
            text="{}: {} release(s)".format(art["name"], len(rel)))

    def open_release(self):
        sel = self.rtree.selection()
        if not sel:
            messagebox.showinfo("Pick one", "Select a release first.")
            return
        AlbumDialog(self.app, album=self.releases[int(sel[0])])


if __name__ == "__main__":
    if not os.path.exists(DB):
        sys.exit("sync.db not found at " + DB)
    App().mainloop()
