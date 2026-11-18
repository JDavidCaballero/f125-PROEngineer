"""Trazas por distancia, análisis de vuelta, ERS, comparación con rivales y estimaciones de entrenamiento."""
import os, re, unittest
from pathlib import Path

from helpers import cfg, REAL
import assistant as A
import engineer as E
import practice as PR
import quali as Q
import traces as TR
import synth as SY
from f1_parse import NCARS


def lap_rec(tr, car, lap=1, status=1, **kw):
    s, t = SY.lap_samples(**kw)
    SY.drive(tr, car, s, t, lap, status=status)
    return tr.traces.last_timed(car), t


class TraceBookTests(unittest.TestCase):
    def test_closed_lap_has_official_time_full_coverage_and_monotonic_time(self):
        tr = SY.new_tracker(5)
        r, t = lap_rec(tr, 0)
        self.assertEqual(r.time_ms, round(t))
        self.assertGreaterEqual(r.cover, 0.95)
        self.assertEqual(r.t[0], 0.0)
        self.assertEqual(r.t[-1], float(round(t)))
        self.assertTrue(all(b >= a for a, b in zip(r.t, r.t[1:])))
        self.assertEqual(len(r.speed), TR.NB)
        self.assertEqual(len(r.t), TR.NB + 1)
        self.assertLessEqual(max(r.speed), 301)

    def test_quali_lap_must_be_a_flying_lap_to_be_timed(self):
        tr = SY.new_tracker(5)
        s, t = SY.lap_samples()
        SY.drive(tr, 0, s, t, 1, status=3)                    # out-lap
        self.assertIsNone(tr.traces.last_timed(0))
        self.assertIsNotNone(tr.traces.pop_done(0))           # pero la traza se guardó
        SY.drive(tr, 0, s, t, 2, status=1)
        self.assertIsNotNone(tr.traces.last_timed(0))

    def test_race_laps_are_timed_without_the_flying_status(self):
        tr = SY.new_tracker(15)
        r, _ = lap_rec(tr, 0, status=4)
        self.assertIsNotNone(r)

    def test_invalid_and_pit_laps_are_not_timed(self):
        tr = SY.new_tracker(5)
        s, t = SY.lap_samples()
        SY.drive(tr, 0, s, t, 1, invalid=True)
        self.assertIsNone(tr.traces.last_timed(0))
        SY.drive(tr, 0, s, t, 2, pit=True)
        self.assertIsNone(tr.traces.last_timed(0))

    def test_partial_lap_is_rejected_not_invented(self):
        tr = SY.new_tracker(5)
        s, t = SY.lap_samples(skip=(1000, 2500))              # faltan 30% de la vuelta
        SY.drive(tr, 0, s, t, 1)
        self.assertIsNone(tr.traces.pop_done(0))
        self.assertEqual(tr.traces.best, {})

    def test_best_lap_per_car_and_independent_cars(self):
        tr = SY.new_tracker(5)
        for car, slow in ((0, {}), (3, {1: 15})):
            lap_rec(tr, car, slow=slow)
        self.assertLess(tr.traces.best[0].time_ms, tr.traces.best[3].time_ms)
        r, _ = lap_rec(tr, 0, lap=2, slow={0: 30})
        self.assertEqual(tr.traces.best[0].lap, 1)            # la nueva fue más lenta: sigue la primera

    def test_no_track_length_means_no_samples(self):
        tr = SY.new_tracker(5)
        tr.ses["track_length_m"] = None
        SY.set_car(tr, 0, 1, 100.0, 1000.0, 200, 1.0, 0.0, 0, 3e6, 1, False, False, 16, 1, 0)
        tr.traces.feed(tr)
        self.assertEqual(tr.traces.cur, {})


