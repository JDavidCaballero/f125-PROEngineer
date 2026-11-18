"""Memoria entre sesiones y plan de carrera previo (informe real del 26 oct: el entreno y la clasificación no servían para la carrera)."""
import unittest
from types import SimpleNamespace

from helpers import demo, cfg
import assistant as A
import engineer as E
import raceplan as RP
import strategy as S
from test_point1 import SessionEnd
from test_sessions import patched, run
from test_brain import Scripted, race_engine
from test_assistant import run_until

SOFT, MED, HARD = 16, 17, 18


def tables(**kw):
    base = {SOFT: {"comp": SOFT, "pace": 90.0, "laps": 5, "trend": 0.1, "wear_rate": 3.0},
            MED: {"comp": MED, "pace": 90.8, "laps": 6, "trend": 0.05, "wear_rate": 1.6},
            HARD: {"comp": HARD, "pace": 91.6, "laps": 8, "trend": 0.03, "wear_rate": 1.0}}
    base.update(kw)
    return base


class Options(unittest.TestCase):
    p = S.Params()

    def test_a_long_race_needs_two_compounds_and_respects_the_wear_limit(self):
        opts, info = RP.options(30, tables(), self.p, 22.0)
        self.assertTrue(opts)
        for o in opts:
            self.assertGreaterEqual(len({c for c, _ in o["stints"]}), 2)
            self.assertEqual(sum(n for _, n in o["stints"]), 30)
            for c, n in o["stints"]:
                self.assertLessEqual(n * info["rate"][c], self.p.wear_limit)

    def test_best_plan_is_not_slower_than_the_alternatives(self):
        opts, _ = RP.options(30, tables(), self.p, 22.0)
        self.assertEqual(opts[0]["delta"], 0)
        self.assertTrue(all(o["time"] >= opts[0]["time"] for o in opts))

    def test_stop_is_in_the_usual_zone_of_the_race(self):
        opts, _ = RP.options(30, tables(), self.p, 22.0)
        first = opts[0]["stints"][0][1]
        self.assertTrue(9 <= first <= 23, first)

    def test_a_short_race_can_be_run_without_stopping(self):
        opts, _ = RP.options(8, tables(), self.p, 22.0)
        self.assertEqual(opts[0]["stops"], 0)

    def test_if_a_compound_cannot_finish_the_race_the_plan_stops(self):
        t = tables()
        for c in (MED, HARD):
            t[c]["wear_rate"] = 4.0                                 # nada aguanta 20 vueltas al 70%
        t.pop(SOFT)
        opts, _ = RP.options(20, t, self.p, 22.0)
        self.assertTrue(all(o["stops"] >= 1 for o in opts))

    def test_wear_not_measured_is_estimated_and_said(self):
        t = tables()
        t[HARD]["wear_rate"] = None
        opts, info = RP.options(30, t, self.p, 22.0)
        self.assertIn(HARD, info["est"])
        txt = RP.describe(opts, info, 30, 22.0, "valor por defecto", t)
        self.assertIn("estimado", txt)
        self.assertIn("no incluye rivales", txt.lower())

    def test_without_two_dry_compounds_measured_a_long_race_has_no_plan(self):
        opts, _ = RP.options(30, {MED: tables()[MED]}, self.p, 22.0)
        self.assertEqual(opts, [])

    def test_deterministic(self):
        a, _ = RP.options(30, tables(), self.p, 22.0)
        b, _ = RP.options(30, tables(), self.p, 22.0)
        self.assertEqual(a, b)


