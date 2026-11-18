"""Sesgo de la simulación: en una parrilla de coches idénticos no se pueden ganar posiciones solo por optimizar tu parada
(antes, desde P10 la estrategia prometía P8.4 y desde P5, P4.2: el «sales P1» de las carreras reales)."""
import unittest

import helpers  # noqa: F401  (añade la raíz del proyecto al path)
import strategy as S


def field(total, mypos, sc_setting=0, gap=1.5, rate=2.6, lap=3):
    age = lap - 1
    w = rate * age
    sets = [S.SetInfo(17, 0, 38, 0.8), S.SetInfo(18, 0, 42, 1.6), S.SetInfo(16, 0, 22, 0.0)]
    me = S.Me(pos=mypos, compound=16, age=age, wear=w, pace=92.0, rate=rate, sets=sets, used={16})
    riv = [S.Rival(k, f"R{k}", k, (k - mypos) * gap, 92.0, 16, age, w, rate, 0, {16}) for k in range(1, 23) if k != mypos]
    sit = S.Situation(total_laps=total, lap=lap, laps_left=total - lap + 1, sc_setting=sc_setting)
    return sit, me, riv


class SymmetricField(unittest.TestCase):
    def test_no_free_positions(self):
        for total, pos in ((30, 10), (30, 5), (15, 10)):
            sit, me, riv = field(total, pos)
            rep = S.evaluate(sit, me, riv, S.Params(), nsim=200, budget_s=20)
            self.assertLessEqual(abs(rep.best.e_pos - pos), 0.8, f"{total} vueltas desde P{pos}: P{rep.best.e_pos:.2f}")

    def test_rivals_plan_a_sensible_stop(self):
        sit, me, riv = field(30, 10)
        ctx = S.Ctx(sit, me, riv, S.Params())
        for plan in ctx.ai_plan[1:]:
            self.assertIsNotNone(plan)                   # con la regla de dos compuestos todos tienen que parar
            step, comp = plan
            self.assertNotEqual(comp, 16)                # ya usaron el blando
            self.assertTrue(5 <= sit.lap + step <= 26, plan)


if __name__ == "__main__":
    unittest.main()
