"""Rama seguridad/carrera: arreglos que evitan que el ingeniero haga algo peligroso o diga algo que no se entiende en carrera."""
import struct, types, unittest

from helpers import demo, cfg
import assistant as A
import engineer as E
import speech
import tactics as T
from f1_parse import read_capture
from test_assistant import run_until


class YesNo(unittest.TestCase):
    def test_only_whole_yes_or_no_phrases(self):
        cases = {"si": "si", "no": "no", "dale": "si", "si dale": "si", "vale perfecto": "si", "ok ingeniero": "si",
                 "de acuerdo": "si", "si por favor": "si", "no gracias": "no", "mejor no": "no", "ni hablar": "no",
                 "no no": "no", "no por ahora": "no",
                 # frases que empiezan por «no»/«vale» pero no responden a una propuesta (antes aceptaban o rechazaban la parada)
                 "no agarra nada": None, "vale cuanto queda": None, "no frena": None, "no hay agarre": None,
                 "venga dime": None, "no quiero parar": None, "si pero en dos vueltas": None, "por": None}
        for text, want in cases.items():
            self.assertEqual(A.yes_no(text, text.split()), want, text)


class OpenQuestionExpires(unittest.TestCase):
    def setUp(self):
        self.eng, _, _ = run_until("corta", 200)
        self.a = self.eng.assistant

    def test_short_answer_still_works(self):
        self.a.handle("menos avisos")
        self.a.handle("de combustible")
        self.assertEqual(self.eng.tac.topics.get("combustible"), -1)

    def test_later_question_does_not_mute_alerts(self):
        self.a.handle("menos avisos")
        self.a.handle("cuánto le saco al de atrás")
        self.a.handle("y el combustible cuánto me queda")
        self.assertNotEqual(self.eng.tac.topics.get("combustible", 0), -1)
        self.assertIsNone(self.a.awaiting)


class WrongUdpFormat(unittest.TestCase):
    def test_says_how_to_fix_once_and_gives_no_alerts(self):
        path, _ = demo("sprint")
        out = []
        eng = E.Engineer(cfg(), out=out.append)
        for t, d in read_capture(path):
            b = bytearray(d)
            b[:2] = struct.pack("<H", 2026)
            eng.on_packet(t, bytes(b))
        self.assertEqual(len(out), 1, out)
        self.assertIn("formato UDP 2026", out[0])
        self.assertIn("2025", out[0])


class PitWithoutTyres(unittest.TestCase):
    """Un drive-through pasa por el pit lane sin cambiar neumáticos: no cumple el plan aceptado."""

    def setUp(self):
        self.eng, self.out, _ = run_until("corta", 200)
        self.done = []
        self.eng.dialog.accepted = types.SimpleNamespace(state="aceptada", summary=lambda: {})
        self.eng.dialog.on_pit_out = lambda: self.done.append(1) or None
        self.eng._maybe_strategy = lambda sig: None          # aquí solo importa la salida de boxes

    def feed(self, sigs, t):
        tr = self.eng.tr
        tr.on_packet = lambda *_: sigs
        tr.t = t
        self.eng._on_packet(t, b"")

    def test_drive_through_does_not_execute_plan(self):
        self.feed([("pit_in",)], 300)
        self.feed([("pit_out", {"lane_s": 20, "stop_s": 0, "lap": 5})], 320)
        self.feed([], 340)
        self.assertEqual(self.done, [])
        self.assertIsNone(self.eng._pit_wait)

    def test_stop_with_tyres_executes_plan(self):
        self.feed([("pit_in",)], 300)
        self.eng.tr.prev_pit = 1
        self.feed([("tyres", 17)], 305)
        self.eng.tr.prev_pit = 0
        self.feed([("pit_out", {"lane_s": 22, "stop_s": 2.5, "lap": 5})], 320)
        self.assertEqual(self.done, [1])

    def test_tyres_reported_just_after_pit_exit(self):
        self.feed([("pit_in",)], 300)
        self.feed([("pit_out", {"lane_s": 22, "stop_s": 2.5, "lap": 5})], 320)
        self.feed([("tyres", 17)], 321)
        self.assertEqual(self.done, [1])


class Speech(unittest.TestCase):
    def test_sign_is_spoken(self):
        self.assertEqual(speech.to_speech("tendencia +0.12 s"), "tendencia más 12 centésimas")
        self.assertEqual(speech.to_speech("Delta -0.3 s"), "Delta menos 3 décimas")
        self.assertNotIn("+", speech.to_speech("Frente a seguir fuera: +1.3 puntos"))

    def test_parts_have_their_article(self):
        self.assertTrue(all(n.split(" ", 1)[0] in ("el", "la") for n in {**T.PARTS, **T.COMPONENTS}.values()))

    def test_fuel_margin_in_words(self):
        eng, _, _ = run_until("corta", 200)
        tr = eng.tr
        tr.fuel_margin = lambda: -1.2
        self.assertIn("te faltan 1.2 vueltas", eng.assistant.t_combustible(None))
        tr.fuel_margin = lambda: 0.5
        self.assertIn("te sobra para 0.5 vueltas", eng.assistant.t_combustible(None))


if __name__ == "__main__":
    unittest.main()


class NoGameData(unittest.TestCase):
    """Sin paquetes del juego el programa lo dice (antes se quedaba callado: «5 vueltas y no pasó nada»)."""

    def setUp(self):
        self.now = [0.0]
        self.w = E.LinkWatch(20777, None, clock=lambda: self.now[0])

    def at(self, t):
        self.now[0] = t
        return self.w.tick()

    def test_warns_after_10_s_without_data_and_repeats(self):
        self.assertIsNone(self.at(5))
        msg = self.at(10)
        self.assertIn("No me llega ningún dato", msg)
        self.assertIn("20777", msg)
        self.assertIn("cortafuegos", msg)
        self.assertIsNone(self.at(20))
        self.assertIn("No me llega", self.at(40))

    def test_status_line_and_stop_notice(self):
        self.at(1)
        self.w.packet()
        self.assertIsNone(self.at(5))
        self.assertIn("Han dejado de llegar", self.at(12))
        self.assertIsNone(self.at(13))                          # una vez por corte
        self.now[0] = 61
        self.w.packet()                                        # vuelven los datos
        self.assertIn("datos del juego: 2 paquetes", self.at(61.5))
