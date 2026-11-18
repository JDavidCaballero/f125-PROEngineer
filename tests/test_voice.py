import json, tempfile, threading, time, unittest
from pathlib import Path

from helpers import cfg
from test_assistant import run_until
import assistant as A
import radio as R
import speaker as SP
import stt as STT
import voice as V
import voice_audit as VA


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class FakePopen:
    """Sustituye a `say`: 'habla' durante dur segundos y se puede cortar."""
    log = []

    def __init__(self, cmd, dur=0.05):
        self.cmd, self.dur, self.t0, self.dead = cmd, dur, time.time(), False
        FakePopen.log.append(cmd[-1])

    def poll(self):
        return 0 if (self.dead or time.time() - self.t0 >= self.dur) else None

    def wait(self):
        while self.poll() is None:
            time.sleep(0.005)

    def terminate(self):
        self.dead = True


def make_speaker(dur=0.05):
    FakePopen.log = []
    return SP.Speaker(voice="Mónica", popen=lambda cmd: FakePopen(cmd, dur))


class RadioProtocol(unittest.TestCase):
    def test_not_understood_repeats_forever_when_nothing_is_pending(self):
        r = R.Radio(cfg())
        for k in range(6):
            acts = r.heard(100 + k, understood=False, pending=False)
            self.assertEqual([a.text for a in acts], [R.NOT_UNDERSTOOD])
        self.assertEqual(r.state, "idle")

    def test_empty_gets_a_different_message(self):
        self.assertEqual(R.Radio(cfg()).heard(1, False, empty=True)[0].text, R.NOT_HEARD)

    def test_two_failures_with_a_pending_proposal_enter_fallback(self):
        r = R.Radio(cfg())
        self.assertEqual(r.heard(100, False, pending=True)[0].text, R.NOT_UNDERSTOOD)
        acts = r.heard(104, False, pending=True)
        self.assertEqual(acts[0].text, R.FALLBACK)
        self.assertEqual(r.state, "fallback")
        self.assertIsNone(r.deadline)                         # aún se está explicando: la ventana no corre
        r.arm(110)                                            # terminó de hablar
        self.assertAlmostEqual(r.deadline, 110 + r.window_s)

    def test_failures_inside_fallback_repeat_short_and_restart_the_window(self):
        r = R.Radio(cfg())
        r.heard(100, False, pending=True)
        r.heard(101, False, pending=True)
        r.arm(110)
        acts = r.heard(112, False, pending=True)
        self.assertEqual([a.text for a in acts], [R.FALLBACK_AGAIN])
        self.assertEqual(r.state, "fallback")
        self.assertIsNone(r.deadline)                         # se vuelve a armar cuando termine de hablar
        self.assertLess(len(R.FALLBACK_AGAIN), len(R.FALLBACK))

    def test_tap_in_fallback_confirms(self):
        r = R.Radio(cfg())
        r.heard(100, False, pending=True)
        r.heard(101, False, pending=True)
        acts = r.tap(103, pending=True)
        self.assertEqual([a.kind for a in acts], ["accept", "say"])
        self.assertEqual(r.state, "idle")

    def test_tap_outside_fallback_does_nothing(self):
        r = R.Radio(cfg())
        self.assertEqual(r.tap(100, pending=True), [])
        r.heard(100, False, pending=True)                    # un solo fallo: aún no hay respaldo
        self.assertEqual(r.tap(101, pending=True), [])

    def test_silence_declines_when_the_window_ends(self):
        r = R.Radio(cfg())
        r.heard(100, False, pending=True)
        r.heard(101, False, pending=True)
        r.arm(103)                                           # la explicación terminó de sonar a las 103
        self.assertEqual(r.tick(105, True), [])              # aún dentro de la ventana
        acts = r.tick(103 + r.window_s + 0.01, True)
        self.assertEqual([a.kind for a in acts], ["decline", "say"])
        self.assertEqual(r.state, "idle")
        self.assertEqual(r.tick(200, True), [])              # no se repite

    def test_window_does_not_run_while_the_explanation_is_being_spoken(self):
        # el fallo real: la ventana de 7 s corría mientras sonaba la explicación (de ~8 s)
        r = R.Radio(cfg())
        r.heard(100, False, pending=True)
        r.heard(101, False, pending=True)
        self.assertEqual(r.tick(101 + 9, True), [])          # 9 s después, aún sin armar: NO rechaza
        self.assertEqual(r.tick(101 + r.arm_timeout_s + 1, True), [])   # pasa el seguro: se arma ahora
        self.assertIsNotNone(r.deadline)
        self.assertEqual(r.tick(101 + r.arm_timeout_s + 1 + r.window_s - 1, True), [])
        self.assertEqual(r.tick(101 + r.arm_timeout_s + 1 + r.window_s + 0.1, True)[0].kind, "decline")

    def test_fallback_prompt_is_short_enough_to_say_in_a_few_seconds(self):
        self.assertLessEqual(len(R.FALLBACK.split()), 14)

    def test_understood_cancels_fallback(self):
        r = R.Radio(cfg())
        r.heard(100, False, pending=True)
        r.heard(101, False, pending=True)
        self.assertEqual(r.heard(103, True, pending=True), [])
        self.assertEqual((r.state, r.misses), ("idle", 0))
        self.assertEqual(r.tick(500, True), [])

    def test_proposal_gone_clears_fallback_without_acting(self):
        r = R.Radio(cfg())
        r.heard(100, False, pending=True)
        r.heard(101, False, pending=True)
        self.assertEqual(r.tick(102, pending=False), [])
        self.assertEqual(r.state, "idle")

    def test_new_proposal_resets_misses(self):
        r = R.Radio(cfg())
        r.heard(100, False, pending=True)
        r.new_proposal()
        self.assertEqual(r.misses, 0)

    def test_tap_detection(self):
        r = R.Radio(cfg())
        self.assertTrue(r.is_tap(0.2, spoke=False))
        self.assertFalse(r.is_tap(0.2, spoke=True))
        self.assertFalse(r.is_tap(1.5, spoke=False))


