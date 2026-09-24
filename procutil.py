"""Run child processes without flashing console windows.

Under pythonw.exe (the GUI and the scheduled task) the parent has no console, so
every console child - powershell, yt-dlp, ffmpeg - creates its own window. A
twelve-track album turns into a stream of popups.

CREATE_NO_WINDOW gives the child a console that is never displayed. Grandchildren
inherit it, so hiding the powershell call also hides yt-dlp and ffmpeg beneath it.
"""
import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000


def hidden_kwargs():
    """Extra subprocess kwargs that suppress console windows on Windows."""
    if sys.platform != "win32":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": CREATE_NO_WINDOW, "startupinfo": si}


def run(cmd, **kwargs):
    """subprocess.run with console windows suppressed."""
    kwargs.update(hidden_kwargs())
    return subprocess.run(cmd, **kwargs)