class CornerAnalysis(unittest.TestCase):
    def test_corners_found_in_order_and_segments_tile_the_lap(self):
        r, _ = lap_rec(SY.new_tracker(5), 0)
        corners = Q.find_corners(r)
        self.assertEqual(len(corners), 3)
        for found, (dist, _) in zip(corners, SY.CORNERS):
            self.assertAlmostEqual(found * SY.TRACK / TR.NB, dist, delta=60)
        segs = Q.segments(r)
        self.assertEqual(segs[0]["b0"], 0)
        self.assertEqual(segs[-1]["b1"], TR.NB)
        self.assertTrue(all(a["b1"] == b["b0"] for a, b in zip(segs, segs[1:])), "los tramos deben cubrir toda la vuelta sin huecos")

    def test_lost_time_is_found_in_the_right_corner(self):
        tr = SY.new_tracker(5)
        fast, _ = lap_rec(tr, 0, lap=1)
        slow, _ = lap_rec(tr, 0, lap=2, slow={1: 25})            # segunda curva 25 km/h más lenta
        rows = Q.losses(slow, fast, n=3)
        self.assertEqual(rows[0]["seg"]["name"], "la curva 2")
        self.assertLessEqual(rows[0]["d_vmin"], -20)
        total = sum(r["dt_s"] for r in Q.compare(slow, fast))
        self.assertAlmostEqual(total, (slow.time_ms - fast.time_ms) / 1000.0, places=1)       # los tramos suman la vuelta
        txt = " ".join(Q.lap_report(slow, fast))
        self.assertIn("la curva 2", txt)
        self.assertIn("más lenta", txt)
        self.assertIn("km/h menos en el vértice", txt)

    def test_identical_laps_report_nothing_to_improve(self):
        tr = SY.new_tracker(5)
        a, _ = lap_rec(tr, 0, lap=1)
        b, _ = lap_rec(tr, 0, lap=2)
        self.assertEqual(Q.losses(b, a), [])
        self.assertIn("repartida", " ".join(Q.lap_report(b, a)))

    def test_different_tracks_are_not_compared(self):
        tr = SY.new_tracker(5)
        a, _ = lap_rec(tr, 0)
        b, _ = lap_rec(tr, 0, lap=2)
        b.track_m = 4000.0
        self.assertEqual(Q.compare(a, b), [])


class ErsAnalysis(unittest.TestCase):
    def test_spare_battery_is_reported_with_the_straights_to_use(self):
        tr = SY.new_tracker(5)
        r, _ = lap_rec(tr, 0, deploy=40.0, store0=3.9e6)         # despliega muy poco
        txt = " ".join(Q.ers_report(r))
        self.assertIn("Te faltó usar el", txt)
        self.assertRegex(txt, r"la recta")

    def test_battery_ran_out_is_reported_with_the_km(self):
        tr = SY.new_tracker(5)
        r, _ = lap_rec(tr, 0, deploy=900.0, harvest=10.0, store0=1.0e6)
        e = Q.ers_summary(r)
        self.assertIsNotNone(e["ran_out_km"])
        self.assertIn("La batería se agotó en el km", " ".join(Q.ers_report(r)))

    def test_comparison_with_a_rival_names_total_deployment(self):
        tr = SY.new_tracker(5)
        mine, _ = lap_rec(tr, 0, deploy=100.0)
        rival, _ = lap_rec(tr, 3, deploy=400.0)
        txt = " ".join(Q.ers_report(mine, rival, "Hansen"))
        self.assertRegex(txt, r"despliegue permitido frente al \d+% de Hansen")

    def test_rival_report_says_where_the_rival_deploys_more(self):
        tr = SY.new_tracker(5)
        mine, _ = lap_rec(tr, 0, deploy=60.0, slow={0: 20})
        rival, _ = lap_rec(tr, 3, deploy=300.0)
        txt = " ".join(Q.rival_report(mine, rival, "Hansen"))
        self.assertIn("Hansen te saca", txt)
        self.assertIn("te gana", txt)


class PracticeEstimates(unittest.TestCase):
    def tr(self):
        from test_compare import hist, fake_tr
        mine = hist([90000] + [80000] * 6 + [91000] + [79500] * 5, [(7, 17), (255, 18)])
        a = hist([90000] + [79000] * 8, [(255, 17)])
        b = hist([90000] + [81500] * 8, [(255, 18)])
        tr = fake_tr({0: mine, 1: a, 2: b}, player=0)
        tr.stint_log = [{"comp": 17, "age": 7, "wear": 14.0}]
        return tr

    def test_quali_estimate_ranks_by_best_valid_lap(self):
        q = PR.quali_estimate(self.tr())
        self.assertEqual((q["rank"], q["n"]), (2, 3))
        self.assertAlmostEqual(q["gap_s"], 0.5)

    def test_race_estimate_uses_long_clean_runs(self):
        r = PR.race_estimate(self.tr())
        self.assertEqual((r["rank"], r["n"]), (2, 3))

    def test_compound_table_has_pace_laps_and_wear_rate(self):
        tab = {t["comp"]: t for t in PR.compound_table(self.tr())}
        self.assertAlmostEqual(tab[17]["pace"], 80.0)
        self.assertAlmostEqual(tab[17]["wear_rate"], 2.0)
        self.assertIsNone(tab[18]["wear_rate"])

    def test_summary_is_labelled_as_an_estimate(self):
        txt = " ".join(PR.summary(self.tr()))
        self.assertIn("P2 de 3", txt)
        self.assertIn("estimación", txt.lower())

    def test_not_enough_data_says_so_instead_of_guessing(self):
        from test_compare import hist, fake_tr
        tr = fake_tr({0: hist([90000, 80000], [(255, 17)]), 1: hist([90000, 79000], [(255, 17)])}, player=0)
        tr.stint_log = []
        txt = " ".join(PR.summary(tr))
        self.assertIn("Aún no tienes una tanda", txt)
        self.assertIsNone(PR.race_estimate(tr))


