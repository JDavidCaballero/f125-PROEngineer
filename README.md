# Ingeniero de pista F1 25 (local)

Ingeniero de pista para **F1 25**: lee la telemetría UDP del juego, calcula estrategia y te habla por voz. Corre en tu equipo: Windows, Mac o Linux.
Opcionalmente usa una IA local (LM Studio) para entender mejor tu voz; sin IA funciona igual con reglas.

> Proyecto personal, no oficial y sin relación con EA, Codemasters ni F1. «F1» y «F1 25» son marcas de sus dueños.

> 🚧 **Prototipo en desarrollo activo.** Se sigue puliendo y mejorando, y entre versiones pueden cambiar cosas. Hoy el programa habla español; se está trabajando para que funcione en más idiomas.

## Requisitos
- **Python 3.11 o superior.**
- **F1 25** con el formato de telemetría **2025** (ver «Configurar el juego»).
- Micrófono y auriculares (o `--text` para escribir las preguntas).
- Opcional: [LM Studio](https://lmstudio.ai) para la IA.

## Instalar
| Paso | Mac | Windows (PowerShell) | Linux |
|---|---|---|---|
| Requisitos del sistema (1 vez) | — | — | `sudo apt install libportaudio2 espeak-ng python3-venv` (o `dnf`/`pacman`) |
| Instalar | `./setup_mac.sh` | `Set-ExecutionPolicy -Scope Process Bypass` y `.\setup_windows.ps1` | `./setup_linux.sh` |
| Activar el entorno (cada vez) | `source .venv/bin/activate` | `.\.venv\Scripts\Activate.ps1` | `source .venv/bin/activate` |

El instalador crea el entorno en `./.venv` sin pedir administrador y guarda su salida en `setup.log`.
Con `DRY=1 ./setup_linux.sh` (o `-DryRun` en Windows) solo comprueba, sin instalar.

## Configurar el juego
En F1 25: *Ajustes › Telemetría*:
- **UDP Telemetry: On**
- **UDP Format: 2025** (con otro formato los datos salen mal y el programa avisa)
- **UDP IP:** la IP del equipo donde corre el programa (`127.0.0.1` si es el mismo PC que el juego)
- **UDP Port: 20777**

## Primer uso
```bash
python voice.py selftest --say-test   # comprueba voz, micrófono y Whisper
python voice.py boton                 # aprende el botón del mando para hablar (p. ej. R3), una sola vez
python voice.py lab --live            # sesión completa: mantén el botón y habla
```
Al cerrar, se guarda una carpeta con `INFORME.txt` y los paquetes del juego (útil para reportar un fallo). Para probar sin juego: `python engineer.py --file captures/demo_corta.f1raw --speed 0 --verbose` (antes: `python make_demo.py all --out captures`).

## IA opcional (LM Studio)
1. En LM Studio carga un modelo, abre *Developer › Start Server* y, si el programa va en otro equipo, activa **Serve on Local Network**.
2. `cp config.local.example.toml config.local.toml` y pon tus datos (no edites `config.toml`):
   ```toml
   [llm]
   url   = "http://127.0.0.1:1234/v1"   # el servidor; termina en /v1
   model = "qwen/qwen3.5-9b"            # nombre exacto del modelo (`python config.py` los lista)
   ```
3. `python config.py` comprueba la conexión; `python voice.py llm` mide cómo responde tu modelo.

**Si juegas y usas el programa en el mismo PC**, deja el modelo en CPU (en LM Studio, *GPU Offload* = 0) para no quitarle fotogramas al juego. Si va lento o con tirones, usa un modelo más pequeño o `--no-llm`.
Todos los ajustes: [`docs/CONFIGURACION.md`](docs/CONFIGURACION.md).

## Si algo falla
| Síntoma | Causa más probable |
|---|---|
| «No llegan datos del juego» | IP o puerto del juego mal puestos, formato UDP distinto de 2025, o el cortafuegos bloquea UDP 20777 (Windows: permite Python en redes privadas) |
| El micrófono no graba o sale en silencio | Permiso de micrófono: Mac, *Ajustes del Sistema › Privacidad y seguridad › Micrófono*; en Linux, falta `libportaudio2` |
| No se oye al ingeniero | Mac: `say`; Windows: voces SAPI (para español, *Configuración › Hora e idioma › Voz*); Linux: `espeak-ng` |
| La IA tarda o falla | Sube `timeout_s` en `[llm]` (20–30), usa un modelo más pequeño, o `--no-llm` |

## Más información
- [`docs/CONFIGURACION.md`](docs/CONFIGURACION.md): ajustes y problemas frecuentes · [`LEEME_VOZ.md`](LEEME_VOZ.md): voz y botón del mando
- [`docs/DETALLES.md`](docs/DETALLES.md): estrategia, voz y notas de instalación por sistema · [`docs/ARQUITECTURA.md`](docs/ARQUITECTURA.md): mapa del código
- [`docs/PENDIENTES.md`](docs/PENDIENTES.md): estado y pendientes · [`docs/HISTORIAL.md`](docs/HISTORIAL.md): cambios por ronda · [`CONTRIBUTING.md`](CONTRIBUTING.md): cómo colaborar

## Licencia
**GPL-3.0** (ver [`LICENSE`](LICENSE)). Copyright © 2026 Juan David Caballero. Puedes usar, estudiar y modificar el programa; si distribuyes una versión modificada, debe seguir siendo GPL-3.0, publicar su código y mantener este aviso de autoría.

---
**English:** this is a work-in-progress prototype for F1 25 (telemetry-based race engineer with voice). It is being actively improved and polished to work in more languages — for now the voice, phrases and docs are in Spanish. Feedback and contributions are welcome: see [`CONTRIBUTING.md`](CONTRIBUTING.md). Licensed under GPL-3.0.
