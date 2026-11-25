"""Interpretación por el LLM: frases cortas sin signos, «voy a pits», distancia al líder, lecturas sin trabas y charla sin valoraciones."""
import unittest

import helpers  # noqa: F401
import brain as B
import assistant as A
from test_brain import Scripted, race_engine


def plan_for(text, obj, eng=None):
    eng = eng or race_engine(Scripted())
    return eng.assistant.brain.to_plan(text, obj), eng


class ShortStopQuestions(unittest.TestCase):
    def test_pits_plural_is_supported(self):
        for t in ("voy a pits", "voy a pits ya", "entro a pits"):
            p, _ = plan_for(t, {"tools": [{"t": "parar_ya"}], "say": ""})
            self.assertEqual(p.tools, ["parar_ya"], t)
            self.assertFalse(p.dropped, t)

    def test_voy_a_pits_is_not_a_position_question_through_the_llm(self):
        srv = Scripted(route=lambda c: {"tools": [{"t": "parar_ya"}], "say": ""})
        eng = race_engine(srv)
        eng.evaluate_now = lambda include=(): (None, "en boxes")
        eng.ask("voy a pits")
        self.assertEqual(eng.assistant.last["tools"], ["parar_ya"])

    def test_rules_without_llm_understand_short_questions(self):
        tr = race_engine(Scripted()).tr
        for t in ("¿paro?", "¿voy a pits?", "¿entro?", "¿paro a boxes ya?"):
            self.assertEqual(A.parse_rules(tr, t).name, "parar_ya", t)
        for t in ("¿voy bien?", "¿de qué posición voy?"):
            self.assertEqual(A.parse_rules(tr, t).name, "posicion", t)

    def test_acting_tools_still_need_words_from_the_pilot(self):
        p, _ = plan_for("no me siento cómodo con las gomas", {"tools": [{"t": "avisos", "topic": "ers", "level": -1}], "say": ""})
        self.assertFalse(p.intents)
        p, _ = plan_for("qué tal el ritmo", {"tools": [{"t": "silencio"}], "say": ""})
        self.assertFalse(p.intents)


class TrustReads(unittest.TestCase):
    def test_a_read_tool_chosen_by_the_model_is_respected_when_trust_is_on(self):
        # sin una sola palabra de SUPPORT en la frase: el modelo entendió la intención
        eng = race_engine(Scripted())
        eng.assistant.brain.trust_reads = True
        p, _ = plan_for("cómo está la pista de agua", {"tools": [{"t": "clima"}], "say": ""}, eng)
        self.assertEqual(p.tools, ["clima"])

    def test_default_keeps_the_measured_guards(self):
        self.assertFalse(race_engine(Scripted()).assistant.brain.trust_reads)

    def test_trust_can_be_switched_off(self):
        eng = race_engine(Scripted())
        p, _ = plan_for("cómo está la pista de agua", {"tools": [{"t": "clima"}], "say": ""}, eng)
        self.assertFalse(p.intents)


class LeaderGap(unittest.TestCase):
    def setUp(self):
        self.eng = race_engine(Scripted())
        self.tr = self.eng.tr
        self.leader = self.eng.assistant.brain.leader()

    def test_gap_to_the_leader(self):
        if self.leader is None:
            self.skipTest("en la simulación voy primero")
        p, _ = plan_for("qué tan lejos estoy del líder", {"tools": [{"t": "hueco", "con": "lider"}], "say": ""}, self.eng)
        self.assertEqual(p.intents[0].name, "hueco")
        self.assertEqual(p.intents[0].driver[0], self.leader[0])

    def test_model_answers_piloto_pos1_for_a_distance_question(self):
        if self.leader is None:
            self.skipTest("en la simulación voy primero")
        p, _ = plan_for("qué tan lejos estoy del primero", {"tools": [{"t": "piloto", "pos": 1}], "say": ""}, self.eng)
        self.assertEqual(p.intents[0].name, "hueco")
        self.assertEqual(p.intents[0].driver[0], self.leader[0])

    def test_who_is_first_stays_a_piloto_question(self):
        p, _ = plan_for("quién va primero", {"tools": [{"t": "piloto", "pos": 1}], "say": ""}, self.eng)
        self.assertEqual(p.intents[0].name, "piloto")

    def test_if_i_lead_the_answer_is_my_position(self):
        self.eng.assistant.brain.leader = lambda: None
        p, _ = plan_for("qué tan lejos estoy del líder", {"tools": [{"t": "hueco", "con": "lider"}], "say": ""}, self.eng)
        self.assertEqual(p.intents[0].name, "posicion")


class NoInventedPraise(unittest.TestCase):
    def test_status_chat_with_assessment_is_rejected(self):
        for s in ("Vamos bien, sigue así.", "Vas muy bien.", "Todo bien, piloto.", "Voy bien."):
            self.assertEqual(B.clean_say(s, "como vamos"), "", s)
        self.assertEqual(B.clean_say("Cuenta conmigo, piloto.", "mandame animos"), "Cuenta conmigo, piloto.")


class StopContext(unittest.TestCase):
    def test_stop_question_mentions_the_accepted_plan(self):
        import types
        eng = race_engine(Scripted())
        a = eng.assistant
        sit = types.SimpleNamespace(lap=eng.tr.cur_lap)
        opt = types.SimpleNamespace(stops=((eng.tr.cur_lap, 17),), e_pos=5.0, utility=-5.0, id=1, desc="X")
        rep = types.SimpleNamespace(best=opt, baseline=None, options=[opt])
        eng.evaluate_now = lambda include=(): (rep, sit, None, None, None)
        a._pit_now_option = lambda rep, sit: opt
        a._propose = lambda *x, **k: None
        eng.dialog.accepted = types.SimpleNamespace(option=types.SimpleNamespace(stops=((eng.tr.cur_lap + 4, 18),)))
        eng.dialog.pending = None
        self.assertIn(f"El plan aceptado era parar en la vuelta {eng.tr.cur_lap + 4}", a.t_parar_ya(None))


if __name__ == "__main__":
    unittest.main()
