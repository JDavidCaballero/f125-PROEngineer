# Laboratorio de voz — pasos en TU Mac

Todo es local. Nada se envía a ningún sitio salvo lo que tú decidas compartir (por ejemplo, adjuntar el INFORME.txt a un issue).

**Cada comando (`selftest`, `bench`, `lab`, `script`) crea una carpeta `runs/session-FECHA-comando/` y, al terminar —aunque falle o lo
cierres con Ctrl+C—, escribe en ella un único archivo de texto, `INFORME.txt`, con todo lo necesario para auditar: versiones y sistema,
resumen de auditoría, errores con su traceback, lo que salió por pantalla, lo que dijiste y entendió, y las decisiones del ingeniero.
Ese es el único archivo que hace falta adjuntar a un issue** (sin audio: el audio de tus frases se queda en `audio/` dentro de esa carpeta, en tu Mac;
puedes borrarlo). El programa te dice la ruta exacta al terminar.

## 1. Instalar (una vez)
```bash
unzip f1-engineer-voz.zip && cd f1-engineer
cat setup_mac.sh            # léelo: no usa sudo y solo crea la carpeta .venv
./setup_mac.sh              # instala el micrófono y los dos motores de voz a texto (si uno falla, sigue con el otro)
source .venv/bin/activate
```
La primera vez macOS pedirá permiso de micrófono para la Terminal: acéptalo.

### En Windows o Linux (sin probar)
`.\setup_windows.ps1` (PowerShell; antes `Set-ExecutionPolicy -Scope Process Bypass`) o `./setup_linux.sh`. Instalan solo faster-whisper.
La voz del ingeniero usa SAPI en Windows (viene con Windows) y `espeak-ng` en Linux (`sudo apt install espeak-ng`); implementado pero
**sin probar en máquinas reales**. Los mismos comandos de abajo valen (`python` en vez de `python3`); sin `--model` usa `small`.
Si la voz falla o no hay motor, sigue sin voz; puedes forzarlo con `--no-voice`. Más detalles en [`docs/DETALLES.md`](docs/DETALLES.md) (sección «Windows / Linux: detalles»).

## 2. Autocomprobación  →  adjunta su INFORME.txt a un issue
```bash
python voice.py selftest --say-test
```
Comprueba: voz en español, micrófono, motor de voz a texto y un viaje voz→texto con una frase generada por el Mac.
Si algo falla, el mensaje dice qué; esas piezas (Whisper y el micrófono) las escribí sin poder ejecutarlas, así que es normal que haya que ajustar algo.

## 3. Qué modelo de Whisper conviene en tu Mac  →  adjunta su INFORME.txt a un issue
```bash
python voice.py bench --models small,medium,turbo
```

## 4. Carrera de prueba hablando  →  habla 5 a 10 minutos
```bash
python make_demo.py all --out captures
python voice.py lab --file captures/demo_corta.f1raw --model small
```
ENTER para empezar a hablar, ENTER para terminar. **Dos ENTER seguidos rápido = un "toque"** del botón.
Cosas que probar: "¿cómo voy de posición?", "¿cuánto combustible tengo?", "qué hacemos", "quiero parar ya",
"no me siento cómodo con las gomas", "quiero acabar quinto", "necesito pasar a (un piloto)", y cuando el ingeniero haga una propuesta
(sale sola, por ejemplo con el Safety Car): "sí", "no", "explícame", y también **mumbles o frases sin sentido** para probar el
canal de respaldo (dos fallos seguidos → te explica; toque = confirma; silencio 7 s = rechaza).
Termina con Ctrl+C.

