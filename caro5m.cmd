@echo off
rem caro5m - abre el panel (TUI) con el Python del entorno virtual. Mejor en Windows Terminal (cmd.exe clasico se ve peor).
setlocal
set "DIR=%~dp0"
set "CARO5M_HOME=%DIR:~0,-1%"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
chcp 65001 >nul 2>&1
if not exist "%DIR%.venv\Scripts\python.exe" (
  echo No encuentro .venv. Ejecuta primero:  powershell -ExecutionPolicy Bypass -File install.ps1
  exit /b 1
)
"%DIR%.venv\Scripts\python.exe" -m caro5m %*
