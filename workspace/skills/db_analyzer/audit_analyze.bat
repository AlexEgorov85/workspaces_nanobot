@echo off
setlocal
cd /d "%~dp0"
python scripts\cli.py %*
endlocal
