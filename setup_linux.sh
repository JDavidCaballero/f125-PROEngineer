#!/usr/bin/env bash
# Instala lo necesario para el laboratorio de voz en Linux. No usa sudo y no toca nada fuera de esta carpeta.
# SIN PROBAR en un Linux real (escrito en un Mac). LÉELO ANTES DE EJECUTARLO. Qué hace:
#   1) elige Python 3.13, 3.12 o 3.11 si existen (los motores de voz suelen tardar en soportar versiones muy nuevas)
#   2) comprueba lo que hace falta del sistema (módulo venv de Python y PortAudio para el micrófono). Si falta algo,
#      te dice el comando exacto para instalarlo (con sudo, lo ejecutas tú); este script nunca usa sudo
#   3) crea un entorno virtual en ./.venv (si había uno de otra versión de Python, lo borra y lo recrea: solo esa carpeta)
#   4) instala numpy y sounddevice (micrófono) y faster-whisper (voz a texto; mlx-whisper es solo para Mac)
#   5) guarda TODA la salida en ./setup.log (adjúntalo a un issue si algo falla)
# Uso:  ./setup_linux.sh                      (o: bash setup_linux.sh)
#       ./setup_linux.sh faster               (igual: faster-whisper es el único motor en Linux)
#       PYTHON=python3.12 ./setup_linux.sh    (forzar una versión)
#       DRY=1 ./setup_linux.sh                (solo comprueba y dice qué haría; no instala nada)
# Voz del ingeniero (salida): usa espeak-ng (o spd-say) si está instalado; el script solo lo comprueba y te dice cómo instalarlo.
set -uo pipefail
cd "$(dirname "$0")"
exec > >(tee setup.log) 2>&1

if [ "$(uname -s)" != "Linux" ]; then
  if [ "${DRY:-}" = "1" ]; then
    echo "AVISO: este sistema es $(uname -s), no Linux. Modo DRY: solo compruebo, no instalo nada."
  else
    echo "Este script es para Linux y este sistema es $(uname -s)."
    [ "$(uname -s)" = "Darwin" ] && echo "En Mac usa:  ./setup_mac.sh"
    echo "(para ver qué comprobaría sin instalar nada:  DRY=1 ./setup_linux.sh)"
    exit 1
  fi
fi

WHICH="${1:-faster}"
case "$WHICH" in
  faster|both) ;;
  mlx) echo "mlx-whisper solo funciona en Mac (Apple Silicon). En Linux se instala faster-whisper."; WHICH=faster ;;
  *) echo "Opción desconocida: $WHICH (usa: faster)"; exit 1 ;;
esac

choose_python() {
  if [ -n "${PYTHON:-}" ]; then echo "$PYTHON"; return; fi
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1; then echo "$c"; return; fi
  done
}

# Familia de la distribución (para sugerir el comando correcto). Lee /etc/os-release sin ejecutar nada.
distro_family() {
  local id="" like=""
  if [ -r /etc/os-release ]; then
    id="$(. /etc/os-release 2>/dev/null; echo "${ID:-}")"
    like="$(. /etc/os-release 2>/dev/null; echo "${ID_LIKE:-}")"
  fi
  case " $id $like " in
    *" debian "*|*" ubuntu "*) echo debian ;;
    *" fedora "*|*" rhel "*|*" centos "*) echo fedora ;;
    *" arch "*) echo arch ;;
    *" suse "*|*" opensuse "*) echo suse ;;
    *) echo otra ;;
  esac
}
FAM="$(distro_family)"

PY="$(choose_python)"
if [ -z "$PY" ]; then
  echo "No encuentro Python 3. Instálalo con el gestor de paquetes de tu distribución y repite. Por ejemplo:"
  case "$FAM" in
    debian) echo "  sudo apt install python3 python3-venv" ;;
    fedora) echo "  sudo dnf install python3" ;;
    arch)   echo "  sudo pacman -S python" ;;
    suse)   echo "  sudo zypper install python3" ;;
    *)      echo "  (paquete python3 de tu distribución, versión 3.11 a 3.13)" ;;
  esac
  exit 1
fi
if ! command -v "$PY" >/dev/null 2>&1; then echo "No encuentro el Python indicado: $PY"; exit 1; fi
VER="$($PY -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "Python elegido: $PY ($VER) en $(command -v "$PY")"
case "$VER" in
  3.11|3.12|3.13) ;;
  2.*|3.[0-9]|3.10)
     echo "AVISO: Python $VER. El programa necesita 3.11 o más. Instala Python 3.13 y luego:  PYTHON=python3.13 ./setup_linux.sh" ;;
  *) echo "AVISO: Python $VER. faster-whisper (y ctranslate2) suelen tardar en publicar paquetes para versiones muy nuevas."
     echo "       Si la instalación falla, instala Python 3.13 y luego:  PYTHON=python3.13 ./setup_linux.sh" ;;
esac
echo "Distribución: $FAM"

