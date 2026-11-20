@echo off
rem Arranque manual del PTB temprano (opcional; solo lectura, sin claves). Salida en data\ptb.log
setlocal
set "DIR=%~dp0"
set "CARO5M_HOME=%DIR:~0,-1%"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if not exist "%DIR%data" mkdir "%DIR%data"
set "PY=%DIR%.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m caro5m.ptb >> "%DIR%data\ptb.log" 2>&1