## 4b. Hablar con un botón del mando (p. ej. R3), juego en Windows y programa en el Mac
El juego manda por la telemetría UDP los botones que pulsas, así que no hay que instalar nada en Windows.
1. Con el juego enviando la telemetría a tu Mac, entra en pista o en el garaje (no en un menú).
2. En el equipo donde corre el programa (Mac, o Windows/Linux): `python voice.py boton` y **pulsa y suelta R3 tres veces**, despacio. Se guarda en `data/mando.json` (solo tuyo, no se sube).
3. `python voice.py lab --live`: **mantén R3 mientras hablas** y suéltalo al terminar (un toque corto = «toque», como el doble ENTER).
   Si el juego no avisara al soltar, pulsa una vez para empezar y otra para terminar. ENTER sigue funcionando.
- El micrófono es el del **Mac** (o unos auriculares conectados al Mac). Si R3 ya hace algo en el juego, lo seguirá haciendo:
  puedes elegir otro botón libre (repite el paso 2) o asignar en el juego una «Acción UDP» a un botón.
- Si en el paso 2 dice que no llega ningún botón, la telemetría no llega al programa: revisa IP y puerto del juego.
- **Juego y programa en el mismo PC** (Windows): en los ajustes de telemetría del juego pon la IP `127.0.0.1` y permite a
  Python en el cortafuegos. El botón funciona igual (es la misma telemetría UDP).

## 5. Lista de 48 frases (mide cuántas entiende)
```bash
python voice.py script --file captures/demo_corta.f1raw
```
Te va diciendo qué frase leer (los nombres de pilotos cambian al azar). Es lo más útil para auditar.

## 6. Si algo se cerró mal o quieres rehacer el informe
```bash
python voice.py pack            # regenera el INFORME.txt de la última sesión
python voice.py report runs/session-*   # solo el resumen de auditoría, por pantalla
```

## Si algo se cuelga
- Usa **auriculares**: con altavoces, el micrófono graba al ingeniero y se mezcla con tu voz.
- Una grabación se corta sola a los **20 s**; la transcripción tiene un tiempo máximo de 40 s y no bloquea la sesión.
- Mientras grabas, el ingeniero **guarda sus avisos en silencio** y los dice al soltar.
- **Ctrl+C cierra y genera el informe.** Si aun así no responde, en otra pestaña: `pkill -f "voice.py lab"` y luego
  `python voice.py pack` (rehace el `INFORME.txt` de la última sesión; no se pierde nada, salvo la frase que estuvieras grabando).
- En el respaldo, la ventana de silencio (7 s) empieza **cuando el ingeniero termina de explicarlo**.

## Con tu carrera real
Si copias `red.f1raw` a `captures/`: `python voice.py lab --file captures/red.f1raw --start-at 7540` (salta los menús; los nombres de pilotos son los reales).

## Probar sin micrófono
`python voice.py lab --text --file captures/demo_corta.f1raw`: escribes la frase en vez de decirla (`/tap` = toque, `/quit` = salir).


## LLM por defecto y prueba del modelo
- Ahora el LLM (LM Studio) es el cerebro por defecto: **enciende el servidor de LM Studio antes** (el modelo `qwen/qwen3.5-9b`, Think apagado).
  Si no está encendido, el programa lo avisa y sigue con reglas; lo reintenta cada 30 s.
- Para medir cómo lo hace el modelo real: `python voice.py llm` (usa la demo corta; no necesita micrófono). Adjunta el `INFORME.txt` a un issue.
- Si quieres que otra máquina de tu red llame a LM Studio: Developer -> Server settings -> **Serve on Local Network**.

## Ronda 6 (tras tu prueba completa)
- Usa **auriculares**: si el micrófono recoge la voz del ingeniero, esa grabación se ignora («eco») pero te quedas sin escuchar tu propia frase.
- Dile **«carrera de N vueltas»** en los entrenos y te prepara un plan de salida con lo que hayas rodado; al empezar la carrera lo repite solo.
- Prueba durante un duelo: «¿cómo adelanto a Verstappen?», «¿dónde puedo pasarlo?».
- El informe ahora trae más diagnóstico (por qué una vuelta no se midió, qué guardó la memoria, qué avisos se recortaron). Adjunta `INFORME.txt` a un issue como siempre.
