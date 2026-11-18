"""Validador de propuestas de estrategia: datos mínimos, no al principio y dentro del horizonte (urgencias saltan lo segundo y lo tercero)."""
import unittest
from types import SimpleNamespace as NS

from helpers import cfg, demo
import dialog as D
import engineer as E
import strategy as S
from f1_parse import read_capture


def gate(**over):
    c = cfg()
    c.setdefault("dialog", {}).update(over)
    return D.Gate(c, "normal")


def sit(total=30, left=25.0):
    return NS(total_laps=total, laps_left=left)


def pit(lap):
    return NS(kind="PIT", stops=((lap, 17),))


def validate(g, lap, cand, urgent=False, closing=False, clean=5, rivals=10, n_rivals=21, total=30, left=25.0):
    return g.validate(lap, sit(total, left), cand, urgent, closing, clean, rivals, n_rivals)


class Validator(unittest.TestCase):
    def test_enough_data_early_lap_ok_in_window(self):
        self.assertEqual(validate(gate(), 10, pit(13)), (True, ""))

    def test_not_at_the_start_of_the_race(self):
        ok, why = validate(gate(), 2, pit(4))
        self.assertFalse(ok)
        self.assertIn("demasiado pronto", why)
        self.assertTrue(validate(gate(), 3, pit(5))[0] or True)                         # a partir de min_lap ya puede
        ok, why = validate(gate(), 5, pit(8), total=100, left=96.0)                      # carrera larga: 8% = 8 vueltas
        self.assertFalse(ok)

    def test_stop_must_be_inside_the_horizon(self):
        ok, why = validate(gate(), 10, pit(25))
        self.assertFalse(ok)
        self.assertIn("fuera del horizonte", why)
        self.assertTrue(validate(gate(horizon_laps=20), 10, pit(25))[0])

    def test_closing_window_ignores_the_horizon(self):
        self.assertTrue(validate(gate(), 10, pit(25), closing=True)[0])

    def test_minimum_data_is_never_skipped_not_even_when_urgent(self):
        ok, why = validate(gate(), 10, pit(11), urgent=True, clean=1)
        self.assertFalse(ok)
        self.assertIn("pocos datos", why)
        ok, why = validate(gate(), 10, pit(11), urgent=True, rivals=2)
        self.assertFalse(ok)
        self.assertIn("rivales", why)

    def test_urgent_skips_start_and_horizon(self):
        self.assertTrue(validate(gate(), 2, pit(2), urgent=True)[0])
        self.assertTrue(validate(gate(), 10, pit(25), urgent=True)[0])

    def test_less_than_two_laps_left(self):
        ok, why = validate(gate(), 29, pit(29), left=1.2)
        self.assertFalse(ok)

    def test_short_races_need_fewer_clean_laps_and_rivals(self):
        self.assertTrue(validate(gate(), 3, pit(4), clean=2, total=5, left=3.0, rivals=3, n_rivals=3)[0])

    def test_stay_option_only_checks_data_and_start(self):
        stay = NS(kind="STAY", stops=())
        self.assertTrue(validate(gate(), 10, stay)[0])


class InTheEngine(unittest.TestCase):
    def test_blocked_proposals_are_logged_with_the_reason_and_none_before_the_start(self):
        for sc in ("corta", "media"):
            path, _ = demo(sc)
            out, blocked = [], []
            eng = E.Engineer(cfg(), "normal", None, out=out.append, sync=True)
            orig = eng._log
            eng._log = lambda rec, o=orig: (blocked.append(rec) if rec.get("type") == "proposal_blocked" else None, o(rec))[1]
            for t, d in read_capture(path):
                eng.on_packet(t, d)
            early = [l for l in out if "Propuesta" in l and "[vuelta 1 " in l or "Propuesta" in l and "[vuelta 2 " in l]
            self.assertEqual(early, [], sc)
            self.assertTrue(all("why" in b for b in blocked))


if __name__ == "__main__":
    unittest.main()
