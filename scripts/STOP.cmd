@echo off
call "%~dp0..\STOP.cmd" %*
exit /b %ERRORLEVEL%
