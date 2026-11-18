# Arquitectura (mapa para trabajar en paralelo)

Un **ingeniero de pista de F1 25** local: el PC del juego manda telemetría UDP (puerto 20777, formato 2025) a un Mac que corre Python + un LLM local
(LM Studio) + voz (Whisper para escuchar, `say` de macOS para hablar). Solo librería estándar salvo la voz (`requirements.txt`, `setup_mac.sh`).

**Regla de diseño que no se negocia:** el *código calcula* y el *LLM solo entiende y redacta*. Toda cifra, nombre o posición sale de los datos del juego;
si el modelo cambia una cifra, un piloto o el sentido de una frase, se descarta y se dice el texto original (`brain.py`). Separa siempre lo **medido**
de lo **estimado** en lo que dice el ingeniero.

## Flujo
```
UDP del juego ─► f1_parse.py ─► state.py (RaceTracker: estado de las 3 sesiones)
                                  │
              ┌───────────────────┼─────────────────────────┐
              ▼                   ▼                         ▼
        tactics.py           strategy.py              traces.py / quali.py / coach.py
   alertas por reglas   Monte Carlo (22 coches)     vueltas por distancia, ERS, rivales
              │                   │                         │
              └──────► engineer.py (decide si hablar: dialog.py = silencio, propuestas, objetivos) ◄── raceplan.py (memoria entre sesiones)
                                  │
 micrófono ─► stt.py (Whisper) ─► assistant.py (reglas rápidas) ◄─► brain.py (LLM: elige herramientas, redacta, validado)
                                  │
                              speech.py ─► speaker.py (voz)        blackbox.py / voice_audit.py ─► INFORME.txt
```

## Módulos
| Área | Archivos | Para qué |
|---|---|---|
| Datos del juego | `f1_parse.py`, `f1_udp.py`, `state.py`, `config.py`, `config.toml` | Parser UDP F1 25, captura/reproducción `.f1raw`, estado y configuración |
| Estrategia | `strategy.py`, `timing.py`, `compare.py`, `fuelzones.py`, `learn.py`, `backtest.py`, `debrief.py` | Simulación, undercut/overcut, ritmo de rivales, combustible, calibración con capturas, resumen |
| Entreno / quali | `traces.py`, `quali.py`, `practice.py`, `raceplan.py`, `coach.py` | Trazas por distancia de los 22 coches, análisis de vuelta y ERS, plan previo, cómo adelantar |
| Avisos | `tactics.py`, `dialog.py`, `engineer.py` | Alertas con presupuesto por vuelta, propuestas con sí/no, orquestación |
| Lenguaje | `assistant.py`, `answers.py`, `brain.py`, `llm.py`, `speech.py` | Intenciones por reglas, herramientas del LLM, validación de lo que dice, números hablados |
| Voz | `voice.py`, `stt.py`, `speaker.py`, `radio.py`, `voice_audit.py`, `blackbox.py` | Laboratorio de voz, push-to-talk, protocolo de radio, informe de cada sesión |
| Datos de prueba | `make_demo.py`, `tests/` | Carreras inventadas de 22 coches, pruebas |

## Cómo se prueba
- `python3 make_demo.py all` genera capturas de demostración en `captures/` (no se versionan). Las pruebas generan las suyas en un directorio temporal.
- `python3 -m unittest discover -s tests < /dev/null` (≈4 min; **con la entrada cerrada**: sin eso algunas pruebas de voz esperan teclado).
- Pruebas opcionales con datos/servicios reales (se omiten si no están):
  - `F1_REAL_CAPTURE=/ruta/carrera.f1raw` → pruebas contra una captura real.
  - `F1_LLM_URL=http://127.0.0.1:1234/v1` → `tests/test_brain.py::RealModel` y `python3 voice.py llm` miden el modelo real con `llm_phrases*.txt`.
- Las medidas reales del modelo quedan como evidencia en `tests/data/llm_real_*.json`.

## Convenciones
- Español en textos al piloto, comentarios y docstrings (el programa habla en español); nombres de código en el estilo que ya tenga cada módulo.
- Un cambio de comportamiento = una prueba que reproduce el fallo real que lo motivó (mira `tests/test_point1.py`, `test_round7.py`, `test_round8.py`).
- Lo que dice el ingeniero nunca inventa: si falta un dato, lo dice. Nada de «estimado» presentado como «medido».
