@echo off
rem Flex Bulk: one-command setup and start for Windows 10/11.
rem The offline (jump-drive) copy carries uv, Python 3.11 and every package in offline\win-x64 and installs without
rem internet in 1-2 min. Other copies download them from astral.sh and PyPI, and build the demo data if there is none.
setlocal
cd /d "%~dp0"
set "REQ=requirements.txt"
set "PY=3.11"
set "OFFARGS="
if not exist offline\win-x64\uv.exe goto online
set "UV=%CD%\offline\win-x64\uv.exe"
set "PY=%CD%\offline\win-x64\python\python.exe"
set "REQ=offline\win-x64\requirements.txt"
set "OFFARGS=--offline --no-index --find-links offline\win-x64\wheels"
set "UV_OFFLINE=1"
set "UV_PYTHON_DOWNLOADS=never"
set "UV_NO_CONFIG=1"
echo Offline install kit: offline\win-x64
goto haveuv

:online
set "UV="
for %%X in (uv.exe) do if not "%%~$PATH:X"=="" set "UV=%%~$PATH:X"
if not defined UV if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"
if defined UV goto haveuv
echo Installing uv (Python installer from astral.sh) into %USERPROFILE%\.local\bin ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$env:UV_NO_MODIFY_PATH=1; irm https://astral.sh/uv/install.ps1 | iex"
set "UV=%USERPROFILE%\.local\bin\uv.exe"
if not exist "%UV%" (echo uv could not be installed. Ask IT whether astral.sh downloads are blocked. & pause & exit /b 1)

:haveuv
rem a folder moved after the first run leaves .venv pointing at the old place: start it over
if not exist .venv\Scripts\python.exe goto mkvenv
.venv\Scripts\python.exe -c "" >nul 2>&1
if not errorlevel 1 goto install
echo The Python environment was made in another place; recreating it ...
rmdir /s /q .venv
:mkvenv
"%UV%" venv --python "%PY%" .venv
if errorlevel 1 (pause & exit /b 1)
:install
echo Installing packages (skipped if already installed) ...
"%UV%" pip install --python .venv\Scripts\python.exe %OFFARGS% -r "%REQ%"
if errorlevel 1 (echo Package install failed. & pause & exit /b 1)
.venv\Scripts\python.exe -m flexbulk.launch %*
pause
