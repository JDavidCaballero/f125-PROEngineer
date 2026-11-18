"""Avisos por episodio, no por reloj: en la demo «media» (30 vueltas con lluvia) antes salían «Pista mojada» x7, el resumen de vuelta
en casi todas las vueltas y «modo adelantamiento» al mismo rival x6. Medido con `python3 repite.py media`."""
import collections
import unittest

import helpers  # noqa: F401
import engineer as E
from helpers import cfg, demo
from voice import read_capture


def say_log(name):
    path, _ = demo(name)
    eng = E.Engineer(cfg(False), "normal", None, out=lambda x: None, sync=True)
    msgs = []
    eng.on_say.append(lambda text, prio: msgs.append((eng.tr.cur_lap or 0, text)))
    for t, d in read_capture(path):
        eng.on_packet(t, d)
    return msgs


class Episodios(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.msgs = say_log("media")
        cls.laps = max(l for l, _ in cls.msgs)

    def count(self, start):
        return sum(1 for _, t in self.msgs if t.startswith(start))

    def test_wet_track_is_announced_once_per_episode(self):
        self.assertLessEqual(self.count("Pista mojada"), 2)

    def test_lap_summary_is_not_every_lap(self):
        self.assertLessEqual(self.count("Vuelta "), self.laps // 3 + 4)       # cada 3 vueltas + las que son tu mejor

    def test_attack_is_not_repeated_for_the_same_rival_within_a_duel(self):
        per_rival = collections.Counter(t.split(":")[1].split(" está")[0] for _, t in self.msgs if t.startswith("Usa el modo adelantamiento"))
        self.assertLessEqual(max(per_rival.values() or [0]), 3, per_rival)


if __name__ == "__main__":
    unittest.main()