class SpeakerQueue(unittest.TestCase):
    def test_priority_one_cuts_current_speech(self):
        s = make_speaker(dur=0.4)
        s.say("mensaje largo", 2)
        time.sleep(0.05)
        s.say("¡urgente!", 1)
        s.wait(3)
        self.assertIn("¡urgente!", s.spoken)
        self.assertIn(("mensaje largo", "cortado"), s.dropped)
        s.close()

    def test_priority_three_is_dropped_while_busy(self):
        s = make_speaker(dur=0.3)
        s.say("hablando", 2)
        time.sleep(0.05)
        self.assertFalse(s.say("dato menor", 3))
        s.wait(3)
        self.assertNotIn("dato menor", s.spoken)
        s.close()

    def test_barge_in_clears_queue(self):
        s = make_speaker(dur=0.4)
        s.say("uno", 2)
        s.say("dos", 2)
        time.sleep(0.05)
        s.stop()
        s.wait(3)
        self.assertNotIn("dos", s.spoken)
        s.close()

    def test_queue_is_bounded(self):
        s = make_speaker(dur=0.3)
        for k in range(8):
            s.say(f"m{k}", 2)
        s.wait(5)
        self.assertLess(len(s.spoken), 8)
        self.assertTrue(s.dropped)
        s.close()

    def test_hold_silences_new_messages_until_release(self):
        s = make_speaker(dur=0.05)
        s.hold()
        self.assertTrue(s.say("aviso 1", 2))
        self.assertFalse(s.say("dato menor", 3))
        s.say("aviso 2", 1)
        time.sleep(0.15)
        self.assertEqual(FakePopen.log, [])                   # nada suena mientras el piloto habla
        s.release()
        s.wait(3)
        self.assertEqual(FakePopen.log[0], "aviso 2")          # lo crítico primero
        self.assertIn("aviso 1", FakePopen.log)
        self.assertNotIn("dato menor", FakePopen.log)
        s.close()

    def test_release_keeps_only_the_latest_three(self):
        s = make_speaker(dur=0.02)
        s.hold()
        for k in range(6):
            s.say(f"m{k}", 2)
        s.release()
        s.wait(3)
        self.assertEqual(FakePopen.log, ["m3", "m4", "m5"])
        self.assertEqual(sum(1 for _, why in s.dropped if why == "retenido y viejo"), 3)
        s.close()

    def test_hold_cuts_what_is_playing(self):
        s = make_speaker(dur=0.5)
        s.say("larga", 2)
        time.sleep(0.05)
        s.hold()
        s.wait(2)
        self.assertIn(("larga", "cortado"), s.dropped)
        s.release()
        s.close()

    def test_find_spanish_voice_parses_say_output(self):
        out = ("Alex                en_US    # Most people recognize me\nPaulina             es_MX    # Hola, me llamo Paulina.\n"
               "Mónica              es_ES    # Hola, me llamo Mónica.\nThomas              fr_FR    # Bonjour\n")

        class P:
            stdout = out
        self.assertEqual(SP.find_spanish_voice(lambda *a, **k: P), "Mónica")

        class Q:
            stdout = "Alex en_US # x\n"
        self.assertIsNone(SP.find_spanish_voice(lambda *a, **k: Q))
        self.assertIsNone(SP.find_spanish_voice(lambda *a, **k: (_ for _ in ()).throw(OSError())))


