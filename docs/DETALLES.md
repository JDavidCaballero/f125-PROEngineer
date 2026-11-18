# Detalles del motor, la voz y la instalación

Material de referencia que antes estaba en el README. Lo básico para instalar y usar está en el [README](../README.md).

## Qué hace según la longitud de la carrera
| Tramo | Qué calcula |
|---|---|
| sprint ≤ 8 vueltas | Sin paradas. Huecos, DRS, defensa, ERS, combustible. Solo habla de estrategia ante SC, daño o lluvia |
| corta ≤ 20 | 0 o 1 parada, undercut, reacción al SC, regla de dos compuestos |
| media ≤ 35 | 1 o 2 paradas, ventana de neumáticos, rivales |
| larga | 1–3 paradas, más escenarios, clima |

## Cómo habla (silencio por defecto)
- **Propuestas** (`dialog.py`): solo salen si la mejora es material (`conservador` 1.0 / `normal` 0.7 / `arriesgado` 0.4 pts), se mantienen
  2 evaluaciones, no repiten una que rechazaste ni una pendiente, y esperan a que estés en una recta (salvo urgencia: SC, daño, bandera roja,
  sanción, lluvia). Cada una lleva el motivo con números ("Es obligatorio...", "Acabarías por delante de X (97%)", "Parar cuesta 22 s").
  Respondes **sí** (se vigila el plan y te recuerda la vuelta), **no** (se silencia), **explícame** (motivos y rango P10-P90) o **repite**.
- **Objetivos**: "quiero acabar quinto" / "necesito pasar a Piastri" → ritmo requerido ("necesitas vueltas de 1:33.2, ahora haces 1:33.9"),
  estado *en camino / en riesgo / inalcanzable* con histéresis (2 evaluaciones), y si es inalcanzable baja la escalera (asegurar tu puesto).
  Al pasar al rival: "Objetivo cumplido... vamos bien encaminados".
- **Tú empiezas**: `assistant.py` clasifica tu frase en una intención fija con reglas (sin acentos, tolera "Hanson"→Hansen); si fallan, el LLM solo
  *clasifica* y el piloto que nombre se valida contra la parrilla. Si no está claro: "No he entendido. ¿Puedes repetirlo?" (nunca asume).
  Si el nombre es ambiguo pregunta cuál. "No me siento cómodo" pregunta con qué; "no tengo agarre"/"no me siento cómodo con las gomas" entra
  como parámetro (−0.3 s/vuelta y límite de desgaste −10 pts hasta cambiar neumáticos) y recalcula. "Silencio" mutea lo no crítico.
- UDP es solo de salida: **"aceptar" es un compromiso que el programa vigila, no una orden al coche**.

## Estrategia (`strategy.py`)
Planes (seguir fuera, parar en la vuelta X a un compuesto, 2 paradas, "parar solo si sale el SC") simulados con Monte Carlo sobre **los 22 coches**
(SC/VSC, bandera roja, ritmo y desgaste, paradas de la IA —compuesto aprendido de tus capturas—, tráfico, daño). Devuelve P(victoria/podio),
puntos esperados, **P de acabar por delante de cada rival**, rango de llegada y **razones con números**. Modos: solo cambian la utilidad
(media de potencia de los puntos). El motor tiene guardas contra datos del juego incoherentes y propaga la incertidumbre del propio modelo.

