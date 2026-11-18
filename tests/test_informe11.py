"""Arreglos del informe real del 11 nov 2026 (Brasil: libres con lluvia, clasificación, carrera de 18 vueltas con parada)."""
import types, unittest

import helpers  # noqa: F401  (añade la raíz del proyecto al path)
import answers as AN
import assistant as A
from test_assistant import run_until
from test_brain import Scripted, race_engine

RAINY = {"weather": 3, "session_type": 4, "forecast": [
    {"session_type": 4, "minutes": 0, "weather": 3, "rain_pct": 56}, {"session_type": 4, "minutes": 10, "weather": 5, "rain_pct": 86},
    {"session_type": 4, "minutes": 30, "weather": 3, "rain_pct": 58}, {"session_type": 9, "minutes": 0, "weather": 2, "rain_pct": 17},
    {"session_type": 15, "minutes": 0, "weather": 1, "rain_pct": 10}]}


class Weather(unittest.TestCase):
    def test_when_does_the_rain_stop(self):
        t = AN.forecast_text(RAINY)
        self.assertIn("sigue lloviendo al menos 30 min", t)
        self.assertIn("Para la clasificación: nublado", t)
        self.assertIn("Para la carrera: poco nublado", t)

    def test_rain_that_stops(self):
        ses = dict(RAINY, forecast=[{"session_type": 4, "minutes": 0, "weather": 3, "rain_pct": 60},
                                    {"session_type": 4, "minutes": 15, "weather": 2, "rain_pct": 20}])
        self.assertIn("deja de llover en unos 15 min", AN.forecast_text(ses))

    def test_dry_with_rain_coming(self):
        ses = {"weather": 1, "session_type": 15, "forecast": [{"session_type": 15, "minutes": 5, "weather": 1, "rain_pct": 10},
                                                                {"session_type": 15, "minutes": 10, "weather": 3, "rain_pct": 70}]}
        self.assertIn("lluvia ligera en unos 10 min (70%", AN.forecast_text(ses))

    def test_wet_track_tyres(self):
        self.assertIn("intermedios", AN.wet_tyres(RAINY))
        self.assertIn("de lluvia", AN.wet_tyres(dict(RAINY, weather=4)))
        self.assertIsNone(AN.wet_tyres(dict(RAINY, weather=1)))


class RaceStrategyAnswers(unittest.TestCase):
    def setUp(self):
        self.eng, _, _ = run_until("corta", 330)
        self.a = self.eng.assistant
        self.eng.evaluate_now = lambda include=(): (None, "en boxes")       # la simulación no corre en el pit lane

    def test_accepted_plan_is_repeated_not_the_preplan(self):
        lap = self.eng.tr.cur_lap
        self.eng.dialog.accepted = types.SimpleNamespace(option=types.SimpleNamespace(stops=((lap, 17),)))
        self.assertEqual(self.a._run(A.Intent("estrategia"), "recuérdame la parada"),
                         "Seguimos con el plan aceptado: parar esta vuelta a Medio.")

    def test_in_the_pit_lane(self):
        self.eng.dialog.accepted = None
        self.eng.tr.prev_pit = 1
        self.assertIn("Estás en boxes", self.a._run(A.Intent("estrategia"), "x"))


class StopOnPlan(unittest.TestCase):
    def test_executing_the_accepted_plan_is_not_an_off_plan_stop(self):
        eng, _, _ = run_until("corta", 330)
        said = []
        eng.say = lambda text, *a, **k: said.append(text) or True
        eng.preplan, eng.preplan_said = {"stints": [(16, 14), (17, 4)]}, False
        eng.dialog.accepted = types.SimpleNamespace(state="aceptada", summary=lambda: {})
        eng._stop_done({"lap": 10, "lane_s": 23, "stop_s": 2.5})
        self.assertFalse([s for s in said if "plan previo" in s], said)


class CatchUp(unittest.TestCase):
    def test_laps_until_the_rival_catches_you(self):
        eng, _, _ = run_until("corta", 330)
        tr = eng.tr
        car = next(i for i in tr.names if i != tr.player)
        lap = tr.cur_lap
        tr.gaps_by_lap = {lap - 2: {car: 12.3, tr.player: 10.0}, lap - 1: {car: 11.7, tr.player: 10.0}}
        tr.pit_log = []
        tr.rel_gap = lambda i: 1.7
        r = eng.assistant._run(A.Intent("alcance", driver=(car, tr.name(car))), "x")
        self.assertIn("0.6 s por vuelta", r)
        self.assertIn("te alcanza en unas 3 vueltas", r)


class Routing(unittest.TestCase):
    def plan(self, text, obj):
        return race_engine(Scripted()).assistant.brain.to_plan(text, obj)

    def test_my_position(self):
        p = self.plan("¿De qué posición voy?", {"tools": [{"t": "piloto", "pos": 1}], "say": ""})
        self.assertEqual(p.intents[0].name, "posicion")

    def test_another_car_stopping_is_not_my_pit_stop(self):
        p = self.plan("El coche de adelante ya paró.", {"tools": [{"t": "parar_ya"}], "say": ""})
        self.assertEqual(p.intents[0].name, "faltan_parar")
        self.assertEqual(self.plan("quiero parar ya", {"tools": [{"t": "parar_ya"}], "say": ""}).intents[0].name, "parar_ya")

    def test_question_without_question_marks_is_not_a_statement(self):
        p = self.plan("Mejor neumático para estas condiciones.", {"tools": [{"t": "compuesto"}], "say": ""})
        self.assertNotEqual(p.say, "Entendido.")
        self.assertTrue(p.intents)


if __name__ == "__main__":
    unittest.main()