class Memory(unittest.TestCase):
    def practice_engine(self):
        path, _ = demo("media")
        eng = E.Engineer(cfg(), "normal", None, out=lambda *_: None, sync=True)
        run(eng, SessionEnd.as_practice(path, 111))
        return eng

    def test_what_was_measured_in_practice_survives_the_next_session(self):
        eng = self.practice_engine()
        self.assertFalse(eng.memory.tracks)                         # aún no ha terminado
        short, _ = demo("sprint")
        run(eng, list(patched(short, 222))[:3])
        m = next(iter(eng.memory.tracks.values()))
        self.assertTrue(m["tables"])
        self.assertEqual(m["from"], ["practice"])

    def test_the_session_in_progress_counts_before_it_ends(self):
        eng = self.practice_engine()
        self.assertIsNotNone(eng.memory.view(eng.tr))

    def test_old_data_is_used_but_flagged(self):
        eng = self.practice_engine()
        short, _ = demo("sprint")
        run(eng, list(patched(short, 222))[:3])
        tid = next(iter(eng.memory.tracks))
        eng.memory.clock = lambda: eng.memory.tracks[tid]["t"] + 3 * 86400
        self.assertIsNotNone(eng.memory.get(tid))           # sin caducidad (se decidió medir antes cuánto ocupa)
        text, why = RP.build(eng, 20)
        self.assertTrue(text and text.startswith("Ojo: uso datos de hace 3 días."), (text, why))

    def test_memory_survives_closing_the_program(self):
        import tempfile
        from pathlib import Path
        eng = self.practice_engine()
        short, _ = demo("sprint")
        run(eng, list(patched(short, 222))[:3])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "memory.json"
            mem = RP.SessionMemory(path=path)
            mem.tracks = eng.memory.tracks
            mem.save()
            again = RP.SessionMemory(path=path)
            self.assertEqual(set(again.tracks), set(eng.memory.tracks))
            tid = next(iter(again.tracks))
            self.assertEqual(set(again.tracks[tid]["tables"]), set(eng.memory.tracks[tid]["tables"]))
            path.write_text("{roto", encoding="utf-8")
            self.assertEqual(RP.SessionMemory(path=path).tracks, {})

    def test_the_race_starts_with_a_plan_made_from_practice(self):
        eng = self.practice_engine()
        lines = []
        eng.out = lines.append
        short, _ = demo("corta")
        run(eng, list(patched(short, 222))[:3500])
        plan = [l for l in lines if "Plan previo para" in l]
        self.assertEqual(len(plan), 1, lines[:6])
        self.assertIn("con lo medido en entrenos", plan[0])

    def test_no_practice_gives_a_plan_marked_as_estimated(self):
        lines = []
        eng = E.Engineer(cfg(), "normal", None, out=lines.append, sync=True)
        short, _ = demo("corta")
        run(eng, list(patched(short, 222))[:3500])
        self.assertFalse([l for l in lines if "Plan previo" in l])            # no dice «con lo medido» sin haber medido nada
        plan = [l for l in lines if "Plan estimado" in l]
        self.assertEqual(len(plan), 1, lines[:5])
        self.assertIn("sin datos de entrenos", plan[0])
        self.assertIn("sal con", plan[0])


class Asking(unittest.TestCase):
    def test_race_laps_phrases_are_understood(self):
        tr = race_engine(Scripted()).tr
        for s in ("carrera de 18 vueltas", "la carrera es de 12 vueltas", "son 20 vueltas de carrera"):
            r = A.parse_rules(tr, s)
            self.assertEqual((r.name, r.topic), ("plan_carrera", "".join(c for c in s if c.isdigit())), s)

    def test_telling_the_race_length_is_remembered_and_answered(self):
        eng = TestPractice.engine()
        out = eng.ask("carrera de 18 vueltas")
        self.assertIn("Anotado: carrera de 18 vueltas", out)
        self.assertEqual(next(iter(eng.memory.tracks.values()))["race_laps"], 18)

    def test_strategy_question_in_practice_gives_a_plan_or_says_what_is_missing(self):
        eng = TestPractice.engine()
        eng.ask("carrera de 20 vueltas")
        out = eng.ask("¿Qué estrategia planeas para la carrera?")
        self.assertTrue("Plan previo para 20 vueltas" in out or "Aún no puedo armar" in out, out)

    def test_a_statement_about_the_race_length_is_not_just_acknowledged(self):
        eng = race_engine(Scripted(route=lambda ctx: {"tools": [], "say": "Entendido."}))
        plan = eng.assistant.prepare("La carrera es de 18 vueltas")
        self.assertEqual(plan.intents[0].name, "plan_carrera")

    def test_absurd_distance_is_rejected(self):
        eng = TestPractice.engine()
        self.assertIn("no me cuadra", eng.ask("carrera de 95 vueltas"))


class TestPractice:
    @staticmethod
    def engine():
        path, _ = demo("media")
        eng = E.Engineer(cfg(), "normal", None, out=lambda *_: None, sync=True)
        run(eng, SessionEnd.as_practice(path, 111))
        return eng


if __name__ == "__main__":
    unittest.main()
