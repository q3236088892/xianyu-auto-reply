@echo off
set "PORTABLE_ROOT=%~dp0.."
for %%I in ("%PORTABLE_ROOT%") do set "PORTABLE_ROOT=%%~fI"
set "PYTHONHOME=%PORTABLE_ROOT%\runtime\python"
set "PLAYWRIGHT_BROWSERS_PATH=%PORTABLE_ROOT%\runtime\playwright"
set "PATH=%PORTABLE_ROOT%\runtime\python;%PORTABLE_ROOT%\runtime\python\Scripts;%PORTABLE_ROOT%\runtime\node;%PORTABLE_ROOT%\runtime\git\cmd;%PATH%"