class STTHelpers(unittest.TestCase):
    def test_wav_is_read_without_ffmpeg_or_numpy(self):
        import wave, struct
        p = Path(tempfile.mkdtemp()) / "t.wav"
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(struct.pack("<4h", 0, 16384, -16384, 32767))
        pcm, rate, ch = STT.read_wav_pcm(p)
        self.assertEqual((rate, ch, struct.unpack("<4h", pcm)), (16000, 1, (0, 16384, -16384, 32767)))


    def test_hallucinations_and_low_confidence_are_rejected(self):
        ok = {"text": "¿Cómo voy de gomas?", "avg_logprob": -0.3, "no_speech_prob": 0.02}
        self.assertEqual(STT.clean(ok), ("¿Cómo voy de gomas?", None))
        for bad in ({"text": "Subtítulos por la comunidad de Amara.org"}, {"text": "  "}, {"text": "hola", "no_speech_prob": 0.9},
                    {"text": "hola", "avg_logprob": -2.0}, {"text": "Gracias por ver el vídeo"}):
            self.assertIsNone(STT.clean(bad)[0], bad)
            self.assertTrue(STT.clean(bad)[1])

    def test_the_blablabla_loop_from_the_real_session_is_rejected(self):
        loop = "Blablabla" + "bla" * 120 + " " + " ".join(["Blabl"] * 40)
        self.assertTrue(STT.looks_like_loop(loop))
        self.assertEqual(STT.clean({"text": loop, "avg_logprob": -0.3, "no_speech_prob": 0.3})[1], "bucle de repetición de Whisper")
        self.assertTrue(STT.looks_like_loop("bla bla bla bla bla bla bla bla bla bla bla bla bla bla"))
        self.assertFalse(STT.looks_like_loop("Blablabla."))                    # una palabra rara corta no es un bucle
        self.assertFalse(STT.looks_like_loop("¿Cuánto combustible tengo y cómo van las gomas de atrás?"))

    def test_text_too_long_for_the_audio_is_rejected(self):
        res = {"text": "a " * 120, "avg_logprob": -0.3, "no_speech_prob": 0.1, "duration_s": 2.0}
        res["text"] = " ".join(f"palabra{k}" for k in range(30))
        self.assertEqual(STT.clean(res)[1], "texto demasiado largo para el audio")
        res["duration_s"] = 30.0
        self.assertIsNone(STT.clean(res)[1])

    def test_similarity(self):
        self.assertEqual(STT.similarity("¿Cómo voy?", "como voy"), 1.0)
        self.assertGreater(STT.similarity("cómo voy contra Piastri", "como voy contra Pi astri"), 0.5)
        self.assertLess(STT.similarity("quiero parar ya", "buenos días señor"), 0.3)
        self.assertEqual(STT.similarity("", ""), 1.0)

    def test_prompt_contains_names_and_vocabulary(self):
        p = STT.build_prompt(["Piastri", "Verstappen"])
        self.assertIn("Piastri", p)
        self.assertIn("undercut", p)
        self.assertIn("neumáticos", p)

    def test_make_stt_without_backends_explains_how_to_fix(self):
        try:
            STT.make_stt("auto", "small")
        except RuntimeError as e:
            self.assertIn(STT.setup_hint(), str(e))          # el instalador de ESTE sistema (Mac: setup_mac.sh; Linux/CI: setup_linux.sh)
        else:                                   # si el equipo ya tiene un motor instalado, también es válido
            pass


def session(t_stop=388, texts=(), dur=0.02, mode="normal"):
    eng, out, it = run_until("corta", t_stop, mode)
    clock = Clock()
    log = Path(tempfile.mkdtemp()) / "voice.jsonl"
    sp = make_speaker(dur)
    sess = V.VoiceSession(cfg(), eng, sp, STT.FakeSTT(texts), None, log, None, clock=clock, echo=lambda *a: None)
    sess.rest = it                                           # lo que queda de la carrera por reproducir
    return eng, sess, clock, log, sp


def records(log):
    return [json.loads(l) for l in Path(log).read_text().splitlines()]


