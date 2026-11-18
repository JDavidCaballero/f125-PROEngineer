<#
Instala lo necesario para el laboratorio de voz en Windows 10/11. No pide permisos de administrador y no toca nada fuera de esta carpeta.
SIN PROBAR en un Windows real (escrito en un Mac). LÉELO ANTES DE EJECUTARLO. Qué hace:
  1) elige Python 3.13, 3.12 o 3.11 con el lanzador `py` (o `python` si no hay lanzador)
  2) crea un entorno virtual en .\.venv (si había uno de otra versión de Python, lo borra y lo recrea: solo esa carpeta)
  3) instala numpy y sounddevice (micrófono) y faster-whisper (voz a texto; mlx-whisper es solo para Mac)
  4) guarda TODA la salida en .\setup.log (adjúntalo a un issue si algo falla)
Uso (en PowerShell, dentro de la carpeta del proyecto):
  Set-ExecutionPolicy -Scope Process Bypass     # solo para esta ventana; no cambia nada de forma permanente
  .\setup_windows.ps1                           # instalar
  .\setup_windows.ps1 -DryRun                   # solo comprueba y dice qué haría; no instala nada
  .\setup_windows.ps1 -Python "py -3.12"        # forzar una versión (o la ruta a python.exe)
Voz del ingeniero (salida): usa las voces SAPI de Windows (vienen con Windows); no hay que instalar nada más.
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [string]$Python = ""
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Continue"
Set-Location -LiteralPath $PSScriptRoot
$log = Join-Path $PSScriptRoot "setup.log"
try { Start-Transcript -LiteralPath $log -Force | Out-Null } catch { Write-Host "(no pude abrir setup.log: $_)" }

function Finish([int]$code) {
    try { Stop-Transcript | Out-Null } catch { }
    exit $code
}

# La salida de programas externos (python, pip) se pasa por Write-Host para que Start-Transcript la guarde en setup.log
# (en PowerShell 5.1 no la guarda si va directa a la consola). Los avisos de stderr se convierten en texto normal.
function Show-Output { process { Write-Host "$_" } }

# Un "candidato" es el ejecutable y sus argumentos previos, p. ej. @("py", "-3.13") o @("C:\Python313\python.exe").
function Invoke-Py([string[]]$cand, [string[]]$pyArgs) {
    $exe = $cand[0]
    $pre = @()
    if ($cand.Count -gt 1) { $pre = $cand[1..($cand.Count - 1)] }
    & $exe @pre @pyArgs
}

function Get-PyVersion([string[]]$cand) {
    try {
        $v = Invoke-Py $cand @("-c", "import sys; print('%d.%d' % sys.version_info[:2])") 2>$null
        if ($LASTEXITCODE -eq 0 -and $v -match '^\d+\.\d+$') { return [string]$v }
    } catch { }
    return $null
}

function Find-Python {
    if ($Python) {
        if (Test-Path -LiteralPath $Python) { return ,([string[]]@($Python)) }   # ruta a python.exe (puede tener espacios)
        return ,([string[]]($Python -split '\s+' | Where-Object { $_ }))      # p. ej. "py -3.12"
    }
    $cands = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $cands += ,@("py", "-3.13")
        $cands += ,@("py", "-3.12")
        $cands += ,@("py", "-3.11")
        $cands += ,@("py", "-3")
    }
    # Ojo: en Windows `python` puede ser el atajo de la Microsoft Store (abre la tienda y no hace nada); Get-PyVersion lo descarta.
    if (Get-Command python -ErrorAction SilentlyContinue) { $cands += ,@("python") }
    foreach ($c in $cands) {
        if (Get-PyVersion $c) { return ,([string[]]$c) }
    }
    return $null
}