## Ahorro de combustible por zonas (`fuelzones.py`)
Divide la pista en 40 tramos y, vuelta a vuelta, mide combustible, tiempo y acelerador de cada uno; con tu propia variación entre vueltas ajusta
cuántos kg se ahorran y cuántos segundos cuestan, y recomienda los de más kg/s ("Levanta antes en el tramo 22 (~2270 m): ahorras ~0.007 kg por
vuelta y pierdes ~0.004 s"). Sin variación (nunca levantas) **no inventa cifras**: solo marca candidatas antes de frenadas. Guarda lo aprendido por pista.
F1 25 solo tiene mezcla normal y pobre: no hay avisos de "mezcla rica".

## Casos probados (`make_demo.py`)
sprint · corta (SC, combustible justo) · media (daño de alerón, lluvia) · **sc_final** (SC en las últimas 3 vueltas: no propone parar) ·
**sc_tras_parada** · **vsc** · **pinchazo** · **roja** (parada gratis) · **sancion** (drive-through pendiente + SC) · **rival_fresco** ·
**datos_perdidos** (20% de paquetes) · **ahorro** (tramos con ahorro plantado que debe encontrar). Pendientes (requieren modelado nuevo): pista mojada
(cruce intermedio/slick), juegos de neumáticos agotados, overcut en tráfico.

## Qué mide el backtest (y qué no)
`backtest.py` toma, en cada vuelta, la opción que coincide con lo que el piloto hizo después y compara con el resultado real: error de posición,
cobertura del rango P10-P90 (ideal ≈80%), Brier de "¿acabo por delante de X?", fiabilidad por tramos, estabilidad del consejo y mensajes por vuelta.
No dice qué habría pasado con otro plan. Resultados sobre carreras sintéticas (no sobre carreras reales largas, que no tengo):
- sin paradas (8 sprints): error 0.33 posiciones, cobertura 94%, sesgo 0.03 → bien calibrado.
- con paradas (corta+media): error 1.6, cobertura 68%, sesgo +1.5 (pesimista). Aprendiendo de otras 4 carreras (`learn.py`): error 1.3, sesgo +1.2.
  Es el orden de magnitud a vigilar en carreras reales; no ajusto el motor a mi demo (es circular): hacen falta capturas largas tuyas.

## Qué está medido y qué es estimado
- **Validado con tu captura real** (Singapur, 5 vueltas): paquetes, LapData, Session, eventos, clasificación, historial, juegos de neumáticos,
  desgaste del Blando (~2.3 %/vuelta), `k` (ritmo por % de desgaste, medido en vivo ~0.02 s/%), tiempos de parada reales del pit lane.
- **Estimado** (config.toml, marcado): acantilado del neumático, coste de parada por defecto (22 s; en Singapur es ~30), frecuencia de SC, cuándo para la IA,
  pérdida por daño (aleron 0.015 s/%, neumático 0.01 s/%), por "no tengo agarre" (0.3 s), temperaturas máximas, regla de dos compuestos desde 12 vueltas.
- **No verificado**: formato UDP de F1 26 (el motor avisa si no es 2025), cómo termina una bandera roja (heurística: el coche vuelve a >100 km/h),
  si un drive-through se puede cumplir en una parada normal, el ahorro de combustible real (probado con estructura plantada).
- Pista mojada: no se simula (solo reglas de cambio a intermedios).

## Voz (laboratorio, `LEEME_VOZ.md`)
`voice.py lab` une micrófono → Whisper (mlx o faster, intercambiables; `bench` mide cuál va mejor en tu Mac) → `ask()` → voz del sistema (`say` en Mac, SAPI en Windows, espeak-ng en Linux), con la radio de
respaldo (`radio.py`): "no he entendido, repite" hasta entender; con una propuesta pendiente y 2 fallos seguidos pasa al respaldo (silencio 7 s = no, un toque = sí).
`speaker.py`: cola con prioridades y corte al hablar tú. Whisper alucina con silencio/ruido: se descartan resultados con poca voz, poca confianza o frases típicas.
`voice.py script` mide cuántas de 48 frases entiende y clasifica bien. **Caja negra** (`blackbox.py`): cada ejecución crea `runs/session-*/` con consola
(errores y tracebacks, también de hilos), sistema y versiones, registro de voz y decisiones, y deja un único `INFORME.txt` (texto, sin audio) para auditar.
`selftest` comprueba Whisper, el micrófono (`sounddevice`) y el motor de voz de tu sistema.
Botón del mando para hablar (p. ej. R3): `python voice.py boton` lo aprende de la telemetría UDP del juego (evento BUTN), sin instalar nada en Windows; funciona en los tres sistemas (ver LEEME_VOZ.md §4b). El micrófono es el del equipo donde corre el programa; si juego y programa están en el mismo PC, IP `127.0.0.1` en el juego.

## Windows / Linux: detalles
**Los comandos del ingeniero son los mismos en los tres sistemas**; solo cambia cómo se instala y se activa el entorno la primera vez:

| Paso | Mac | Windows (PowerShell) | Linux |
|---|---|---|---|
| Requisitos del sistema (1 vez) | — | — (la voz SAPI viene con Windows) | `sudo apt install libportaudio2 espeak-ng python3-venv` (o `dnf`/`pacman`) |
| Instalar | `./setup_mac.sh` | `Set-ExecutionPolicy -Scope Process Bypass` y `.\setup_windows.ps1` | `./setup_linux.sh` |
| Activar el entorno (cada vez) | `source .venv/bin/activate` | `.\.venv\Scripts\Activate.ps1` | `source .venv/bin/activate` |
| Comprobar | `python voice.py selftest --say-test` | igual | igual |
| Aprender el botón (R3) | `python voice.py boton` | igual | igual |
| Sesión completa | `python voice.py lab --live` | igual | igual |

Por debajo: voz del ingeniero con `say` (Mac), SAPI (Windows) o `espeak-ng` (Linux); tu voz con mlx-whisper `medium` (Mac) o
faster-whisper `small` en CPU (Windows/Linux; `--model medium` para forzar). Juego y programa en el mismo PC: IP `127.0.0.1` en el juego.

Hay instaladores equivalentes a `setup_mac.sh` (venv en `./.venv`, sin sudo ni administrador, salida en `setup.log`), Instalan numpy, sounddevice y faster-whisper (mlx-whisper es solo para Mac).
```bash
./setup_linux.sh                 # Linux. DRY=1 ./setup_linux.sh solo comprueba; PYTHON=python3.12 fuerza versión
```
```powershell
Set-ExecutionPolicy -Scope Process Bypass   # solo para esta ventana
.\setup_windows.ps1                         # Windows. -DryRun solo comprueba; -Python "py -3.12" fuerza versión
.\.venv\Scripts\Activate.ps1
```
- En Linux el micrófono necesita **PortAudio** del sistema (`sudo apt install libportaudio2`, `sudo dnf install portaudio`, `sudo pacman -S portaudio`)
  y en Debian/Ubuntu el módulo `python3-venv`; el script lo comprueba y te dice el comando, pero no ejecuta sudo.
- **Voz del ingeniero (salida), disponible en Windows y Linux**: se elige sola por sistema (`[voice] tts = "auto"`
  en `config.toml`; también `say`, `sapi`, `espeak`, `spd` o `none`). Mac: `say` (sin cambios). Windows: voces SAPI con PowerShell
  (vienen con Windows; para una voz en español: Configuración > Hora e idioma > Voz). Linux: `espeak-ng` (`sudo apt install espeak-ng`)
  o, si no, `spd-say`. Si no hay motor, lo avisa una vez y sigue como con `--no-voice` (el texto sale por pantalla).
- **Tu voz (entrada)**: en Windows/Linux `--stt auto` usa faster-whisper (mlx solo existe en Mac con Apple Silicon) y, sin `--model`,
  el modelo `small` (en Mac sigue `medium`): `python voice.py lab --live --model small`. Sin micrófono: `--text`.
- `python voice.py selftest --say-test` prueba voz, micrófono y Whisper también ahí (con `spd-say` se omite el viaje voz→texto:
  no sabe generar audio). Al cerrar, la carpeta del informe se abre con el Explorador (Windows) o `xdg-open` (Linux).
- Telemetría UDP: el juego debe enviar a la IP de este equipo y al puerto de `[network] port` (20777). Si no llega nada, revisa el
  cortafuegos (Windows: permite Python en redes privadas; Linux: `ufw`/`firewalld` si están activos, UDP 20777).

### Un solo PC (juego + programa + LLM): el LLM en CPU
Si el juego y el ingeniero comparten equipo, **el LLM no debe usar la tarjeta gráfica**: le quitaría fotogramas al juego. Este programa no
carga el modelo (lo hace LM Studio), así que hay que dejarlo en CPU **en LM Studio** y comprobarlo antes de jugar:
1. Al cargar el modelo, en *GPU Offload* pon **0** (todas las capas en CPU). En la pestaña *My Models* → ajustes del modelo puedes dejarlo guardado.
   Si tu versión de LM Studio usa otro nombre o ubicación, busca "GPU Offload"; no lo he podido comprobar en tu versión.
2. **Comprueba** con el modelo cargado y una pregunta en curso: el consumo de **memoria de vídeo (VRAM)** no debe subir.
   Windows: Administrador de tareas → Rendimiento → GPU (*Memoria de GPU dedicada*). NVIDIA: `nvidia-smi`. Linux AMD/Intel: `nvtop` o `radeontop`.
3. Mide el efecto en el juego (fotogramas con y sin el LLM cargado); no lo des por bueno sin medirlo.
- La CPU también se reparte con el juego: un modelo de 9B en CPU **responde más lento que en GPU** (no medido aquí; en el Mac se midió con GPU).
  Si notas tirones o latencia alta, usa un modelo más pequeño, limita los hilos de CPU de LM Studio, o ejecuta **sin LLM** (`--no-llm`: todo funciona con reglas).
- Lo ideal sigue siendo repartir: juego en un PC y ingeniero + LLM en otro (en `config.toml`, `[llm] url` con la IP de ese equipo).

## La IA (LLM)
El código calcula y decide; el modelo solo (1) clasifica tu frase cuando las reglas fallan, (2) elige entre opciones ya calculadas y las dentro del
margen del modo, y redacta el aviso, siempre con validador de cifras. Sin servidor todo funciona igual. No se entrena el LLM: "aprender" es
`learn.py` (coste de parada, desgaste, frecuencia de SC, compuestos de la IA, zonas de ahorro) y `runs/*.jsonl` (decisiones, silencios, lo que oyó).
Falta: voz (push-to-talk del control → Whisper → este mismo `ask()` → voz sintética → auriculares del PC) y el canal de respaldo
("si callas = no, si pulsas radio otra vez = sí").

## Archivos
`f1_parse.py` parser · `state.py` estado · `strategy.py` motor · `tactics.py` alertas · `dialog.py` filtro, propuestas, objetivos ·
`assistant.py` intenciones · `fuelzones.py` ahorro · `llm.py` IA · `engineer.py` programa · `learn.py` · `backtest.py` · `make_demo.py` ·
`f1_udp.py` captura/replay · `config.toml` · `tests/`.
