#!/usr/bin/env python3
"""Laboratorio de voz: tú hablas, el programa + LLM responden, y todo queda registrado para auditarlo.

  python voice.py selftest                              # comprueba micrófono, voz, Whisper y un viaje voz->texto
  python voice.py bench --models small,medium           # tiempos de cada modelo de Whisper en TU Mac
  python voice.py lab --file captures/demo_corta.f1raw  # sesión libre mientras corre una carrera (repetida)
  python voice.py lab --file red.f1raw --start-at 7540  # tu carrera real (salta los menús)
  python voice.py script --file red.f1raw --start-at 7540   # lista de frases: mide cuántas entiende y clasifica bien
  python voice.py report runs/voice-*.jsonl             # resumen de texto para pegarme (sin audio)
  (añade --text para escribir en vez de hablar: sirve para probar el protocolo sin micrófono)

Pulsar para hablar: ENTER para empezar, ENTER para terminar. Dos ENTER seguidos en menos de medio segundo = un "toque"
(en el canal de respaldo confirma la propuesta). En --text: escribe la frase; "/tap" es el toque; "/quit" sale.
Todo lo que se oye, entiende y responde va a runs/voice-*.jsonl (el audio, si lo guardas, se queda en TU Mac).
"""
import argparse, json, os, queue, random, re, signal, socket, subprocess, sys, tempfile, threading, time
from collections import deque
from datetime import datetime
from pathlib import Path

import blackbox as BB
import config as C
import llm as L
import stt as STT
from engineer import Engineer
from f1_parse import read_capture, NCARS
from radio import Radio, FALLBACK as R_FALLBACK, FALLBACK_AGAIN as R_AGAIN
import speaker as SP
from speaker import Speaker


def _now():
    return datetime.now().isoformat(timespec="milliseconds")


ACK = "Recibido, lo miro."


