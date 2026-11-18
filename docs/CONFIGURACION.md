# Guía de configuración (para quien clona el proyecto)

**Regla de oro:** no edites `config.toml` (es el valor de serie y está en git; si lo cambias, tus ajustes acaban en un commit o en un conflicto).
Copia el ejemplo y deja solo lo que quieras cambiar:

```bash
cp config.local.example.toml config.local.toml     # este archivo NO se sube a git
```
`config.local.toml` se mezcla sobre `config.toml` sección a sección: lo que no pongas, se toma del de serie. Para ver qué se está usando de verdad
y comprobar que tu servidor responde:

```bash
python3 config.py
```

## 1. El modelo (LLM): cualquiera que quieras
**Qwen3.5 9B es solo lo que hemos probado**, no una obligación: nada del código depende de un modelo concreto. Puedes usar el que quieras (otro tamaño,
otra familia, uno tuyo) siempre que lo sirva algo compatible con la API de OpenAI (`/v1/models` y `/v1/chat/completions`). Lo único que se cambia es **este archivo**:

### En 2 minutos
1. `cp config.local.example.toml config.local.toml`
2. Abre `config.local.toml` y pon **dos cosas** en `[llm]`:
   ```toml
   [llm]
   url   = "http://127.0.0.1:1234/v1"   # (1) la dirección del servidor
   model = "qwen/qwen3.5-9b"            # (2) el nombre del modelo que quieras usar
   ```
3. `python3 config.py` → comprueba que el servidor responde y te lista los nombres de modelo válidos (copia el que quieras en `model`).
4. `python3 voice.py llm` → mide cómo lo hace tu modelo con ~50 frases (herramienta correcta, JSON válido, latencia).

### (1) La dirección: `url`
- **Mismo equipo que el programa:** `http://127.0.0.1:1234/v1` (puerto por defecto de LM Studio).
- **Otro equipo de tu red** (p. ej. el modelo corre en un PC potente): su IP, `http://192.168.1.50:1234/v1`. En LM Studio: *Developer → Start Server* y
  **Serve on Local Network**; si no responde, el cortafuegos de ese equipo. IP en Mac: `ipconfig getifaddr en0`.
- **Otro programa servidor:** Ollama (`http://127.0.0.1:11434/v1`), llama.cpp (`http://127.0.0.1:8080/v1`), vLLM, etc. Deberían servir porque solo usan esos dos endpoints,
  pero **solo LM Studio está probado**; si pruebas otro, apúntalo en `docs/PENDIENTES.md`. Debe terminar en `/v1`.

### (2) El modelo: `model`
El **nombre exacto** que devuelve el servidor (`python3 config.py` lo lista). Con `model = ""` LM Studio usa el modelo que tenga cargado (otros servidores, como Ollama, exigen el nombre).

### Y otros ajustes de `[llm]` según tu modelo
| Ajuste | Para qué | Cuándo tocarlo |
|---|---|---|
| `timeout_s` (12) | segundos que se espera una respuesta antes de seguir con reglas | modelo lento o equipo cargado: sube a 20–30 |
| `primary` (true) | el modelo entiende todas las frases y elige herramientas; `false` = solo ayuda cuando las reglas no entienden | modelo pequeño que se equivoca mucho: pon `false` |
| `compose` (true) | el modelo redacta con naturalidad las respuestas (validadas: si cambia una cifra o un nombre, se descarta) | si `python3 voice.py llm` muestra muchas «redacciones rechazadas», pon `false` |
| `enabled` (true) | `false` = sin modelo, solo reglas (también `--no-llm`) | para comparar o si no tienes servidor |
| `temperature`, `max_tokens` | parámetros de la petición | rara vez; el enrutador pide ya su propia temperatura y longitud |
| `send_enable_thinking_false` | si tu servidor admite `enable_thinking: false` por la API | modelos que «piensan» y no puedes apagar *Think* en el servidor |
| `reasoning_effort` | `"none"` (por defecto) se manda en cada petición: el modelo no razona aunque el servidor tenga *Think* | `"auto"` = no mandarlo (servidores que no lo admiten) |
| `down_cooldown_s` (30) | espera tras un fallo del servidor antes de reintentar | casi nunca |

Cosas a tener en cuenta con otro modelo:
- **Debe responder JSON** cuando se le pide (es como elige herramientas). Si no lo hace, el programa lo detecta y sigue con las reglas; verás la tasa de JSON usable en `python3 voice.py llm`.
- **Modelos que «piensan»** (razonamiento): el programa ya pide `reasoning_effort = "none"` (medido el 11 nov con Qwen3.5 9B en
  LM Studio con *Think* activo: sin eso pensaba 600 tokens y 22 s sin responder; con eso, 0.5–3 s). `enable_thinking` y `/no_think`
  no lo apagaban. Razonar no compensa en carrera: con razonamiento nativo tardó 199 s sin responder; un «plan» corto no mejoró el acierto.
