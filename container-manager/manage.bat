@echo off
setlocal
py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if errorlevel 1 goto use_python
py -3 "%~dp0manager.py" %*
goto finish

:use_python
python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if errorlevel 1 goto no_python
python "%~dp0manager.py" %*
goto finish

:no_python
echo Python 3.10 or newer is required.
set "result=1"
goto report

:finish
set "result=%errorlevel%"
:report
if "%~1"=="" if not "%result%"=="0" if not "%result%"=="130" pause
exit /b %result%
