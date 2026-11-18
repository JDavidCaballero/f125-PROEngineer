"""Correcciones tras el informe real del 26 oct (entreno + clasificación + carrera de 18 vueltas): cada prueba reproduce un fallo
observado en ese informe."""
import json, struct, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace

from helpers import demo, cfg
import assistant as A
import blackbox as BB
import brain as B
import dialog as D
import engineer as E
import f1_parse as P
import tactics as T
import traces as TR
from f1_parse import read_capture
from test_brain import Scripted, race_engine
from test_sessions import patched, run
from test_voice import session


class TyreDamageIsWear(unittest.TestCase):
    """En la captura real el «daño del neumático» es idéntico al desgaste: no es un daño aparte."""

    def test_damage_equal_to_wear_is_no_extra_damage(self):
        dm = {"tyre_wear_pct": (49, 40, 30, 20), "tyre_damage_pct": (49, 40, 30, 20)}
        self.assertEqual(P.tyre_extra_damage(dm), [0, 0, 0, 0])

    def test_damage_above_wear_is_a_real_damage(self):
        dm = {"tyre_wear_pct": (10, 5, 5, 5), "tyre_damage_pct": (70, 5, 5, 5)}
        self.assertEqual(P.tyre_extra_damage(dm), [60, 0, 0, 0])

    def test_real_capture_says_nothing_about_tyre_damage(self):
        from helpers import REAL
        if not REAL.exists():
            self.skipTest("sin captura real")
        eng = E.Engineer(cfg(), "normal", None, out=lambda *_: None, sync=True)
        lines = []
        eng.out = lines.append
        for t, d in read_capture(REAL):
            eng.on_packet(t, d)
        self.assertFalse([l for l in lines if "Daño en el neumático" in l], "el desgaste ya se avisa como desgaste")


class PlanWindow(unittest.TestCase):
    def accepted_dialog(self):
        eng = race_engine(Scripted())
        o = next(o for o in eng.assistant.brain.eng.evaluate_now()[0].options if o.stops)
        d = D.Dialog(cfg(), "normal")
        d.propose(o, "x", 5, 0.0)
        d.accept(5)
        return d, o.stops[0][0]

    def test_crossing_the_line_in_the_pit_lane_is_not_a_missed_plan(self):
        d, stop = self.accepted_dialog()
        self.assertEqual([t for t, *_ in d.on_lap(stop + 1, in_pit=True) if "Pasó la ventana" in t], [])
        self.assertIsNotNone(d.accepted)

    def test_a_really_missed_window_is_still_reported(self):
        d, stop = self.accepted_dialog()
        self.assertTrue([t for t, *_ in d.on_lap(stop + 1, in_pit=False) if "Pasó la ventana" in t])

    def test_the_same_stop_is_not_proposed_twice_with_new_numbers(self):
        eng = race_engine(Scripted())
        o = next(o for o in eng.evaluate_now()[0].options if o.stops)
        d = D.Dialog(cfg(), "normal")
        p1 = d.propose(o, "x", 5, 0.0)
        p2 = d.propose(o, "x", 6, 10.0, "piloto")
        self.assertIs(p1, p2)


