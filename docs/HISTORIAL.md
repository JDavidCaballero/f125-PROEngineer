# Historial de rondas

Diario de cambios por ronda de pruebas (antes en el README). Para el estado actual mira [`PENDIENTES.md`](PENDIENTES.md).

## Novedades (ronda 3)
- **Compuestos y rivales** (`compare.py`): ritmo y desgaste medidos de los demás coches por compuesto; "¿es mejor el medio o el duro?",
  "¿cómo va mi compañero?", avisos de paradas y neumáticos al límite de los rivales cercanos (tema `rivales`).
- **Parar antes / después** (`timing.py`): junto a una propuesta de parada, una sola vez por ventana, ofrece una alternativa
  (undercut/overcut) si cuesta poco y mejora claramente la probabilidad de superar a un rival; a petición: «parar antes», «alargar».
  Aire sucio medido solo dentro de esas respuestas. Silenciable con "menos avisos de alternativas".
- **Resumen de carrera** (`debrief.py`): al acabar (y a petición): resultado, paradas, propuestas y error de la predicción.
- **Avisos**: combustible solo si es crítico y al final; "llegas con margen" una sola vez. "Más/menos avisos de X" por voz.
- El campeonato no está en la telemetría del juego (comprobado con la captura real): el programa lo dice en vez de inventarlo.

## Novedades (ronda 4): entrenos y clasificación
- **Trazas por distancia** (`traces.py`): vueltas de los 22 coches en 200 tramos (tiempo, velocidad, acelerador, freno, ERS). Validado con la
  captura real: los tiempos de las trazas coinciden con el historial del juego.
- **Análisis de vuelta** (`quali.py`): tras cada vuelta cronometrada en clasificación/entrenos, sin ánimos: la primera dice ERS (desplegado,
  batería final, si se agotó y dónde); las siguientes, en qué curvas pierdes tiempo frente a tu mejor vuelta y, si un rival está a <1 s
  y hay datos suyos, qué hace mejor (tiempo y ERS por tramo). A petición: «¿cómo fue mi vuelta?», «¿cómo uso el ERS?», «compárame con el primero / mi compañero / Hansen».
- **Estimación de entrenos** (`practice.py`): clasificación y ritmo de carrera estimados, tabla por compuesto; se dice al acabar el entreno y a petición.
- Limitaciones medidas: las curvas se detectan por velocidad y se numeran en orden (pueden no coincidir con las oficiales; en Singapur se detectan ~9);
  los coches despliegan 7-10 MJ por vuelta, así que no se asume un límite de despliegue de 4 MJ (la batería sí es de 4 MJ).

## Correcciones tras la prueba real del 26 oct (entrenos + clasificación + carrera)
- **Causa del silencio en clasificación y carrera:** el reloj del juego vuelve a 0 en cada sesión y el ingeniero conservaba los tiempos del entreno
  (cooldowns, último aviso). Ahora reinicia su estado al cambiar de sesión (`Engineer._new_session`), manteniendo tus preferencias de avisos.
- **Fuera de carrera** todo se compara por mejor vuelta (posición, huecos, compañero) y ya no dice «fin de carrera» ni «P1 en la vuelta 6 de 1».
- **Preguntas nuevas:** sesión, mejor vuelta (tuya, de la sesión, de otro), quién va primero/segundo…, información de un piloto, «¿alguna actualización?»,
  estrategias y compuestos en entrenos (con lo medido), y acuse de recibo («entro a boxes» → «Entendido.»).
- **Diagnóstico:** el registro incluye `lap_trace` / `lap_rejected` (por qué una vuelta no se midió) y `new_session`.

## El LLM como cerebro (ronda 5)
- Por defecto el LLM (LM Studio) **entiende tu frase y elige qué herramientas del programa consultar** (`brain.py`); luego **redacta** la respuesta.
  Las cifras salen siempre del código: si el modelo cambia un número o añade un piloto, se descarta y se dice el texto original.
- Charla y ánimos ya no son errores: «mándame ánimos» -> frase corta del modelo (sin cifras). Lo que ninguna herramienta puede dar ->
  «No tengo ese dato en la telemetría…» (no «no he entendido»). Ruido o mala transcripción -> se pide repetir, como antes.
