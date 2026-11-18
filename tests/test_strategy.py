import time, unittest

from helpers import cfg
import strategy as S

P = S.Params.from_cfg(cfg())


def sets_for(cur, wear, comps=(16, 17, 18)):
    """Juegos nuevos con deltas coherentes con el modelo (así los entrega el juego: ritmo vs el montado)."""
    out = []
    for c in comps:
        if c != cur:
            d = P.off_prior[c] - P.off_prior[cur] - P.pace_loss_per_wear * wear - S.cliff(P, wear)
            out.append(S.SetInfo(c, 0, S.DEFAULT_USABLE[c], d))
    return out


def me(**k):
    d = dict(pos=5, compound=17, age=6, wear=12.0, pace=92.5, rate=2.0, life_game=32, used={17}, stops=0)
    d.update(k)
    d["sets"] = sets_for(d["compound"], d["wear"])
    return S.Me(**d)


def rivals(n=10, comp=17, wear=12, age=6, rate=2.0, used=None):
    out = []
    for i in range(1, n + 1):
        ahead = i < 5
        out.append(S.Rival(i, f"R{i}", i if ahead else i + 1, (i - 5) * 3.0 if ahead else (i - 4) * 2.5, 92.0 + i * 0.15,
                           comp, age, wear, rate, 0, set(used or {comp})))
    return out


class Basics(unittest.TestCase):
    def test_tiers_by_race_length(self):
        self.assertEqual([S.tier_of(n)[0] for n in (5, 8, 15, 20, 30, 35, 50, 70)],
                         ["sprint", "sprint", "corta", "corta", "media", "media", "larga", "larga"])

    def test_sprint_has_no_pit_plan_that_wins(self):
        rep = S.evaluate(S.Situation(5, 2, 3.6), me(wear=5, age=1, rate=2.4, compound=16, used={16}),
                         rivals(8, 16, 5, 1, 2.4), P)
        self.assertEqual(rep.tier, "sprint")
        self.assertEqual(rep.best.kind, "STAY")
        self.assertFalse(any(len(o.stops) == 2 for o in rep.options))
        pit = [o for o in rep.options if o.kind == "PIT"]
        self.assertTrue(all(o.e_pos > rep.best.e_pos + 1 for o in pit))     # parar en 3 vueltas cuesta posiciones

    def test_last_laps_stay_out(self):
        rep = S.evaluate(S.Situation(30, 28, 2.5), me(wear=60, age=20, rate=2.5, compound=16, used={16, 17}, life_game=4),
                         rivals(8, 17, used={16, 17}), P)
        self.assertEqual(rep.best.kind, "STAY")

    def test_deterministic(self):
        a = S.evaluate(S.Situation(30, 12, 19.0), me(wear=45, age=12, rate=3.5, compound=16, used={16}), rivals(8), P, seed=3)
        b = S.evaluate(S.Situation(30, 12, 19.0), me(wear=45, age=12, rate=3.5, compound=16, used={16}), rivals(8), P, seed=3)
        self.assertEqual([(o.desc, o.e_pts) for o in a.options], [(o.desc, o.e_pts) for o in b.options])