- Los umbrales con los que se descartan las redacciones del modelo (`COMPOSE_KEEP`, `COMPOSE_NEW` en `brain.py`) y los prompts se calibraron con Qwen3.5 9B;
  con otro modelo pueden pedir un ajuste. Los resultados medidos de Qwen quedan en `tests/data/llm_real_run8.json` para comparar. Si afinas los prompts para otro modelo, comparte la medida.
- **Sin modelo** todo funciona con reglas: solo entiende frases más rígidas y no charla.

## 2. El juego (telemetría)
En F1 25: *Ajustes → Telemetría* → UDP **activado**, formato **2025**, IP = la del equipo donde corre este programa, puerto = `[network] port` (20777).
```toml
[network]
port = 20777
```

## 3. Voz (solo macOS)
Se elige por línea de comandos, no en el TOML (salvo el motor de la voz: `[voice] tts = "auto"`, o `say`/`sapi`/`espeak`/`spd`/`none`):
```bash
python3 voice.py lab --live --model small     # Whisper: tiny | base | small | medium (por defecto) | large | turbo
python3 voice.py lab --live --stt faster      # motor: auto | mlx | faster
python3 voice.py lab --live --voice Paulina   # voz de `say` (lista: say -v ?). Sin --voice usa una española instalada
                                              # (Windows: nombre SAPI, p. ej. "Microsoft Helena Desktop"; Linux espeak-ng: es, es-419)
python3 voice.py lab --text --file captures/demo_corta.f1raw   # sin micrófono: escribes las frases
python3 voice.py lab --live --no-voice        # sin hablar, solo texto
```
Usa auriculares: si el micrófono recoge al ingeniero, esas grabaciones se descartan como «eco».

## 4. Comportamiento del ingeniero
```toml
[engineer]
mode = "normal"          # conservador | normal | arriesgado (cuánto exige para proponerte una parada)
game_year = 2025
budget_prio2 = 4         # avisos «de ambiente» por vuelta (0 = sin límite); los críticos nunca se recortan
budget_prio3 = 2
[ers]
deploy_limit_mj = 4.0    # despliegue máximo por vuelta del MGU-K; cámbialo si tu versión del juego usa otro
[strategy]
two_compound_rule = "auto"   # auto | true | false: obligatoriedad de usar dos compuestos (auto = desde 12 vueltas)
pit_loss_s = 22.0            # pérdida de una parada; se aprende/mide por circuito
```
Umbrales de avisos (`[thresholds]`), simulación (`[strategy]`) y filtro de silencio (`[dialog]`) están comentados en `config.toml`.

## 5. Archivos que genera (y cómo apagarlo)
```toml
[paths]
runs = "runs"                # carpeta de sesiones (fuera de git)
export_dir = "~/Downloads"   # al cerrar `lab --live`: carpeta con INFORME.txt + udp.f1raw.gz ("" = no crear)
record_raw = true            # grabar todos los paquetes del juego (≈100 MB/hora)
learn_at_end = true          # aprender pérdida de parada y desgaste por circuito (data/profile.json, fuera de git)
open_folder_at_end = true
```
Nada de esto se sube a git (`.gitignore`). Los informes pueden contener rutas de tu equipo: revísalos antes de compartirlos.

## 6. Pruebas con servicios reales (opcional)
`F1_REAL_CAPTURE=/ruta/carrera.f1raw` y `F1_LLM_URL=http://127.0.0.1:1234/v1` activan las pruebas que usan una captura real o el modelo real
(sin ellas se omiten). Las pruebas **ignoran** tu `config.local.toml` para dar el mismo resultado en todos los equipos.

## Si algo no funciona
| Síntoma | Qué mirar |
|---|---|
| `python3 config.py` dice «NO responde» | servidor apagado, sin modelo cargado, URL sin `/v1`, IP/puerto equivocados, cortafuegos |
| «OJO: tu `model` no está en la lista» | copia el nombre exacto de la lista, o `model = ""` |
| Responde con «No he entendido» a casi todo | el modelo no está activo (`enabled`) o va muy lento (`timeout_s`); mira `python3 voice.py llm` |
| El programa no oye al juego | IP/puerto en el juego, formato UDP 2025, `[network] port`, cortafuegos del Mac |
| `config.local.toml no es un TOML válido` | comillas o `=` mal puestos; compáralo con `config.local.example.toml` |
