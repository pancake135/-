@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=python
where py >nul 2>nul && set PY=py
%PY% -m pip --version >nul 2>nul || %PY% -m ensurepip --upgrade --user
%PY% -m pip install -q --user -r requirements.txt
%PY% server.py
pause
