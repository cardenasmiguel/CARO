<#
caro5m — instalador para Windows (PowerShell 5.1 o 7+). Idempotente: puedes repetirlo sin perder tu .env ni tus ajustes.

  powershell -ExecutionPolicy Bypass -File install.ps1            # recomendado (no cambia la política del sistema)
  powershell -ExecutionPolicy Bypass -File install.ps1 -Cli       # configura la cuenta aquí, con preguntas
  powershell -ExecutionPolicy Bypass -File install.ps1 -NoWizard  # sin configurar la cuenta ahora (luego: panel, tecla A)
  ... -NoLock            usa requirements.txt en vez de requirements.lock.txt
  ... -Python C:\ruta\python.exe
  ... -NonInteractive    sin preguntas (lee CARO5M_SETUP_* del entorno; ver: python -m caro5m.setup -h)
  ... -NoOpen            no ofrece abrir el panel al terminar

Crea .venv, instala dependencias, prepara .env (solo tu usuario puede leerlo) y config\settings.env, y deja los
accesos directos caro5m.cmd / start.bat (panel), run-bot.cmd y run-ptb.cmd. systemd y logrotate son solo de Linux:
aquí no se instalan. El modo PRUEBA (DRY_RUN=1) sigue activado: este script nunca activa el dinero real. El proxy HTTP/SOCKS es opcional (para redes u operadores que bloquean el DNS o restringen la salida).
#>
[CmdletBinding()]
param(
  [switch]$Cli,
  [switch]$Panel,
  [switch]$NoWizard,
  [switch]$NoLock,
  [switch]$NonInteractive,
  [switch]$NoOpen,
  [string]$Python = ""
)
$ErrorActionPreference = "Stop"
$Dir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Dir
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:CARO5M_HOME = $Dir

function Say($msg) { Write-Host "==> $msg" -ForegroundColor Green }
function Die($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

function Test-Py($exe, $extra) {
  try {
    $out = & $exe @extra -c "import sys; print('%d.%d' % sys.version_info[:2]); sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) { return $out }
  } catch {}
  return $null
}

# ── Python 3.10+ (py launcher preferido) ──
$PyExe = $null; $PyArgs = @()
if ($Python) {
  if (Test-Py $Python @()) { $PyExe = $Python } else { Die "$Python no es Python 3.10 o superior" }
} else {
  $cands = @(
    @{ Exe = "py"; Args = @("-3") },
    @{ Exe = "python"; Args = @() },
    @{ Exe = "python3"; Args = @() }
  )
  foreach ($cand in $cands) {
    if (Get-Command $cand.Exe -ErrorAction SilentlyContinue) {
      $v = Test-Py $cand.Exe $cand.Args
      if ($v) { $PyExe = $cand.Exe; $PyArgs = $cand.Args; break }
    }
  }
}
if (-not $PyExe) {
  Die "No encuentro Python 3.10 o superior. Instálalo desde https://www.python.org/downloads/windows/ (marca «Add python.exe to PATH») o con: winget install Python.Python.3.12"
}
Say "Python: $PyExe $($PyArgs -join ' ') ($(Test-Py $PyExe $PyArgs))"

# ── antes de instalar/actualizar: parar el bot y el PTB de ESTA carpeta (si no, seguiría corriendo la versión vieja
#    y Windows no deja reemplazar los ficheros en uso). Solo procesos de esta carpeta; nunca de otros proyectos. ──
$VenvPy = Join-Path $Dir ".venv\Scripts\python.exe"
Say "Comprobando si hay un bot o PTB de esta carpeta en marcha"
$StopArgs = @("-m", "caro5m.procs", "--root", $Dir, "--stop")
if ($NonInteractive -or -not [Environment]::UserInteractive) { $StopArgs += "--yes" }
$OldPyPath = $env:PYTHONPATH
$env:PYTHONPATH = $Dir
try {
  if (Test-Path $VenvPy) { & $VenvPy @StopArgs } else { & $PyExe @PyArgs @StopArgs }
  if ($LASTEXITCODE -ne 0) { Write-Host "(aviso: no se pudo parar todo; revisa lo anterior)" -ForegroundColor Yellow }
} catch { Write-Host "(aviso: no se pudo comprobar si el bot estaba en marcha: $_)" -ForegroundColor Yellow }
$env:PYTHONPATH = $OldPyPath

# ── entorno virtual y dependencias ──
Say "Entorno virtual en $Dir\.venv"
if (-not (Test-Path $VenvPy)) {
  & $PyExe @PyArgs -m venv (Join-Path $Dir ".venv")
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvPy)) { Die "no se pudo crear el entorno virtual (.venv)" }
}
& $VenvPy -m pip install --quiet --upgrade pip
if ($LASTEXITCODE -ne 0) { Die "pip no funciona en el entorno virtual" }
$Req = if ($NoLock) { "requirements.txt" } else { "requirements.lock.txt" }
Say "Instalando dependencias ($Req)"
& $VenvPy -m pip install --quiet -r (Join-Path $Dir $Req)
if ($LASTEXITCODE -ne 0) { Die "falló la instalación de dependencias (prueba: -NoLock)" }
& $VenvPy -m pip install --quiet --no-deps -e $Dir
if ($LASTEXITCODE -ne 0) { Die "no se pudo instalar caro5m en el entorno virtual" }

