"""Entrenador de carrera: cómo adelantar o recuperar una posición con las trazas de los dos coches, y cobertura ante un undercut."""
import unittest
from types import SimpleNamespace

from helpers import cfg
import assistant as A
import coach as K
import dialog as D
from test_assistant import run_until
from test_brain import Scripted, race_engine


def row(name, kind, dt, d_ers=0.0, d_vmin=0.0, b0=10):
    return {"seg": {"name": name, "kind": kind, "b0": b0, "b1": b0 + 5}, "dt_s": dt, "d_ers_kj": d_ers, "d_vmin": d_vmin}


REC = SimpleNamespace(km=lambda b: b * 0.025)


class Advice(unittest.TestCase):
    def test_attack_on_the_straight_where_you_are_faster_and_save_the_battery(self):
        rows = [row("la recta hacia la curva 3", "straight", -0.19, 80), row("la curva 5", "corner", 0.2, d_vmin=-6)]
        txt = " ".join(K.advice("Verstappen", rows, -0.5, 0.3, 0.12, "Medio", 8, 12, rec=REC))
        self.assertIn("ataca en la recta hacia la curva 3", txt)
        self.assertIn("le ganas 0.19 s", txt)
        self.assertIn("gastas un 2% más de batería", txt)
        self.assertIn("Guarda la batería", txt)
        self.assertIn("Cuidado en la curva 5", txt)
        self.assertIn("no podrá defenderse con ERS", txt)
        self.assertIn("12 puntos más gastados", txt)
        self.assertIn("0.5 s delante", txt)

    def test_no_straight_advantage_says_so(self):
        txt = " ".join(K.advice("Russell", [row("la curva 2", "corner", 0.1)], -0.4, None, None))
        self.assertIn("No veo ninguna recta donde seas más rápido", txt)

    def test_without_traces_it_says_what_is_missing_and_invents_nothing(self):
        txt = " ".join(K.advice("Russell", [], 0.6, -0.2, None, "Duro", 3, None))
        self.assertIn("Aún no tengo una vuelta limpia de los dos", txt)
        self.assertIn("0.6 s detrás", txt)
        self.assertNotIn("ataca en", txt)

    def test_small_differences_are_not_mentioned(self):
        txt = " ".join(K.advice("Alves", [row("la recta final hacia meta", "straight", -0.01)], -0.5, 0.02, 0.8, None, None, 2))
        self.assertNotIn("ataca en la recta", txt)
        self.assertNotIn("batería", txt)
        self.assertNotIn("ritmo", txt)

    def test_recovering_a_position_says_so(self):
        txt = " ".join(K.advice("Hamilton", [row("la recta hacia la curva 4", "straight", -0.1)], -0.8, mode="recover", rec=REC))
        self.assertIn("Para recuperar la posición", txt)


class Asking(unittest.TestCase):
    def test_phrases(self):
        tr = race_engine(Scripted()).tr
        for s in ("¿Cómo adelanto a Alves?", "¿Dónde puedo pasarlo?", "¿Qué hago para adelantar al de delante?", "cómo le gano al de adelante"):
            r = A.parse_rules(tr, s)
            self.assertEqual(r.name, "como_adelantar", s)

    def test_tyre_and_pit_questions_are_not_overtaking(self):
        tr = race_engine(Scripted()).tr
        for s in ("¿Cuándo paso por boxes?", "¿Cuántas vueltas aguantan los neumáticos para pasar la bandera?"):
            r = A.parse_rules(tr, s)
            self.assertNotEqual(getattr(r, "name", None), "como_adelantar", s)

    def test_in_a_race_it_answers_with_the_rival_in_front(self):
        eng = race_engine(Scripted(), t_stop=250)
        ans = eng.ask("¿Cómo adelanto al de delante?")
        self.assertTrue(ans and ("Aún no tengo una vuelta limpia" in ans or "Para pasarlo" in ans or "No veo ninguna recta" in ans), ans)
        ahead = K.car_ahead(eng.tr)
        if ahead is not None:
            self.assertIn(eng.tr.name(ahead), ans)

    def test_leader_has_nobody_to_pass(self):
        eng = race_engine(Scripted(), t_stop=250)
        eng.tr.lap_all[eng.tr.player]["position"] = 1
        for i, la in enumerate(eng.tr.lap_all):
            if la and i != eng.tr.player and la["position"] <= 1:
                la["position"] = 2
        self.assertIn("Vas primero", eng.ask("¿Cómo adelanto al de delante?"))

    def test_outside_a_race_it_is_not_available(self):
        from test_point1 import SessionEnd
        from test_raceplan import TestPractice
        eng = TestPractice.engine()
        self.assertIn("Eso es de carrera", eng.ask("¿Cómo adelanto al de delante?"))


