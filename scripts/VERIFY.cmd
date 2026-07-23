@echo off
call "%~dp0..\VERIFY.cmd" %*
exit /b %ERRORLEVEL%
