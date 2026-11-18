# AGENTS.md — contexto para asistentes de IA (Copilot, Claude, Cursor, etc.)

Léelo entero antes de tocar código. Las personas que trabajan aquí hablan **español**: documentación, mensajes al usuario y commits en español.

## Qué es esto y cómo se trabaja
- Proyecto **personal y de aprendizaje** (no oficial, sin relación con EA/Codemasters): un ingeniero de pista local para **F1 25**. Recibe telemetría UDP del juego, calcula avisos y estrategia, escucha al piloto (Whisper) y le habla, con un LLM local (LM Studio) solo para entender y redactar.
- El repo es público. Cada colaborador trabaja en **su propio fork** y propone cambios al repo original con un **Pull Request**. No se hace push directo a `main` del original.
- Trabaja en una **rama** (`git switch -c windows-voz`), con commits pequeños. Antes de abrir el PR corre las pruebas (abajo).
- **Nunca subas**: capturas (`*.f1raw`, `*.f1raw.gz`), audios (`*.wav`), `runs/`, `data/profile.json`, `config.local.toml`, ni tokens, claves o rutas personales. Ya están en `.gitignore`; no lo quites.
- Lee antes: `README.md`, `docs/ARQUITECTURA.md` (mapa de módulos), `docs/CONFIGURACION.md`, `CONTRIBUTING.md`, `docs/PENDIENTES.md`.

## Reglas de diseño (no se negocian)
1. **El código calcula; el LLM solo entiende y redacta.** Toda cifra, nombre o posición sale de los datos del juego. Si el modelo inventa o cambia algo, `brain.py` lo descarta.
2. Separa siempre lo **medido** de lo **estimado** en lo que dice el ingeniero. No inventes datos, reglas del juego ni campos del protocolo UDP.
3. El motor (UDP, estado, estrategia, avisos) usa **solo la librería estándar** de Python 3.11+. No añadas dependencias al núcleo; las de voz van aparte (`requirements.txt`, scripts de instalación).
4. Cambios **aditivos y por plataforma**: lo que ya funciona en Mac no debe romperse. Elige el comportamiento por plataforma (`sys.platform`) o por `config.toml`.
5. Los tests no deben necesitar micrófono, altavoz, LLM ni el juego. Para lo que toque el sistema operativo, inyecta la dependencia (como ya hace `Speaker(popen=...)`).

## Estado actual de la plataforma (verificado leyendo el código)
Hoy solo está probado en **macOS**. El resto del núcleo es independiente del sistema operativo en principio, pero **no se ha ejecutado en Windows ni Linux**; el CI de GitHub (`.github/workflows/tests.yml`, Ubuntu) no se ha confirmado en verde.

Dependencias de macOS encontradas:
| Dónde | Qué | Qué falta |
|---|---|---|
| `speaker.py`: `choose_backend` y `Speaker._run` | Mac `say`; **implementado, sin probar en Windows/Linux reales**: SAPI (PowerShell + System.Speech, texto por stdin), `espeak-ng`/`espeak`, `spd-say`; `[voice] tts` | Probarlo en máquinas reales (voz, corte al hablar, retraso de arranque de PowerShell) |
| `voice.py`: `synth_wav` y `cmd_selftest` | Usan el motor de cada sistema; sin motor o con `spd-say` (no genera WAV) avisan y omiten | Probarlo en máquinas reales |
| `voice.py`: `open_folder` | `open` (Mac), `os.startfile` (Windows), `xdg-open` (Linux); nunca falla | Probarlo en máquinas reales |
| `stt.py` | `auto`: mlx en Mac con Apple Silicon, faster-whisper (CPU, int8; modelo `small` por defecto) en el resto | Probar faster-whisper en Windows/Linux; GPU no se usa |
| `setup_mac.sh` | `setup_windows.ps1` y `setup_linux.sh` escritos, sin probar en máquinas reales | Ejecutarlos en Windows y Linux |
| Pruebas | Se corren con `< /dev/null` (entrada cerrada) | Equivalente en PowerShell (`< NUL`) |

El micrófono usa `sounddevice` + `numpy` (multiplataforma). El speaker espera un proceso externo que hable el texto y se pueda terminar para interrumpir.

## Tarea: portar a Windows y Linux (voz de entrada y salida)
Objetivo: que `python voice.py lab --live` funcione con **micrófono (push-to-talk), voz del ingeniero y LLM opcional** en Windows 10/11 y en Linux, además de Mac.