class CoachTick(unittest.TestCase):
    def engine(self, front_gaps=(0.6, 0.7)):
        eng = race_engine(Scripted(), t_stop=250)
        tr = eng.tr
        car = K.car_ahead(tr)
        self.assertIsNotNone(car)
        lap = tr.cur_lap
        tr.gap_hist = {lap - 1: {"front": front_gaps[0], "front_car": car}, lap - 2: {"front": front_gaps[1], "front_car": car}}
        tr.low_workload = lambda: True
        eng.tr.cur_lap = max(lap, 3)
        tr.gap_hist = {eng.tr.cur_lap - 1: tr.gap_hist[lap - 1], eng.tr.cur_lap - 2: tr.gap_hist[lap - 2]}
        return eng, car

    def test_a_long_duel_triggers_a_hint_once_per_four_laps(self):
        eng, car = self.engine()
        said = []
        eng.say = lambda text, *a, **k: said.append(text) or True
        eng._coach_tick([])
        eng._coach_tick([])
        self.assertEqual(len(said), 1, said)
        self.assertIn(eng.tr.name(car), said[0])
        eng.tr.cur_lap += 4
        eng.tr.gap_hist = {eng.tr.cur_lap - 1: {"front": 0.5, "front_car": car}, eng.tr.cur_lap - 2: {"front": 0.6, "front_car": car}}
        eng._coach_tick([])
        self.assertEqual(len(said), 2)

    def test_no_hint_if_the_gap_is_big(self):
        eng, car = self.engine((2.5, 2.8))
        said = []
        eng.say = lambda text, *a, **k: said.append(text) or True
        eng._coach_tick([])
        self.assertEqual(said, [])

    def test_the_pilot_can_turn_it_off(self):
        eng, car = self.engine()
        eng.tac.topics["adelantamiento"] = -1
        said = []
        eng.say = lambda text, *a, **k: said.append(text) or True
        eng._coach_tick([])
        self.assertEqual(said, [])

    def test_being_overtaken_triggers_a_recovery_hint(self):
        eng, car = self.engine((5.0, 5.0))
        tr = eng.tr
        said = []
        eng.say = lambda text, *a, **k: said.append(text) or True
        eng._coach_tick([("event", {"code": "OVTK", "car": car, "overtaken": tr.player})])
        self.assertEqual(said, [])                              # espera unos segundos a que suene «te adelantó»
        g = tr.gaps()[0]
        tr.t += 7
        for i in range(len(tr.lap_all)):                         # el rival queda justo delante
            pass
        eng._coach_tick([])
        self.assertTrue(not said or "te pasó" in said[0])


class Cover(unittest.TestCase):
    def setup(self, pit_pos, stay_pos):
        eng = race_engine(Scripted(), t_stop=250)
        opt = SimpleNamespace(e_pos=pit_pos, stops=[(eng.tr.cur_lap, 17)], phrase="Entra a boxes ahora, Medio.", kind="PIT", id="X",
                              utility=0.0, desc="Parar ahora")
        stay = SimpleNamespace(e_pos=stay_pos, kind="STAY", stops=[], phrase="", id="S", utility=0.0, desc="Seguir")
        eng.assistant._pit_now_option = lambda rep, sit, comp=None: opt
        said = []
        eng.say = lambda text, *a, **k: said.append(text) or True
        return eng, SimpleNamespace(options=[stay, opt]), SimpleNamespace(lap=eng.tr.cur_lap), said

    def test_if_covering_is_better_it_proposes_pitting_now(self):
        eng, rep, sit, said = self.setup(2.0, 3.1)
        self.assertTrue(eng._cover("Amenaza de undercut de Verstappen (a 0.5 s): si para ahora te pasaría en pista.", rep, sit, sit.lap))
        self.assertIn("¿Lo hacemos?", said[0])
        self.assertIn("P2.0", said[0])
        self.assertIn("P3.1", said[0])
        self.assertEqual(eng.dialog.pending.reason, "amenaza de undercut")

    def test_if_staying_is_better_it_says_defend_on_track(self):
        eng, rep, sit, said = self.setup(3.0, 2.2)
        self.assertTrue(eng._cover("Amenaza de undercut de Verstappen (a 0.5 s).", rep, sit, sit.lap))
        self.assertIn("defiende en pista", said[0])
        self.assertIsNone(eng.dialog.pending)

    def test_with_the_mandatory_stop_pending_it_compares_with_the_later_stop(self):
        eng, rep, sit, said = self.setup(2.0, 3.1)
        later = SimpleNamespace(e_pos=3.1, kind="PIT", stops=[(sit.lap + 5, 18)], phrase="", id="L", utility=0.0, desc="Parar luego")
        rep.options = [later, rep.options[1]]               # sin «seguir fuera»: falta la parada obligatoria
        self.assertTrue(eng._cover("Amenaza de undercut de Verstappen (a 0.5 s).", rep, sit, sit.lap))
        self.assertIn(f"si paras en la vuelta {sit.lap + 5}", said[0])
        self.assertEqual(eng.dialog.pending.reason, "amenaza de undercut")

    def test_it_is_not_repeated_within_two_minutes(self):
        eng, rep, sit, said = self.setup(3.0, 2.2)
        eng.say = lambda text, *a, **k: said.append(text) or eng.cool.__setitem__("cover", eng.tr.t) or True
        eng._cover("Amenaza de undercut de X.", rep, sit, sit.lap)
        eng._cover("Amenaza de undercut de X.", rep, sit, sit.lap)
        self.assertEqual(len(said), 1)

    def test_with_an_accepted_plan_the_plain_note_is_used(self):
        eng, rep, sit, said = self.setup(2.0, 3.1)
        eng.dialog.accepted = object()
        self.assertFalse(eng._cover("Amenaza de undercut de X.", rep, sit, sit.lap))


if __name__ == "__main__":
    unittest.main()
