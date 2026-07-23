@echo off
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install-update-task.ps1" %*
exit /b %ERRORLEVEL%
