"""Resumen de carrera y aviso único de combustible sobrante."""
import re, unittest

from helpers import demo, cfg
import assistant as A
import debrief as DB
import engineer as E
from f1_parse import read_capture
from test_assistant import run_until


def play(name, topics=None, mode="normal"):
    path, info = demo(name)
    out = []
    eng = E.Engineer(cfg(), mode, None, out=out.append, sync=True)
    eng.tac.topics.update(topics or {})
    for t, d in read_capture(path):
        eng.on_packet(t, d)
    return eng, out, info


class Calibration(unittest.TestCase):
    def test_picks_thirds_and_end(self):
        log = [(2, 6.0), (5, 5.0), (10, 4.2), (15, 4.0)]
        cal = DB.calibration(log, 15, 4)
        self.assertEqual([c[0] for c in cal], [5, 10, 15])
        self.assertAlmostEqual(cal[0][2], -1.0)

    def test_no_data_is_empty(self):
        self.assertEqual(DB.calibration([], 15, 4), [])
        self.assertEqual(DB.calibration([(3, 5.0)], 0, 4), [])
        self.assertEqual(DB.calibration([(3, 5.0)], 15, None), [])


class Debrief(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.res = {sc: play(sc) for sc in ("sprint", "corta", "media")}

    def test_summary_is_spoken_once_and_matches_the_real_result(self):
        for sc, (eng, out, info) in self.res.items():
            said = [l for l in out if "Resumen: saliste" in l]
            self.assertEqual(len(said), 1, sc)
            self.assertIn(f"terminaste P{info['final_pos']}", said[0], sc)

    def test_summary_logs_are_consistent_with_dialog_history(self):
        eng, out, _ = self.res["corta"]
        line = next(l for l in out if "Te hice" in l)
        n = int(re.search(r"Te hice (\d+) propuestas", line).group(1))
        self.assertEqual(n, len(DB._proposals(eng.dialog)))

    def test_on_demand_summary_before_the_end_says_so(self):
        eng, _, _ = run_until("corta", 200)
        r = eng.ask("¿cómo va la carrera?")
        self.assertTrue(r.startswith("La carrera no ha terminado"), r)
        self.assertEqual(A.parse_rules(eng.tr, "dame un resumen de la carrera").name, "resumen")
        self.assertEqual(A.parse_rules(eng.tr, "¿cómo lo hice?").name, "resumen")

    def test_on_demand_summary_after_the_end(self):
        eng, out, info = play("corta")
        self.assertIn(f"terminaste P{info['final_pos']}", eng.ask("resumen de la carrera"))


class FuelSurplus(unittest.TestCase):
    def test_surplus_is_said_at_most_once_per_race_by_default(self):
        for sc in ("sprint", "media", "sc_final"):
            _, out, _ = play(sc)
            self.assertLessEqual(len([l for l in out if "Llegas con margen de combustible" in l]), 1, sc)

    def test_surplus_never_when_fuel_is_short_or_silenced(self):
        _, out, _ = play("corta")                                  # esta carrera va justa de combustible
        self.assertFalse([l for l in out if "Llegas con margen" in l])
        _, out, _ = play("media", {"combustible": -1})
        self.assertFalse([l for l in out if "Llegas con margen" in l])

    def test_surplus_only_in_the_last_laps(self):
        for sc in ("sprint", "media", "sc_final"):
            _, out, info = play(sc)
            for l in out:
                if "Llegas con margen" in l:
                    lap = int(re.search(r"vuelta (\d+)", l).group(1))
                    self.assertGreaterEqual(lap, info.get("total", 99) - 4 if "total" in info else 0, sc)


if __name__ == "__main__":
    unittest.main()