class VoiceSession:
    """Une: ingeniero (hilo de telemetría) + voz a texto + radio + voz. Todo inyectable para probar sin sonido."""

    def __init__(self, cfg, eng, speaker, stt, recorder=None, log_path=None, audio_dir=None, radio=None,
                 clock=time.time, echo=print, min_peak=0.02):
        self.cfg, self.eng, self.speaker, self.stt, self.recorder = cfg, eng, speaker, stt, recorder
        self.radio = radio or Radio(cfg)
        self.clock, self.echo, self.min_peak = clock, echo, min_peak
        self.log_path, self.audio_dir = log_path, audio_dir
        self.n = 0
        self.said = deque(maxlen=8)                     # (reloj, texto) de lo último que dijo el ingeniero: para detectar su eco en el micro
        self._f = None
        self._pid = None
        self.lock = threading.RLock()
        eng.on_say.append(self._on_engine_say)
        speaker.on_event = self._on_tts

    # ------------------------------------------------------------------ registro
    def log(self, rec):
        rec = {"ts": _now(), **rec}
        if not self.log_path:
            return
        with self.lock:
            if self._f is None:
                Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
                self._f = open(self.log_path, "a", encoding="utf-8")
            self._f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            self._f.flush()

    def close(self):
        self.speaker.close()
        if self._f:
            self._f.close()

    # ------------------------------------------------------------------ lo que dice el ingeniero
    def _on_engine_say(self, text, prio):
        if prio >= 2 and getattr(self.eng, "say_forced", False):
            self.speaker.say(text, prio, expires=False)               # una propuesta o una respuesta nunca caduca (si no, se daría por oída sin serlo)
        else:
            self.speaker.say(text, prio)
        self.log({"type": "engine_say", "text": text, "prio": prio})
        self._track_proposal()

    def _on_tts(self, kind, text, t0):
        if kind == "start" and text:
            self.said.append((self.clock(), text))
        self.log({"type": "tts", "kind": kind, "text": text, "t0": t0})

    def is_echo(self, text, window_s=120.0):
        """El micrófono recogió la voz del propio ingeniero (sin auriculares): casi todas las palabras de la frase están en algo
        que acaba de decir. Una frase del piloto rara vez repite 7 de cada 10 palabras de lo último que oyó."""
        words = [w for w in re.findall(r"\w+", text.lower()) if len(w) > 2]
        if len(words) < 4:
            return False
        now = self.clock()
        for t, said in self.said:
            if now - t > window_s:
                continue
            sw = set(re.findall(r"\w+", said.lower()))
            if sum(1 for w in words if w in sw) / len(words) >= 0.7:
                return True
        return False

    def _ack(self):
        self._acked = True
        self.speaker.say(ACK, 1)
        self.echo(f"[radio] {ACK}")

    def _track_proposal(self):
        p = self.eng.dialog.pending
        if p and p.id != self._pid:
            self._pid = p.id
            self.radio.new_proposal()

    def pending_spoken(self):
        """Hay una propuesta que el piloto ya ha podido oír (no está retenida esperando una recta)."""
        return self.eng.dialog.pending is not None and self.eng.held is None

    # ------------------------------------------------------------------ acciones de la radio
    def run_actions(self, actions, source):
        out = []
        for a in actions:
            if a.kind == "say":
                self.speaker.say(a.text, a.prio)
                self.echo(f"[radio] {a.text}")
                out.append({"kind": "say", "text": a.text})
                if a.text in (R_FALLBACK, R_AGAIN):                             # la ventana de silencio empieza cuando termine de explicarlo
                    threading.Thread(target=self._arm_after_speech, daemon=True).start()
            elif a.kind == "accept":
                with self.eng.lock:
                    msg = self.eng.dialog.accept(self.eng.tr.cur_lap)
                self.echo(f"[radio] (respaldo) {msg}")
                out.append({"kind": "accept", "text": msg})
            elif a.kind == "decline":
                with self.eng.lock:
                    msg = self.eng.dialog.decline(self.eng.tr.cur_lap)
                self.echo(f"[radio] (respaldo) {msg}")
                out.append({"kind": "decline", "text": msg})
        if out:
            self.log({"type": "radio", "source": source, "actions": out, "state": self.radio.state})
        return out

    def _arm_after_speech(self):
        self.speaker.wait(30)
        self.radio.arm(self.clock())
        self.log({"type": "radio_armed", "window_s": self.radio.window_s})

    def tick(self):
        self.run_actions(self.radio.tick(self.clock(), self.pending_spoken()), "silencio")

    def tap(self, press_s=0.0):
        self.log({"type": "tap", "press_s": round(press_s, 3), "state": self.radio.state, "pending": self.pending_spoken()})
        self.run_actions(self.radio.tap(self.clock(), self.pending_spoken()), "toque")

    # ------------------------------------------------------------------ una frase
    def names(self):
        return [self.eng.tr.names[i] for i in range(NCARS) if self.eng.tr.names.get(i) and i != self.eng.tr.player]

    def utterance(self, audio, press_s=0.0, t_press=None):
        """audio: matriz de muestras (micrófono) o str (modo texto). Devuelve el registro de la frase."""
        self.n += 1
        t_rel = self.clock()
        rec = {"type": "utt", "n": self.n, "press_s": round(press_s, 3), "lap": self.eng.tr.cur_lap,
               "t_game": round(self.eng.tr.t, 1), "pending_before": self.pending_spoken(), "audio": None}
        is_text = isinstance(audio, str)
        if not is_text:
            rec["dur_s"] = round(len(audio) / STT.SR, 2)
            rms = self.recorder.rms(audio) if self.recorder else 1.0
            peak = self.recorder.peak(audio) if self.recorder else 1.0
            rec["rms"], rec["peak"] = round(rms, 4), round(peak, 4)
            if self.audio_dir and self.recorder:
                Path(self.audio_dir).mkdir(parents=True, exist_ok=True)
                p = Path(self.audio_dir) / f"utt_{self.n:03d}.wav"
                self.recorder.save_wav(audio, p)
                rec["audio"] = str(p)
            silent = peak < self.min_peak                        # el pico, no el promedio: una palabra corta dicha con calma tiene poco promedio
            res = {"text": "", "latency_s": 0.0} if silent else self.stt.transcribe(audio, STT.build_prompt(self.names()))
            if silent:
                res["reject"] = "silencio"
        else:
            res = self.stt.transcribe(audio, STT.build_prompt(self.names()))
        text, reason = (None, res.get("reject")) if res.get("reject") else STT.clean(res)
        if "dur_s" in rec:
            res["duration_s"] = rec["dur_s"]
        rec["stt"] = {k: res.get(k) for k in ("backend", "model", "latency_s", "avg_logprob", "no_speech_prob")}
        if text is not None and self.is_echo(text):
            text, reason = None, "eco (el micrófono recogió la voz del ingeniero: usa auriculares)"
            res["echo"] = True
        rec["raw"], rec["clean"], rec["reject"] = res.get("text"), text, reason
        if res.get("echo"):                                  # no es una frase del piloto: ni se responde ni cuenta como «no entendí»
            rec.update({"intent": None, "source": None, "understood": False, "reply": None, "radio": [], "misses": self.radio.misses,
                        "radio_state": self.radio.state, "pending_after": self.pending_spoken(), "t_total_s": round(self.clock() - t_rel, 3)})
            self.log(rec)
            self.echo_hits = getattr(self, "echo_hits", 0) + 1
            self.echo("  (eco: el micrófono recogió la voz del ingeniero; ignoro esa grabación. Usa auriculares.)")
            if self.echo_hits in (2, 5):                         # se repite: avisar con claridad en pantalla (no por voz: se volvería a grabar)
                self.echo(f"  ** {self.echo_hits} grabaciones seguidas son el eco del ingeniero: ponte auriculares o baja el volumen, "
                          "y pulsa ENTER solo cuando termine de hablar. **")
            return rec
        self.echo_hits = 0                                   # una frase real corta la racha de ecos
        reply, intent = None, {"intent": None, "source": None, "understood": False}
        t_nlu0 = self.clock()
        if text is not None:
            # si pensar tarda (el LLM local puede tardar varios segundos), se dice «Recibido» como haría un ingeniero real
            wait = self.cfg.get("llm", {}).get("ack_after_s", 4.0)
            ack = threading.Timer(wait, self._ack) if wait else None
            if ack:
                ack.daemon = True
                ack.start()
            try:
                reply = self.eng.ask(text)
            finally:
                if ack:
                    ack.cancel()
            rec["ack"], self._acked = getattr(self, "_acked", False), False
            intent = dict(self.eng.assistant.last)
        rec["t_nlu_s"] = round(self.clock() - t_nlu0, 3)
        self._track_proposal()
        pending = self.pending_spoken()
        actions = self.radio.heard(self.clock(), intent["understood"], empty=text is None, pending=pending)
        if intent["understood"]:
            self.speaker.say(reply, 1)
            spoken = [{"kind": "say", "text": reply}]
        else:
            spoken = self.run_actions(actions, "no_entendido")
        rec.update({"intent": intent["intent"], "source": intent["source"], "understood": intent["understood"],
                    "reply": reply if intent["understood"] else None, "radio": spoken, "misses": self.radio.misses,
                    "radio_state": self.radio.state, "pending_after": self.pending_spoken(),
                    "t_total_s": round(self.clock() - t_rel, 3)})
        self.log(rec)
        return rec


# ---------------------------------------------------------------------------------------------- piezas
def make_engine(args, cfg, session_log=None):
    on = not getattr(args, "no_llm", False) and (bool(args.llm) or cfg.get("llm", {}).get("enabled", True))
    client = L.LLMClient.from_cfg({**cfg, "llm": {**cfg.get("llm", {}), "enabled": on}})
    down = bool(client) and not client.ping()
    if down:
        print("Aviso: el servidor del modelo (LM Studio) no responde ahora; sigo con reglas y lo reintento cada 30 s.")
    eng = Engineer(cfg, args.mode, client, out=print, log_path=session_log, sync=True, profile=C.load_profile(cfg),
                   memory_path=C.memory_path(cfg) if getattr(args, "live", False) else None)
    if down and eng.assistant.brain is not None:
        eng.assistant.brain.down_until = time.time() + eng.assistant.brain.cooldown_s
    return eng