class Decisions(unittest.TestCase):
    def test_safety_car_makes_pitting_now_best(self):
        mk = lambda sc: S.evaluate(S.Situation(30, 12, 19.0, sc=sc), me(wear=45, age=12, rate=3.5, compound=16, used={16, 17},
                                   life_game=7), rivals(8, 17, 25, 12, 2.2, used={16, 17}), P)
        sc, green = mk(1), mk(0)
        self.assertEqual(sc.best.kind, "PIT")
        self.assertEqual(sc.best.stops[0][0], 12)                 # esta misma vuelta
        self.assertLess(sc.best.e_pos, green.best.e_pos - 0.5)    # el SC abarata la parada (aunque los rivales también paren)
        stay = next(o for o in sc.options if o.kind == "STAY") if any(o.kind == "STAY" for o in sc.options) else None
        if stay:
            self.assertGreater(sc.best.e_pts, stay.e_pts)

    def test_worn_tyres_need_a_stop(self):
        rep = S.evaluate(S.Situation(30, 10, 21.0), me(wear=58, age=15, rate=3.2, compound=16, used={16, 17}, life_game=3),
                         rivals(8, 17, 20, 10, 2.0, used={16, 17}), P)
        self.assertEqual(rep.best.kind, "PIT")
        self.assertLessEqual(rep.best.stops[0][0], 14)

    def test_two_compound_rule_forces_a_different_compound(self):
        rep = S.evaluate(S.Situation(30, 12, 19.0), me(compound=17, used={17}), rivals(8, 17), P)
        self.assertTrue(all(o.kind == "PIT" for o in rep.options))            # seguir fuera incumpliría la regla
        self.assertTrue(all(o.stops[0][1] != 17 for o in rep.options))

    def test_rule_off_allows_staying(self):
        p = S.Params.from_cfg(cfg())
        p.two_compound_rule = "false"
        rep = S.evaluate(S.Situation(30, 12, 19.0), me(compound=17, used={17}), rivals(8, 17), p)
        self.assertTrue(any(o.kind == "STAY" for o in rep.options))

    def test_front_wing_damage_favours_pit_repair(self):
        m = me(wear=15, age=6)
        m.dmg_pace_s = 0.9
        rep = S.evaluate(S.Situation(30, 10, 21.0), m, rivals(8, 17, used={16, 17}), P)
        self.assertEqual(rep.best.kind, "PIT")


class Evidence(unittest.TestCase):
    def test_damage_and_felt_loss_appear_in_the_reasons(self):
        m = me(wear=15, age=6)
        m.dmg_pace_s, m.felt_loss_s = 0.9, 0.3
        rep = S.evaluate(S.Situation(30, 10, 21.0), m, rivals(8, 17, used={16, 17}), P)
        pit = [o for o in rep.options if o.stops]
        self.assertTrue(all(any("El daño te cuesta" in e for e in o.evidence) for o in pit))
        self.assertTrue(all(any("Incluyo los 0.3 s" in e for e in o.evidence) for o in pit))

    def test_reasons_have_numbers_and_ranges(self):
        rep = S.evaluate(S.Situation(30, 12, 19.0), me(wear=45, age=12, rate=3.5, compound=16, used={16, 17}, life_game=7),
                         rivals(8, 17, 25, 12, 2.2, used={16, 17}), P)
        ev = rep.best.evidence
        self.assertTrue(ev[0].startswith("Tus Blando"))
        self.assertTrue(any("cuesta unos" in e for e in ev))
        self.assertTrue(ev[-1].startswith("Rango probable de llegada: P"))
        a, b = S.p_range(rep.best.cdf)
        self.assertLessEqual(a, b)
        self.assertEqual(len(rep.best.cdf), 22)
        self.assertAlmostEqual(rep.best.cdf[-1], 1.0)

    def test_per_rival_probabilities_sum_to_expected_position(self):
        sit, m, r = S.Situation(30, 12, 19.0), me(wear=30, age=8, rate=2.5, compound=16, used={16}), rivals(8)
        o = S.evaluate(sit, m, r, P).best
        beaten = sum(o.p_beat.values())
        self.assertAlmostEqual(1 + (len(r) - beaten) + sit.ignored_ahead, o.e_pos, places=6)

    def test_red_flag_makes_stopping_free(self):
        sit = S.Situation(30, 12, 19.0, red=True)
        rep = S.evaluate(sit, me(wear=45, age=12, rate=3.5, compound=16, used={16, 17}, life_game=7), rivals(8, 17, 25, 12, 2.2, used={16, 17}), P)
        self.assertEqual(rep.best.kind, "PIT")
        self.assertEqual(rep.best.stops[0][0], 12)
        self.assertTrue(any("gratis" in e for e in rep.best.evidence))

    def test_urgent_damage_allows_stop_with_few_laps_left(self):
        m = me(wear=15, age=6)
        m.dmg_pace_s = 2.0                                   # daño enorme
        rep = S.evaluate(S.Situation(30, 27, 3.5), m, rivals(8, 17, used={16, 17}), P)
        self.assertTrue(any(o.kind == "PIT" for o in rep.options))

    def test_pending_penalty_note_under_safety_car(self):
        m = me(wear=20)
        m.pen_pending = "drive-through"
        rep = S.evaluate(S.Situation(30, 12, 19.0, sc=1), m, rivals(8, 17, used={16, 17}), P)
        self.assertTrue(any("drive-through pendiente" in n for n in rep.notes))


