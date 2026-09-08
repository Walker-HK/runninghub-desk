@echo off
setlocal
cd /d "%~dp0"
py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 (
    py -3 app.py %*
    goto finished
)
python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 (
    python app.py %*
    goto finished
)
echo Python 3.10+ is required. Install Python and add it to PATH.
pause
exit /b 1
:finished
set "app_exit=%errorlevel%"
if not "%app_exit%"=="0" pause
exit /b %app_exit%
