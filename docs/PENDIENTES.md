# Estado y pendientes

Estado: 510 pruebas verdes (una omitida si no hay servidor LLM real). Probado en juego real por una sola persona (3 informes completos de sesiones con voz).
Medido con Qwen3.5 9B (LM Studio): ~92–96 % de acierto de herramienta en 115 frases, ruta mediana ≈ 2 s.

## Limitaciones conocidas (honestas)
1. La memoria de entrenos/clasificación (`raceplan.py`) vive en el proceso: se pierde al cerrar el programa (decidido a propósito: no mezclar días/coches).
2. El límite de despliegue de ERS (4 MJ/vuelta, contorno verde del HUD) sale de foros y guías, no de la especificación UDP (no hay un campo del límite).
3. Sin datos crudos de varios circuitos no se puede validar: sesgo de la estrategia («esperado P1.0»), zonas de DRS por circuito, desgaste real por compuesto, acierto del Monte Carlo.
4. Whisper se equivoca con nombres/jerga («Quema 1», «los bits»); hay correcciones en `stt.fix_jargon` pero no se ha medido qué modelo falla menos con audio real.
5. Latencia del LLM local variable (1.7–5.8 s de mediana según la carga del equipo; timeout de 12 s y se sigue con reglas).
6. Formato UDP «2026 Season Pack» de F1 25 sin verificar (el parser está validado con formato 2025).
7. Sin lluvia/mojado completo en la estrategia (solo avisos), sin Safety Car aprendido por circuito más allá de lo básico.

## Ideas / trabajo en paralelo (áreas con poco solape)
- **Datos reales:** grabar sesiones (`lab --live` guarda `udp.f1raw`), `learn.py` por circuito, validar DRS/curvas/pérdida de parada, afinar `strategy.py` con `backtest.py`.
- **Voz:** comparar modelos de Whisper con audios propios (`voice.py bench`), vocabulario de jerga, reducir latencia (LLM y STT compiten por la GPU del mismo Mac).
- **Entrenador (`coach.py`, `quali.py`):** consejos por curva con más contexto (frenada, trazada), reacción a undercut, defensa.
- **LLM:** otros modelos, más frases en `llm_phrases_dificiles.txt`, medir con `python3 voice.py llm` y guardar evidencia.
- **Otros juegos/versiones:** parser de formato 2026, otros simuladores (la capa `state.py` aísla el formato).
- **Interfaz:** panel en pantalla / móvil con el estado y las propuestas.

Para no pisaros: una rama por tema (`voz/…`, `datos/…`, `estrategia/…`) y PRs pequeños; evitad tocar a la vez `engineer.py` y `assistant.py` (son los archivos grandes).
