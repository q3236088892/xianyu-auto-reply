@echo off
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\remove-update-task.ps1" %*
exit /b %ERRORLEVEL%
