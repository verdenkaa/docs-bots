@echo off
setlocal
cd /d "%~dp0"
where node >nul 2>nul
if errorlevel 1 (
  echo Install Node.js 22 or newer first.
  pause
  exit /b 1
)
if not exist node_modules\ (
  call npm ci
  if errorlevel 1 goto error
)
call npm start -- %*
if errorlevel 1 goto error
exit /b 0
:error
echo.
echo Documentation failed. See the error above.
pause
exit /b 1