# --- dependencias del sistema (solo se comprueban; nunca se instalan desde aquí) ---
MISSING=0
# Módulo venv: en Debian/Ubuntu viene aparte (python3-venv); sin él, `python -m venv` falla al instalar pip.
if ! "$PY" -c 'import venv, ensurepip' >/dev/null 2>&1; then
  MISSING=1
  echo "FALTA: el módulo venv/ensurepip de Python $VER (necesario para crear ./.venv)."
  case "$FAM" in
    debian) echo "  Instálalo con:  sudo apt install python3-venv   (o python${VER}-venv si usas esa versión)" ;;
    fedora) echo "  Instálalo con:  sudo dnf install python3" ;;
    arch)   echo "  Instálalo con:  sudo pacman -S python" ;;
    suse)   echo "  Instálalo con:  sudo zypper install python3" ;;
    *)      echo "  Instala el paquete venv de Python de tu distribución." ;;
  esac
fi
# PortAudio: el paquete sounddevice de Linux NO lo incluye (en Mac y Windows sí); sin él no hay micrófono.
has_portaudio() {
  if command -v ldconfig >/dev/null 2>&1 && ldconfig -p 2>/dev/null | grep -q 'libportaudio\.so'; then return 0; fi
  if [ -x /sbin/ldconfig ] && /sbin/ldconfig -p 2>/dev/null | grep -q 'libportaudio\.so'; then return 0; fi
  if command -v pkg-config >/dev/null 2>&1 && pkg-config --exists portaudio-2.0 2>/dev/null; then return 0; fi
  local d
  for d in /usr/lib /usr/lib64 /usr/lib/x86_64-linux-gnu /usr/lib/aarch64-linux-gnu /usr/local/lib; do
    ls "$d"/libportaudio.so* >/dev/null 2>&1 && return 0
  done
  return 1
}
if ! has_portaudio; then
  MISSING=1
  echo "FALTA: PortAudio (lo usa sounddevice para el micrófono)."
  case "$FAM" in
    debian) echo "  Instálalo con:  sudo apt install libportaudio2   (si algo pide compilar: sudo apt install portaudio19-dev)" ;;
    fedora) echo "  Instálalo con:  sudo dnf install portaudio" ;;
    arch)   echo "  Instálalo con:  sudo pacman -S portaudio" ;;
    suse)   echo "  Instálalo con:  sudo zypper install portaudio" ;;
    *)      echo "  Instala el paquete portaudio (la biblioteca, no solo las cabeceras) de tu distribución." ;;
  esac
else
  echo "PortAudio: encontrado"
fi
# Voz del ingeniero: opcional (sin ella los avisos salen por pantalla), por eso no detiene la instalación.
if command -v espeak-ng >/dev/null 2>&1 || command -v espeak >/dev/null 2>&1; then
  echo "Voz del ingeniero: espeak-ng encontrado"
elif command -v spd-say >/dev/null 2>&1; then
  echo "Voz del ingeniero: spd-say encontrado (espeak-ng suena más rápido y permite el selftest completo)"
else
  echo "RECOMENDADO: espeak-ng para la voz del ingeniero (sin él, los avisos solo salen por pantalla)."
  case "$FAM" in
    debian) echo "  Instálalo con:  sudo apt install espeak-ng" ;;
    fedora) echo "  Instálalo con:  sudo dnf install espeak-ng" ;;
    arch)   echo "  Instálalo con:  sudo pacman -S espeak-ng" ;;
    suse)   echo "  Instálalo con:  sudo zypper install espeak-ng" ;;
    *)      echo "  Instala el paquete espeak-ng de tu distribución." ;;
  esac
fi

if [ "${DRY:-}" = "1" ]; then
  echo
  echo "DRY=1: no instalo nada. Haría: crear ./.venv con $PY e instalar numpy, sounddevice y faster-whisper."
  [ "$MISSING" = "1" ] && echo "Antes instala lo marcado como FALTA (comandos de arriba)."
  exit 0
fi
if [ "$MISSING" = "1" ]; then
  echo
  echo "Instala primero lo marcado como FALTA (comandos de arriba, con sudo: los ejecutas tú) y repite ./setup_linux.sh"
  exit 1
fi

if [ -d .venv ]; then
  OLD="$(.venv/bin/python -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "?")"
  if [ "$OLD" != "$VER" ]; then echo "El entorno .venv era de Python $OLD: lo recreo con $VER (se borra solo ./.venv)"; rm -rf ./.venv; fi
fi
[ -d .venv ] || "$PY" -m venv .venv || { echo "No pude crear el entorno virtual (¿falta python3-venv?)"; exit 1; }
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip >/dev/null
echo "== micrófono"; python -m pip install numpy sounddevice || { echo "FALLÓ numpy/sounddevice"; exit 1; }
python -c 'import sounddevice' >/dev/null 2>&1 || echo "AVISO: sounddevice instalado pero no carga (¿PortAudio?). Revisa los comandos de arriba."
echo "== faster-whisper"; python -m pip install faster-whisper || echo "(faster-whisper no se pudo instalar)"
echo; echo "== instalado:"; python -m pip list 2>/dev/null | grep -i -E "^(numpy|sounddevice|faster-whisper|ctranslate2) " || true
echo
echo "Listo. Activa el entorno con:  source .venv/bin/activate"
echo "Primer paso:                    python voice.py selftest --say-test"
echo "Sin micrófono:                  python voice.py lab --text --file captures/demo_corta.f1raw"
echo "Con micrófono:                  python voice.py lab --live --model small   (añade --no-voice si la voz da problemas)"
echo "(antes, si no tienes capturas:  python make_demo.py all --out captures)"