# ── carpetas y configuración ──
Say "Carpetas y ficheros de configuración"
New-Item -ItemType Directory -Force -Path (Join-Path $Dir "data\control"), (Join-Path $Dir "config") | Out-Null
$EnvFile = Join-Path $Dir ".env"
if (-not (Test-Path $EnvFile)) {
  Copy-Item (Join-Path $Dir ".env.example") $EnvFile
  Say ".env creado vacío. Lo rellena el asistente de cuenta (siguiente paso) o el panel [A]."
} else {
  Say ".env ya existe: no se toca."
}
# Equivalente a chmod 600: solo tu usuario puede leer/escribir el .env (quita la herencia de permisos de la carpeta).
try {
  & icacls $EnvFile /inheritance:r /grant:r "$($env:USERNAME):(R,W)" *> $null
  if ($LASTEXITCODE -eq 0) { Say ".env: acceso solo para $($env:USERNAME)" }
} catch { Write-Host "(no se pudieron ajustar los permisos del .env; déjalo dentro de tu carpeta de usuario)" }
$Settings = Join-Path $Dir "config\settings.env"
if (-not (Test-Path $Settings)) { Copy-Item (Join-Path $Dir "config\settings.env.example") $Settings }

# ── asistente de cuenta ──
$Interactive = [Environment]::UserInteractive -and -not $NonInteractive -and -not [Console]::IsInputRedirected
$Mode = "auto"
if ($NoWizard) { $Mode = "no" } elseif ($Cli) { $Mode = "guided" } elseif ($Panel) { $Mode = "panel" } elseif ($NonInteractive) { $Mode = "noninteractive" }
if ($Mode -eq "auto") {
  if (Get-ChildItem env: | Where-Object { $_.Name -like "CARO5M_SETUP_*" }) { $Mode = "noninteractive" }
  elseif ($Interactive) {
    Write-Host ""
    Write-Host "¿Cómo quieres configurar tu cuenta de Polymarket (cartera, claves CLOB, proxy)?"
    Write-Host "  1) En el panel (recomendado)"
    Write-Host "  2) Aquí, en la terminal (asistente por preguntas)"
    Write-Host "  3) Más tarde (desde el panel, tecla A)"
    $c = Read-Host "Elige 1, 2 o 3 [1]"
    switch ($c) { "2" { $Mode = "guided" } "3" { $Mode = "no" } default { $Mode = "panel" } }
  } else { $Mode = "no" }
}
$env:CARO5M_FROM_INSTALLER = "1"
switch ($Mode) {
  "guided" {
    Say "Asistente de cuenta (puedes saltarte cada paso; se repite con: .venv\Scripts\python -m caro5m.setup o en el panel con [A])"
    & $VenvPy -m caro5m.setup --guided
    if ($LASTEXITCODE -ne 0) { Write-Host "(el asistente terminó con avisos: revisa lo anterior; puedes repetirlo cuando quieras)" }
  }
  "noninteractive" {
    Say "Asistente de cuenta (sin preguntas, variables CARO5M_SETUP_*)"
    & $VenvPy -m caro5m.setup --non-interactive
  }
  "panel" { Say "La configuración de cuenta se abrirá en el panel. Al terminar, pulsa Esc para ir al panel del bot." }
  default { Say "Asistente de cuenta omitido. Cuando quieras: panel, tecla [A] Cuenta" }
}
Remove-Item Env:\CARO5M_FROM_INSTALLER -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "════════════════════════════════════════════════════════════════" -ForegroundColor Green
Write-Host " ✔ Instalación terminada. ¿Y ahora qué?" -ForegroundColor Green
Write-Host "════════════════════════════════════════════════════════════════" -ForegroundColor Green
Write-Host "  1) Abre el panel del bot:   .\caro5m.cmd   (o doble clic en start.bat)"
Write-Host "  2) En el panel:"
Write-Host "       [A] cuenta (cartera, claves CLOB, proxy y comprobaciones)"
Write-Host "       [B] arranca el bot   (siempre empieza en modo PRUEBA, sin dinero real)"
Write-Host "       [M] pasa a modo REAL (pide confirmación; antes comprueba saldo y permisos)"
Write-Host "       [L] log · [C] ajustes (en caliente) · [R] reiniciar si algo lo pide · [Q] salir"
Write-Host "  3) Sin panel (en otra ventana):  .\run-bot.cmd     (PTB opcional: .\run-ptb.cmd)"
Write-Host "  4) Cuenta desde la terminal:     .venv\Scripts\python -m caro5m.setup --menu"
Write-Host "  Consejo: usa Windows Terminal (en el cmd.exe clásico el panel se ve peor)."
if ($Interactive -and -not $NoOpen -and $Mode -ne "noninteractive") {
  Write-Host ""
  $o = Read-Host "¿Abrir el panel ahora? [S/n]"
  if ($o -notmatch '^[nN]') { & (Join-Path $Dir "caro5m.cmd") } else { Write-Host "Vale. Cuando quieras: .\caro5m.cmd" }
}
