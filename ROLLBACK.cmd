@echo off
setlocal
set "ROOT=%~dp0"
"%ROOT%runtime\python\python.exe" "%ROOT%scripts\portable_manager.py" rollback %*
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
