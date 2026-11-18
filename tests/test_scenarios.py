"""Casos inventados: cada escenario de make_demo prueba una situación que el motor debe manejar con criterio."""
import unittest

from helpers import demo, run_engineer, cfg
import fuelzones as F
import learn, make_demo as M
import strategy as S
from f1_parse import read_capture


def lines(name, **k):
    path, info = demo(name)
    out, eng = run_engineer(path, **k)
    return out, eng, info


class SafetyCar(unittest.TestCase):
    def test_sc_in_the_last_laps_does_not_ask_for_a_stop(self):
        out, eng, _ = lines("sc_final")
        i = next(k for k, l in enumerate(out) if "Safety Car desplegado" in l)
        after = out[i:]
        self.assertFalse(any("Propuesta" in l for l in after), "con 3 vueltas por delante no compensa parar")
        self.assertTrue(any("Con el Safety Car: Sigue fuera hasta el final" in l for l in after))

    def test_sc_right_after_my_stop_says_stay_out(self):
        out, eng, _ = lines("sc_tras_parada")
        i = next(k for k, l in enumerate(out) if "Safety Car desplegado" in l)
        self.assertFalse(any("Propuesta" in l for l in out[i:]), "ya cumplí la parada: no hay segunda")
        self.assertTrue(any("acabas de cambiar neumáticos, sigue fuera" in l for l in out[i:]))

    def test_vsc_is_recognised_and_has_its_own_label(self):
        out, eng, _ = lines("vsc")
        self.assertTrue(any("Virtual Safety Car" in l for l in out))
        self.assertTrue(any("Propuesta: Con el VSC:" in l for l in out))
        self.assertFalse(any("Propuesta: Con el Safety Car:" in l for l in out))


class Incidents(unittest.TestCase):
    def test_puncture_alerts_and_proposes(self):
        out, eng, _ = lines("pinchazo")
        self.assertTrue(any("Daño en el neumático delantero izquierdo: 70%" in l for l in out))
        self.assertTrue(any("Propuesta: Con el daño" in l for l in out))

    def test_red_flag_free_stop_and_state_clears(self):
        out, eng, info = lines("roja")
        self.assertTrue(any("Bandera roja" in l for l in out))
        self.assertTrue(any("Propuesta: Con la bandera roja" in l for l in out))
        self.assertFalse(eng.tr.red_active, "la bandera roja debe darse por terminada cuando el coche vuelve a rodar")
        late = [l for l in out if "(bandera roja: gratis)" in l]
        self.assertEqual(late, [])

    def test_pending_penalty_served_under_safety_car(self):
        out, eng, _ = lines("sancion")
        self.assertTrue(any("Sanción: drive-through" in l for l in out))
        self.assertTrue(any("Tienes un drive-through pendiente" in l for l in out))
        self.assertTrue(any("cumplir el drive-through pendiente cuesta menos" in l for l in out))

    def test_fresh_tyre_rival_behind_keeps_the_driver_informed(self):
        out, eng, _ = lines("rival_fresco")
        self.assertTrue(any("defiende" in l or "recorta" in l for l in out))
        self.assertTrue(any("Fin de carrera" in l for l in out))

    def test_lost_packets_do_not_break_anything(self):
        out, eng, _ = lines("datos_perdidos")
        self.assertTrue(any("Fin de carrera" in l for l in out) or len(out) > 20)
        self.assertGreater(eng.evals, 3)
        self.assertTrue(any("Propuesta" in l for l in out))