def feed_file(eng, path, speed, start_at=0.0, stop=None, until_lap=None):
    """Reproduce una captura. Hasta start_at va a toda velocidad (para saltar menús); luego en tiempo real."""
    t_start = None
    wall = None
    for t, d in read_capture(path):
        if stop is not None and stop.is_set():
            return
        if t < start_at:
            eng.on_packet(t, d)
            continue
        if until_lap is not None and eng.tr.cur_lap >= until_lap and eng.tr.names and eng.tr.is_race():
            return
        if until_lap is None:
            if t_start is None:
                t_start, wall = t, time.time()
            wait = wall + (t - t_start) / speed - time.time()
            if wait > 0:
                time.sleep(wait)
        eng.on_packet(t, d)
    print("(fin de la captura)")


def button_status(d):
    """Máscara de botones de un paquete de evento BUTN del juego, o None si el paquete es otra cosa."""
    import f1_parse as P
    if len(d) >= P.HS + 8 and d[6] == 3 and d[P.HS:P.HS + 4] == b"BUTN":          # byte 6 de la cabecera = tipo de paquete
        return int.from_bytes(d[P.HS + 4:P.HS + 8], "little")
    return None


class PadButton:
    """El botón del mando como pulsar-para-hablar: al pulsar y al soltar mete un ENTER en la cola del teclado (mantener = grabar;
    un toque corto = el «toque» del canal de respaldo). Si el juego no avisa al soltar, la siguiente pulsación termina."""

    def __init__(self, mask, lines):
        self.mask, self.lines, self.down = mask, lines, False

    def update(self, status):
        now_down = bool(status & self.mask)
        if now_down != self.down:
            self.down = now_down
            self.lines.q.put("\n")


def load_pad(cfg):
    """Botón aprendido con `voice.py boton` (data/mando.json) o [voice] ptt_button_mask en la configuración; None si no hay."""
    mask = (cfg.get("voice", {}) or {}).get("ptt_button_mask")
    p = C.ROOT / "data" / "mando.json"
    if not mask and p.exists():
        try:
            mask = json.loads(p.read_text(encoding="utf-8")).get("mask")
        except (OSError, ValueError):
            mask = None
    return int(mask) if mask else None


def feed_live(eng, port, stop, record=None, on_buttons=None):
    """Escucha el juego. Si se da `record` (ruta), guarda además TODOS los paquetes tal cual (formato de f1_udp.py: se puede
    reproducir con `voice.py lab --file` y sirve para analizar o calibrar después); si el disco falla, el ingeniero sigue."""
    import f1_udp as U
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    sock.settimeout(0.5)
    t0 = time.time()
    f = None
    if record:
        try:
            Path(record).parent.mkdir(parents=True, exist_ok=True)
            f = open(record, "wb")
            f.write(U.MAGIC)
        except OSError as e:
            print(f"Aviso: no puedo grabar los paquetes en {record} ({e}); sigo sin grabar.")
            f = None
    from engineer import LinkWatch
    watch = LinkWatch(port, eng)
    try:
        while not stop.is_set():
            msg = watch.tick()
            if msg:
                print(msg)
            try:
                d, _ = sock.recvfrom(4096)
            except socket.timeout:
                continue
            watch.packet()
            if on_buttons is not None:
                st = button_status(d)
                if st is not None:
                    on_buttons(st)
            now = time.time()
            if f is not None:
                try:
                    f.write(U.REC.pack(now - t0, len(d)) + d)
                except OSError:
                    f = None
            if len(d) >= 29:
                eng.on_packet(now - t0, d)
    finally:
        sock.close()
        if f is not None:
            f.close()


def make_speaker(args, cfg, which=None, platform=None, popen=subprocess.Popen, run=subprocess.run):
    """Voz del ingeniero con el motor de este sistema ([voice] tts). Sin motor: avisa una vez de cómo instalar uno y sigue sin voz."""
    tts = SP.choose_backend(SP.tts_name(cfg), platform, **({"which": which} if which else {}))
    if not tts.available:
        if not args.no_voice:
            print(f"Aviso: {tts.hint}")
        return Speaker(backend=tts, popen=popen, enabled=False)
    return Speaker(voice=args.voice or tts.find_voice(run), enabled=not args.no_voice, backend=tts, popen=popen, run=run,
                   max_age_s=(cfg.get("voice", {}) or {}).get("max_age_s", 8.0))