class Modes(unittest.TestCase):
    def test_power_mean_orders_risk(self):
        gamble = [1] * 5 + [12] * 5      # 50% victoria / 50% sin puntos
        safe = [5] * 10                  # P5 seguro (10 puntos)
        u = lambda pos, g: S.utility(pos, g)
        self.assertGreater(u(gamble, 1.0), u(safe, 1.0))          # normal: mejor la apuesta (media 12.5 > 10)
        self.assertLess(u(gamble, 0.7), u(safe, 0.7))             # conservador: prefiere lo seguro
        self.assertGreater(u(gamble, 1.6), u(safe, 1.6))          # arriesgado: aún más la apuesta

    def test_wider_margin_for_risky(self):
        self.assertLess(S.MODES["conservador"]["margin"], S.MODES["normal"]["margin"])
        self.assertLess(S.MODES["normal"]["margin"], S.MODES["arriesgado"]["margin"])

    def test_eligible_follows_margin(self):
        args = (S.Situation(30, 12, 19.0), me(wear=30, age=8, rate=2.5, compound=16, used={16}), rivals(8))
        c = S.evaluate(*args, P, mode="conservador")
        a = S.evaluate(*args, P, mode="arriesgado")
        self.assertLessEqual(len(c.eligible), len(a.eligible))
        self.assertIn(c.best.id, c.eligible)


class Robustness(unittest.TestCase):
    def test_wet_and_missing_pace_are_skipped(self):
        self.assertIn("mojada", S.evaluate(S.Situation(30, 10, 21.0, wet=True), me(), rivals(), P).skipped)
        self.assertTrue(S.evaluate(S.Situation(30, 10, 21.0), me(pace=None), rivals(), P).skipped)

    def test_inconsistent_game_deltas_are_ignored(self):
        m = me(compound=16, wear=60, age=20, rate=2.5, used={16, 17})
        m.sets = [S.SetInfo(17, 0, 38, 9.0), S.SetInfo(18, 0, 42, -7.0)]      # absurdos: ±8 s por vuelta
        ctx = S.Ctx(S.Situation(30, 28, 2.5), m, rivals(4, 17), P)
        self.assertAlmostEqual(ctx.off[17], P.off_prior[17], places=6)         # se queda con el prior
        self.assertAlmostEqual(ctx.off[18], P.off_prior[18], places=6)
        ok = me(compound=16, wear=60, age=20, rate=2.5, used={16, 17})         # coherentes: se aceptan
        self.assertNotAlmostEqual(S.Ctx(S.Situation(30, 28, 2.5), ok, rivals(4, 17), P).off[17], 99.0)

    def test_long_race_within_budget(self):
        t = time.time()
        rep = S.evaluate(S.Situation(62, 3, 60.5), me(), rivals(12), P)
        self.assertLess(time.time() - t, 6.0)
        self.assertEqual(rep.tier, "larga")
        self.assertTrue(any(len(o.stops) == 2 for o in rep.options))           # en carrera larga se consideran 2 paradas

    def test_probabilities_are_consistent(self):
        rep = S.evaluate(S.Situation(30, 12, 19.0), me(), rivals(8), P)
        for o in rep.options:
            self.assertTrue(0 <= o.p_win <= o.p_podium <= o.p_points <= 1.0)
            self.assertTrue(1 <= o.e_pos <= 22)
        self.assertEqual(S.pct(0.0), 1)
        self.assertEqual(S.pct(1.0), 99)

    def test_keep_stop_is_reevaluated(self):
        sit, m, r = S.Situation(30, 12, 19.0), me(wear=30, age=8, rate=2.5, compound=16, used={16}), rivals(8)
        rep = S.evaluate(sit, m, r, P, keep=(17, 17))
        self.assertTrue(any(o.stops and o.stops[0] == (17, 17) for o in rep.options))


if __name__ == "__main__":
    unittest.main()
