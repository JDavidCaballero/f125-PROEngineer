import unittest

from helpers import demo, cfg
import backtest as B
import dialog as D
import strategy as S


class Helpers(unittest.TestCase):
    HIST = {"stints": [{"end_lap": 10, "actual": 16, "visual": 16}, {"end_lap": 255, "actual": 18, "visual": 18}], "laps": []}

    def test_actual_stops_from_history(self):
        # end_lap es índice 0 de la vuelta de entrada: 10 -> entra en la vuelta 11 y sale con compuesto 18
        self.assertEqual(B.actual_stops(self.HIST, 5), [(11, 18)])
        self.assertEqual(B.actual_stops(self.HIST, 12), [])          # ya pasó: desde la vuelta 12 no quedan paradas

    def test_match_option(self):
        rep = S.Report(options=[S.Option("A", "STAY", "x", "x", (), False), S.Option("B", "PIT", "y", "y", ((11, 18),), True),
                                S.Option("C", "PIT", "z", "z", ((13, 17),), True)])
        sit = S.Situation(30, 8, 22.0)
        self.assertEqual(B.match_option(rep, sit, [(11, 18)]).id, "B")
        self.assertEqual(B.match_option(rep, sit, [(12, 18)]).id, "B")            # ±1 vuelta
        self.assertIsNone(B.match_option(rep, sit, [(11, 16)]))                    # otro compuesto: no coincide
        self.assertEqual(B.match_option(rep, sit, []).id, "A")


class Run(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.res = B.run(demo("media")[0], "normal", cfg()) + B.run(demo("sprint")[0], "normal", cfg())
        cls.s = B.summarize(cls.res)

    def test_produces_finite_calibrated_metrics(self):
        s = self.s
        self.assertEqual(s["races"], 2)
        self.assertGreater(s["matched"], 5)
        self.assertTrue(0 <= s["coverage_p10_p90"] <= 1)
        self.assertGreaterEqual(s["mean_abs_pos_error"], 0)
        self.assertTrue(0 <= s["pairwise_brier"] <= 1)
        self.assertIn("reliability", s)
        self.assertTrue(all(0 <= b["predicted"] <= 1 and 0 <= b["observed"] <= 1 for b in s["reliability"]))

    def test_no_stop_race_is_well_calibrated(self):
        sprint = B.summarize(B.run(demo("sprint")[0], "normal", cfg()))
        self.assertLess(sprint["mean_abs_pos_error"], 1.0)
        self.assertGreater(sprint["pairwise_skill_vs_50pct"], 0.5)

    def test_messages_per_lap_is_reported_and_bounded(self):
        self.assertLess(self.s["msgs_per_lap"], 12)             # el ingeniero no habla sin parar

    def test_stability_counts_changes(self):
        evs = [{"best_first": (11, 17)}, {"best_first": (12, 17)}, {"best_first": (15, 18)}, {"best_first": None}]
        ch, n = B.stability(evs)
        self.assertEqual((ch, n), (2, 3))                         # 11->12 mismo plan (±1); 12->15 y 15->None cambian


if __name__ == "__main__":
    unittest.main()