def build_session(args, cfg, stamp, eng=None):
    folder = Path(args.folder)
    eng = eng or make_engine(args, cfg, folder / "engine.jsonl")
    speaker = make_speaker(args, cfg)
    recorder = None
    if args.text:
        stt = STT.FakeSTT()
    else:
        try:
            recorder = STT.Recorder()
        except Exception as e:                                   # noqa: BLE001
            how = STT.setup_hint()
            sys.exit(f"No puedo abrir el micrófono ({type(e).__name__}: {e}). Instala la voz con: {how}; revisa los permisos del "
                     "micrófono, o prueba sin micrófono con --text.")
        stt = STT.make_stt(args.stt, args.model)
        print(f"Cargando el modelo {stt.name}/{stt.model} (la primera frase no tardará más)...")
        try:                                                      # calentamiento: la primera llamada incluye el arranque del modelo
            stt.transcribe(recorder.np.zeros(STT.SR // 2, dtype="float32"), STT.build_prompt([]))
        except Exception as e:                                    # noqa: BLE001
            print(f"Aviso: el calentamiento falló ({type(e).__name__}: {e}); sigo.")
    audio_dir = None if (args.text or args.no_audio) else folder / "audio"
    sess = VoiceSession(cfg, eng, speaker, stt, recorder, folder / "voice.jsonl", audio_dir)
    return eng, sess


def ticker(sess, stop):
    while not stop.is_set():
        sess.tick()
        time.sleep(0.2)


class LineReader:
    """Lee el teclado en un hilo aparte para que el hilo principal siga atendiendo Ctrl+C y los tiempos máximos."""

    def __init__(self, stream=None):
        self.q = queue.Queue()
        self.stream = stream or sys.stdin
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            line = self.stream.readline()
            self.q.put(None if line == "" else line)
            if line == "":
                return

    def get(self, timeout=None):
        """Siguiente línea (Enter). queue.Empty si pasó el tiempo; EOFError si se cerró la entrada."""
        item = self.q.get(timeout=timeout)
        if item is None:
            self.q.put(None)
            raise EOFError
        return item


def run_with_timeout(fn, timeout):
    """(ok, resultado). Ejecuta fn en un hilo y espera en trozos cortos (Ctrl+C sigue funcionando). Si pasa el tiempo
    devuelve (False, None) y deja el hilo atrás: una llamada nativa colgada no puede bloquear toda la sesión."""
    box = {}
    done = threading.Event()

    def work():
        try:
            box["r"] = fn()
        except BaseException as e:                              # noqa: BLE001
            box["e"] = e
        done.set()
    threading.Thread(target=work, daemon=True).start()
    end = time.time() + timeout
    while not done.wait(0.2):
        if time.time() > end:
            return False, None
    if "e" in box:
        raise box["e"]
    return True, box.get("r")


def _real_recorder():
    """Otro grabador tras reiniciar PortAudio (si el audio del sistema se quedó bloqueado, el flujo viejo ya no sirve)."""
    try:
        import sounddevice as sd
        sd._terminate()
        sd._initialize()
        sd.query_devices(kind="input")                          # sin micrófono de entrada (auriculares quitados) esto falla:
    except Exception as e:                                      # noqa: BLE001  el intento cuenta como fallido y se reintenta
        if type(e).__name__ == "PortAudioError":
            raise
    return STT.Recorder()


def _mic_call(fn, timeout):
    """(ok, resultado) de una llamada al micrófono; un error del audio cuenta como «no respondió» en vez de cerrar el programa
    (informe del 11 nov: «Error querying device -1» al cambiar de micrófono cerró la sesión)."""
    try:
        return run_with_timeout(fn, timeout)
    except Exception:                                           # noqa: BLE001
        return False, None


RECOVER_WAIT_S = 1.0


def _recover_mic(sess, where, attempts=3, wait_s=None, timeout_s=6.0):
    """El audio del sistema dejó de responder. NO se cierra nada: la telemetría y el ingeniero siguen en sus hilos.
    Se reinicia el audio y se crea otro grabador; si no se logra, se sigue sin micrófono (se puede escribir) y se reintenta."""
    sess.log({"type": "mic_timeout", "where": where})
    print("  El micrófono dejó de responder; lo reinicio (el ingeniero sigue activo)...")
    make = getattr(sess, "recorder_factory", None) or _real_recorder
    for _ in range(attempts):
        try:
            ok, rec = run_with_timeout(make, timeout_s)
        except Exception:                                       # noqa: BLE001  (audio roto: cuenta como intento fallido)
            ok, rec = False, None
        if ok and rec is not None:
            sess.recorder, sess.mic_ok = rec, True
            sess.log({"type": "mic_recovered"})
            print("  Micrófono recuperado.")
            return True
        time.sleep(RECOVER_WAIT_S if wait_s is None else wait_s)
    sess.mic_ok = False
    sess.log({"type": "mic_lost"})
    print("  No consigo recuperar el micrófono: sigo SIN voz de entrada (puedes escribir tus preguntas); reintento cada 15 s.")
    return False


def read_ptt(sess, stop, lines=None, max_rec_s=20.0, stt_timeout=40.0, mic_timeout=5.0, retry_s=15.0):
    """Bucle de pulsar-para-hablar con ENTER (micrófono real). Mientras grabas, el ingeniero guarda sus avisos en silencio.
    Nunca termina por un fallo del micrófono: se recupera o se sigue en modo texto."""
    lines = lines or LineReader()
    sess.mic_ok = True
    while not stop.is_set():
        if not sess.mic_ok:
            print("[sin micrófono: escribe tu pregunta] ", end="", flush=True)
            try:
                line = lines.get(timeout=retry_s)
            except queue.Empty:
                _recover_mic(sess, "retry")
                continue
            except EOFError:
                return
            if line.strip():
                r = sess.utterance(line.strip(), 1.0)
                print(f"  -> {r['intent']} ({r['t_total_s']:.2f} s)")
            continue
        rec = sess.recorder
        print("[ENTER para hablar] ", end="", flush=True)
        try:
            lines.get()
        except EOFError:
            return
        t0 = time.time()
        sess.speaker.hold()
        try:
            ok, _ = _mic_call(rec.start, mic_timeout)
            if not ok:
                _recover_mic(sess, "start")
                continue
            print(f"  grabando... (ENTER para terminar; máximo {max_rec_s:.0f} s)")
            try:
                lines.get(timeout=max_rec_s)
            except queue.Empty:
                print("  (límite de grabación alcanzado: la corto aquí)")
            ok, audio = _mic_call(rec.stop, mic_timeout)
            if not ok:
                _recover_mic(sess, "stop")
                continue
            press = time.time() - t0
            if sess.radio.is_tap(press, rec.peak(audio) >= sess.min_peak):
                sess.tap(press)
                continue
            print("  procesando...")
            try:
                ok, r = run_with_timeout(lambda: sess.utterance(audio, press), stt_timeout)
            except Exception as e:                              # noqa: BLE001  una frase rota no cierra la sesión
                import traceback
                print(f"  (no pude procesar esa frase: {type(e).__name__}: {e}; sigo)")
                sess.log({"type": "utt_error", "error": repr(e), "trace": traceback.format_exc()[-1500:]})
                continue
            if not ok:
                print(f"  (no pude procesar esa frase en {stt_timeout:.0f} s; sigo)")
                sess.log({"type": "utt_timeout", "press_s": round(press, 2), "dur_s": round(len(audio) / STT.SR, 2)})
                continue
            print(f"  oí: «{r['clean'] or r['raw']}»" + (f"  [descartado: {r['reject']}]" if r["reject"] else "") +
                  f"  -> {r['intent']} ({r['t_total_s']:.1f} s)")
        finally:
            sess.speaker.release()


def read_text(sess, stop):
    """Modo texto: cada línea es una frase; /tap = toque del botón; /quit sale."""
    for line in sys.stdin:
        line = line.strip()
        if line == "/quit":
            return
        if line == "/tap":
            sess.tap(0.1)
        elif line:
            r = sess.utterance(line, 1.0)
            print(f"  -> {r['intent']} ({r['t_total_s']:.2f} s)")
    stop.set()


# ---------------------------------------------------------------------------------------------- comandos
def cmd_lab(args, cfg):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    eng, sess = build_session(args, cfg, stamp)
    stop = threading.Event()
    lines = None                                                      # teclado (y botón del mando, si lo hay)
    threads = [threading.Thread(target=ticker, args=(sess, stop), daemon=True)]
    if args.file:
        threads.append(threading.Thread(target=feed_file, args=(eng, args.file, args.speed, args.start_at, stop), daemon=True))
    elif args.live:
        rec = Path(args.folder) / "udp.f1raw" if cfg.get("paths", {}).get("record_raw", True) else None
        if rec:
            print(f"Grabando los paquetes del juego en {rec} (para calibrar después; ~100 MB por hora de juego; "
                  "desactiva con record_raw = false en [paths] de config.toml).")
        mask = None if args.text else load_pad(cfg)
        if mask:
            lines = LineReader()
            pad = PadButton(mask, lines)
            print("Botón del mando activo para hablar (mantén pulsado mientras hablas; un toque corto = «toque»). ENTER también vale.")
        threads.append(threading.Thread(target=feed_live, args=(eng, args.port or cfg["network"]["port"], stop, rec,
                                                                pad.update if mask else None), daemon=True))
    for t in threads:
        t.start()
    print(f"Laboratorio de voz | motor: {sess.stt.name}/{sess.stt.model} | voz: {sess.speaker.voice} | registro: {sess.log_path}")
    print("Consejo: usa auriculares (si no, el micrófono graba al ingeniero). Ctrl+C cierra y deja el informe.")
    signal.signal(signal.SIGTERM, lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        read_text(sess, stop) if args.text else read_ptt(sess, stop, lines)
    except KeyboardInterrupt:
        print("\nCerrando...")
    stop.set()
    guard = threading.Timer(8.0, _hard_exit, args=(args.folder,))        # si algo se cuelga al cerrar, se sale igual y con informe
    guard.daemon = True
    guard.start()
    sess.speaker.wait(2)
    sess.close()
    eng.close()
    guard.cancel()
    print("\nListo.")


def _hard_exit(folder):
    """Salida de emergencia: escribe el informe y termina el proceso aunque algún hilo esté colgado."""
    try:
        print("\n(el cierre tardó demasiado: salgo ya; el informe queda guardado)", file=sys.stderr)
        print(pack_folder(folder))
        sys.stdout.flush()
    finally:
        os._exit(1)


def load_phrases(path):
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        out.append({"text": parts[0], "intent": parts[1] if len(parts) > 1 else None,
                    "driver": parts[2] if len(parts) > 2 and parts[2] else None})
    return out


def cmd_script(args, cfg):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    eng, sess = build_session(args, cfg, stamp)
    if not args.file:
        sys.exit("--file es necesario: el script usa los nombres de los pilotos de esa carrera.")
    sess.speaker.enabled = False                                       # durante la carga previa no se habla
    feed_file(eng, args.file, 1.0, args.start_at, until_lap=3)        # llena el estado (nombres, vueltas) y se detiene
    sess.speaker.enabled = not args.no_voice and sess.speaker.backend.available
    names = sess.names()
    if not names:
        sys.exit("La captura no trae nombres de pilotos todavía; usa --start-at para empezar en la carrera.")
    rnd = random.Random(args.seed)
    phrases = load_phrases(args.phrases)
    rnd.shuffle(phrases)
    ok = 0
    print(f"{len(phrases)} frases. Pulsa ENTER, di la frase y ENTER otra vez." if not args.text else f"{len(phrases)} frases (modo texto).")
    for k, ph in enumerate(phrases, 1):
        driver = rnd.choice(names) if "{P}" in ph["text"] else None
        expected = ph["text"].replace("{P}", driver or "")
        print(f"\n[{k}/{len(phrases)}] Di: «{expected}»")
        if args.text:
            audio = expected
        else:
            input("  [ENTER para hablar] ")
            sess.recorder.start()
            input("  grabando... (ENTER)")
            audio = sess.recorder.stop()
        rec = score_phrase(sess, audio, expected, ph, driver)
        ok += rec["ok"]
        print(f"  oí: «{rec['heard']}» | intención: {rec['got']} (esperada {ph['intent']}) | similitud {rec['sim']:.2f} -> "
              + ("OK" if rec["ok"] else "FALLO"))
    sess.log({"type": "script_end", "ok": ok, "n": len(phrases)})
    print(f"\nResultado: {ok}/{len(phrases)} frases bien entendidas y clasificadas.")
    sess.close()
    eng.close()


def score_phrase(sess, audio, expected, ph, driver):
    """Transcribe, clasifica y compara con lo esperado. No ejecuta la consulta (solo mide entender)."""
    prompt = STT.build_prompt(sess.names())
    if isinstance(audio, str):
        res, rms, peak = sess.stt.transcribe(audio, prompt), 1.0, 1.0
    else:
        rms, peak = sess.recorder.rms(audio), sess.recorder.peak(audio)
        res = {"text": "", "latency_s": 0.0, "reject": "silencio"} if peak < sess.min_peak else sess.stt.transcribe(audio, prompt)
    text, reason = (None, res.get("reject")) if res.get("reject") else STT.clean(res)
    intent = sess.eng.assistant.classify(text) if text else None
    got = intent.name if intent else None
    drv_ok = True
    if ph["driver"] and driver:
        drv_ok = bool(intent and intent.driver and sess.eng.tr.plain(intent.driver[1]) == sess.eng.tr.plain(driver))
    sim = STT.similarity(expected, text or "")
    ok = got == ph["intent"] and drv_ok
    rec = {"type": "script", "expected": expected, "expected_intent": ph["intent"], "expected_driver": driver,
           "heard": text, "raw": res.get("text"), "reject": reason, "got": got,
           "got_driver": intent.driver[1] if (intent and intent.driver) else None, "source": intent.source if intent else None,
           "sim": round(sim, 3), "ok": bool(ok), "stt_latency_s": res.get("latency_s"),
           "avg_logprob": res.get("avg_logprob"), "no_speech_prob": res.get("no_speech_prob"),
           "rms": round(rms, 4), "peak": round(peak, 4)}
    sess.log(rec)
    return rec


def synth_wav(text, voice, path, tts=None, run=subprocess.run):
    """Genera un WAV con el motor de voz (macOS `say` a 16 kHz mono; Windows SAPI; Linux espeak-ng) para pruebas sin micrófono."""
    tts = tts or SP.SayTTS()
    if not tts.can_synth:
        raise RuntimeError(f"el motor de voz «{tts.name}» no puede generar audio de prueba (en Linux instala espeak-ng)")
    cmd, data = tts.synth_command(text, voice, path)
    if data is None:
        run(cmd, check=True, timeout=30)
    else:                                                      # el texto por stdin, nunca en la línea de comandos
        run(cmd, check=True, timeout=30, input=data.encode("utf-8"))


def cmd_selftest(args, cfg):
    """Comprueba, una a una, las piezas que NO he podido ejecutar fuera de tu Mac."""
    results = []
    state = {}
    tts = SP.choose_backend(SP.tts_name(cfg))

    def check(name, fn):
        t0 = time.time()
        try:
            ok, detail = True, fn() or ""
        except Exception as e:                                   # noqa: BLE001
            ok, detail = False, f"{type(e).__name__}: {e}"
        results.append((name, ok))
        print(f"{'OK   ' if ok else 'FALLA'} {name:30s} {time.time() - t0:5.1f}s  {detail}")

    def voice_check():
        if not tts.available:
            raise RuntimeError(tts.hint)
        voice = args.voice or tts.find_voice()
        if not voice and tts.needs_voice:
            raise RuntimeError(tts.hint)
        state["voice"] = voice
        return voice or f"{tts.name}: voz «es» por defecto"

    def packages_check():
        import numpy, sounddevice                                # noqa: F401
        return f"numpy {numpy.__version__}, sounddevice {sounddevice.__version__}"

    def mic_check():
        print("      (di algo en voz alta durante este segundo)")
        r = STT.Recorder()
        r.start()
        time.sleep(1.0)
        level = r.rms(r.stop())
        if level == 0.0:
            raise RuntimeError("nivel EXACTAMENTE 0: casi seguro el permiso de micrófono está denegado. Ajustes del Sistema > "
                               "Privacidad y seguridad > Micrófono: activa la app desde la que ejecutas esto (Terminal, iTerm, VS Code...) "
                               "y vuelve a ejecutar")
        if level < 0.002:
            return f"nivel {level:.4f} (muy bajo: ¿hablaste? ¿micrófono correcto? ver `python -c \"import sounddevice as s; print(s.query_devices())\"`)"
        return f"nivel {level:.4f}"

    def stt_check():
        state["stt"] = STT.make_stt(args.stt, args.model)
        return f"{state['stt'].name}/{state['stt'].model}"

    def roundtrip():
        phrase = "¿Cómo voy de neumáticos y de combustible?"
        with tempfile.TemporaryDirectory() as d:
            wav = Path(d) / "t.wav"
            synth_wav(phrase, state["voice"], wav, tts)
            res = state["stt"].transcribe(str(wav), STT.build_prompt(["Verstappen", "Piastri"]))
        sim = STT.similarity(phrase, res["text"])
        if sim < 0.7:
            raise RuntimeError(f"entendió «{res['text']}» (similitud {sim:.2f})")
        return f"«{res['text']}» similitud {sim:.2f}, {res['latency_s']:.1f} s"

    def say_check():
        phrase = "Prueba de radio. Si me oyes, funciona."
        if tts.name == "say":                                    # Mac: igual que siempre
            subprocess.run(["say", "-v", state["voice"], phrase], check=True, timeout=20)
            return
        cmd, data = tts.command(phrase, state["voice"], 190)
        subprocess.run(cmd, check=True, timeout=30, **({"input": data.encode("utf-8")} if data is not None else {}))

    check("voz española instalada", voice_check)
    check("paquetes de audio", packages_check)
    check("micrófono (1 s)", mic_check)
    check(f"motor de voz {args.stt}/{args.model}", stt_check)
    if "stt" in state and "voice" in state:
        if tts.can_synth:
            check(f"viaje voz a texto ({tts.name}->STT)", roundtrip)
        else:
            print(f"OMITIDA viaje voz a texto: el motor «{tts.name}» no genera audio de prueba (instala espeak-ng para probarlo)")
    if args.say_test and "voice" in state:
        check("hablar por los altavoces", say_check)
    bad = [n for n, ok in results if not ok]
    print("\n" + ("Todo en orden." if not bad else f"{len(bad)} comprobación(es) fallaron: pégame esta salida completa."))
    return 1 if bad else 0


def cmd_bench(args, cfg):
    tts = SP.choose_backend(SP.tts_name(cfg))
    if not tts.can_synth:
        sys.exit(f"bench necesita generar audio de prueba y el motor de voz «{tts.name}» no puede. {tts.hint or 'En Linux: instala espeak-ng.'}")
    voice = args.voice or tts.find_voice()
    phrases = ["¿Cómo voy de neumáticos?", "Quiero parar ya, no me siento cómodo con las gomas",
               "¿Puedo hacer undercut a Piastri?", "Sí"]
    with tempfile.TemporaryDirectory() as d:
        wavs = []
        for k, p in enumerate(phrases):
            w = Path(d) / f"p{k}.wav"
            synth_wav(p, voice, w, tts)
            wavs.append((p, str(w)))
        print(f"{'modelo':28s} {'carga s':>8s} {'mediana s':>10s} {'peor s':>8s}  similitud media")
        for model in [m.strip() for m in args.models.split(",")]:
            t0 = time.time()
            try:
                stt = STT.make_stt(args.stt, model)
            except RuntimeError as e:
                print(f"{model:28s} no disponible: {str(e)[:100]}")
                continue
            load = time.time() - t0
            stt.transcribe(wavs[0][1])                           # calentamiento (la primera llamada es lenta)
            lat, sims = [], []
            for _ in range(2):
                for p, w in wavs:
                    r = stt.transcribe(w, STT.build_prompt(["Piastri", "Verstappen"]))
                    lat.append(r["latency_s"])
                    sims.append(STT.similarity(p, r["text"]))
            lat.sort()
            print(f"{stt.name + '/' + model:28s} {load:8.1f} {lat[len(lat) // 2]:10.2f} {lat[-1]:8.2f}  {sum(sims) / len(sims):.2f}")


def cmd_llm(args, cfg):
    """Mide el LLM real como cerebro: para cada frase de llm_phrases.txt, qué herramientas elige, cuánto tarda y si la redacción
    se acepta. Corre sobre una captura (por defecto, la demo corta) sin micrófono ni voz."""
    import statistics
    path = Path(args.file or C.ROOT / "captures" / "demo_corta.f1raw")
    if not path.exists():
        sys.exit(f"No encuentro la captura {path}.")
    args.no_llm = False
    eng = make_engine(args, cfg, args.folder / "engine.jsonl")
    brain = eng.assistant.brain
    if brain is None or not brain.available():
        sys.exit("El LLM no responde (¿LM Studio con el servidor encendido y el modelo cargado?): sin él no hay nada que medir.")
    feed_file(eng, path, 0, until_lap=5)
    names = sorted({n for i, n in eng.tr.names.items() if i != eng.tr.player})
    rows, ok_json, hits, lat_r, lat_c = [], 0, 0, [], []
    print(f"\n{'frase':58s} {'esperado':16s} {'elegido':24s} {'ruta s':>6s} {'total s':>7s}")
    for ph in load_phrases(args.phrases if Path(args.phrases).name.startswith("llm_phrases") else C.ROOT / "llm_phrases.txt"):
        text = ph["text"].replace("{P}", names[0] if names else "Hansen")
        want = ph["intent"] or ""
        brain.down_until = 0.0            # medir cada frase con el modelo: un fallo no debe apagarlo para las siguientes
        t0 = time.time()
        plan = eng.assistant.prepare(text)
        t1 = time.time()
        with eng.lock:
            reply = eng.assistant.execute(plan, text)
        final = eng.assistant.finish(plan, text, reply)
        t2 = time.time()
        got = ",".join(plan.tools) or ("charla" if plan.say else "-")
        valid = plan.source == "llm"
        ok_json += valid
        lat_r.append(plan.llm_s or (t1 - t0))
        done = set(plan.tools) | {i.name for i in plan.intents}     # las reglas rápidas (sí/no/avisos/silencio) no pasan por el modelo
        hit = (not want and got in ("charla", "-")) or bool(want and done & set(want.split("/")))
        hits += bool(hit)
        print(f"{text[:58]:58s} {want[:16]:16s} {got[:24]:24s} {plan.llm_s:6.1f} {t2 - t0:7.1f} {'OK ' if hit else 'MAL'} {final[:90]!r}")
        rows.append({"frase": text, "esperado": want, "elegido": got, "ok": bool(hit), "ruta_s": round(plan.llm_s, 2), "router_raw": plan.raw,
                     "total_s": round(t2 - t0, 2), "respuesta": final, "en_bruto": reply})
        eng.assistant.awaiting = None
        eng.dialog.pending = None            # cada frase se mide sola, sin una propuesta abierta de la anterior
        eng.dialog.muted_until = -1
        eng.tac.topics.clear()
    n = len(rows)
    st = brain.stats
    print(f"\nRESULTADO: {hits}/{n} frases con la herramienta esperada ({hits / n:.0%}); respuesta del router utilizable en {ok_json}/{n}; "
          f"ruta mediana {statistics.median(lat_r):.1f} s (máx {max(lat_r):.1f}); total mediana "
          f"{statistics.median(r['total_s'] for r in rows):.1f} s; redacciones rechazadas {st['compose_rejected']}; errores del servidor {st['errors']}.")
    (args.folder / "llm_eval.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


def cmd_boton(args, cfg):
    """Aprende qué botón del mando usarás para hablar: el juego manda los botones pulsados por UDP (evento BUTN). Pulsas el botón
    (p. ej. R3) tres veces y se guarda su bit en data/mando.json. No hace falta saber de antemano qué bit es."""
    port = args.port or cfg["network"]["port"]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    sock.settimeout(0.5)
    print(f"Escuchando el juego en UDP {port}. Entra en pista (o en el garaje) y PULSA Y SUELTA el botón para hablar (p. ej. R3) "
          "3 veces, despacio. Tienes 40 s.")
    rises, falls, prev, seen, t0 = {}, {}, 0, 0, time.time()
    while time.time() - t0 < getattr(args, "wait_s", 40):
        try:
            d, _ = sock.recvfrom(4096)
        except socket.timeout:
            continue
        st = button_status(d)
        if st is None:
            continue
        seen += 1
        for bit in range(32):
            m = 1 << bit
            if st & m and not prev & m:
                rises[m] = rises.get(m, 0) + 1
                print(f"  pulsado (bit {bit})")
            elif prev & m and not st & m:
                falls[m] = falls.get(m, 0) + 1
        prev = st
        best = max(rises, key=rises.get) if rises else None
        if best and rises[best] >= 3:
            break
    sock.close()
    if not seen:
        sys.exit("No llegó ningún botón del juego. Comprueba que la telemetría UDP llega a este equipo (IP y puerto del juego) "
                 "y que estás en pista o en el garaje, no en un menú.")
    best = max(rises, key=rises.get) if rises else None
    if not best or rises[best] < 2:
        sys.exit("No vi el mismo botón pulsado al menos 2 veces. Repite `python voice.py boton` y pulsa solo ese botón.")
    out = Path(getattr(args, "out", None) or C.ROOT / "data" / "mando.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"mask": best, "bit": best.bit_length() - 1, "suelta": bool(falls.get(best))}, indent=1), encoding="utf-8")
    how = "mantén pulsado mientras hablas" if falls.get(best) else "pulsa para empezar y otra vez para terminar"
    print(f"Listo: botón guardado (bit {best.bit_length() - 1}) en {out}. En `python voice.py lab --live`: {how}.")


def cmd_report(args, cfg):
    import voice_audit
    print(voice_audit.render(voice_audit.summarize(args.files)))


def cmd_pack(args, cfg):
    """Vuelve a generar INFORME.txt de una sesión (por defecto, la última)."""
    runs = C.ROOT / cfg.get("paths", {}).get("runs", "runs")
    folder = Path(args.files[0]) if args.files else max(runs.glob("session-*"), key=lambda p: p.name, default=None)
    if folder is None:
        sys.exit("No hay sesiones en runs/.")
    print(pack_folder(folder))


def learn_from_recording(folder, cfg):
    """Aprende de la grabación de la sesión (pérdida de parada y desgaste por compuesto de ESTE circuito, de todos los coches) y guarda el
    perfil: la próxima vez la estrategia usa esos valores medidos en vez de los de serie. Solo cuenta carreras; nunca rompe el cierre."""
    import learn
    raw = Path(folder) / "udp.f1raw"
    if not (cfg.get("paths", {}) or {}).get("learn_at_end", True) or not raw.exists() or raw.stat().st_size == 0:
        return
    try:
        profile = C.load_profile(cfg)
        profile.setdefault("tracks", {})
        s = learn.learn_file(raw, profile, cfg)
        if s["races"]:
            where = C.save_profile(profile, cfg)
            print(f"Aprendido de esta sesión: {s['races']} carrera(s) nuevas en el perfil ({where}).")
    except Exception as e:                                         # noqa: BLE001
        print(f"Aviso: no pude aprender de la grabación ({type(e).__name__}: {e}).")


def open_folder(path, platform=None, popen=subprocess.Popen, startfile=None, which=None):
    """Abre la carpeta en el explorador del sistema (Finder / Explorador / xdg-open). Siempre «si se puede»: nunca falla."""
    import shutil
    platform = platform or sys.platform
    try:
        if platform == "darwin":
            popen(["open", str(path)])
        elif platform.startswith("win"):
            (startfile or os.startfile)(str(path))            # noqa: os.startfile solo existe en Windows
        elif (which or shutil.which)("xdg-open"):
            popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            return False
        return True
    except Exception:                                              # noqa: BLE001
        return False


def export_for_sharing(folder, report, cfg):
    """Deja el informe y los paquetes grabados (comprimidos) juntos en UNA carpeta fácil de arrastrar: [paths] export_dir (por defecto
    ~/Downloads/f1-informe-<sesión>). Nunca rompe el cierre: cualquier fallo solo se avisa."""
    import gzip, shutil
    try:
        base = (cfg.get("paths", {}) or {}).get("export_dir", "~/Downloads")
        if not base:
            return None
        out = Path(base).expanduser() / f"f1-informe-{Path(folder).name.replace('session-', '')}"
        out.mkdir(parents=True, exist_ok=True)
        shutil.copy2(report, out / "INFORME.txt")
        raw = Path(folder) / "udp.f1raw"
        msg = f"  {out / 'INFORME.txt'}"
        if raw.exists() and raw.stat().st_size > 0:
            gz = out / "udp.f1raw.gz"
            with open(raw, "rb") as src, gzip.open(gz, "wb", compresslevel=6) as dst:
                shutil.copyfileobj(src, dst)
            msg += f"\n  {gz} ({gz.stat().st_size / 1e6:.0f} MB; se reproduce igual con `lab --file`)"
        print(f"Carpeta lista para enviarme (informe + paquetes del juego):\n{msg}")
        if (cfg.get("paths", {}) or {}).get("open_folder_at_end", True):
            open_folder(out)
        return out
    except Exception as e:                                         # noqa: BLE001
        print(f"Aviso: no pude preparar la carpeta para enviar ({type(e).__name__}: {e}); los archivos siguen en {folder}.")
        return None


def pack_folder(folder):
    import voice_audit
    return BB.pack(folder, lambda f: voice_audit.render(voice_audit.summarize([f])))


def main():
    C.utf8_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["lab", "script", "selftest", "bench", "report", "pack", "llm", "boton"])
    ap.add_argument("files", nargs="*", help="para 'report'/'pack': carpetas runs/session-* (o archivos voice.jsonl)")
    ap.add_argument("--file", help="captura .f1raw a reproducir")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--port", type=int)
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--start-at", type=float, default=0.0, help="segundos de captura a saltar a toda velocidad")
    ap.add_argument("--stt", default="auto", choices=["auto", "mlx", "faster", "fake"])
    ap.add_argument("--model", default=None, help="tiny|base|small|medium|large|turbo (o un repo/modelo completo; por defecto "
                    "medium en Mac y small en Windows/Linux, donde faster-whisper va en CPU)")
    ap.add_argument("--models", default="small,medium", help="para bench")
    ap.add_argument("--voice")
    ap.add_argument("--text", action="store_true", help="escribir en vez de hablar")
    ap.add_argument("--no-voice", action="store_true", help="no hablar (solo texto en pantalla)")
    ap.add_argument("--no-audio", action="store_true", help="no guardar el audio de tus frases")
    ap.add_argument("--say-test", action="store_true")
    ap.add_argument("--llm", action="store_true", help="(ya es el valor por defecto)")
    ap.add_argument("--no-llm", action="store_true", help="solo reglas, sin LLM")
    ap.add_argument("--mode", default=None)
    ap.add_argument("--phrases", default=str(C.ROOT / "voice_phrases.txt"))
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--config")
    a = ap.parse_args()
    a.model = a.model or STT.default_model()
    cfg = C.load(a.config)
    fn = {"lab": cmd_lab, "script": cmd_script, "selftest": cmd_selftest, "bench": cmd_bench, "report": cmd_report,
          "pack": cmd_pack, "llm": cmd_llm, "boton": cmd_boton}[a.cmd]
    if a.cmd in ("report", "pack", "boton"):
        sys.exit(fn(a, cfg) or 0)
    runs = C.ROOT / cfg.get("paths", {}).get("runs", "runs")
    a.folder = runs / f"session-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{a.cmd}"
    bb = BB.Session(a.folder, sys.argv, (C.ROOT / "config.toml").read_text(encoding="utf-8"))
    code = 0
    try:
        code = fn(a, cfg) or 0
    except SystemExit as e:                                        # sys.exit("mensaje"): que el mensaje quede en el registro
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            code = 1
        else:
            code = e.code or 0
    except Exception:                                              # noqa: BLE001  (el traceback se escribe MIENTRAS se graba)
        import traceback
        traceback.print_exc()
        code = 1
    finally:
        bb.stop()
        path = pack_folder(a.folder)
        print(f"\nTodo quedó registrado. Envíame este archivo (texto, sin audio): {path}")
        if a.cmd == "lab" and a.live:                                  # solo la sesión en vivo graba paquetes; las demás órdenes no exportan nada
            learn_from_recording(a.folder, cfg)
            export_for_sharing(a.folder, path, cfg)
    sys.exit(code)


if __name__ == "__main__":
    main()
