#!/usr/bin/env bash
# Instala lo necesario para el laboratorio de voz en TU Mac. No usa sudo y no toca nada fuera de esta carpeta.
# LÉELO ANTES DE EJECUTARLO. Qué hace:
#   1) elige Python 3.13, 3.12 o 3.11 si existen (los motores de voz suelen tardar en soportar versiones muy nuevas)
#   2) crea un entorno virtual en ./.venv (si había uno de otra versión de Python, lo borra y lo recrea: solo esa carpeta)
#   3) instala numpy y sounddevice (micrófono) y uno o ambos motores de voz a texto
#   4) guarda TODA la salida en ./setup.log (adjúntalo a un issue si algo falla)
# Uso:  ./setup_mac.sh            (ambos motores; si uno falla, sigue con el otro)
#       ./setup_mac.sh mlx        (solo mlx-whisper)      ./setup_mac.sh faster   (solo faster-whisper)
#       PYTHON=python3.12 ./setup_mac.sh                  (forzar una versión)
set -uo pipefail
cd "$(dirname "$0")"
exec > >(tee setup.log) 2>&1

choose_python() {
  if [ -n "${PYTHON:-}" ]; then echo "$PYTHON"; return; fi
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1; then echo "$c"; return; fi
  done
}
PY="$(choose_python)"
[ -z "$PY" ] && { echo "No encuentro Python 3. Instálalo (brew install python@3.13) y repite."; exit 1; }
VER="$($PY -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "Python elegido: $PY ($VER) en $(command -v "$PY")"
case "$VER" in
  3.11|3.12|3.13) ;;
  *) echo "AVISO: Python $VER. mlx-whisper y faster-whisper suelen tardar en publicar paquetes para versiones muy nuevas."
     echo "       Si la instalación falla: brew install python@3.13  y luego:  PYTHON=python3.13 ./setup_mac.sh" ;;
esac
[ "${DRY:-}" = "1" ] && exit 0

if [ -d .venv ]; then
  OLD="$(.venv/bin/python -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "?")"
  if [ "$OLD" != "$VER" ]; then echo "El entorno .venv era de Python $OLD: lo recreo con $VER (se borra solo ./.venv)"; rm -rf .venv; fi
fi
[ -d .venv ] || "$PY" -m venv .venv || { echo "No pude crear el entorno virtual"; exit 1; }
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip >/dev/null
echo "== micrófono"; python -m pip install numpy sounddevice || { echo "FALLÓ numpy/sounddevice"; exit 1; }
WHICH="${1:-both}"
if [ "$WHICH" = "mlx" ] || [ "$WHICH" = "both" ]; then
  echo "== mlx-whisper"; python -m pip install mlx-whisper || echo "(mlx-whisper no se pudo instalar; se prueba el otro)"
fi
if [ "$WHICH" = "faster" ] || [ "$WHICH" = "both" ]; then
  echo "== faster-whisper"; python -m pip install faster-whisper || echo "(faster-whisper no se pudo instalar)"
fi
echo; echo "== instalado:"; python -m pip list 2>/dev/null | grep -i -E "^(numpy|sounddevice|mlx|mlx-whisper|faster-whisper|ctranslate2) " || true
echo
echo "Listo. Activa el entorno con:  source .venv/bin/activate"
echo "Primer paso:                    python voice.py selftest --say-test"
echo "(la primera vez macOS pedirá permiso de micrófono para la Terminal: acéptalo)"