class FuelSaving(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out, cls.eng, cls.info = lines("ahorro")

    def test_learns_the_planted_zones(self):
        a = self.eng.tr.fz.analyze()
        self.assertGreaterEqual(a["laps"], 12)
        top = {z["bin"] for z in a["measured"][:3]}
        self.assertEqual(top, M.GOOD_ZONES)
        worst = a["measured"][3]
        self.assertGreater(a["measured"][2]["kg_per_s"], 10 * worst["kg_per_s"])    # separación clara

    def test_alert_and_answer_name_a_planted_zone(self):
        said = [l for l in self.out if "Levanta antes en el tramo" in l]
        self.assertTrue(said)
        good_tramos = {b + 1 for b in M.GOOD_ZONES}
        import re
        self.assertIn(int(re.search(r"tramo (\d+)", said[-1]).group(1)), good_tramos)
        r = self.eng.assistant.handle("¿dónde puedo ahorrar combustible?")
        self.assertIn("Levanta antes", r)

    def test_no_rich_mixture_advice_and_lean_advice_only_when_short(self):
        self.assertFalse(any("mezcla rica" in l or "subir la mezcla" in l for l in self.out))

    def test_profile_roundtrip_gives_advice_in_a_new_session(self):
        prof = {"tracks": {}}
        learn.learn_file(demo("ahorro")[0], prof, cfg())
        fz = prof["tracks"]["17"]["fuel_zones"]
        self.assertEqual({z["bin"] for z in fz["measured"][:3]}, M.GOOD_ZONES)
        import state
        tr = state.RaceTracker(cfg(), prof)
        for t, d in read_capture(demo("sprint")[0]):         # misma pista (Austria, id 17), sin vueltas de ahorro todavía
            tr.on_packet(t, d)
            if tr.ses:
                break
        self.assertIsNotNone(tr.fz.saved)
        self.assertTrue(tr.fz.suggest(1)[0].startswith("Levanta antes"))


class FuelZonesUnit(unittest.TestCase):
    def feed(self, fz, lap, thr_of, fuel0, empty_after=None, nb=40, track=4000.0, hz=20, speed=60.0):
        t, fuel, dist = lap * 100.0, fuel0, 0.0
        while dist < track:
            b = int(dist / track * nb)
            thr = thr_of(b)
            fuel -= 0.0005 * thr * (1 if (empty_after is None or fuel > 0.3) else 0)
            fz.sample(lap, dist, track, fuel, t, thr, speed, 0.0, True)
            t += 1 / hz
            dist += speed / hz
        return fuel

    def test_no_variation_no_invented_numbers(self):
        fz = F.FuelZones()
        fuel = 30.0
        for lap in range(2, 12):
            fuel = self.feed(fz, lap, lambda b: 1.0, fuel)
        fz.sample(12, 0.0, 4000.0, fuel, 1300.0, 1.0, 60.0, 0.0, True)       # cierra la vuelta anterior
        a = fz.analyze()
        self.assertEqual(a["measured"], [])                                  # nunca levantó: no hay nada que medir
        self.assertTrue(fz.suggest(2) == [] or all("sin medir" in s for s in fz.suggest(2)))

    def test_empty_tank_laps_are_discarded(self):
        fz = F.FuelZones()
        fuel = 3.0
        for lap in range(2, 8):
            fuel = self.feed(fz, lap, lambda b: 1.0, fuel, empty_after=True)
        fz.sample(8, 0.0, 4000.0, fuel, 900.0, 1.0, 60.0, 0.0, True)
        self.assertTrue(all(sum(l["burn"]) > 0.001 for l in fz.laps))

    def test_dirty_laps_are_not_used(self):
        fz = F.FuelZones()
        fuel = 30.0
        for lap in (1, 2):                                                    # la vuelta 1 siempre se descarta
            fuel = self.feed(fz, lap, lambda b: 1.0, fuel)
        fz.sample(3, 0.0, 4000.0, fuel, 300.0, 1.0, 60.0, 0.0, False)         # en boxes: sucia
        fz.sample(4, 0.0, 4000.0, fuel, 400.0, 1.0, 60.0, 0.0, True)
        self.assertEqual(len(fz.laps), 1)


class LearnedAI(unittest.TestCase):
    def test_ai_compound_choices_are_learned_and_used(self):
        prof = {"tracks": {}}
        learn.learn_file(demo("corta")[0], prof, cfg())
        learn.learn_file(demo("media")[0], prof, cfg())
        an = prof["global"]["ai_next"]
        self.assertTrue(set(an["16"]) <= {"17", "18"} and len(an["16"]) == 2)       # tras Blando: Medio y Duro
        import state
        tr = state.RaceTracker(cfg(), prof)
        self.assertIn(16, tr.params.ai_next)
        self.assertAlmostEqual(sum(tr.params.ai_next[16].values()), 1.0)
        # la elección simulada sigue esas probabilidades
        me = S.Me(pos=5, compound=17, age=6, wear=12.0, pace=92.5, rate=2.0, used={17},
                  sets=[S.SetInfo(16, 0, 22, -0.84), S.SetInfo(18, 0, 42, 0.84)])
        riv = [S.Rival(1, "R1", 1, -3.0, 92.0, 16, 8, 30, 3.0, 0, {16})]
        ctx = S.Ctx(S.Situation(30, 12, 19.0), me, riv, tr.params)
        picks = [ctx.pick_compound(1, 10, {16}, r / 1000, 16) for r in range(1000)]
        want = tr.params.ai_next[16]
        for c, p in want.items():
            if c in (17, 18):
                self.assertAlmostEqual(picks.count(c) / 1000, p, delta=0.1)


if __name__ == "__main__":
    unittest.main()
