"""Parar antes/después (undercut/overcut), aire sucio medido y la regla de no repetirse."""
import unittest
from types import SimpleNamespace as NS

from helpers import demo, cfg
import assistant as A
import engineer as E
import strategy as S
import timing as TM
from f1_parse import read_capture
from test_assistant import run_until


def opt(i, lap, comp=17, util=5.0, e_pos=4.0, beat=None, cdf=None, nstops=1):
    cdf = cdf or [0.0, 0.0, 0.1, 0.5, 0.9, 1.0] + [1.0] * 16
    return S.Option(id=i, kind="PIT", desc=f"parar en la vuelta {lap}", phrase="", stops=tuple((lap + 10 * k, comp) for k in range(nstops)),
                    sc_react=False, e_pos=e_pos, utility=util, cdf=cdf, p_beat=beat or {})


class Alternatives(unittest.TestCase):
    def setUp(self):
        self.plan = opt("A", 12, util=5.0, e_pos=4.0, beat={"Hansen": 0.30, "Costa": 0.70})
        self.under = opt("B", 10, util=4.9, e_pos=3.9, beat={"Hansen": 0.60})
        self.over = opt("C", 14, util=4.8, e_pos=4.1, beat={"Hansen": 0.20, "Costa": 0.75}, nstops=1)
        self.rep = NS(options=[self.plan, self.under, self.over])

    def test_undercut_offered_when_it_clearly_helps_against_one_rival(self):
        a = TM.alternatives(self.rep, self.plan, 8)
        self.assertEqual(a[0]["kind"], "undercut")
        self.assertEqual(a[0]["rival"], "Hansen")
        self.assertAlmostEqual(a[0]["dp"], 0.30)
        self.assertEqual(a[0]["shift"], -2)

    def test_direction_filters(self):
        self.assertTrue(all(a["kind"] == "undercut" for a in TM.alternatives(self.rep, self.plan, 8, "antes")))
        despues = TM.alternatives(self.rep, self.plan, 8, "despues", min_dp=0.05)
        self.assertEqual([a["kind"] for a in despues], ["overcut"])

    def test_too_costly_or_too_little_gain_is_not_offered(self):
        self.under.utility = 3.0                                    # cuesta 2.0 puntos
        self.assertEqual([a for a in TM.alternatives(self.rep, self.plan, 8) if a["kind"] == "undercut"], [])
        self.under.utility = 4.9
        self.under.p_beat = {"Hansen": 0.35}                        # mejora de solo 5 puntos porcentuales
        self.assertEqual(TM.alternatives(self.rep, self.plan, 8), [])

    def test_other_compound_other_stops_or_past_laps_are_ignored(self):
        self.under.stops = ((10, 18),)
        self.assertEqual(TM.alternatives(self.rep, self.plan, 8), [])
        self.under.stops = ((10, 17),)
        self.assertEqual(TM.alternatives(self.rep, self.plan, 11), [])        # ya pasó la vuelta 10
        self.assertEqual(TM.alternatives(self.rep, self.plan, 8)[0]["rival"], "Hansen")

    def test_nothing_for_a_stay_option(self):
        stay = S.Option(id="S", kind="STAY", desc="", phrase="", stops=(), sc_react=False)
        self.assertEqual(TM.alternatives(self.rep, stay, 8), [])
        self.assertEqual(TM.alternatives(self.rep, None, 8), [])

    def test_describe_uses_the_simulation_numbers(self):
        a = TM.alternatives(self.rep, self.plan, 8)[0]
        txt = TM.describe(a, self.plan, 8, {"loss_s": 0.4, "n": 9})
        for piece in ("undercut a Hansen", "30%", "60%", "vuelta 10", "0.4 s", "9 vueltas"):
            self.assertIn(piece, txt)

    def test_risk_utility_prefers_the_better_distribution(self):
        good = opt("g", 9, cdf=[0.5, 0.9, 1.0] + [1.0] * 19)
        bad = opt("b", 9, cdf=[0.0, 0.0, 0.0, 0.2, 0.6, 1.0] + [1.0] * 16)
        self.assertGreater(TM.risk_utility(good), TM.risk_utility(bad))
        self.assertAlmostEqual(sum(TM.pmf(good.cdf)), 1.0)


