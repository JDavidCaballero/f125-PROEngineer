"""Informe real del sprint de Brasil (11 nov): el plan largo retrasaba 14 s las alertas, el resultado nunca se dijo, y los adelantamientos y
los avisos de defensa se repetían. Cada prueba cubre un hecho MEDIDO en esa captura; son rápidas y no usan datos externos."""
import threading
import time
import unittest
from types import SimpleNamespace

import helpers  # noqa: F401
import raceplan
import speaker as SP
import tactics as T
from helpers import cfg
from test_voice import FakePopen
import engineer as E


class ShortPlan(unittest.TestCase):
    """El plan previo eran 51 palabras ≈ 19 s de voz en la vuelta 1."""

    def test_a_sprint_without_stops_is_one_short_sentence(self):
        plan = {"stints": [(16, 5)], "estimated": True}
        text = raceplan.short(plan, "Plan estimado para 5 vueltas, ... larguísimo ...", 5)
        self.assertEqual(text, "Plan estimado para 5 vueltas, sin datos de entrenos: sal con Blando y no pares.")
        self.assertLess(len(text.split()), 16)       # eran 51 palabras

    def test_with_a_stop_it_still_says_when_and_with_what(self):
        text = raceplan.short({"stints": [(17, 10), (18, 8)]}, "x", 18)
        self.assertEqual(text, "Plan previo para 18 vueltas con lo medido en entrenos: sal con Medio, para en la vuelta 10 y monta Duro.")

    def test_old_data_warning_is_kept_and_a_malformed_plan_falls_back_to_the_full_text(self):
        self.assertTrue(raceplan.short({"stints": [(16, 5)]}, "Ojo: uso datos de hace 3 días. Plan previo ...", 5).startswith("Ojo: uso datos de hace 3 días."))
        self.assertEqual(raceplan.short({}, "texto completo", 5), "texto completo")

    def test_the_race_start_really_says_the_short_plan(self):
        """El cableado: engineer._intro -> say(preplan). Antes decía el texto completo (51 palabras)."""
        from test_assistant import run_until
        eng, out, _ = run_until("corta", 120)
        plan = [l.split("] ", 1)[1] for l in out if "] Plan" in l]
        self.assertTrue(plan, out[:6])
        self.assertLess(len(plan[0].split()), 28)                    # una frase (con una parada cabe en ~20 palabras); el completo pasaba de 50
        self.assertNotIn("Alternativa", plan[0])


class StaleAlertsAreDropped(unittest.TestCase):
    """«Contacto con Tsunoda: revisa los daños» sonó 14.9 s tarde, detrás del plan largo. Sin esperas: los avisos se inyectan ya viejos."""

    def speak(self, max_age=8.0):
        FakePopen.log = []
        gate = threading.Event()

        class Slow(FakePopen):
            def __init__(self, cmd, dur=0):
                super().__init__(cmd, dur)
                self.first = len(FakePopen.log) == 1

            def poll(self):
                return None if (self.first and not gate.is_set()) else 0
        sp = SP.Speaker(voice="Mónica", popen=lambda cmd: Slow(cmd), max_age_s=max_age)
        sp.say("bloqueo", 2)
        deadline = time.time() + 2
        while len(FakePopen.log) < 1 and time.time() < deadline:
            time.sleep(0.005)                                # el altavoz ya está ocupado con el primero
        with sp.cv:
            for prio, text, expires in ((2, "viejo informativo", True), (1, "viejo crítico", True), (2, "viejo propuesta", False), (2, "reciente", True)):
                it = sp._item(prio, text, expires)
                if text.startswith("viejo"):
                    it.t -= 60                               # lleva un minuto esperando
                sp.q.append(it)
        gate.set()
        sp.wait(3)
        return FakePopen.log[:], list(sp.dropped)

    def test_old_informative_alerts_are_dropped_everything_else_sounds(self):
        said, dropped = self.speak()
        self.assertNotIn("viejo informativo", said)
        self.assertIn(("viejo informativo", "caducado"), dropped)
        self.assertIn("viejo crítico", said)                 # prio 1 nunca caduca
        self.assertIn("viejo propuesta", said)               # una propuesta o respuesta nunca caduca (si no, se daría por oída sin serlo)
        self.assertIn("reciente", said)

    def test_without_expiry_the_old_alert_would_sound_late(self):
        said, _ = self.speak(max_age=0)
        self.assertIn("viejo informativo", said)

    def test_a_forced_engine_message_is_not_allowed_to_expire(self):
        """El cableado real: engine.say(force=True) -> VoiceSession -> Speaker.say(expires=False)."""
        import voice as V
        calls = []
        sess = V.VoiceSession.__new__(V.VoiceSession)
        sess.speaker = SimpleNamespace(say=lambda *a, **k: calls.append((a, k)))
        sess.log = lambda rec: None
        sess._track_proposal = lambda: None
        sess.eng = SimpleNamespace(say_forced=True)
        sess._on_engine_say("¿Entro a boxes?", 2)
        sess.eng.say_forced = False
        sess._on_engine_say("Contacto con X", 2)
        self.assertEqual(calls, [(("¿Entro a boxes?", 2), {"expires": False}), (("Contacto con X", 2), {})])


