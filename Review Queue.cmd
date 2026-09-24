@echo off
rem Opens the GUI with no console window, using pythonw.exe from PATH
rem (or the py launcher's pyw.exe if pythonw isn't on PATH).
cd /d "%~dp0"
where pythonw.exe >nul 2>nul && (start "" pythonw.exe "%~dp0gui.py") || (start "" pyw.exe "%~dp0gui.py")
