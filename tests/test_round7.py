"""Correcciones tras el segundo informe real (fin de semana en Spa: libres, sprint, clasificación, carrera de 11 vueltas)."""
import unittest
from types import SimpleNamespace

from helpers import cfg, demo, REAL
import assistant as A
import brain as B
import coach as K
import engineer as E
import raceplan as RP
import strategy as S
import tactics as T
from f1_parse import read_capture
from test_brain import Scripted, race_engine
from test_sessions import patched, run
from test_point1 import SessionEnd
from test_raceplan import tables, TestPractice


class GhostSessions(unittest.TestCase):
    """Captura real: entre sesiones el juego manda paquetes sueltos con session_uid 0. Antes cada uno creaba una «sesión nueva»
    (32 en un fin de semana de 5): intro y plan repetidos, finales perdidos, memoria duplicada."""

    def test_uid_zero_packets_do_not_reset_anything(self):
        path, _ = demo("corta")
        eng = E.Engineer(cfg(), "normal", None, out=lambda *_: None, sync=True)
        pk = list(read_capture(path))
        run(eng, pk[:3000])
        epoch, lap = eng.tr.epoch, eng.tr.cur_lap
        run(eng, list(patched(path, 0))[3000:3400])                 # paquetes de menús intercalados
        run(eng, pk[3000:3400])
        self.assertEqual(eng.tr.epoch, epoch)
        self.assertGreaterEqual(eng.tr.cur_lap, lap)

    def test_the_final_result_is_spoken_even_with_menu_packets_around_it(self):
        path, _ = demo("sprint")
        lines = []
        eng = E.Engineer(cfg(), "normal", None, out=lines.append, sync=True)
        pk = list(read_capture(path))
        mix = []
        for k, (t, d) in enumerate(pk):
            mix.append((t, d))
            if k % 400 == 0:
                mix.append((t, patched_one(d, 0)))
        run(eng, mix)
        self.assertEqual(len([l for l in lines if "Fin de carrera" in l]), 1, [l for l in lines if "Fin" in l])
        self.assertEqual(len([l for l in lines if l.endswith("sin paradas de neumáticos; céntrate en huecos, DRS y ERS.")]), 1)

    def test_real_capture_has_a_single_session_per_uid(self):
        if not REAL.exists():
            self.skipTest("sin captura real")
        eng = E.Engineer(cfg(), "normal", None, out=lambda *_: None, sync=True)
        for t, d in read_capture(REAL):
            eng.on_packet(t, d)
        self.assertLessEqual(eng.tr.epoch, 3)                       # antes: 8 (uid 0 incluido)


def patched_one(d, uid):
    import struct
    b = bytearray(d)
    struct.pack_into("<Q", b, 7, uid)
    return bytes(b)


class Events(unittest.TestCase):
    def test_garbage_fastest_lap_is_ignored(self):
        tr = SimpleNamespace(player=0, name=lambda i: f"coche {i}", is_race=lambda: False)
        t = T.Tactics(cfg())
        self.assertEqual(t._event(tr, {"code": "FTLP", "car": 12, "time_s": 3.07e17}), [])
        self.assertEqual(t._event(tr, {"code": "FTLP", "car": 12, "time_s": 0.0}), [])

    def test_others_fastest_lap_matters_in_qualifying_not_in_a_race(self):
        t = T.Tactics(cfg())
        q = SimpleNamespace(player=0, name=lambda i: "Leclerc", is_race=lambda: False)
        r = SimpleNamespace(player=0, name=lambda i: "Leclerc", is_race=lambda: True)
        self.assertTrue(t._event(q, {"code": "FTLP", "car": 5, "time_s": 103.4}))
        self.assertEqual(t._event(r, {"code": "FTLP", "car": 5, "time_s": 103.4}), [])
        self.assertTrue(t._event(r, {"code": "FTLP", "car": 0, "time_s": 103.4}))                      # la tuya sí

    def test_engine_damage_already_known_is_not_repeated_next_session(self):
        path, _ = demo("sprint")
        eng = E.Engineer(cfg(), "normal", None, out=lambda *_: None, sync=True)
        eng.tac.levels["dmg_engine"] = 2
        eng.tac.levels["wear"] = 2
        eng._new_session()
        self.assertEqual(eng.tac.levels.get("dmg_engine"), 2)
        self.assertNotIn("wear", eng.tac.levels)                    # el desgaste de neumáticos sí empieza de cero


