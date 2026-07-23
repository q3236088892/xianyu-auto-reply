@echo off
call "%~dp0..\STATUS.cmd" %*
exit /b %ERRORLEVEL%