class PitLaneStart(unittest.TestCase):
    """La vuelta 1 de una clasificación sale de boxes: no se descarta por empezar en el pit lane."""

    def fake(self, pit_first_samples):
        tl = 5000.0
        laps, ses = [None], {"track_length_m": tl}
        tr = SimpleNamespace(ses=ses, lap_all=laps, sta_all=[{"ers_deployed_j": 0, "ers_store_j": 0, "visual_compound": 16}],
                             tel_all=[{"speed": 200, "throttle": 1.0, "brake": 0.0}], is_race=lambda: False)
        tb = TR.TraceBook()
        n = 400
        for k in range(n):
            d = tl * k / (n - 1)
            laps[0] = {"result_status": 2, "lap": 1, "lap_distance": d, "pit_status": 1 if k < pit_first_samples else 0,
                       "current_ms": int(66000 * k / (n - 1)), "last_ms": 0, "driver_status": 1, "lap_invalid": False}
            tb.feed(tr)
        laps[0] = {**laps[0], "lap": 2, "lap_distance": 5.0, "last_ms": 66000, "pit_status": 0}
        tb.feed(tr)
        return tb

    def test_a_lap_that_starts_in_the_pit_lane_is_kept(self):
        tb = self.fake(pit_first_samples=12)           # ~3% de la vuelta en el pit lane
        rec = tb.recent.get(0)
        self.assertTrue(rec and rec[-1].timed, tb.rejects)

    def test_entering_the_pits_mid_lap_still_invalidates_it(self):
        tl = 5000.0
        tr = SimpleNamespace(ses={"track_length_m": tl}, lap_all=[None], sta_all=[{"ers_deployed_j": 0, "ers_store_j": 0, "visual_compound": 16}],
                             tel_all=[{"speed": 200, "throttle": 1.0, "brake": 0.0}], is_race=lambda: False)
        tb = TR.TraceBook()
        for k in range(400):
            tr.lap_all[0] = {"result_status": 2, "lap": 1, "lap_distance": tl * k / 399, "pit_status": 1 if k == 200 else 0,
                             "current_ms": 1000 + k, "last_ms": 0, "driver_status": 1, "lap_invalid": False}
            tb.feed(tr)
        tr.lap_all[0] = {**tr.lap_all[0], "lap": 2, "lap_distance": 5.0, "last_ms": 66000, "pit_status": 0}
        tb.feed(tr)
        self.assertFalse(tb.recent[0][-1].timed)


class Report(unittest.TestCase):
    def test_diagnostics_reach_the_report(self):
        for k in ("lap_trace", "lap_rejected", "new_session", "proposal_blocked", "note_skipped" if False else "lap_report"):
            self.assertIn(k, BB.KEEP_ENGINE)

    def test_voice_log_is_compacted(self):
        line = json.dumps({"type": "utt", "raw": "¡" * 500, "audio": "/Users/x/proyectos/runs/audio/utt_001.wav", "reply": "ok"})
        out = json.loads(BB._compact_voice(line))
        self.assertEqual(out["audio"], "utt_001.wav")
        self.assertLess(len(out["raw"]), 400)


class SessionEnd(unittest.TestCase):
    """Cuando empieza la sesión siguiente se resume el entreno/clasificación que acabó, con su propio estado."""

    @staticmethod
    def as_practice(path, uid):
        for t, d in patched(path, uid):
            if d[6] == 1:                                    # paquete Session: session_type en el byte 6 del cuerpo
                b = bytearray(d)
                b[P.HS + 6] = 1
                d = bytes(b)
            yield t, d

    def test_practice_summary_is_spoken_when_the_next_session_starts(self):
        path, _ = demo("media")
        short, _ = demo("sprint")
        lines = []
        eng = E.Engineer(cfg(), "normal", None, out=lines.append, sync=True)
        run(eng, self.as_practice(path, 111))
        self.assertEqual(eng.tr.session_kind(), "practice")
        run(eng, list(patched(short, 222))[:3])
        self.assertTrue([l for l in lines if "Resumen del entrenamiento" in l], lines[-5:])

    def test_final_classification_of_a_practice_says_end_of_session_not_end_of_race(self):
        tr = SimpleNamespace(ses={}, prev_kind="practice", player=0, session_kind=lambda: "other", is_race=lambda: False)
        out = T.Tactics(cfg())._final(tr, {"cars": [{"idx": 0, "position": 1, "points": 0}]}) if hasattr(T, "Tactics") else None
        if out is None:
            self.skipTest("sin clase Tactics")
        self.assertEqual(out[0].text, "Fin de la sesión.")

    def test_final_classification_of_a_race_keeps_points(self):
        tr = SimpleNamespace(ses={}, prev_kind="race", player=0, session_kind=lambda: "other", is_race=lambda: False)
        out = T.Tactics(cfg())._final(tr, {"cars": [{"idx": 0, "position": 1, "points": 25}]})
        self.assertIn("Fin de carrera: terminas P1, 25 puntos", out[0].text)


