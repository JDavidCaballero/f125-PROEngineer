"""Avisos dinámicos y con presupuesto (informe real del 26 oct: 125 avisos en 18 vueltas; el mismo neumático «sobrecalentado» cada
vuelta, el mismo duelo avisado 27 veces, previsión de lluvia cada 7 minutos aunque nunca llovió)."""
import unittest
from types import SimpleNamespace

from helpers import cfg, demo
import engineer as E
import tactics as T
from test_assistant import run_until
from f1_parse import read_capture

HOT = "tyre_surface_hot"


def tac():
    t = T.Tactics(cfg())
    t.th = {**t.th, HOT: 135}
    return t


def stub(lap=3, age=3):
    tr = SimpleNamespace(cur_lap=lap, traces=SimpleNamespace(last_timed=lambda c: None, best={}), player=0, ses={"track_length_m": 5000})
    me = {"lap_distance": 1300.0, "lap": lap}
    sa = {"tyre_age_laps": age}
    return tr, me, sa


class TyreTemperature(unittest.TestCase):
    def test_same_overheating_every_lap_is_said_once(self):
        t = tac()
        said = []
        for lap in range(3, 12):
            tr, me, sa = stub(lap, lap)
            a = t._tyre_hot(tr, me, sa, "tyre_surface_temp", 1, 135, 135, "superficie")
            if a:
                said.append(a.text)
        self.assertEqual(len(said), 1, said)
        self.assertIn(T.WHEELS[1], said[0])

    def test_it_speaks_again_only_if_it_keeps_rising(self):
        t = tac()
        out = []
        for lap, v in ((3, 135), (4, 137), (5, 140), (6, 144), (7, 144), (8, 152)):
            tr, me, sa = stub(lap, lap)
            a = t._tyre_hot(tr, me, sa, "tyre_surface_temp", 1, v, 135, "superficie")
            out.append(a.text if a else None)
        self.assertEqual([o is not None for o in out], [True, False, False, True, False, True])
        self.assertIn("sigue subiendo", out[3])

    def test_new_tyres_rearm_it(self):
        t = tac()
        tr, me, sa = stub(5, 5)
        self.assertIsNotNone(t._tyre_hot(tr, me, sa, "tyre_surface_temp", 0, 140, 135, "superficie"))
        tr, me, sa = stub(6, 6)
        self.assertIsNone(t._tyre_hot(tr, me, sa, "tyre_surface_temp", 0, 140, 135, "superficie"))
        tr, me, sa = stub(7, 0)                                 # box: neumáticos nuevos
        self.assertIsNone(t._tyre_hot(tr, me, sa, "tyre_surface_temp", 0, 100, 135, "superficie"))
        tr, me, sa = stub(8, 1)
        self.assertIsNotNone(t._tyre_hot(tr, me, sa, "tyre_surface_temp", 0, 141, 135, "superficie"))

    def test_below_the_threshold_nothing(self):
        tr, me, sa = stub()
        self.assertIsNone(tac()._tyre_hot(tr, me, sa, "tyre_surface_temp", 0, 120, 135, "superficie"))


class Rain(unittest.TestCase):
    def ses(self, pct, minutes=10, weather=0):
        return SimpleNamespace(ses={"weather": weather, "forecast": [{"minutes": minutes, "rain_pct": pct}]})

    def alert(self, t, pct, minutes=10):
        return t._weather(self.ses(pct, minutes), {"visual_compound": 16})

    def test_forecast_is_said_once_and_again_only_if_it_worsens_or_nears(self):
        t = tac()
        self.assertIsNotNone(self.alert(t, 62))
        self.assertIsNone(self.alert(t, 62))
        self.assertIsNone(self.alert(t, 66))
        self.assertIsNotNone(self.alert(t, 80))                  # +18 puntos
        self.assertIsNone(self.alert(t, 80, 8))
        self.assertIsNotNone(self.alert(t, 80, 4))               # ya casi encima

    def test_a_cleared_forecast_rearms_the_alert(self):
        t = tac()
        self.assertIsNotNone(self.alert(t, 70))
        self.assertIsNone(self.alert(t, 10))
        self.assertIsNotNone(self.alert(t, 70))


class WeatherChange(unittest.TestCase):
    def test_cloud_changes_are_not_news_but_rain_is(self):
        t = tac()
        tr = SimpleNamespace()
        self.assertEqual(t.on_signals(tr, [("weather", 1)]), [])
        self.assertEqual(t.on_signals(tr, [("weather", 2)]), [])
        out = t.on_signals(tr, [("weather", 3)])
        self.assertTrue(out and "Cambia el tiempo" in out[0].text)
        out = t.on_signals(tr, [("weather", 1)])                 # deja de llover: sí interesa
        self.assertTrue(out)


class Sectors(unittest.TestCase):
    def run_sector(self, race, topic=0, driver_status=1):
        t = tac()
        t.topics["sectores"] = topic
        hist = {0: {"laps": [{"ms": 70000, "valid": True, "s1": 20000, "s2": 25000}]}}
        tr = SimpleNamespace(hist=hist, player=0, sc=0, sta_all=[{"fia_flag": 0}], is_race=lambda: race)
        me = {"sector": 1, "sector1_ms": 20600, "pit_status": 0, "driver_status": driver_status, "lap_invalid": False}
        t.last_sector = 0
        return t._sectors(tr, me)

    def test_in_a_race_sector_losses_are_silent_by_default(self):
        self.assertEqual(self.run_sector(True), [])

    def test_in_a_race_they_come_back_if_asked(self):
        self.assertTrue(self.run_sector(True, topic=1))

    def test_in_practice_flying_laps_still_report(self):
        self.assertTrue(self.run_sector(False))