$PY = Find-Python
if (-not $PY) {
    Write-Host "No encuentro Python 3. Instala Python 3.13 desde https://www.python.org/downloads/windows/"
    Write-Host "(marca 'Add python.exe to PATH' y deja el lanzador 'py') y repite."
    Finish 1
}
$VER = Get-PyVersion $PY
if (-not $VER) {
    Write-Host "No pude ejecutar Python con: $($PY -join ' ')"
    Finish 1
}
$where = Invoke-Py $PY @("-c", "import sys; print(sys.executable)") 2>$null
Write-Host "Python elegido: $($PY -join ' ') ($VER) en $where"
$parts = $VER.Split(".")
$major = [int]$parts[0]; $minor = [int]$parts[1]
if ($major -ne 3 -or $minor -lt 11) {
    Write-Host "AVISO: Python $VER. El programa necesita 3.11 o más. Instala Python 3.13 y luego:  .\setup_windows.ps1 -Python 'py -3.13'"
} elseif ($minor -ge 14) {
    Write-Host "AVISO: Python $VER. faster-whisper (y ctranslate2) suelen tardar en publicar paquetes para versiones muy nuevas."
    Write-Host "       Si la instalación falla: instala Python 3.13 y luego:  .\setup_windows.ps1 -Python 'py -3.13'"
}

$venv = Join-Path $PSScriptRoot ".venv"
$vpy = Join-Path $venv "Scripts\python.exe"

if ($DryRun) {
    Write-Host ""
    Write-Host "-DryRun: no instalo nada. Haría: crear $venv con $($PY -join ' ') e instalar numpy, sounddevice y faster-whisper."
    Finish 0
}

if (Test-Path -LiteralPath $venv) {
    $old = "?"
    if (Test-Path -LiteralPath $vpy) { $o = Get-PyVersion @($vpy); if ($o) { $old = $o } }
    if ($old -ne $VER) {
        Write-Host "El entorno .venv era de Python ${old}: lo recreo con $VER (se borra solo .\.venv)"
        Remove-Item -LiteralPath $venv -Recurse -Force
    }
}
if (-not (Test-Path -LiteralPath $vpy)) {
    Invoke-Py $PY @("-m", "venv", $venv) 2>&1 | Show-Output
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $vpy)) { Write-Host "No pude crear el entorno virtual"; Finish 1 }
}

& $vpy -m pip install --upgrade pip 2>&1 | Out-Null
Write-Host "== micrófono"
& $vpy -m pip install numpy sounddevice 2>&1 | Show-Output
if ($LASTEXITCODE -ne 0) { Write-Host "FALLÓ numpy/sounddevice"; Finish 1 }
Write-Host "== faster-whisper"
& $vpy -m pip install faster-whisper 2>&1 | Show-Output
if ($LASTEXITCODE -ne 0) { Write-Host "(faster-whisper no se pudo instalar)" }

Write-Host ""
Write-Host "== instalado:"
& $vpy -m pip list 2>$null | Select-String -Pattern '^(numpy|sounddevice|faster-whisper|ctranslate2) ' | Show-Output
Write-Host ""
Write-Host "Listo. Activa el entorno con:   .\.venv\Scripts\Activate.ps1"
Write-Host "  (si PowerShell no deja ejecutar scripts:  Set-ExecutionPolicy -Scope Process Bypass  ; solo para esta ventana)"
Write-Host "Primer paso:                     python voice.py selftest --say-test"
Write-Host "Sin micrófono:                   python voice.py lab --text --file captures\demo_corta.f1raw"
Write-Host "Con micrófono:                   python voice.py lab --live --model small   (añade --no-voice si la voz da problemas)"
Write-Host "Voz en español para el ingeniero: Configuración > Hora e idioma > Voz > Agregar voces (si no tienes ninguna)."
Write-Host "(antes, si no tienes capturas:   python make_demo.py all --out captures)"
Write-Host "La primera vez Windows puede pedir permiso de micrófono (Configuración > Privacidad > Micrófono) y de red para Python"
Write-Host "(Firewall de Windows: permite 'redes privadas' para que llegue la telemetría UDP del juego)."
Finish 0
