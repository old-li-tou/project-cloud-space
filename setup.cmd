@echo off
setlocal
pushd "%~dp0"
if exist ".venv\Scripts\python.exe" goto install
if not "%~1"=="" (
  "%~1" -c "import sys; assert sys.version_info[:2] == (3,12), 'Python 3.12 required'"
  if errorlevel 1 goto fail
  "%~1" -m venv .venv
  if errorlevel 1 goto fail
  goto install
)
where py >nul 2>nul
if not errorlevel 1 (
  py -3.12 -m venv .venv
  if not errorlevel 1 goto install
)
python -c "import sys; assert sys.version_info[:2] == (3,12), 'Python 3.12 required'"
if errorlevel 1 goto fail
python -m venv .venv
if errorlevel 1 goto fail
:install
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip check
if errorlevel 1 goto fail
echo Ready. Run start.cmd.
popd
exit /b 0
:fail
echo Setup failed. Install Python 3.12 x64 or pass its full path to setup.cmd.
popd
pause
exit /b 1
