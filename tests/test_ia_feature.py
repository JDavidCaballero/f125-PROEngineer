"""Rama ia/razonamiento (11 nov 2026): el LLM entiende más (contexto, seguimientos, hipótesis) sin razonar; el código calcula.
Medido con Qwen3.5 9B real: compuestas 62% -> 100%, difíciles 92% -> 95%, seguimientos 5/5, ruta mediana ~3 s."""
import json, unittest

import helpers  # noqa: F401  (añade la raíz del proyecto al path)
import llm as L
from test_brain import Scripted, race_engine


def tool(t, **args):
    return lambda ctx: {"tools": [{"t": t, **args}], "say": ""}


class NoThinking(unittest.TestCase):
    def test_every_request_asks_for_no_reasoning(self):
        seen = []
        cl = L.LLMClient(transport=lambda p: seen.append(p) or {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})
        cl.chat("s", "u")
        self.assertEqual(seen[0]["reasoning_effort"], "none")      # con Think activo en LM Studio, Qwen pensaba 22 s y no respondía

    def test_auto_does_not_send_it(self):
        seen = []
        cl = L.LLMClient(transport=lambda p: seen.append(p) or {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]},
                         reasoning="auto")
        cl.chat("s", "u")
        self.assertNotIn("reasoning_effort", seen[0])


class JsonRepair(unittest.TestCase):
    def test_unclosed_tool_object_is_repaired(self):
        self.assertEqual(L.extract_json('{"tools":[{"t":"estado_coche"],"say":""}'), {"tools": [{"t": "estado_coche"}], "say": ""})


class Context(unittest.TestCase):
    def test_router_gets_the_race_situation(self):
        srv = Scripted(route=tool("posicion"))
        eng = race_engine(srv)
        eng.ask("¿cómo voy?")
        ctx = json.loads(srv.payloads[-1][1] if srv.payloads[-1][0].startswith("Eres el cerebro") else srv.payloads[0][1])
        self.assertIn("yo", ctx)
        self.assertIn("delante", ctx)


class NewTools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = race_engine(Scripted(), t_stop=330)

    def run_tool(self, t, text, **args):
        eng = self.eng
        eng.assistant.brain.client = L.LLMClient(transport=Scripted(route=tool(t, **args)))
        eng.assistant.brain.down_until = 0
        eng.dialog.pending = None
        return eng.ask(text), eng.assistant.last

    def test_what_if_answers_without_proposing(self):
        r, last = self.run_tool("que_pasa_si", "si paro ahora, ¿cómo acabo?", parar="ahora")
        self.assertIn("terminarías hacia P", r)
        self.assertIsNone(self.eng.dialog.pending)                # una hipótesis no deja una propuesta pendiente

    def test_what_if_needs_a_stop_word(self):
        r, last = self.run_tool("que_pasa_si", "¿me alcanza la gasolina si ataco hasta el final?")
        self.assertNotIn("terminarías", r)                         # no es una hipótesis de parada: mandan las reglas

    def test_safety_car_hypothesis_goes_to_strategy(self):
        r, last = self.run_tool("que_pasa_si", "si sale el safety car, ¿paro?", parar="ahora")
        self.assertEqual(last["intent"], "estrategia")

    def test_rival_pace(self):
        r, _ = self.run_tool("ritmo_rival", "¿cómo va de ritmo el de delante?", dir="delante")
        self.assertRegex(r, r"(por vuelta|mismo ritmo|Aún no tengo)")

    def test_car_status_in_one_answer(self):
        r, _ = self.run_tool("estado_coche", "¿cómo está el coche?")
        self.assertIn("Combustible", r)
        self.assertIn("Neumático", r)

    def test_pit_window(self):
        r, _ = self.run_tool("ventana_parada", "¿entre qué vueltas paro?")
        self.assertRegex(r, r"(ventana|no parar|Lo mejor|no puedo)")

    def test_who_still_has_to_stop(self):
        r, _ = self.run_tool("faltan_parar", "¿quién tiene que parar todavía?")
        self.assertRegex(r, r"(aún tienen que parar|ya han parado|Detrás)")


class FollowUps(unittest.TestCase):
    def test_and_him_uses_the_last_driver(self):
        srv = Scripted(route=tool("hueco"))
        eng = race_engine(srv)
        tr = eng.tr
        name = next(n for i, n in tr.names.items() if i != tr.player)
        srv.route = tool("hueco", driver=name)
        eng.ask(f"cuánto le saco a {name}")
        srv.route = tool("neumaticos")                            # el modelo no repite el piloto
        r = eng.ask("¿y qué gomas lleva?")
        self.assertIn(name, r)

    def test_my_own_tyres_are_not_hijacked(self):
        srv = Scripted(route=tool("hueco"))
        eng = race_engine(srv)
        tr = eng.tr
        name = next(n for i, n in tr.names.items() if i != tr.player)
        srv.route = tool("hueco", driver=name)
        eng.ask(f"cuánto le saco a {name}")
        srv.route = tool("neumaticos")
        r = eng.ask("¿y mis gomas?")
        self.assertNotIn(name, r)


if __name__ == "__main__":
    unittest.main()
