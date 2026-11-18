import json, threading, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from helpers import cfg
import llm as L
import strategy as S
from test_strategy import me, rivals, P


def report(mode="arriesgado"):
    return S.evaluate(S.Situation(30, 12, 19.0), me(wear=30, age=8, rate=2.5, compound=16, used={16, 17}), rivals(8, used={16, 17}),
                      P, mode=mode)


def fake(content, finish="stop", calls=None):
    def tr(payload):
        if calls is not None:
            calls.append(payload)
        return {"choices": [{"message": {"content": content}, "finish_reason": finish}]}
    return L.LLMClient(transport=tr, no_think=True)


class Numbers(unittest.TestCase):
    def test_numbers_must_come_from_input(self):
        allowed = L.allowed_numbers({"vuelta": 12, "desgaste": 68.4, "txt": "vuelta 15"})
        self.assertTrue(L.numbers_ok("Entra a boxes en la vuelta 12", allowed))
        self.assertTrue(L.numbers_ok("Desgaste del 68%, vuelta 15", allowed))
        self.assertFalse(L.numbers_ok("Entra a boxes en la vuelta 13", allowed))
        self.assertTrue(L.numbers_ok("Sin cifras aquí", allowed))

    def test_the_signed_number_trap_from_the_conversation(self):
        # el 9B dijo "margen de 0.4 vueltas" cuando el dato era faltante 0.4: el número existe, así que la
        # validación de cifras no basta; por eso el código redacta las alertas críticas con plantilla fija
        allowed = L.allowed_numbers({"combustible_faltante_vueltas": 0.4})
        self.assertTrue(L.numbers_ok("margen de 0.4 vueltas", allowed))


class Choose(unittest.TestCase):
    def test_no_client_uses_code_decision(self):
        rep = report()
        d = L.choose(None, {}, rep, "normal")
        self.assertEqual((d["opcion"], d["source"]), (rep.best.id, "code"))

    def test_single_eligible_option_does_not_call_the_model(self):
        calls = []
        rep = S.evaluate(S.Situation(5, 2, 3.6), me(wear=5, age=1, rate=2.4, compound=16, used={16}), rivals(8, 16, 5, 1, 2.4),
                         P, mode="conservador")
        rep.eligible = [rep.best.id]
        d = L.choose(fake("{}", calls=calls), {}, rep, "conservador")
        self.assertEqual(calls, [])
        self.assertEqual(d["source"], "code")

    def test_valid_model_choice_is_used(self):
        rep = report()
        self.assertGreaterEqual(len(rep.eligible), 2)
        pick = rep.eligible[1]
        o = next(x for x in rep.options if x.id == pick)
        lap = o.stops[0][0] if o.stops else 12
        c = fake(json.dumps({"opcion": pick, "frase": f"Entra a boxes en la vuelta {lap}."}))
        d = L.choose(c, {"vuelta": 12}, rep, "arriesgado")
        self.assertEqual((d["opcion"], d["source"]), (pick, "llm"))
        self.assertIn(str(lap), d["frase"])

    def test_option_outside_margin_is_rejected(self):
        rep = report()
        rep.eligible = rep.eligible[:2]                      # solo las 2 primeras son elegibles
        outside = rep.options[-1].id
        self.assertNotIn(outside, rep.eligible)
        d = L.choose(fake(json.dumps({"opcion": outside, "frase": "ok"})), {}, rep, "conservador")
        self.assertEqual((d["opcion"], d["source"]), (rep.best.id, "code"))
        self.assertIn("no eligió una opción válida", d["reason"])

    def test_invented_number_falls_back_to_template(self):
        rep = report()
        pick = rep.eligible[0]
        d = L.choose(fake(json.dumps({"opcion": pick, "frase": "Entra a boxes en la vuelta 99."})), {}, rep, "arriesgado")
        self.assertEqual(d["opcion"], pick)
        o = next(x for x in rep.options if x.id == pick)
        self.assertEqual(d["frase"], o.phrase)
        self.assertIn("descartada", d["reason"])

    def test_garbage_and_errors_fall_back(self):
        rep = report()
        for c in (fake("no es json"), fake(json.dumps({"opcion": "Z", "frase": "x"})), fake("", finish="length"),
                  L.LLMClient(url="http://127.0.0.1:9", timeout=1)):
            d = L.choose(c, {}, rep, "arriesgado")
            self.assertEqual((d["opcion"], d["source"]), (rep.best.id, "code"))

    def test_prefer_keeps_previous_plan(self):
        rep = report()
        pref = rep.eligible[-1]
        d = L.choose(None, {}, rep, "arriesgado", prefer=pref)
        self.assertEqual(d["opcion"], pref)

    def test_thinking_switch_is_sent_only_when_asked(self):
        calls = []
        fake('{"opcion":"A","frase":"x"}', calls=calls).chat("s", "u")
        self.assertEqual(calls[0]["chat_template_kwargs"], {"enable_thinking": False})
        calls2 = []
        c = L.LLMClient(transport=lambda p: (calls2.append(p), {"choices": [{"message": {"content": "hola"}}]})[1])
        c.chat("s", "u")
        self.assertNotIn("chat_template_kwargs", calls2[0])


class Answer(unittest.TestCase):
    def test_answer_validates_numbers_and_defaults_to_no_data(self):
        s = {"posicion": 4, "vuelta": 7}
        self.assertEqual(L.answer(fake("Vas P4 en la vuelta 7."), s, "¿posición?"), "Vas P4 en la vuelta 7.")
        self.assertEqual(L.answer(fake("Vas P2 en la vuelta 7."), s, "¿posición?"), L.NO_DATA)
        self.assertEqual(L.answer(None, s, "¿?"), L.NO_DATA)
        self.assertEqual(L.answer(L.LLMClient(url="http://127.0.0.1:9", timeout=1), s, "¿?"), L.NO_DATA)


class Http(unittest.TestCase):
    """Cliente real contra un servidor HTTP local que imita a LM Studio."""

    @classmethod
    def setUpClass(cls):
        cls.seen = []
        outer = cls

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj):
                b = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                self._send({"data": [{"id": "qwen/qwen3.5-9b"}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.seen.append((self.path, body))
                self._send({"choices": [{"message": {"content": "Vas P4.", "reasoning_content": ""},
                                         "finish_reason": "stop"}]})

        cls.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_ping_and_chat(self):
        c = L.LLMClient(self.url, "qwen/qwen3.5-9b", no_think=True)
        self.assertTrue(c.ping())
        self.assertEqual(c.chat("sys", "user"), "Vas P4.")
        path, body = self.seen[-1]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(body["model"], "qwen/qwen3.5-9b")
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})

    def test_from_cfg(self):
        self.assertIsNone(L.LLMClient.from_cfg(cfg()))          # desactivado por defecto
        c = cfg()
        c["llm"]["enabled"], c["llm"]["url"] = True, self.url
        self.assertTrue(L.LLMClient.from_cfg(c).ping())


if __name__ == "__main__":
    unittest.main()