- Órdenes de control cortas (sí, no, repite, silencio, reanuda, explica) van directas por reglas, sin esperar al modelo.
- Acciones que cambian el estado (silencio, objetivo, avisos…) en frases largas o junto a otra orden: primero pregunta («¿Quieres que me calle?»).
- Sin servidor o con respuesta inservible: reglas de siempre (el servidor caído no se reintenta durante 30 s).
- `python voice.py llm` mide el LLM real con ~50 frases (herramienta elegida, latencia, redacciones rechazadas) y deja el resultado en el informe.
- `--no-llm` en voice.py/engineer.py = solo reglas. Se configura en `[llm]` de `config.toml` (`primary`, `compose`, `timeout_s`).
- Un fallo del micrófono ya NO cierra el programa: se reinicia el audio, y si no vuelve se sigue escribiendo las preguntas (reintento cada 15 s).

## Ronda 6: tras la prueba real completa del 26 oct (entreno + clasificación + carrera de 18 vueltas)
**Correcciones**
- **El «daño del neumático» no existe aparte del desgaste:** en la captura real son idénticos (`f1_parse.tyre_extra_damage`). Solo se avisa/estima daño si supera al desgaste.
  Antes duplicaba los avisos y sumaba el desgaste dos veces a la estrategia («el daño te cuesta 0.3 s por vuelta»).
- **Eco del micrófono:** si el micrófono recoge la voz del ingeniero (sin auriculares) la grabación se ignora y no cuenta como «no entendí» (`VoiceSession.is_echo`).
- Vuelta de clasificación que sale de boxes: ya no se descarta por empezar en el pit lane. «Fin de la sesión» (no «fin de carrera») al acabar entreno y clasificación.
- Plan aceptado: cruzar la meta por el pit lane ya no da «pasó la ventana del plan»; no hay propuestas duplicadas con otro número; «Parada completada» sube de prioridad;
  los rivales que entran a boxes ya no salen «a 0.0 s».
- Un undercut ya no se ofrece cuando el plan dice «sigue fuera». «Cuántas vueltas aguanta el neumático» responde con la vida del neumático; las afirmaciones largas
  («termino el entrenamiento con el medio») reciben «Entendido.»; los nombres pegados por el reconocedor («motoverstappen») se encuentran.
- El informe `INFORME.txt` ahora incluye `lap_trace`, `lap_rejected`, `memory`, `preplan`, `coach`, `cover`, `budget`… y compacta el registro de voz.

**Novedades**
- **Memoria entre sesiones (`raceplan.py`):** lo medido en entrenos y clasificación (ritmo y desgaste por compuesto, parada medida, mejor vuelta) se guarda mientras el programa siga abierto
  (no se escribe a disco: así no se mezclan datos de otro día o de otro estado del coche; caduca a las 8 h). Al acabar un entreno o clasificación dice un resumen.
- **Plan de carrera previo:** al empezar la carrera (o si preguntas «¿qué estrategia para la carrera?» / dices «carrera de 18 vueltas») compara planes de 0, 1 o 2 paradas con tu ritmo medido,
  las reglas de compuestos y el límite de desgaste; dice de dónde sale, qué es estimado y que no incluye rivales ni Safety Car (en carrera lo recalcula el Monte Carlo).
- **Entrenador de carrera (`coach.py`):** «¿cómo adelanto a X?» (o al de delante) compara tu vuelta y la suya por tramos: dónde le ganas (rectas, ERS), dónde te saca tiempo, su batería,
  su desgaste y su ritmo. Lo dice solo en un duelo largo (una vez por rival cada 4 vueltas) y al perder una posición («X te pasó. Para recuperar…»). Se apaga con «menos avisos de adelantamiento».
- **Amenaza de undercut:** en vez de solo avisar, compara «parar ahora» con «seguir fuera» y propone cubrirte (o dice que defiendas en pista) con los P esperados de cada una.
- **Avisos dinámicos:** temperatura de neumático una vez por juego (con la curva donde se calienta) y de nuevo solo si sube 8 °C; previsión de lluvia solo si empeora o se acerca;
  «cambia el tiempo» solo con lluvia; sectores en carrera solo si los pides («más avisos de sectores»); un duelo se avisa una vez (y a las 3 vueltas); DRS cada 3 vueltas por rival;
  análisis de vuelta en entrenos solo en vueltas de ataque.
- **Presupuesto de avisos por vuelta** (`[engineer] budget_prio2 = 4`, `budget_prio3 = 2`; 0 = sin límite): solo recorta los avisos de «ambiente»; seguridad, daños, banderas,
  estrategia y respuestas a tus preguntas nunca se recortan.

