@echo off
rem Follow Up: Windows-обёртка над follow_up_mcp (тот же скрипт, см. его докстринг).
rem Для ручного запуска на Windows (--where, --check); gateway зовёт лаунчер сам своим Python.
where python >nul 2>&1
if %ERRORLEVEL%==0 (
  python "%~dp0follow_up_mcp" %*
) else (
  py -3 "%~dp0follow_up_mcp" %*
)
exit /b %ERRORLEVEL%
