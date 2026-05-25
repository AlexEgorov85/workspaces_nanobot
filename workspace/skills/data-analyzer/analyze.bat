@echo off
setlocal
cd /d "%~dp0"
if not exist "python.exe" where python >nul 2>&1 || (
    echo Ошибка: Python не найден в PATH.
    exit /b 1
)
python scripts\analyze.py %*
endlocal