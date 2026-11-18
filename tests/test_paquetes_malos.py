"""Un paquete truncado, corrupto o de otro formato no puede tumbar el motor (antes lanzaba struct.error y, en vivo, el hilo de
telemetría moría en silencio: 392 excepciones en 3593 paquetes con 19 % de paquetes dañados)."""
import random
import unittest

import helpers  # noqa: F401
import engineer as E
from helpers import cfg, demo
from voice import read_capture


class PaquetesMalos(unittest.TestCase):
    def test_damaged_packets_are_counted_and_the_good_ones_still_work(self):
        random.seed(7)
        path, _ = demo("corta")
        eng = E.Engineer(cfg(False), "normal", None, out=lambda x: None, sync=True)
        for t, d in list(read_capture(path))[:2500]:
            eng.on_packet(t, d)                                       # no debe lanzar
            r = random.random()
            if r < 0.10:
                eng.on_packet(t, d[:random.randint(29, len(d) - 1)])    # truncado (pasa el filtro de 29 bytes de feed_live)
            elif r < 0.15:
                eng.on_packet(t, bytes(random.randrange(256) for _ in range(random.randint(29, 200))))
        self.assertGreater(eng.bad_packets, 0)
        self.assertIsNotNone(eng.last_bad)
        self.assertTrue(eng.tr.summary(), "los paquetes buenos dejaron de procesarse")


if __name__ == "__main__":
    unittest.main()