class SessionFlow(unittest.TestCase):
    def test_yes_accepts_the_pending_proposal_and_logs_everything(self):
        eng, sess, clock, log, sp = session()
        self.assertIsNotNone(eng.dialog.pending)
        r = sess.utterance("sí, hazlo", 1.2)
        self.assertTrue(r["understood"])
        self.assertEqual((r["intent"], r["source"]), ("si", "reglas"))
        self.assertIsNotNone(eng.dialog.accepted)
        sp.wait(2)
        self.assertTrue(any("plan confirmado" in s for s in sp.spoken))
        u = [x for x in records(log) if x["type"] == "utt"][0]
        for k in ("stt", "raw", "clean", "intent", "reply", "t_total_s", "t_nlu_s", "pending_before", "misses"):
            self.assertIn(k, u)
        self.assertTrue(u["pending_before"])

    def test_not_understood_twice_then_tap_confirms(self):
        eng, sess, clock, log, sp = session()
        r1 = sess.utterance("blablabla xyz", 1.0)
        self.assertFalse(r1["understood"])
        self.assertEqual(r1["radio"][0]["text"], R.NOT_UNDERSTOOD)
        r2 = sess.utterance("mmm eh", 1.0)
        self.assertEqual(r2["radio"][0]["text"], R.FALLBACK)
        self.assertEqual(sess.radio.state, "fallback")
        sess.tap(0.2)
        self.assertIsNotNone(eng.dialog.accepted)
        self.assertEqual(sess.radio.state, "idle")
        kinds = [x["source"] for x in records(log) if x["type"] == "radio"]
        self.assertIn("toque", kinds)

    def test_silence_in_fallback_declines(self):
        eng, sess, clock, log, sp = session()
        sess.utterance("xx yy", 1.0)
        sess.utterance("zz ww", 1.0)
        sess.tick()                                          # mientras suena la explicación, la ventana no corre
        self.assertEqual(sess.radio.state, "fallback")
        end = time.time() + 5
        while sess.radio.deadline is None and time.time() < end:
            time.sleep(0.02)                                 # al terminar de hablar se arma la ventana
        self.assertIsNotNone(sess.radio.deadline)
        self.assertTrue(any(x["type"] == "radio_armed" for x in records(log)))
        clock.t += sess.radio.window_s + 1
        sess.tick()
        self.assertIsNone(eng.dialog.pending)
        self.assertIsNone(eng.dialog.accepted)
        self.assertEqual(eng.dialog.history[-1]["state"], "rechazada")
        self.assertIn("silencio", [x["source"] for x in records(log) if x["type"] == "radio"])

    def test_understanding_again_after_one_failure_does_not_trigger_fallback(self):
        eng, sess, clock, log, sp = session()
        sess.utterance("xx yy", 1.0)
        r = sess.utterance("no", 1.0)
        self.assertTrue(r["understood"])
        self.assertEqual(sess.radio.state, "idle")
        self.assertEqual(eng.dialog.history[-1]["state"], "rechazada")

    def test_no_pending_never_enters_fallback(self):
        eng, sess, clock, log, sp = session(t_stop=100)
        self.assertIsNone(eng.dialog.pending)
        for _ in range(5):
            r = sess.utterance("qwerty asdf", 1.0)
            self.assertEqual(r["radio"][0]["text"], R.NOT_UNDERSTOOD)
        self.assertEqual(sess.radio.state, "idle")

    def test_whisper_hallucination_counts_as_not_heard(self):
        eng, sess, clock, log, sp = session(texts=[{"text": "Subtítulos por la comunidad de Amara.org", "avg_logprob": -0.4}])
        r = sess.utterance(sess.stt.texts.pop(0), 1.0)
        self.assertIsNotNone(r["reject"])
        self.assertFalse(r["understood"])
        self.assertEqual(r["radio"][0]["text"], R.NOT_HEARD)

    def test_engine_speech_goes_to_the_speaker_and_the_log(self):
        eng, sess, clock, log, sp = session(t_stop=330)       # justo antes del Safety Car de la demo
        for t, d in sess.rest:
            eng.on_packet(t, d)
            if t > 400:
                break
        sp.wait(3)
        said = [x for x in records(log) if x["type"] == "engine_say"]
        self.assertTrue(any("Safety Car desplegado" in x["text"] for x in said))
        self.assertTrue(any("Propuesta" in x["text"] for x in said))
        self.assertTrue(any("Safety Car desplegado" in s for s in sp.spoken))
        self.assertTrue(all(x["prio"] in (1, 2, 3) for x in said))

    def test_new_proposal_resets_radio(self):
        eng, sess, clock, log, sp = session()
        sess.utterance("xx yy", 1.0)
        self.assertEqual(sess.radio.misses, 1)
        import copy
        other = copy.copy(eng.dialog.pending.option)
        other.stops = [(other.stops[0][0] + 3, other.stops[0][1])]          # otra parada distinta = propuesta nueva
        eng.dialog.propose(other, "otra", eng.tr.cur_lap, eng.tr.t)
        sess._track_proposal()
        self.assertEqual(sess.radio.misses, 0)

    def test_tap_without_fallback_is_ignored_and_logged(self):
        eng, sess, clock, log, sp = session()
        sess.tap(0.2)
        self.assertIsNone(eng.dialog.accepted)
        t = [x for x in records(log) if x["type"] == "tap"][0]
        self.assertEqual(t["state"], "idle")

    def test_held_proposal_cannot_be_confirmed_by_tap(self):
        eng, sess, clock, log, sp = session()
        eng.held = ("texto", eng.tr.t)                       # propuesta retenida: el piloto aún no la ha oído
        self.assertFalse(sess.pending_spoken())
        sess.utterance("xx yy", 1.0)
        sess.utterance("zz ww", 1.0)
        self.assertEqual(sess.radio.state, "idle")           # sin propuesta oída no hay canal de respaldo