class DirtyAir(unittest.TestCase):
    def data(self, loss):
        gaps = {c: {l: (0.5 if l <= 5 else 3.0) for l in range(1, 11)} for c in (1, 2, 3)}
        times = {c: {l: 80000 + (loss * 1000 if l <= 5 else 0) for l in range(1, 11)} for c in (1, 2, 3)}
        return gaps, times

    def test_measures_loss_per_car_against_itself(self):
        r = TM.dirty_air(*self.data(0.4))
        self.assertAlmostEqual(r["loss_s"], 0.4, places=3)

    def test_not_enough_data_returns_none(self):
        self.assertIsNone(TM.dirty_air({1: {1: 0.5}}, {1: {1: 80000}}))
        self.assertIsNone(TM.dirty_air(*self.data(0.0)))               # sin pérdida no hay nada que decir
        self.assertIsNone(TM.dirty_air(*self.data(5.0)))               # absurdo (>2 s): se descarta


class InTheRace(unittest.TestCase):
    def play(self, name, topics=None, mode="normal"):
        path, _ = demo(name)
        out, offered = [], []
        eng = E.Engineer(cfg(), mode, None, out=out.append, sync=True)
        eng.tac.topics.update(topics or {})
        orig = eng._log
        eng._log = lambda rec: (offered.append(rec) if rec.get("type") == "alt_offered" else None, orig(rec))[1]
        for t, d in read_capture(path):
            eng.on_packet(t, d)
        return eng, out, offered

    def test_alternative_is_offered_at_most_once_per_stop_window(self):
        for sc in ("corta", "media"):
            eng, out, offered = self.play(sc, mode="arriesgado")
            laps = [o["lap"] for o in offered]
            self.assertLessEqual(len(offered), len(eng.timing_asked))
            self.assertEqual(len(eng.timing_asked), len(set(eng.timing_asked)))
            self.assertTrue(any("Fin de carrera" in l for l in out), sc)

    def test_conservative_mode_and_silenced_topic_never_offer(self):
        self.assertEqual(self.play("media", mode="conservador")[2], [])
        self.assertEqual(self.play("media", {"alternativas": -1})[2], [])

    def test_offer_is_spoken_with_the_proposal_and_only_once(self):
        """Fuerza una alternativa para comprobar todo el camino: propuesta + alternativa en el mismo mensaje."""
        real = TM.alternatives
        def fake(rep, chosen, lap, *a, **k):
            o = opt("Z", chosen.stops[0][0] - 2, comp=chosen.stops[0][1], util=chosen.utility - 0.1)
            return [{"option": o, "kind": "undercut", "rival": "Hansen", "p_alt": 0.6, "p_plan": 0.3, "dp": 0.3,
                     "cost": 0.1, "risk_gain": 0.2, "shift": -2}]
        TM.alternatives = fake
        try:
            eng, out, offered = self.play("media", mode="arriesgado")
        finally:
            TM.alternatives = real
        said = [l for l in out if "Alternativa arriesgada" in l]
        self.assertTrue(said)
        self.assertIn("Propuesta", said[0])
        self.assertIn("Di «parar antes» o «alargar»", said[0])
        self.assertEqual(len(offered), len(eng.timing_asked))          # una oferta por ventana de parada
        self.assertEqual(len({o["lap"] for o in offered}), len(offered))

    def test_phrases(self):
        eng, _, _ = run_until("media", 600)
        tr = eng.tr
        for text, want in (("parar antes", "alternativa"), ("quiero parar antes", "alternativa"), ("¿hago overcut?", "overcut"),
                           ("¿y si alargo la parada?", "overcut"), ("¿puedo hacer undercut?", "undercut"),
                           ("prefiero la alternativa", "alternativa")):
            self.assertEqual(A.parse_rules(tr, text).name, want, text)
        for text in ("parar antes", "¿hago overcut?", "¿puedo hacer undercut?"):
            r = eng.ask(text)
            self.assertTrue(r and "No he entendido" not in r, (text, r))

    def test_accepting_the_offered_alternative_makes_it_the_plan(self):
        eng, _, _ = run_until("media", 600)
        lap = eng.tr.cur_lap
        o = opt("Z", lap + 2, util=5.0)
        eng.dialog.alt = {"option": o, "kind": "undercut", "rival": "Hansen", "p_alt": 0.6, "p_plan": 0.3, "dp": 0.3,
                          "cost": 0.1, "risk_gain": 0.2, "shift": -2}
        r = eng.ask("parar antes")
        self.assertIn("plan confirmado", r)
        self.assertEqual(eng.dialog.accepted.option.stops[0][0], lap + 2)


if __name__ == "__main__":
    unittest.main()
