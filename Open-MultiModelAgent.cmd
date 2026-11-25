@echo off
setlocal
cd /d "%~dp0"
if exist "MultiModelAgent.exe" (
    start "" "%~dp0MultiModelAgent.exe" %*
    exit /b 0
)
echo MultiModelAgent.exe is missing from this folder.
pause
exit /b 1
