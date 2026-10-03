@echo off
setlocal
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 -B "%~dp0scripts\demo_app.py" %*
goto finished
:use_python
python -B "%~dp0scripts\demo_app.py" %*
:finished
set "demo_exit=%errorlevel%"
if not "%demo_exit%"=="0" pause
exit /b %demo_exit%
