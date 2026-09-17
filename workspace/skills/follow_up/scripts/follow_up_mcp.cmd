@echo off
rem Follow Up: Windows-обёртка над follow_up_mcp (тот же скрипт, см. его докстринг).
rem Нанобот через shutil.which подбирает .cmd по PATHEXT и оборачивает вызов в cmd /c.
where python >nul 2>&1
if %ERRORLEVEL%==0 (
  python "%~dp0follow_up_mcp" %*
) else (
  py -3 "%~dp0follow_up_mcp" %*
)
exit /b %ERRORLEVEL%