class Chat(unittest.TestCase):
    def test_encouragement_rotates_and_is_not_the_models_job(self):
        eng = race_engine(Scripted(route=lambda c: {"tools": [], "say": ""}))
        got = {eng.ask("Mándeme ánimos que empiezo la vuelta.") for _ in range(4)}
        self.assertGreaterEqual(len(got), 3)
        self.assertTrue(got <= set(A.Assistant.ANIMOS))

    def test_garbled_encouragement_request_still_gets_encouragement(self):
        eng = race_engine(Scripted(route=lambda c: {"tools": [], "say": ""}))
        self.assertIn(eng.ask("Mandéme ánimos, empezaré mi vuelta de QALE."), A.Assistant.ANIMOS)

    def test_the_model_cannot_say_it_does_not_understand_or_claim_actions(self):
        for s in ("No entiendo qué quieres decir con 'bits'.", "Entendido, iniciando sesión de Sprint.", "Abriendo la sesión ahora."):
            self.assertEqual(B.clean_say(s, "entran los bits"), "", s)

    def test_a_driver_being_annoyed_is_not_a_strategy_question(self):
        eng = race_engine(Scripted(route=lambda c: {"tools": [], "say": "Mantén la calma y concéntrate."}))
        eng.tr.names[7] = "Ocon"
        self.assertIsNone(A.parse_rules(eng.tr, "Ocon está molesto conmigo, ¿qué hago?"))
        self.assertEqual(A.parse_rules(eng.tr, "¿qué hago?").name, "estrategia")
        self.assertIn(A.parse_rules(eng.tr, "¿qué hago con Ocon, paro ya?").name, ("estrategia", "parar_ya"))

    def test_chat_is_not_remembered_as_context(self):
        eng = race_engine(Scripted(route=lambda c: {"tools": [], "say": "Aquí estoy, piloto."}))
        eng.ask("hola ingeniero ¿qué tal estás hoy?")
        self.assertEqual(list(eng.assistant.brain.history), [])
        eng.ask("¿en qué posición voy?")
        self.assertEqual(len(eng.assistant.brain.history), 1)


class PlanDetails(unittest.TestCase):
    def test_practice_laps_are_not_taken_as_the_race_distance(self):
        eng = TestPractice.engine()
        short, _ = demo("sprint")
        run(eng, list(patched(short, 222))[:3])
        self.assertTrue(all(m["race_laps"] is None for m in eng.memory.tracks.values()))

    def test_no_pointless_same_compound_alternative_in_a_short_race(self):
        opts, info = RP.options(11, {16: tables()[16]}, S.Params(), 22.0)
        self.assertEqual([o["stops"] for o in opts], [0])

    def test_a_no_stop_plan_does_not_talk_about_pit_loss(self):
        opts, info = RP.options(5, tables(), S.Params(), 22.0)
        txt = RP.describe(opts, info, 5, 22.0, "valor por defecto", tables())
        self.assertNotIn("Parada de 22 s", txt)
        self.assertIn("Sin paradas", txt)


class CoachSanity(unittest.TestCase):
    def test_absurd_segment_gaps_are_ignored(self):
        rows = [{"seg": {"name": "la recta hacia la curva 1", "kind": "straight", "b0": 1, "b1": 6}, "dt_s": 5.06, "d_ers_kj": 0, "d_vmin": -112}]
        kept = [r for r in rows if abs(r["dt_s"]) <= K.MAX_SEG_S]
        self.assertEqual(kept, [])

    def test_straights_never_mention_corner_apex_speed(self):
        rows = [{"seg": {"name": "la recta hacia la curva 3", "kind": "straight", "b0": 10, "b1": 15}, "dt_s": 0.3, "d_ers_kj": -90, "d_vmin": -50}]
        txt = " ".join(K.advice("Gasly", rows, -0.4, rec=SimpleNamespace(km=lambda b: b * 0.03)))
        self.assertNotIn("vértice", txt)
        self.assertIn("gasta un 2% más de batería", txt)


class RawRecording(unittest.TestCase):
    """`lab --live` guarda los paquetes tal cual para poder analizarlos/calibrar después (antes solo quedaba el informe de texto)."""

    def test_live_feed_records_a_replayable_capture(self):
        import socket, tempfile, threading, time
        from pathlib import Path
        import voice
        path, _ = demo("sprint")
        pk = [d for _, d in list(read_capture(path))[:300]]
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        rec = Path(tempfile.mkdtemp()) / "udp.f1raw"
        eng = E.Engineer(cfg(), "normal", None, out=lambda *_: None, sync=True)
        stop = threading.Event()
        th = threading.Thread(target=voice.feed_live, args=(eng, port, stop, rec), daemon=True)
        th.start()
        time.sleep(0.5)
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for d in pk:
            tx.sendto(d, ("127.0.0.1", port))
            time.sleep(0.001)
        time.sleep(0.8)
        stop.set()
        th.join(3)
        back = [d for _, d in read_capture(rec)]
        self.assertGreater(len(back), 200)
        self.assertEqual(back[:50], pk[:50])
        self.assertGreater(eng.tr.t, 0)                              # y el ingeniero siguió funcionando a la vez

    def test_unwritable_path_does_not_stop_the_engineer(self):
        import socket, threading, time
        import voice
        eng = E.Engineer(cfg(), "normal", None, out=lambda *_: None, sync=True)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        stop = threading.Event()
        th = threading.Thread(target=voice.feed_live, args=(eng, port, stop, "/proc/no/puede/escribir/udp.f1raw"), daemon=True)
        th.start()
        time.sleep(0.5)
        path, _ = demo("sprint")
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for _, d in list(read_capture(path))[:100]:
            tx.sendto(d, ("127.0.0.1", port))
        time.sleep(0.5)
        stop.set()
        th.join(3)
        self.assertGreater(eng.tr.t, 0)


class Stt(unittest.TestCase):
    def test_vocabulary_has_the_weekend_terms(self):
        import stt
        p = stt.build_prompt(["Ocon"])
        for w in ("Q1", "sprint", "libres", "pits"):
            self.assertIn(w, p)


if __name__ == "__main__":
    unittest.main()
