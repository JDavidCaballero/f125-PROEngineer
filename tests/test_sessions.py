"""Informe real (26 oct): tras un entreno de ~1700 s el ingeniero quedó mudo en la clasificación y la carrera, porque los
tiempos guardados (cooldowns, último aviso) eran del reloj de la sesión anterior. Estas pruebas lo impiden."""
import struct, unittest

from helpers import demo, cfg
import engineer as E
from f1_parse import read_capture


def patched(path, uid):
    """Paquetes de una captura con otro session_uid (como si fuera otra sesión del juego)."""
    for t, d in read_capture(path):
        b = bytearray(d)
        struct.pack_into("<Q", b, 7, uid)
        yield t, bytes(b)


def run(eng, packets):
    n0 = len(eng.log)
    for t, d in packets:
        eng.on_packet(t, d)
    return eng.log[n0:]


class NewSession(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.long, _ = demo("media")
        cls.short, _ = demo("sprint")
        cls.alone = run(E.Engineer(cfg(), "normal", None, out=lambda *_: None, sync=True), read_capture(cls.short))

    def new_engine(self):
        return E.Engineer(cfg(), "normal", None, out=lambda *_: None, sync=True)

    def test_a_session_after_a_long_one_speaks_exactly_as_if_it_were_alone(self):
        eng = self.new_engine()
        eng._session_summary = lambda old: None                   # aquí solo se prueba el reinicio de relojes; la memoria entre sesiones: test_raceplan.py
        run(eng, patched(self.long, 111))                         # sesión larga: el reloj llega a ~1500 s
        self.assertGreater(eng.tr.t, 600)
        after = run(eng, patched(self.short, 222))
        self.assertGreater(len(self.alone), 3)
        self.assertEqual(after, self.alone)

    def test_state_is_reset_but_pilot_preferences_survive(self):
        eng = self.new_engine()
        eng.tac.topics["combustible"] = -1
        run(eng, patched(self.long, 111))
        self.assertTrue(eng.cool)
        epoch = eng._epoch
        run(eng, patched(self.short, 222)[:1] if False else list(patched(self.short, 222))[:5])
        self.assertNotEqual(eng._epoch, epoch)
        self.assertEqual(eng.tac.topics["combustible"], -1)
        self.assertIsNone(eng.dialog.pending)
        self.assertEqual(eng.pred_log, [])

    def test_same_session_does_not_reset(self):
        eng = self.new_engine()
        run(eng, patched(self.long, 111))
        e0 = eng._epoch
        eng.on_packet(*next(iter(patched(self.long, 111))))
        self.assertEqual(eng._epoch, e0)

    def test_three_sessions_in_a_row_all_speak(self):
        eng = self.new_engine()
        for uid, path in ((1, self.long), (2, self.short), (3, self.short)):
            lines = run(eng, patched(path, uid))
            self.assertGreater(len(lines), 3, uid)


if __name__ == "__main__":
    unittest.main()