def make_engine(session_type=5):
    out = []
    eng = E.Engineer(cfg(), "normal", None, out=out.append, sync=True)
    SY.config(eng.tr, session_type)
    eng.tr.names = {0: "Tu", 3: "Hansen", 4: "Costa"}
    eng.tr.team = {0: 7, 4: 7}
    eng.tr.lap_all = [None] * NCARS
    eng.tr.sta_all = [None] * NCARS
    eng.tr.tel_all = [None] * NCARS
    eng.tr.dmg_all = [None] * NCARS
    return eng, out


def run_lap(eng, car, lap, **kw):
    s, t = SY.lap_samples(**{k: v for k, v in kw.items() if k in ("corners", "slow", "deploy", "harvest", "store0")})
    eng.tr.t += 100.0                                              # entre vueltas pasa tiempo (el ingeniero no habla encima)
    SY.drive(eng.tr, car, s, t, lap, status=kw.get("status", 1), position=kw.get("position", 1))
    if car == eng.tr.player:
        eng._poll_laps()
    return t


class QualifyingFlow(unittest.TestCase):
    def test_first_flying_lap_reports_ers_and_no_cheering(self):
        eng, out = make_engine(5)
        run_lap(eng, 0, 1, status=3)                                  # out-lap: calla
        self.assertEqual(out, [])
        run_lap(eng, 0, 2, deploy=40.0, store0=3.9e6)
        said = " ".join(out)
        self.assertIn("Vuelta lanzada", said)
        self.assertIn("Te faltó usar el", said)
        self.assertNotRegex(said, r"(?i)ánimo|vamos|genial|bien hecho")
        self.assertNotIn("Propuesta", said)                           # sin estrategia de carrera en clasificación

    def test_second_lap_names_the_corners_where_time_is_lost(self):
        eng, out = make_engine(5)
        run_lap(eng, 0, 2)
        out.clear()
        run_lap(eng, 0, 3, slow={2: 30})
        said = " ".join(out)
        self.assertIn("la curva 3", said)
        self.assertIn("pierdes", said)

    def test_rival_within_a_second_gets_a_comparison_and_none_without_trace(self):
        eng, out = make_engine(5)
        run_lap(eng, 3, 2, slow={}, deploy=400.0)                     # rival rápido, con traza
        run_lap(eng, 0, 2, slow={1: 12}, deploy=60.0)
        said = " ".join(out)
        self.assertIn("Hansen te saca", said)
        eng2, out2 = make_engine(5)
        run_lap(eng2, 0, 2, slow={1: 12})
        self.assertNotIn("te saca", " ".join(out2))                   # sin datos del rival se descarta la idea

    def test_rival_too_far_is_not_compared(self):
        eng, out = make_engine(5)
        run_lap(eng, 3, 2, corners=((800.0, 140.0), (2000.0, 140.0), (3600.0, 140.0)))   # mucho más rápido
        run_lap(eng, 0, 2)
        self.assertNotIn("te saca", " ".join(out))

    def test_topic_can_be_silenced_and_race_never_speaks_laps(self):
        eng, out = make_engine(5)
        eng.tac.topics["vueltas"] = -1
        run_lap(eng, 0, 2)
        self.assertEqual(out, [])
        eng, out = make_engine(15)
        run_lap(eng, 0, 2, status=4)
        self.assertEqual(out, [])
        self.assertEqual(len(eng.my_timed), 1)


