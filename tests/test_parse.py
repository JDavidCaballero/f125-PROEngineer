import unittest
from collections import Counter

from helpers import demo, REAL
import f1_parse as P


class Layouts(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual((P.LAP.size, P.TEL.size, P.STA.size, P.DMG.size), (57, 60, 55, 46))
        self.assertEqual((P.PART.size, P.TSET.size, P.HIST_LAP.size, P.FINAL.size), (57, 10, 14, 46))
        # tamaños de paquete completos validados con la captura real de F1 25
        self.assertEqual(P.HS + 22 * P.LAP.size + 2, 1285)      # + 2 bytes finales (índices de contrarreloj)
        self.assertEqual(P.HS + 1 + 22 * P.PART.size, 1284)
        self.assertEqual(P.HS + 1 + 22 * P.FINAL.size, 1042)


class DemoRoundtrip(unittest.TestCase):
    """El generador y el parser comparten estructuras: lo que se escribe debe leerse igual."""

    @classmethod
    def setUpClass(cls):
        cls.path, cls.info = demo("sprint")
        cls.pk = {}
        for t, d in P.read_capture(cls.path):
            cls.pk.setdefault(P.header(d)["id"], []).append(d)

    def test_lapdata_status_fields(self):
        d = self.pk[2][len(self.pk[2]) // 2]
        cars = [P.lap_data(d, i) for i in range(22)]
        self.assertEqual(sorted(c["position"] for c in cars), list(range(1, 23)))
        self.assertTrue(all(c["driver_status"] == 4 and c["result_status"] == 2 for c in cars))   # el desfase corregido

    def test_session_and_participants(self):
        s = P.session(self.pk[1][5])
        self.assertEqual((s["total_laps"], s["track_length_m"], s["session_type"]), (5, 4323, 15))
        self.assertEqual(len(s["forecast"]), 4)
        names = [c["name"] for c in P.participants(self.pk[4][0])["cars"]]
        self.assertEqual(names[self.info["player"]], "TU")

    def test_final_classification_matches_sim(self):
        fin = P.final_classification(self.pk[8][0])
        me = next(c for c in fin["cars"] if c["idx"] == self.info["player"])
        self.assertEqual(me["position"], self.info["final_pos"])
        self.assertEqual(sorted(c["position"] for c in fin["cars"]), list(range(1, 23)))

    def test_history_and_tyre_sets(self):
        h = P.session_history(self.pk[11][-1])
        self.assertTrue(all(l["ms"] > 60000 for l in h["laps"]))
        ts = P.tyre_sets(self.pk[12][-1])
        self.assertEqual(sum(1 for s in ts["sets"] if s["fitted"]), 1)


@unittest.skipUnless(REAL.exists(), "captura real no disponible")
class RealCapture(unittest.TestCase):
    """Valida el parser contra datos reales de F1 25 v1.26 (tu captura de Singapur)."""

    @classmethod
    def setUpClass(cls):
        cls.facts = {"cnt": Counter(), "ds": Counter()}
        inrace, last_lap = False, None
        for t, d in P.read_capture(REAL):
            h = P.header(d)
            cls.facts["format"] = (h["format"], h["year"])
            if h["id"] == 1:
                s = P.session(d)
                inrace = s["session_type"] == 15
                if inrace:
                    cls.facts["ses"] = s
            if not inrace:
                continue
            cls.facts["player"] = h["player"]
            if h["id"] == 2:
                last_lap = d
            elif h["id"] == 8:
                cls.facts["final"] = P.final_classification(d)
            elif h["id"] == 3:
                cls.facts["cnt"][P.event(d)["code"]] += 1
        cls.facts["lap"] = [P.lap_data(last_lap, i) for i in range(22)]

    def test_format(self):
        self.assertEqual(self.facts["format"], (2025, 25))

    def test_session(self):
        s = self.facts["ses"]
        self.assertEqual((s["total_laps"], s["track_id"], s["track_length_m"]), (5, 12, 4928))
        self.assertEqual(len(s["forecast"]), 3)
        self.assertEqual(self.facts["player"], 21)

    def test_final_classification(self):
        me = next(c for c in self.facts["final"]["cars"] if c["idx"] == 21)
        self.assertEqual((me["position"], me["grid"], me["result_status"]), (22, 16, 3))

    def test_events(self):
        self.assertGreater(self.facts["cnt"]["OVTK"], 10)
        self.assertEqual(self.facts["cnt"]["STLG"], 5)      # 5 luces de salida
        self.assertEqual(self.facts["cnt"]["LGOT"], 1)
        self.assertEqual(self.facts["cnt"]["RCWN"], 1)


if __name__ == "__main__":
    unittest.main()
