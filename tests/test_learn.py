import json, tempfile, unittest
from pathlib import Path

from helpers import demo, cfg
import learn, make_demo, state


class Learn(unittest.TestCase):
    def test_pit_loss_and_wear_rates_recovered_from_demo(self):
        # el demo usa 22 s de parada y desgaste Blando ~4 %/vuelta (con jitter por coche): learn.py debe recuperarlos
        path, _ = demo("corta")
        prof = {"tracks": {}}
        s = learn.learn_file(path, prof, cfg())
        self.assertEqual(s["races"], 1)
        tk = prof["tracks"]["17"]
        self.assertTrue(18 <= tk["pit_loss_s"] <= 26, tk["pit_loss_s"])
        self.assertGreaterEqual(tk["pit_loss_s_n"], 5)
        w = {int(k): v["rate"] for k, v in tk["wear"].items()}
        self.assertGreater(w[16], w[17])                       # el Blando se gasta más que el Medio
        self.assertTrue(2.5 <= w[16] <= 4.6, w)

    def test_same_race_is_not_counted_twice(self):
        path, _ = demo("sprint")
        prof = {"tracks": {}}
        learn.learn_file(path, prof, cfg())
        n1 = prof["global"]["races"]
        s = learn.learn_file(path, prof, cfg())
        self.assertEqual((s["races"], s["skipped"]), (0, 1))
        self.assertEqual(prof["global"]["races"], n1)
        learn.learn_file(path, prof, cfg(), force=True)
        self.assertEqual(prof["global"]["races"], n1 + 1)

    def test_profile_roundtrip_and_use_by_tracker(self):
        path, _ = demo("corta")
        prof = {"tracks": {}}
        learn.learn_file(path, prof, cfg())
        with tempfile.TemporaryDirectory() as d:
            c = cfg()
            c["paths"]["profile"] = str(Path(d) / "p.json")
            import config
            config.save_profile(prof, c)
            back = config.load_profile(c)
            self.assertEqual(back["tracks"]["17"]["pit_loss_s"], prof["tracks"]["17"]["pit_loss_s"])
        tr = state.RaceTracker(cfg(), prof)
        tr.ses = {"track_id": 17}
        loss, src = tr.pit_loss()
        self.assertEqual(src, "perfil aprendido")
        self.assertAlmostEqual(loss, prof["tracks"]["17"]["pit_loss_s"])


class Demo(unittest.TestCase):
    def test_generator_is_deterministic(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d) / "a.f1raw", Path(d) / "b.f1raw"
            make_demo.generate("sprint", a, seed=5)
            make_demo.generate("sprint", b, seed=5)
            self.assertEqual(a.read_bytes(), b.read_bytes())
            make_demo.generate("sprint", b, seed=6)
            self.assertNotEqual(a.read_bytes(), b.read_bytes())

    def test_pit_loss_excludes_safety_car_laps(self):
        # la demo 'corta' tiene SC en las vueltas 6-7: las paradas bajo SC no deben inflar el coste medido
        path, _ = demo("corta")
        tr = state.RaceTracker(cfg())
        from f1_parse import read_capture
        for t, d in read_capture(path):
            tr.on_packet(t, d)
        samples = state.measure_pit_loss(tr.hist)
        self.assertTrue(samples)
        self.assertTrue(all(15 < s < 30 for s in samples), samples)


if __name__ == "__main__":
    unittest.main()