class FakeRecorder:
    """Micrófono simulado: mide con los valores que se le den (sin numpy)."""
    def __init__(self, rms, peak):
        self._rms, self._peak = rms, peak

    def rms(self, audio):
        return self._rms

    def peak(self, audio):
        return self._peak

    def save_wav(self, audio, path):
        Path(path).write_bytes(b"RIFF")


import queue as _queue


class BlockingStream:
    """Teclado simulado: readline() espera hasta que el test 'pulsa' una tecla."""
    def __init__(self):
        self.q = _queue.Queue()

    def press(self, line="\n"):
        self.q.put(line)

    def close(self):
        self.q.put("")

    def readline(self):
        return self.q.get()


class FakeMic(FakeRecorder):
    def __init__(self, rms=0.03, peak=0.2, stop_delay=0.0):
        super().__init__(rms, peak)
        self.stop_delay, self.started = stop_delay, 0

    def start(self):
        self.started += 1

    def stop(self):
        time.sleep(self.stop_delay)
        return [0.1] * 16000


class SlowSTT(STT.FakeSTT):
    def transcribe(self, audio, prompt=""):
        time.sleep(3)
        return super().transcribe("sí")


def wait_for(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class PushToTalkLoop(unittest.TestCase):
    """Lo que pasó en la sesión real: grabación sin fin, avisos a mitad de tu frase y un cierre que no responde."""

    def build(self, texts=("sí",), mic=None, stt=None, t_stop=388):
        eng, out, it = run_until("corta", t_stop)
        sp = make_speaker(0.05)
        sess = V.VoiceSession(cfg(), eng, sp, stt or STT.FakeSTT(list(texts)), mic or FakeMic(),
                              Path(tempfile.mkdtemp()) / "v.jsonl", None, echo=lambda *a: None)
        stream, stop = BlockingStream(), threading.Event()
        lines = V.LineReader(stream)
        return eng, sess, sp, stream, stop, lines

    def start_loop(self, sess, stop, lines, **kw):
        th = threading.Thread(target=V.read_ptt, args=(sess, stop, lines), kwargs=kw, daemon=True)
        th.start()
        return th

    def recs(self, sess, kind):
        return [r for r in records(sess.log_path) if r["type"] == kind] if Path(sess.log_path).exists() else []

    def utts(self, sess):
        if not Path(sess.log_path).exists():
            return []
        return [r for r in records(sess.log_path) if r["type"] in ("utt", "utt_timeout", "mic_timeout")]

    def test_normal_enter_enter_processes_one_phrase_and_releases_the_speaker(self):
        eng, sess, sp, stream, stop, lines = self.build()
        th = self.start_loop(sess, stop, lines)
        stream.press(); time.sleep(0.05); self.assertTrue(sp.held)       # mientras grabas, el ingeniero calla
        stream.press()
        self.assertTrue(wait_for(lambda: self.utts(sess)))
        self.assertEqual(self.utts(sess)[0]["intent"], "si")
        self.assertTrue(wait_for(lambda: not sp.held))
        stream.close(); th.join(3)

    def test_engine_messages_during_recording_wait_until_the_pilot_finishes(self):
        eng, sess, sp, stream, stop, lines = self.build()
        th = self.start_loop(sess, stop, lines)
        stream.press(); time.sleep(0.1)
        sess._on_engine_say("aviso durante tu frase", 2)
        time.sleep(0.2)
        self.assertNotIn("aviso durante tu frase", FakePopen.log)            # no suena encima de ti
        stream.press()
        self.assertTrue(wait_for(lambda: "aviso durante tu frase" in FakePopen.log))   # y suena después, al soltar
        stream.close(); th.join(3)

    def test_recording_that_never_ends_is_cut_automatically(self):
        eng, sess, sp, stream, stop, lines = self.build()
        th = self.start_loop(sess, stop, lines, max_rec_s=0.3)
        stream.press()                                                       # empieza y nunca pulsa de nuevo
        self.assertTrue(wait_for(lambda: self.utts(sess)), "debe cortar y procesar sola")
        self.assertEqual(self.utts(sess)[0]["type"], "utt")
        stream.close(); th.join(3)

    def test_a_hung_transcription_does_not_block_the_session(self):
        eng, sess, sp, stream, stop, lines = self.build(stt=SlowSTT())
        th = self.start_loop(sess, stop, lines, stt_timeout=0.4)
        stream.press(); time.sleep(0.05); stream.press()
        self.assertTrue(wait_for(lambda: [u for u in self.utts(sess) if u["type"] == "utt_timeout"], 5))
        self.assertTrue(wait_for(lambda: not sp.held))
        stream.press(); time.sleep(0.05)                                     # y se puede seguir hablando
        self.assertTrue(sess.recorder.started >= 2)
        stream.close(); th.join(3)

    def test_a_hung_microphone_stop_is_recovered_and_the_session_continues(self):
        """Informe real: un fallo del micrófono en plena carrera cerraba TODO el programa. Ahora se reinicia el audio y sigue."""
        eng, sess, sp, stream, stop, lines = self.build(mic=FakeMic(stop_delay=3))
        sess.recorder_factory = lambda: FakeMic()
        th = self.start_loop(sess, stop, lines, mic_timeout=0.3)
        stream.press(); time.sleep(0.05); stream.press()
        self.assertTrue(wait_for(lambda: self.recs(sess, "mic_recovered"), 5))
        self.assertTrue(th.is_alive(), "el bucle NO debe terminar")
        stream.press(); time.sleep(0.05); stream.press()                    # y se puede seguir hablando
        self.assertTrue(wait_for(lambda: [u for u in self.utts(sess) if u["type"] == "utt"], 5))
        stream.close(); th.join(3)

    def test_a_hung_microphone_start_is_recovered_too(self):
        class HungStart(FakeMic):
            def start(self):
                time.sleep(3)
        eng, sess, sp, stream, stop, lines = self.build(mic=HungStart())
        sess.recorder_factory = lambda: FakeMic()
        th = self.start_loop(sess, stop, lines, mic_timeout=0.3)
        stream.press()
        self.assertTrue(wait_for(lambda: self.recs(sess, "mic_recovered"), 5))
        self.assertTrue(th.is_alive())
        self.assertTrue(wait_for(lambda: not sp.held))
        stream.close(); th.join(3)

    def test_a_microphone_that_errors_is_recovered(self):
        """Informe del 11 nov: «PortAudioError: Error querying device -1» al quitar los auriculares cerró la sesión."""
        class Gone(FakeMic):
            def start(self):
                raise RuntimeError("Error querying device -1")
        eng, sess, sp, stream, stop, lines = self.build(mic=Gone())
        sess.recorder_factory = lambda: FakeMic()
        th = self.start_loop(sess, stop, lines, mic_timeout=0.3)
        stream.press()
        self.assertTrue(wait_for(lambda: self.recs(sess, "mic_recovered"), 5))
        self.assertTrue(th.is_alive(), "el bucle NO debe terminar")
        stream.press(); time.sleep(0.05); stream.press()
        self.assertTrue(wait_for(lambda: [u for u in self.utts(sess) if u["type"] == "utt"], 5))
        stream.close(); th.join(3)

    def test_a_phrase_that_crashes_does_not_close_the_session(self):
        class Boom:
            name, model = "fake", "-"
            def transcribe(self, audio, prompt=""):
                raise ValueError("fallo interno")
        eng, sess, sp, stream, stop, lines = self.build(stt=Boom())
        th = self.start_loop(sess, stop, lines)
        stream.press(); time.sleep(0.05); stream.press()
        self.assertTrue(wait_for(lambda: self.recs(sess, "utt_error"), 5))
        self.assertTrue(th.is_alive())
        stream.close(); th.join(3)

    def test_unrecoverable_microphone_falls_back_to_typing_and_never_exits(self):
        eng, sess, sp, stream, stop, lines = self.build(mic=FakeMic(stop_delay=3))
        def broken():
            raise OSError("sin audio")
        sess.recorder_factory = broken
        V.RECOVER_WAIT_S = 0.05
        self.addCleanup(setattr, V, 'RECOVER_WAIT_S', 1.0)
        th = threading.Thread(target=V.read_ptt, args=(sess, stop, lines), kwargs=dict(mic_timeout=0.3, retry_s=0.4), daemon=True)
        th.start()
        stream.press(); time.sleep(0.05); stream.press()
        self.assertTrue(wait_for(lambda: self.recs(sess, "mic_lost"), 10))
        self.assertTrue(th.is_alive())
        stream.press_text = None
        stream.press("sí\n")                                                  # sin micrófono, el teclado funciona
        self.assertTrue(wait_for(lambda: [u for u in self.utts(sess) if u["type"] == "utt"], 5))
        self.assertTrue(th.is_alive())
        sess.recorder_factory = lambda: FakeMic()                              # el audio vuelve: se recupera en el siguiente reintento
        self.assertTrue(wait_for(lambda: self.recs(sess, "mic_recovered"), 10))
        stream.close(); th.join(3)

    def test_run_with_timeout(self):
        self.assertEqual(V.run_with_timeout(lambda: 7, 1.0), (True, 7))
        t0 = time.time()
        self.assertEqual(V.run_with_timeout(lambda: time.sleep(3), 0.3), (False, None))
        self.assertLess(time.time() - t0, 1.5)
        with self.assertRaises(ValueError):
            V.run_with_timeout(lambda: (_ for _ in ()).throw(ValueError("x")), 1.0)

    def test_line_reader_reports_eof_and_timeouts(self):
        s = BlockingStream()
        lr = V.LineReader(s)
        with self.assertRaises(_queue.Empty):
            lr.get(timeout=0.1)
        s.press("hola\n")
        self.assertEqual(lr.get(timeout=1), "hola\n")
        s.close()
        with self.assertRaises(EOFError):
            lr.get(timeout=1)
        with self.assertRaises(EOFError):                                     # y sigue cerrado
            lr.get(timeout=1)


class SilenceDetection(unittest.TestCase):
    def sess(self, rms, peak, text="sí"):
        eng, out, it = run_until("corta", 388)
        s = V.VoiceSession(cfg(), eng, make_speaker(), STT.FakeSTT([text]), FakeRecorder(rms, peak),
                           Path(tempfile.mkdtemp()) / "v.jsonl", None, echo=lambda *a: None)
        return s

    def test_a_calm_single_word_has_low_rms_but_is_not_silence(self):
        # caso real: la palabra "silencio" dicha con calma quedó con RMS bajo y se descartó por error
        r = self.sess(rms=0.002, peak=0.08).utterance([0.0] * 16000, 0.9)
        self.assertIsNone(r["reject"])
        self.assertEqual(r["intent"], "si")
        self.assertEqual((r["rms"], r["peak"]), (0.002, 0.08))

    def test_true_silence_is_rejected_without_calling_the_model(self):
        s = self.sess(rms=0.0004, peak=0.006)
        r = s.utterance([0.0] * 16000, 0.9)
        self.assertEqual(r["reject"], "silencio")
        self.assertEqual(s.stt.texts, ["sí"])                    # el motor ni se llamó
        self.assertEqual(r["radio"][0]["text"], R.NOT_HEARD)

    def test_script_records_levels(self):
        s = self.sess(rms=0.003, peak=0.1, text="¿Cómo van las gomas?")
        ph = {"text": "¿Cómo van las gomas?", "intent": "neumaticos", "driver": None}
        rec = V.score_phrase(s, [0.0] * 100, "¿Cómo van las gomas?", ph, None)
        self.assertTrue(rec["ok"])
        self.assertEqual((rec["rms"], rec["peak"]), (0.003, 0.1))


class AuditWords(unittest.TestCase):
    def test_accents_and_digits_do_not_count_as_missed_words(self):
        self.assertEqual(VA.words("¿Cómo voy en la posición 3?"), ["como", "voy", "en", "la", "posicion", "tres"])


class ScriptAndAudit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng, _, _ = run_until("corta", 300)
        cls.names = [cls.eng.tr.names[i] for i in range(22) if cls.eng.tr.names.get(i) and i != cls.eng.tr.player]

    def sess(self, texts):
        log = Path(tempfile.mkdtemp()) / "voice.jsonl"
        return V.VoiceSession(cfg(), self.eng, make_speaker(), STT.FakeSTT(texts), None, log, None, echo=lambda *a: None), log

    def test_phrase_file_is_consistent_with_the_assistant(self):
        ph = V.load_phrases(V.C.ROOT / "voice_phrases.txt")
        self.assertGreaterEqual(len(ph), 40)
        self.assertTrue(all(p["intent"] in A.INTENTS for p in ph))
        bad = []
        for p in ph:
            drv = self.names[2] if "{P}" in p["text"] else None
            it = A.parse_rules(self.eng.tr, p["text"].replace("{P}", drv or ""))
            if (it.name if it else None) != p["intent"] or (p["driver"] and not (it and it.driver)):
                bad.append(p["text"])
        self.assertEqual(bad, [], "las reglas deben entender todas las frases de la lista")

    def test_score_phrase_tolerates_small_errors_but_catches_wrong_driver(self):
        sess, log = self.sess([])
        ph = {"text": "¿Cómo voy contra {P}?", "intent": "hueco", "driver": "{P}"}
        target = "Hansen"
        good = V.score_phrase(sess, "¿Cómo voy contra Hanson?", "¿Cómo voy contra Hansen?", ph, target)
        self.assertTrue(good["ok"])
        wrong = V.score_phrase(sess, "¿Cómo voy contra Ortega?", "¿Cómo voy contra Hansen?", ph, target)
        self.assertFalse(wrong["ok"])
        self.assertEqual(wrong["got"], "hueco")                       # entendió la intención pero el piloto es otro
        miss = V.score_phrase(sess, "banana", "¿Cómo voy contra Hansen?", ph, target)
        self.assertFalse(miss["ok"])
        self.assertIsNone(miss["got"])

    def test_audit_report_summarises_a_session(self):
        eng, sess, clock, log, sp = session()
        sess.utterance("xx yy", 1.0)
        sess.utterance("zz ww", 1.0)
        sess.tap(0.2)
        sess.utterance("¿cuánto combustible tengo?", 1.0)
        ph = {"text": "¿Cómo van las gomas?", "intent": "neumaticos", "driver": None}
        V.score_phrase(sess, "como van las gomas", "¿Cómo van las gomas?", ph, None)
        V.score_phrase(sess, "las gomitas", "¿Cómo van las gomas?", ph, None)
        s = VA.summarize([log])
        self.assertEqual(s["utterances"], 3)
        self.assertEqual(s["script_phrases"], 2)
        self.assertEqual(s["understood_pct"], 33)
        self.assertEqual(s["fallback"]["entered"], 1)
        self.assertEqual(s["fallback"]["accepted_by_tap"], 1)
        self.assertEqual(s["script"]["ok"], 1)
        self.assertTrue(s["script"]["failures"])
        txt = VA.render(s)
        self.assertIn("AUDITORÍA DE VOZ", txt)
        self.assertIn("FALLO", txt)
        self.assertLess(len(txt.splitlines()), 60)                    # cabe pegado en un mensaje

    def test_audit_handles_empty_and_corrupt_logs(self):
        f = Path(tempfile.mkdtemp()) / "x.jsonl"
        f.write_text('{"type": "utt"\nbasura\n')
        s = VA.summarize([f])
        self.assertEqual(s["utterances"], 0)
        self.assertIn("AUDITORÍA", VA.render(s))


class Concurrency(unittest.TestCase):
    def test_questions_while_telemetry_streams_do_not_crash(self):
        from f1_parse import read_capture
        from helpers import demo
        eng, sess, clock, log, sp = session(t_stop=100)
        stop = threading.Event()
        errs = []

        def feed():
            try:
                for t, d in read_capture(demo("corta")[0]):
                    if t < 100:
                        continue
                    if stop.is_set() or t > 700:
                        return
                    eng.on_packet(t, d)
            except Exception as e:                                    # noqa: BLE001
                errs.append(e)
        th = threading.Thread(target=feed)
        th.start()
        for q in ("¿cómo voy de posición?", "¿cuánto combustible tengo?", "¿cómo van las gomas?", "qué hacemos", "sí", "no"):
            sess.utterance(q, 1.0)
            time.sleep(0.02)
        th.join(timeout=60)
        stop.set()
        self.assertEqual(errs, [])


if __name__ == "__main__":
    unittest.main()
