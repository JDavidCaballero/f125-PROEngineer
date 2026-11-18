"""Rama estrategia/plan-dinamico: cuándo se cambia el plan que se sigue (regla de diseño, 11 nov 2026)."""
import unittest
from types import SimpleNamespace as NS

from test_assistant import run_until


def opt(lap, comp, e_pos, desc="x"):
    return NS(e_pos=e_pos, stops=[(lap, comp)], kind="PIT", desc=desc, utility=0.0, id=desc, phrase=desc)


class ChangeRule(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng, _, _ = run_until("corta", 330)

    def setUp(self):
        e = self.eng
        e.preplan = {"stints": [(16, 12), (17, 8)]}           # plan previo: parar al final de la vuelta 12 a Medio
        e.dialog.accepted = None
        e.plan_change_lap = -99
        self.plan = opt(12, 17, 5.0, "plan")
        self.sit = NS(lap=8)

    def ask(self, chosen, pos=5, urgent=False, lap=8):
        rep = NS(options=[self.plan, chosen])
        return self.eng._change_ok(rep, chosen, self.sit, NS(pos=pos), lap, urgent, "mejora")

    def test_small_gain_does_not_change_the_plan(self):
        ok, why, _ = self.ask(opt(9, 17, 4.5))
        self.assertFalse(ok)
        self.assertIn("el plan sigue valiendo", why)

    def test_clear_gain_changes_it(self):
        ok, why, plan_b = self.ask(opt(9, 17, 3.8))
        self.assertTrue(ok)
        self.assertFalse(plan_b)                                # vas en los puntos: es un cambio, no un plan B

    def test_out_of_the_points_it_is_a_plan_b(self):
        ok, _, plan_b = self.ask(opt(9, 17, 3.8), pos=14)
        self.assertTrue(ok)
        self.assertTrue(plan_b)

    def test_serious_event_skips_the_threshold(self):
        ok, _, _ = self.ask(opt(8, 18, 4.9), urgent=True)
        self.assertTrue(ok)

    def test_same_plan_is_always_allowed(self):
        ok, _, _ = self.ask(opt(13, 17, 5.0))
        self.assertTrue(ok)

    def test_no_second_change_right_after_one(self):
        self.eng.plan_change_lap = 6
        ok, why, _ = self.ask(opt(9, 17, 3.0))
        self.assertFalse(ok)
        self.assertIn("no se cambia otra vez", why)

    def test_without_a_plan_the_normal_filter_decides(self):
        self.eng.preplan = None
        ok, _, _ = self.ask(opt(9, 17, 4.9))
        self.assertTrue(ok)



class DamageMeasured(unittest.TestCase):
    def setUp(self):
        self.eng, _, _ = run_until("corta", 330)
        tr = self.tr = self.eng.tr
        laps = [90.0, 90.1, 90.0, 90.1, 91.5, 90.9, 90.8, 90.9]        # golpe en la vuelta 5: después ~0.8 s más lento
        tr.hist[tr.player] = {**tr.hist.get(tr.player, {}), "laps": [{"ms": int(x * 1000), "valid": True} for x in laps]}
        tr.pit_log, tr.dmg_onset = [], 5
        tr.stint_rate = lambda: None

    def test_loss_comes_from_the_laps_after_the_hit(self):
        self.assertAlmostEqual(self.tr.damage_loss(), 0.8, places=2)

    def test_needs_two_laps_after_the_hit(self):
        self.tr.hist[self.tr.player]["laps"] = self.tr.hist[self.tr.player]["laps"][:6]
        self.assertIsNone(self.tr.damage_loss())

    def test_light_damage_is_not_urgent_unless_it_costs_time(self):
        me = NS(dmg_measured=True, dmg_pace_s=0.2)
        dm = dict(self.tr.dmg_all[self.tr.player], front_left_wing=25, front_right_wing=0, rear_wing=0)
        self.tr.dmg_all[self.tr.player] = dm
        self.assertFalse(self.eng._damage_serious(me))
        self.assertTrue(self.eng._damage_serious(NS(dmg_measured=True, dmg_pace_s=0.9)))
        dm["front_left_wing"] = 70
        self.assertTrue(self.eng._damage_serious(me))



class Acknowledge(unittest.TestCase):
    """Si pensar tarda, el ingeniero dice «Recibido, lo miro» antes de la respuesta (como uno real)."""

    def session(self, delay):
        import time
        import stt as STT
        import voice as V
        eng, _, _ = run_until("corta", 200)
        said = []
        speaker = NS(say=lambda text, prio=2: said.append(text), on_event=None, voice="-")
        real_ask = eng.ask
        eng.ask = lambda q: (time.sleep(delay), real_ask(q))[1]
        cfg = {**eng.cfg, "llm": {**eng.cfg.get("llm", {}), "ack_after_s": 0.1}}
        sess = V.VoiceSession(cfg, eng, speaker, STT.FakeSTT(), echo=lambda *a: None)
        return sess, said

    def test_slow_answer_gets_an_acknowledgement_first(self):
        sess, said = self.session(0.3)
        rec = sess.utterance("cuánto combustible me queda")
        self.assertEqual(said[0], "Recibido, lo miro.")
        self.assertTrue(rec["ack"])
        self.assertGreater(len(said), 1)

    def test_fast_answer_has_no_acknowledgement(self):
        sess, said = self.session(0.0)
        rec = sess.utterance("cuánto combustible me queda")
        self.assertNotIn("Recibido, lo miro.", said)
        self.assertFalse(rec["ack"])


if __name__ == "__main__":
    unittest.main()