class Echo(unittest.TestCase):
    """Informe real: el micrófono grabó el texto del ingeniero tres veces seguidas (hasta el límite de 20 s)."""

    def test_the_engineers_own_words_are_detected_and_ignored(self):
        eng, sess, clock, log, sp = session()
        spoken = "Aún no tengo una vuelta completa tuya medida: necesito una vuelta cronometrada y válida."
        sess._on_tts("start", spoken, clock())
        r = sess.utterance(spoken, 4.0)
        self.assertIsNone(r["intent"])
        self.assertIn("eco", r["reject"])
        self.assertEqual(sess.radio.misses, 0)               # no cuenta como «no entendí»

    def test_a_real_question_is_not_taken_for_an_echo(self):
        eng, sess, clock, log, sp = session()
        sess._on_tts("start", "Vas P4 en la vuelta 5 de 15.", clock())
        r = sess.utterance("¿Cuánto combustible me queda?", 2.0)
        self.assertNotIn("eco", str(r["reject"]))

    def test_short_phrases_are_never_echo(self):
        eng, sess, clock, log, sp = session()
        sess._on_tts("start", "De acuerdo, plan confirmado.", clock())
        self.assertFalse(sess.is_echo("Sí."))


class RoutingFromTheReport(unittest.TestCase):
    def plan(self, text, obj):
        return race_engine(Scripted()).assistant.brain.to_plan(text, obj)

    def test_a_long_statement_gets_an_acknowledgement_not_a_strategy(self):
        for s in ("Termino el entrenamiento con el neumático medio en esta vuelta para en boxes.",
                  "Bueno, ya terminé el trabajo con el neumático duro, ahora voy con el neumático medio.",
                  "Ganamos ingeniero, ganamos la carrera."):
            p = self.plan(s, {"tools": [{"t": "estrategia"}, {"t": "compuesto"}], "say": ""})
            self.assertEqual((p.tools, p.say), ([], "Entendido."), s)

    def test_questions_and_orders_still_use_tools(self):
        for s in ("Con los datos que tienes, ¿qué estrategia planeas para la carrera?", "Dime la posición y el combustible ahora mismo",
                  "¿En qué sector debo mejorar para hacer la pole?"):
            p = self.plan(s, {"tools": [{"t": "estrategia"}], "say": ""})
            self.assertEqual(p.tools, ["estrategia"], s)

    def test_two_tools_without_a_conjunction_keep_only_the_first(self):
        p = self.plan("¿En qué sector debo mejorar para hacer la pole, para quedar primero?",
                      {"tools": [{"t": "analisis_vuelta"}, {"t": "piloto", "pos": 1}], "say": ""})
        self.assertEqual(p.tools, ["analisis_vuelta"])

    def test_two_tools_with_a_conjunction_are_kept(self):
        p = self.plan("dime la posición y el combustible", {"tools": [{"t": "posicion"}, {"t": "combustible"}], "say": ""})
        self.assertEqual(p.tools, ["posicion", "combustible"])

    def test_tyre_life_questions_are_tyres_not_strategy(self):
        eng = race_engine(Scripted(route=lambda ctx: {"tools": [{"t": "estrategia"}], "say": ""}), t_stop=200)
        plan = eng.assistant.prepare("¿Cuántas vueltas más aguanta con este neumático?")
        self.assertEqual(plan.intents[0].name, "neumaticos")

    def test_closing_the_session_is_not_a_session_question(self):
        eng = race_engine(Scripted(route=lambda ctx: {"tools": [{"t": "sesion"}], "say": ""}))
        plan = eng.assistant.prepare("Puedes finalizar la sesión.")
        self.assertEqual(plan.intents[0].name, "cerrar")
        self.assertIn("Ctrl+C", eng.assistant.execute(plan, "Puedes finalizar la sesión."))

    def test_names_glued_by_the_recogniser_are_found(self):
        tr = race_engine(Scripted()).tr
        tr.names[5] = "Verstappen"
        found = tr.find_driver("Neumáticos, motoverstappen.")
        self.assertEqual([n for _, n in found], ["Verstappen"])
        self.assertEqual([n for _, n in tr.find_driver("Que neumáticos Montoverstappen.")], ["Verstappen"])


class SttPrompt(unittest.TestCase):
    def test_prompt_has_names_and_examples(self):
        import stt
        p = stt.build_prompt(["Verstappen", "Russell"])
        self.assertIn("Verstappen", p)
        self.assertIn("¿Cómo voy contra Russell?", p)
        self.assertIn("P1", p)


if __name__ == "__main__":
    unittest.main()