Plan sugerido (un PR por punto si es posible):
1. **Abstraer la voz de salida** en `speaker.py`: una pequeña interfaz de motor (construir el comando o proceso, listar voces en español, interrumpir). Mac = `say` (sin cambios de comportamiento). Windows = voces SAPI vía `pyttsx3` o PowerShell `System.Speech`. Linux = `espeak-ng`, `spd-say` o `piper`. Elegir por `sys.platform` con opción en `[voice]` de `config.toml`. Mantener que `to_speech()` (números a palabras) se aplica antes de hablar.
2. **Interrupción y cola**: `Speaker` corta el habla en curso y descarta la cola (`stop`/`_kill`). Comprueba que con cada motor el proceso se puede terminar y que `wait()` y `hold()/release()` siguen funcionando.
3. **Entrada**: probar `--stt faster` con `faster-whisper` (CPU y, si hay, GPU) y `sounddevice` en Windows y Linux. Documentar modelo recomendado y selección del dispositivo de micrófono (`Recorder.devices()`).
4. **Utilidades del sistema**: abrir la carpeta del informe por plataforma (`export_for_sharing` en `voice.py`); revisar rutas (`pathlib`, nada de `/`), `~/Downloads` en Windows con OneDrive, y `synth_wav`/selftest sin `say`.
5. **Instalación**: `setup_windows.ps1` y `setup_linux.sh` equivalentes a `setup_mac.sh` (venv en `./.venv`, sin sudo, log en `setup.log`). Documentar en el README una sección "Windows / Linux".
6. **CI**: ampliar `.github/workflows/tests.yml` a una matriz `ubuntu-latest` y `windows-latest` (Python 3.11–3.13). Las pruebas que requieren audio o LLM ya se omiten si no están.
7. **Documentar** lo probado de verdad y lo que no, en el README y en `docs/PENDIENTES.md`. No afirmes que funciona en una plataforma si no lo ejecutaste.

Criterios de aceptación:
- `python -m unittest discover -s tests < /dev/null` (Git Bash/Linux/Mac) o `< NUL` (PowerShell/cmd) pasa; ~560 pruebas, algunas se omiten sin captura real o LLM.
- `python voice.py selftest` pasa en la plataforma nueva o explica claramente qué falta.
- Sin regresiones en Mac: el comportamiento por defecto en `darwin` no cambia.
- `python voice.py lab --live --no-llm --no-voice --text` funciona en las tres plataformas.

## Cómo ejecutar
- Todos los comandos de voz empiezan por `lab`: `python voice.py lab --live` (completo: voz, micro y LLM), `--no-llm` (solo reglas), `--no-voice` (solo texto), `--text` (escribir en vez de hablar), `--file captures/x.f1raw` (reproducir captura), `--stt faster` (Whisper multiplataforma), `--model small`.
- `python voice.py selftest` comprueba las piezas de voz.
- `python config.py` muestra la configuración efectiva. Los ajustes personales (modelo, IP del LLM) van en `config.local.toml` (ignorado por git; plantilla en `config.local.example.toml`). Detalles en `docs/CONFIGURACION.md`.
- Al cerrar `lab --live` se crea `~/Downloads/f1-informe-<sesión>/` con `INFORME.txt` y `udp.f1raw.gz`. Son lo que se pide para diagnosticar fallos: pídelo a quien reporte un error.
- `python make_demo.py all` genera capturas de demostración sin necesitar el juego.

## Juego y protocolo UDP
- El parser está validado **solo con F1 25 (formato UDP 2025)**. Si el juego manda otro formato (por ejemplo 2026), el motor lo avisa y los datos salen **corruptos** (cifras imposibles como 2816 km/h). No es un fallo de visualización. Para soportar otro formato hace falta la especificación oficial o capturas reales de ese formato; no adivines la estructura.
- Los paquetes con `session_uid == 0` son ruido de menús y se ignoran a propósito.
- Con un formato distinto de 2025 el motor no da avisos y dice cómo cambiarlo en el juego (lo vimos en Windows: formato 2026 → 2816 km/h).
- Puerto UDP por defecto 20777. El juego debe enviar al equipo que ejecuta Python con el formato 2025 (IP del equipo y puerto `[network] port` de `config.toml`).

## Estilo
- Escribe código que se lea como el que lo rodea: nombres, comentarios (en español, breves, explican el porqué) y estructura.
- Mensajes de commit en español, en imperativo y concretos. No mezcles refactors con cambios de comportamiento.
- Si algo no puedes probarlo en tu sistema, dilo en el PR en vez de suponerlo.
