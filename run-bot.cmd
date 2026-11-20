@echo off
rem Arranque manual del bot sin panel (en esta ventana; Ctrl+C para pararlo). Escribe tambien data\caro5m.log (pantalla [L]).
setlocal
set "DIR=%~dp0"
set "CARO5M_HOME=%DIR:~0,-1%"
set "CARO5M_LOG_TO_FILE=1"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
chcp 65001 >nul 2>&1
if not exist "%DIR%data" mkdir "%DIR%data"
set "PY=%DIR%.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m caro5m.bot