class Overtakes(unittest.TestCase):
    """Vuelta 1: «Verstappen te adelantó» tres veces en 12 s. Pero un cambio de sentido SIEMPRE se dice."""

    def alerts(self, seq):
        t = T.Tactics(cfg())
        out = []
        for now, way in seq:
            tr = SimpleNamespace(player=0, t=now, name=lambda i: f"P{i}", ses={})
            e = {"code": "OVTK", "car": 1, "overtaken": 0} if way == "by" else {"code": "OVTK", "car": 0, "overtaken": 1}
            out += [a.text for a in t._event(tr, e)]
        return out

    def test_same_direction_repeats_are_collapsed(self):
        self.assertEqual(self.alerts([(14.9, "by"), (21.0, "by"), (26.6, "by")]), ["P1 te adelantó."])

    def test_a_change_of_direction_is_always_said(self):
        self.assertEqual(self.alerts([(103.5, "to"), (111.4, "by"), (120.8, "to")]),
                         ["Adelantaste a P1.", "P1 te adelantó.", "Adelantaste a P1."])      # antes el último desaparecía: «¿sigue delante?»

    def test_after_20_seconds_the_same_direction_is_news_again(self):
        self.assertEqual(len(self.alerts([(10, "by"), (40, "by")])), 2)


class Defence(unittest.TestCase):
    """«defiende» y «defiende con DRS» con 1.5 s de diferencia."""

    def tick(self, t, now, lap, drs):
        tr = SimpleNamespace(
            name=lambda i: f"P{i}", neighbours=lambda: (None, (5, 0.3)), is_race=lambda: True, player=0, t=now,
            lap_all=[{"position": 3}] + [None] * 21,
            sta_all=[{"drs_allowed": False, "ers_store_j": 4e6, "ers_mode": 0, "fuel_mix": 0}] + [{"drs_allowed": drs}] * 21,
            dmg_all=[None] * 22, fuel_margin=lambda: None, laps_left=lambda: 20, ses={})
        out = t._race(tr, {"lap": lap, "position": 3}, tr.sta_all[0], t.th)
        return [a.text for a in out if "defiende" in a.text]

    def test_drs_variant_right_after_the_first_warning_is_not_repeated(self):
        t = T.Tactics(cfg())
        self.assertEqual(len(self.tick(t, 313.3, 5, False)), 1)
        self.assertEqual(self.tick(t, 314.8, 5, True), [])

    def test_drs_opening_much_later_is_still_worth_saying(self):
        t = T.Tactics(cfg())
        self.tick(t, 100.0, 5, False)
        self.assertEqual(len(self.tick(t, 112.0, 5, True)), 1)


class FinishResult(unittest.TestCase):
    """El juego no mandó la clasificación final en el sprint (la captura no tiene ni un paquete tipo 8): el resultado sale al cruzar la meta."""

    def test_says_position_and_best_lap(self):
        t = T.Tactics(cfg())
        tr = SimpleNamespace(is_race=lambda: True, player=0, t=367.2, hist={0: {"laps": [{"ms": 73027}, {"ms": 71360}, {"ms": 0}]}})
        out = t.on_signals(tr, [("finish", 4)])                      # por la ruta real: la señal de state.py llega aquí
        self.assertEqual([a.text for a in out], ["Terminas P4. Tu mejor vuelta: 1:11.360."])

    def test_not_in_practice_and_not_without_position(self):
        t = T.Tactics(cfg())
        self.assertEqual(t._finish(SimpleNamespace(is_race=lambda: False, player=0, hist={}), 4), [])
        self.assertEqual(t._finish(SimpleNamespace(is_race=lambda: True, player=0, hist={}), 0), [])

    def test_state_emits_finish_once_per_session_when_the_player_status_becomes_finished(self):
        import state as S
        from test_assistant import run_until
        eng, _, _ = run_until("corta", 120)
        tr = eng.tr
        me_idx = tr.player
        base = dict(tr.lap_all[tr.player])
        orig = S.lap_data
        self.addCleanup(setattr, S, "lap_data", orig)
        cur = {"status": 2}
        S.lap_data = lambda d, i: dict(base, result_status=cur["status"], position=4) if i == tr.player else None
        sig = []
        tr._lap(b"", sig)
        self.assertEqual([x for x in sig if x[0] == "finish"], [])               # sigue corriendo: nada
        cur["status"] = 3
        tr._lap(b"", sig)
        tr._lap(b"", sig)
        self.assertEqual([x for x in sig if x[0] == "finish"], [("finish", 4)])  # una sola vez, con la posición
        tr.reset()
        sig2 = []
        tr.player = me_idx
        tr._lap(b"", sig2)
        self.assertEqual([x for x in sig2 if x[0] == "finish"], [("finish", 4)])  # sesión nueva: vuelve a poder avisar


class LinkNoise(unittest.TestCase):
    def test_the_long_explanation_is_said_once_and_status_is_not_repeated_when_nothing_changes(self):
        now = [0.0]
        w = E.LinkWatch(20777, None, clock=lambda: now[0], no_data_s=10, every_s=60)
        now[0] = 11
        first = w.tick()
        now[0] = 41.5
        second = w.tick()
        self.assertIn("Revisa en el juego", first)
        self.assertNotIn("Revisa en el juego", second)               # antes: 14 veces el mismo párrafo de 270 caracteres
        self.assertLess(len(second), 90)
        now[0] = 95
        w.packet(); w.packet()
        now[0] = 154
        w.packet()
        now[0] = 156
        self.assertTrue(w.tick().startswith("[datos del juego: 3 paquetes"))
        now[0] = 170
        self.assertIn("Han dejado de llegar", w.tick())              # se pausa el juego
        now[0] = 240
        self.assertIsNone(w.tick())                                  # el contador no cambia (menús): la línea de estado ya no se repite


if __name__ == "__main__":
    unittest.main()