class PilotQuestions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng, cls.out = make_engine(5)
        run_lap(cls.eng, 4, 2, deploy=300.0)                          # compañero
        run_lap(cls.eng, 3, 2, deploy=400.0, position=1)              # P1
        run_lap(cls.eng, 0, 2, slow={1: 12}, deploy=80.0, position=3)
        run_lap(cls.eng, 0, 3, slow={1: 18}, deploy=80.0, position=3)

    def parse(self, text):
        return A.parse_rules(self.eng.tr, text)

    def test_phrases(self):
        cases = {
            "¿Cómo fue mi vuelta?": "analisis_vuelta", "¿Dónde pierdo tiempo?": "analisis_vuelta",
            "¿En qué curvas puedo mejorar?": "analisis_vuelta", "¿Cómo uso el ERS?": "ers_vuelta",
            "¿Dónde debo usar más el ERS?": "ers_vuelta", "compárame con el primero": "compara_vuelta",
            "compárame con mi compañero": "compara_vuelta", "compárame con Hansen": "compara_vuelta",
            "compárame con el de delante": "compara_vuelta", "¿Qué estimas para la carrera?": "estimacion",
            "¿Cómo va la práctica?": "estimacion",
            "¿cuánta batería tengo?": "ers", "¿por qué estoy perdiendo tiempo?": "por_que_pierdo",
            "¿cómo va mi compañero?": "companero",
        }
        for text, want in cases.items():
            got = self.parse(text)
            self.assertIsNotNone(got, text)
            self.assertEqual(got.name, want, text)
        self.assertEqual(self.parse("compárame con mi compañero").topic, "companero")
        self.assertEqual(self.parse("compárame con el primero").topic, "primero")

    def test_analysis_mentions_the_lost_corner(self):
        r = self.eng.ask("¿dónde pierdo tiempo?")
        self.assertIn("la curva 2", r)

    def test_ers_answer(self):
        r = self.eng.ask("¿cómo uso el ERS?")
        self.assertRegex(r, r"Usaste el \d+% del despliegue permitido")

    def test_comparisons_with_each_kind_of_target(self):
        for text, who in (("compárame con el primero", "Hansen"), ("compárame con mi compañero", "Costa"),
                          ("compárame con Hansen", "Hansen")):
            self.assertIn(who, self.eng.ask(text), text)

    def test_comparison_without_the_rival_trace_says_so(self):
        eng, _ = make_engine(5)
        run_lap(eng, 0, 2, position=2)
        r = eng.ask("compárame con Hansen")
        self.assertIn("No tengo una vuelta completa medida de Hansen", r)
        eng2, _ = make_engine(5)
        run_lap(eng2, 0, 1, status=3)                                 # solo una out-lap: no hay vuelta medida
        self.assertIn("Aún no tengo una vuelta completa", eng2.ask("¿dónde pierdo tiempo?"))

    def test_estimation_answer_is_labelled(self):
        self.assertIn("estimación", self.eng.ask("¿qué estimas para la carrera?").lower())


@unittest.skipUnless(REAL.exists(), "sin captura real")
class RealCapture(unittest.TestCase):
    """Con la captura real de Singapur: los tiempos de las trazas coinciden con el historial del juego."""

    @classmethod
    def setUpClass(cls):
        import state
        from f1_parse import read_capture
        tr, cls.best, cls.hist = state.RaceTracker({}, None), None, None
        top = (0, None, None)
        for t, d in read_capture(REAL):
            tr.on_packet(t, d)
            n = sum(len(v) for v in tr.traces.recent.values())
            if n > top[0]:
                top = (n, tr.traces, dict(tr.hist))
        cls.n, cls.tb, cls.hist = top

    def test_trace_times_equal_game_history(self):
        checked = 0
        for car, rec in self.tb.best.items():
            h = self.hist.get(car)
            if h and rec.lap - 1 < len(h["laps"]):
                self.assertEqual(rec.time_ms, h["laps"][rec.lap - 1]["ms"], car)
                checked += 1
        self.assertGreaterEqual(checked, 15)

    def test_real_traces_are_complete_and_monotonic(self):
        for rec in self.tb.best.values():
            self.assertGreaterEqual(rec.cover, 0.9)
            self.assertTrue(all(b >= a for a, b in zip(rec.t, rec.t[1:])))
            self.assertTrue(0 <= min(rec.store) and max(rec.store) <= 4_000_001)

    def test_corner_count_is_stable_between_cars(self):
        counts = [len(Q.find_corners(r)) for r in self.tb.best.values()]
        self.assertLessEqual(max(counts) - min(counts), 3)
        self.assertGreaterEqual(min(counts), 5)


if __name__ == "__main__":
    unittest.main()
