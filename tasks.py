"""Status and registration of the three scheduled tasks this setup relies on.

    python tasks.py        # show each task's status

Each task is registered by its own script (which knows the schedule); this
module only asks Task Scheduler what exists and runs those scripts. "Expected"
means what the script would register now -- read from its -DryRun output, so
there is one definition of each task, not two that could drift apart.
"""
import json, os, subprocess, sys

import procutil

HERE = os.path.dirname(os.path.abspath(__file__))


def poller_dir():
    """SpotifyPoller's folder: wherever the plays_path setting points."""
    import settings
    return os.path.dirname(settings.get("plays_path"))


def pythonw():
    """pythonw.exe of the Python running this -- the one with this project's
    packages installed. PATH can hold others (the Store's app alias, the
    Python install manager's), so the task scripts are told which one to use
    rather than left to find one."""
    p = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return p if os.path.exists(p) else None


def _script_cmd(task, *extra):
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", task["script"]]
    if task.get("takes_python") and pythonw():
        cmd += ["-Python", pythonw()]
    return cmd + list(extra)


def tasks():
    return [
        {"name": "Spotify Liked Sync",
         "what": "downloads newly liked songs, nightly at 03:00",
         "script": os.path.join(HERE, "register_task.ps1"), "takes_python": True},
        {"name": "Spotify Plays to iTunes",
         "what": "adds new Spotify plays to iTunes, every 2 hours at :03",
         "script": os.path.join(HERE, "register_poll_task.ps1"), "takes_python": True},
        {"name": "SpotifyPlayTracker",
         "what": "logs your Spotify plays, every 2 hours at :48",
         "script": os.path.join(poller_dir(), "register_task.ps1")},
    ]


# Task Scheduler result codes worth naming; anything else is shown as a number.
RESULTS = {0: "succeeded", 0x41301: "running now", 0x41303: "hasn't run yet",
           0x41306: "was stopped", 0x8004131F: "an instance was already running",
           0x800710E0: "refused to start (conditions not met)"}


def _ps(command, timeout=60):
    p = procutil.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                      "-Command", command], capture_output=True, text=True,
                     encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def live(names):
    """{name: {...}} for the tasks that exist."""
    quoted = ",".join("'" + n.replace("'", "''") + "'" for n in names)
    cmd = ("$out = foreach ($n in @(" + quoted + ")) {"
           " $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue;"
           " if ($t) { $i = Get-ScheduledTaskInfo -TaskName $n; $a = $t.Actions[0];"
           " [pscustomobject]@{ name=$n; state=[string]$t.State; execute=$a.Execute;"
           " arguments=$a.Arguments; workdir=$a.WorkingDirectory;"
           " last=$i.LastRunTime.ToString('s'); next=$(if ($i.NextRunTime) {"
           " $i.NextRunTime.ToString('s') } else { '' }); result=[int64]$i.LastTaskResult } } };"
           " ConvertTo-Json -InputObject @($out) -Compress")
    rc, out, err = _ps(cmd)
    if rc != 0 or not out:
        return {}
    data = json.loads(out)
    return {d["name"]: d for d in (data if isinstance(data, list) else [data])}


def expected(task):
    """(what the task's script would register now, from its -DryRun, or None;
    why not, or None)."""
    if not os.path.exists(task["script"]):
        return None, "registration script not found: " + task["script"]
    try:
        p = procutil.run(_script_cmd(task, "-DryRun"), capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=60)
    except Exception as e:
        return None, "couldn't run its registration script: " + str(e)
    got = {}
    for line in (p.stdout or "").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            k = k.strip()
            if k in ("execute", "arguments", "working dir"):
                got[k] = v.strip()
    if len(got) == 3:
        return got, None
    said = ((p.stderr or "") + (p.stdout or "")).strip().splitlines()
    return None, "couldn't check it against its registration script" + (
        ": " + said[0].strip() if said else " (exit code {})".format(p.returncode))


def _same(a, b):
    return os.path.normcase((a or "").strip()) == os.path.normcase((b or "").strip())


def _when(iso):
    if not iso or iso.startswith("1999"):       # Task Scheduler's "never"
        return "never"
    return iso.replace("T", " ")[:16]


def status_all():
    """One dict per task: registered, current, and a human summary."""
    ts = tasks()
    have = live([t["name"] for t in ts])
    out = []
    for t in ts:
        row = dict(t)
        row["script_found"] = os.path.exists(t["script"])
        row["expected"], why = expected(t)
        info = have.get(t["name"])
        row["registered"] = bool(info)
        if not info:
            row.update(level="error", current=False,
                       summary="not registered")
            out.append(row)
            continue
        exp = row["expected"] or {}
        # current: True/False when compared, None when the comparison couldn't run
        row["current"] = None if not exp else all((
            _same(info["execute"], exp.get("execute")),
            _same(info["arguments"], exp.get("arguments")),
            _same(info["workdir"], exp.get("working dir"))))
        result = info["result"] & 0xFFFFFFFF
        said = RESULTS.get(result, "failed (code 0x{:X})".format(result))
        parts = ["{}".format(info["state"].lower()),
                 "last run {} - {}".format(_when(info["last"]), said),
                 "next {}".format(_when(info["next"]) if info["next"] else "not scheduled")]
        level = "ok"
        if info["state"] == "Disabled":
            level = "warn"
        if result not in (0, 0x41301, 0x41303):
            level = "warn"
        if row["current"] is False:
            level = "warn"
            parts.append("points at " + (info["arguments"] or info["execute"]))
        if why:
            level = "warn"
            parts.append(why)
        row.update(level=level, summary="  |  ".join(parts), info=info)
        out.append(row)
    return out


def register(task, task_name=None):
    """Run the task's own registration script. Returns (ok, message)."""
    if not os.path.exists(task["script"]):
        return False, "registration script not found: " + task["script"]
    cmd = _script_cmd(task, *(["-TaskName", task_name] if task_name else []))
    p = procutil.run(cmd, capture_output=True, text=True, encoding="utf-8",
                     errors="replace", timeout=120)
    msg = ((p.stdout or "") + (p.stderr or "")).strip()
    return p.returncode == 0, msg


def run_now(task):
    """Start a registered task now, exactly as its schedule would (same
    account, no window). Returns (ok, message)."""
    rc, out, err = _ps("Start-ScheduledTask -TaskName '" +
                       task["name"].replace("'", "''") + "'")
    return rc == 0, (err or out)


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    for r in status_all():
        print("{:<8} {:<25} {}".format(r["level"].upper(), r["name"], r["summary"]))
        print("{:<8} {:<25} {}{}".format("", "", r["what"],
              "" if r["script_found"] else "  (script missing: " + r["script"] + ")"))