## Ronda 7: segundo informe real (fin de semana en Spa: libres, sprint, clasificación y carrera de 11 vueltas)
- **Sesiones fantasma (causa raíz, verificada en la captura real):** entre sesiones el juego manda paquetes sueltos con `session_uid = 0` (solo eventos / historial). Cada uno contaba como
  «sesión nueva» (32 en un fin de semana de 5): intro y plan repetidos, «fin de carrera» perdido o duplicado, memoria duplicada, «vuelta rápida de coche 12: 307445749164714:40.000».
  Ahora esos paquetes se ignoran (`RaceTracker.on_packet`).
- **Grabación de paquetes:** `lab --live` guarda todo lo que manda el juego en `runs/session-*/udp.f1raw` (reproducible con `lab --file`; ~100 MB por hora; se apaga con `record_raw = false`).
  Antes solo quedaba el informe de texto, así que no había datos crudos para calibrar.
- Ánimos: respuesta fija y variada (ya no depende del modelo ni de que transcriba bien). El modelo ya no puede decir «no entiendo» ni «iniciando sesión…», y su charla no se recuerda
  (repetía sus propias frases: «Te escucho, piloto. Vamos con la vuelta.»). «Ocon está molesto conmigo, ¿qué hago?» ya no se toma por una pregunta de estrategia.
- Daño del motor/alerones ya avisado: no se repite al empezar la sesión siguiente. Contacto y bandera amarilla: una vez cada 45 s. Vuelta rápida de otro: solo fuera de carrera.
- Entrenador: tramos con diferencias > 1.2 s (incidente) se ignoran; en rectas ya no habla de «velocidad de vértice». Plan previo: sin alternativas absurdas ni «parada de 22 s» en un plan sin paradas;
  la distancia de carrera solo la fijas tú o la carrera misma (los «vueltas» de unos libres no lo son).

## Ronda 8: voz, ERS y paquetes (tras el informe de Spa)
- **Números hablados (`speech.py`):** la voz española de macOS leía «1.452» como «mil cuatrocientos cincuenta y dos» (el punto es el separador de miles). Ahora todo lo que se pronuncia se convierte:
  `0.5 s` → «5 décimas», `0.19 s` → «19 centésimas», `0.442 s` → «442 milésimas», `1.4 s` → «1 segundo y 4 décimas», `1:43.450` → «1 minuto, 43 segundos y 450 milésimas»,
  `49%` → «49 por ciento», `136°C` → «136 grados», `P1.2` → «posición 1 coma 2». La pantalla y el informe conservan los números tal cual.
- **ERS en porcentaje y contra el límite permitido:** el MGU-K solo puede desplegar 4 MJ por vuelta (el contorno verde del indicador; tus vueltas nunca pasan de 4.0). Ya no dice «te sobró batería, úsala más»
  si ya usaste todo el despliegue permitido; dice «Usaste el 100% del despliegue permitido» o «Te faltó usar el 50% del despliegue permitido» (y si la batería se agotó antes, lo dice). Nada de kJ ni MJ:
  todo en % de batería. El límite se ajusta en `[ers] deploy_limit_mj`.
- **Parada fuera del plan previo:** si paras cuando el plan decía «sin parar» (o muy antes/después de lo previsto) lo comenta una vez.
- **Pérdida de parada medida en sesiones anteriores** del mismo fin de semana (antes la estrategia usaba 22 s «por defecto» hasta que alguien paraba).
- «¿…? ¿Sí o no?» en estrategia/estimación/plan: primero «Sí.» o «No.», luego el detalle. El ritmo del rival ya no cuenta sus vueltas lentas (bandera, incidente).
- **Al cerrar `lab --live`:** deja `~/Downloads/f1-informe-<sesión>/` con `INFORME.txt` + `udp.f1raw.gz` (los paquetes del juego, comprimidos; se reproducen con `lab --file`) y abre esa carpeta;
  y aprende de la grabación (perfil por circuito: parada y desgaste por compuesto de todos los coches). Se desactiva con `[paths] export_dir = ""`, `learn_at_end = false`.
- **Jerga de Whisper (`stt.fix_jargon`):** «Quema 1» → Q1, «los bits» → pits, «QALE» → quali, «teledimetría» → telemetría, «¿en qué sección?» → sesión. Si el micrófono recoge el eco del ingeniero
  2 veces seguidas, avisa en pantalla (no por voz) de que uses auriculares.
- **Medido con Qwen real tras estos cambios (28 oct):** batería difícil 61/66 (92%) con ruta mediana 2.0 s y 0 errores de servidor; la básica 47/49 (96%). Evidencia en `tests/data/llm_real_run8.json`.
  Guardas nuevas: `plan_carrera` exige palabras de carrera y que el número de vueltas esté en la frase; `como_adelantar` exige palabras de adelantar.