class Duels(unittest.TestCase):
    """Un duelo se avisa una vez, y de nuevo a las 3 vueltas o cuando por fin le abren el DRS."""

    def tick(self, t, lap, behind_car, gap, drs=False):
        tr = SimpleNamespace(
            name=lambda i: f"P{i}", neighbours=lambda: (None, (behind_car, gap)), is_race=lambda: True, player=0,
            lap_all=[{"position": 3}] + [None] * 21,
            sta_all=[{"drs_allowed": False, "ers_store_j": 4e6, "ers_mode": 0, "fuel_mix": 0}] + [{"drs_allowed": drs}] * 21,
            dmg_all=[None] * 22, fuel_margin=lambda: None, laps_left=lambda: 20, ses={})
        me = {"lap": lap, "position": 3}
        out = t._race(tr, me, tr.sta_all[0], t.th)
        return [a.text for a in out if "defiende" in a.text]

    def test_one_alert_for_the_same_follower_over_several_laps(self):
        t = tac()
        n = sum(len(self.tick(t, lap, 5, 0.5)) for lap in range(2, 5))
        self.assertEqual(n, 1)

    def test_again_after_three_laps(self):
        t = tac()
        n = sum(len(self.tick(t, lap, 5, 0.5)) for lap in range(2, 7))
        self.assertEqual(n, 2)

    def test_drs_opening_for_the_follower_is_worth_saying(self):
        t = tac()
        self.assertEqual(len(self.tick(t, 2, 5, 0.5)), 1)
        self.assertEqual(len(self.tick(t, 3, 5, 0.4, drs=True)), 1)
        self.assertEqual(len(self.tick(t, 3, 5, 0.4, drs=True)), 0)

    def test_a_new_follower_is_a_new_duel(self):
        t = tac()
        self.assertEqual(len(self.tick(t, 2, 5, 0.5)), 1)
        self.assertEqual(len(self.tick(t, 3, 6, 0.5)), 1)

    def test_when_he_drops_away_and_comes_back_it_is_a_new_duel(self):
        t = tac()
        self.assertEqual(len(self.tick(t, 2, 5, 0.5)), 1)
        self.tick(t, 3, 5, 3.0)
        self.tick(t, 4, 5, 3.0)
        self.assertEqual(len(self.tick(t, 4, 5, 0.6)), 1)


class Budget(unittest.TestCase):
    def engine(self):
        eng, out, it = run_until("corta", 100)
        eng.tr.cur_lap = 5
        return eng

    def speak(self, eng, n, key_prefix, prio=2, force=False):
        said = 0
        for k in range(n):
            eng.tr.t += 5
            said += bool(eng.say(f"aviso {key_prefix}{k}", prio, f"{key_prefix}{k}", 0, force=force))
        return said

    def test_chatter_is_capped_per_lap(self):
        eng = self.engine()
        self.assertEqual(self.speak(eng, 8, "defend"), eng.budget[2])

    def test_a_new_lap_resets_the_budget(self):
        eng = self.engine()
        self.speak(eng, 8, "defend")
        eng.tr.cur_lap += 1
        self.assertEqual(self.speak(eng, 8, "attack"), eng.budget[2])

    def test_safety_alerts_are_never_cut(self):
        eng = self.engine()
        self.speak(eng, 8, "defend")
        self.assertEqual(self.speak(eng, 6, "dmg_wing"), 6)       # daños: no entran en el presupuesto
        self.assertEqual(self.speak(eng, 3, "blue", prio=2), 3)

    def test_critical_and_forced_are_never_cut(self):
        eng = self.engine()
        self.speak(eng, 8, "defend")
        self.assertEqual(self.speak(eng, 3, "defend", prio=1), 3)
        self.assertEqual(self.speak(eng, 3, "attack", force=True), 3)

    def test_a_dropped_alert_does_not_come_back_late(self):
        eng = self.engine()
        self.speak(eng, 8, "defend")
        eng.tr.cur_lap += 1
        eng.tr.t += 5
        self.assertFalse(eng.say("aviso defend7", 2, "defend7", 60))         # ya contó como dicho: su enfriamiento sigue vigente

    def test_dropped_alerts_are_logged_once_per_key_and_lap(self):
        eng = self.engine()
        eng.log_events = []
        eng._log = lambda r: eng.log_events.append(r)
        self.speak(eng, 8, "defend")
        keys = [r["key"] for r in eng.log_events if r["type"] == "budget"]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(keys)

    def test_budget_can_be_disabled_in_config(self):
        c = cfg()
        c["engineer"]["budget_prio2"] = 0
        eng = E.Engineer(c, "normal", None, out=lambda *_: None, sync=True)
        eng.tr.cur_lap = 5
        self.assertEqual(self.speak(eng, 8, "defend"), 8)


class LapAnalysisInPractice(unittest.TestCase):
    def laps(self, session_type):
        from test_quali import make_engine, run_lap
        eng, out = make_engine(session_type)
        events = []
        eng._log = events.append
        run_lap(eng, 0, 2)
        run_lap(eng, 0, 3, slow={2: 10})                       # +0.9%: vuelta de ataque
        run_lap(eng, 0, 4, slow={2: 30})                       # +2.1%: tanda larga / vuelta lenta
        return events

    def test_in_practice_a_slow_lap_is_logged_but_not_analysed_out_loud(self):
        ev = self.laps(1)
        self.assertEqual(len([e for e in ev if e["type"] == "lap_report"]), 2)
        self.assertEqual(len([e for e in ev if e["type"] == "lap_report_skipped"]), 1)

    def test_in_qualifying_every_flying_lap_is_analysed(self):
        ev = self.laps(5)
        self.assertEqual(len([e for e in ev if e["type"] == "lap_report"]), 3)


if __name__ == "__main__":
    unittest.main()
