"""El LLM como cerebro: entiende la frase, elige herramientas del programa y redacta; todo validado por código."""
import json, re, unittest

from helpers import cfg
import assistant as A
import brain as B
import llm as L
from test_assistant import run_until
from test_quali import make_engine, run_lap


class Scripted:
    """Servidor de mentira: responde según el tipo de llamada (router / redacción) con lo que se programe."""

    def __init__(self, route=None, compose=None, fail=False):
        self.route, self.compose, self.fail = route or (lambda ctx: {"tools": [], "say": ""}), compose, fail
        self.calls = {"route": 0, "compose": 0, "other": 0}
        self.payloads = []

    def __call__(self, payload):
        if self.fail:
            raise L.LLMError("servidor caído")
        sysmsg, user = payload["messages"][0]["content"], payload["messages"][1]["content"]
        self.payloads.append((sysmsg[:20], user))
        if sysmsg.startswith("Eres el cerebro"):
            self.calls["route"] += 1
            out = self.route(json.loads(user))
        elif sysmsg.startswith("Eres el ingeniero de pista de F1 y hablas por radio"):
            self.calls["compose"] += 1
            d = json.loads(user)
            out = self.compose(d["pregunta"], d["respuesta_tecnica"]) if self.compose else d["respuesta_tecnica"]
        else:
            self.calls["other"] += 1
            out = {"opcion": "A", "frase": "x"}
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}


def race_engine(srv, t_stop=200):
    eng, _, _ = run_until("corta", t_stop, client=L.LLMClient(transport=srv), primary=True)
    return eng


class Routing(unittest.TestCase):
    def test_llm_picks_a_tool_and_the_code_answers_with_game_data(self):
        srv = Scripted(route=lambda c: {"tools": [{"t": "piloto", "pos": 2}], "say": ""})
        eng = race_engine(srv)
        r = eng.ask("¿Cómo va el segundo clasificado?")
        second = next(eng.tr.names[k] for k, c in enumerate(eng.tr.lap_all) if c and c["position"] == 2)
        self.assertIn(second, r)
        self.assertEqual(srv.calls["route"], 1)
        self.assertEqual(eng.assistant.last["source"], "llm")
        self.assertEqual(eng.assistant.last["tools"], ["piloto"])

    def test_context_sent_to_the_model_has_session_names_and_previous_exchange(self):
        srv = Scripted(route=lambda c: {"tools": [{"t": "posicion"}], "say": ""})
        eng = race_engine(srv)
        eng.ask("¿cómo voy?")
        eng.ask("¿y ahora?")
        ctx = json.loads(srv.payloads[-1][1]) if srv.payloads[-1][0].startswith("Eres el cerebro") else None
        routed = [json.loads(u) for h, u in srv.payloads if h.startswith("Eres el cerebro")]
        self.assertEqual(routed[0]["sesion"], "race")
        self.assertIn("Hansen", routed[0]["pilotos"])
        self.assertNotIn("previo", routed[0])
        self.assertIn("previo", routed[1])

    def test_control_commands_do_not_wait_for_the_model(self):
        srv = Scripted(route=lambda c: {"tools": [{"t": "posicion"}], "say": ""})
        eng = race_engine(srv)
        for text in ("silencio", "reanuda", "repite", "no"):
            eng.ask(text)
        self.assertEqual(srv.calls["route"], 0)

    def test_two_tools_are_joined(self):
        srv = Scripted(route=lambda c: {"tools": [{"t": "posicion"}, {"t": "neumaticos"}], "say": ""})
        eng = race_engine(srv)
        r = eng.ask("¿cómo voy y cómo van las gomas?")
        self.assertIn("Vas P", r)
        self.assertIn("Neumático", r)

    def test_more_than_two_tools_are_cut(self):
        plan = B.Brain(race_engine(Scripted()), L.LLMClient(transport=Scripted())).to_plan(
            "¿posicion, bateria y clima: va a llover?", {"tools": [{"t": "posicion"}, {"t": "ers"}, {"t": "clima"}]})
        self.assertEqual(plan.tools, ["posicion", "ers"])

    def test_invented_tools_and_drivers_are_ignored(self):
        eng = race_engine(Scripted())
        br = eng.assistant.brain
        plan = br.to_plan("x", {"tools": [{"t": "hackear_la_nasa"}, {"t": "hueco", "driver": "Zzyzx"}]})
        self.assertEqual(plan.tools, ["hueco"])
        self.assertIsNone(plan.intents[0].driver)                         # piloto inexistente: no se inventa
        self.assertIsNone(br.intent_from_tool({"t": "vs_rival", "driver": "Zzyzx"}))
        c = br.intent_from_tool({"t": "compuesto", "comps": ["duro", "medio"]})
        self.assertEqual((c.name, set(c.comps)), ("elegir_compuesto", {17, 18}))
        self.assertEqual(br.intent_from_tool({"t": "compuesto", "comps": ["blando"]}).compound, 16)
        self.assertEqual(br.intent_from_tool({"t": "avisos", "topic": "ers", "level": -1}).topic, "ers")
        self.assertIsNone(br.intent_from_tool({"t": "avisos", "topic": "inventado"}).topic)
        self.assertIsNone(br.intent_from_tool({"t": "objetivo"}))          # sin posición ni piloto no hay objetivo

    def test_tool_arguments_reach_the_handlers(self):
        eng = race_engine(Scripted())
        br = eng.assistant.brain
        i = br.intent_from_tool({"t": "objetivo", "pos": 5})
        self.assertEqual((i.name, i.pos), ("objetivo_pos", 5))
        i = br.intent_from_tool({"t": "neumaticos", "dir": "delante"})
        self.assertEqual((i.name, i.topic), ("rival_neumaticos", "delante"))
        i = br.intent_from_tool({"t": "neumaticos", "otros": True})
        self.assertEqual(i.name, "rivales_neumaticos")
        i = br.intent_from_tool({"t": "comparar", "con": "primero"})
        self.assertEqual((i.name, i.topic), ("compara_vuelta", "primero"))
        self.assertEqual(br.intent_from_tool({"t": "mejor_vuelta", "sesion": True}).topic, "sesion")


class ChatAndUnknown(unittest.TestCase):
    def test_encouragement_is_not_an_error(self):
        srv = Scripted(route=lambda c: {"tools": [], "say": "Tú puedes, concéntrate en cada curva."})
        eng = race_engine(srv)
        r = eng.ask("Hola, empieza la clasificación, mándame ánimos")
        self.assertIn(r, A.Assistant.ANIMOS)                         # los ánimos son una respuesta fija y variada, sin esperar al modelo
        self.assertTrue(eng.assistant.last["understood"])
        r2 = eng.ask("dame ánimos")
        self.assertNotEqual(r, r2)
        srv2 = Scripted(route=lambda c: {"tools": [], "say": "Tú puedes, concéntrate en cada curva."})
        eng2 = race_engine(srv2)
        self.assertEqual(eng2.ask("hola, el viento sopla fuerte hoy, ¿te parece?"), "Tú puedes, concéntrate en cada curva.")    # charla libre: el modelo
        self.assertEqual(eng2.assistant.last["intent"], "charla")

    def test_chat_cannot_state_numbers_or_data(self):
        srv = Scripted(route=lambda c: {"tools": [], "say": "Vas P3 con 1:20.5, genial."})
        eng = race_engine(srv)
        r = eng.ask("¿cómo lo ves?")
        self.assertNotIn("P3", r)
        self.assertEqual(r, B.UNKNOWN)

    def test_no_tool_available_says_so_instead_of_no_he_entendido(self):
        eng = race_engine(Scripted(route=lambda c: {"tools": [], "say": ""}))
        r = eng.ask("¿Quién ganó el mundial de 1994?")
        self.assertEqual(r, B.UNKNOWN)
        self.assertNotIn("No he entendido", r)
        self.assertTrue(eng.assistant.last["understood"])

    def test_with_a_pending_proposal_an_unknown_phrase_is_not_understood_so_the_radio_fallback_works(self):
        srv = Scripted(route=lambda c: {"tools": [], "say": ""})
        eng, _, _ = run_until("corta", 330, client=L.LLMClient(transport=srv), primary=True)
        eng.assistant._run(A.Intent("parar_ya"), "quiero parar ya")          # deja una propuesta pendiente
        self.assertIsNotNone(eng.dialog.pending)
        r = eng.ask("mmm bla bla")
        self.assertFalse(eng.assistant.last["understood"])
        self.assertEqual(r, A.NO_UNDERSTAND)

    def test_noise_asks_to_repeat_like_before(self):
        eng = race_engine(Scripted(route=lambda c: {"tools": [], "say": "", "unclear": True}))
        r = eng.ask("Chimba y trasmisión")
        self.assertEqual(r, A.NO_UNDERSTAND)
        self.assertFalse(eng.assistant.last["understood"])               # la radio sigue con su protocolo de repetir

    def test_acknowledgement_statement(self):
        srv = Scripted(route=lambda c: {"tools": [], "say": "Entendido."})
        eng = race_engine(srv)
        self.assertEqual(eng.ask("Empiezo vuelta lanzada con el neumático blando, listo"), "Entendido.")


class Failures(unittest.TestCase):
    def test_server_down_falls_back_to_rules_and_does_not_retry_every_question(self):
        srv = Scripted(fail=True)
        eng = race_engine(srv)
        self.assertIn("Vas P", eng.ask("¿en qué posición voy?"))           # las reglas lo resuelven
        n = srv.calls["route"]
        eng.ask("¿cuánta batería tengo?")
        eng.ask("¿y el combustible?")
        self.assertEqual(srv.calls["route"], n)                              # 30 s de pausa: no se espera al servidor muerto
        self.assertEqual(eng.assistant.brain.stats["errors"], 1)

    def test_server_down_and_rules_fail_says_no_he_entendido(self):
        eng = race_engine(Scripted(fail=True))
        self.assertEqual(eng.ask("blablabla zzz en Marte"), A.NO_UNDERSTAND)

    def test_garbage_json_falls_back_to_rules(self):
        eng = race_engine(Scripted(route=lambda c: "lo siento, no puedo ayudar con eso"))
        self.assertIn("Vas P", eng.ask("¿en qué posición voy?"))

    def test_json_inside_text_or_code_fence_is_accepted(self):
        eng = race_engine(Scripted(route=lambda c: "```json\n{\"tools\":[{\"t\":\"ers\"}],\"say\":\"\"}\n```"))
        self.assertIn("ERS", eng.ask("¿cómo va la batería, por favor?"))

    def test_nested_json_extractor(self):
        self.assertEqual(L.extract_json('x {"a": {"b": [1, {"c": 2}]}} y')["a"]["b"][1]["c"], 2)
        self.assertIsNone(L.extract_json("sin json"))
        self.assertIsNone(L.extract_json("{roto"))

    def test_brain_is_off_without_a_client_or_without_primary(self):
        eng, _, _ = run_until("corta", 100)
        self.assertIsNone(eng.assistant.brain)
        eng, _, _ = run_until("corta", 100, client=L.LLMClient(transport=Scripted()), primary=False)
        self.assertIsNone(eng.assistant.brain)


class Compose(unittest.TestCase):
    def test_natural_rewrite_is_used_when_numbers_and_names_survive(self):
        def comp(q, tech):
            return tech.replace(". ", ", y ")                          # une frases, mismas palabras y cifras
        srv = Scripted(route=lambda c: {"tools": [{"t": "actualizacion"}], "say": ""}, compose=comp)
        eng = race_engine(srv)
        r = eng.ask("¿alguna novedad?")
        self.assertIn(", y ", r)
        self.assertRegex(r, r"Vas P\d")
        self.assertEqual(srv.calls["compose"], 1)

    def test_rewrite_that_changes_a_number_is_discarded(self):
        srv = Scripted(route=lambda c: {"tools": [{"t": "actualizacion"}], "say": ""},
                       compose=lambda q, t: re.sub(r"P(\d+)", lambda m: f"P{int(m.group(1)) + 3}", t))
        eng = race_engine(srv)
        r = eng.ask("¿alguna novedad?")
        self.assertTrue(r.startswith("Vas P"))
        self.assertEqual(eng.assistant.brain.stats["compose_rejected"], 1)

    def test_rewrite_that_adds_a_driver_is_discarded(self):
        srv = Scripted(route=lambda c: {"tools": [{"t": "actualizacion"}], "say": ""},
                       compose=lambda q, t: t + " Hansen está cerca.")
        eng = race_engine(srv)
        self.assertNotIn("Hansen está cerca", eng.ask("¿alguna novedad?"))

    def test_proposals_keep_their_exact_text_and_are_never_rewritten(self):
        srv = Scripted(route=lambda c: {"tools": [{"t": "parar_ya"}], "say": ""}, compose=lambda q, t: "OTRA COSA")
        eng = race_engine(srv)
        r = eng.ask("estoy pensando en entrar a cambiar gomas ahora mismo si te parece bien")
        self.assertNotIn("OTRA COSA", r)
        self.assertEqual(srv.calls["compose"], 0)

    def test_compose_can_be_turned_off_by_config(self):
        srv = Scripted(route=lambda c: {"tools": [{"t": "posicion"}], "say": ""}, compose=lambda q, t: "Otra redacción P1")
        c = cfg(primary=True)
        c["llm"]["compose"] = False
        eng, _, _ = run_until("corta", 100, client=L.LLMClient(transport=srv), primary=True)
        eng.assistant.brain.compose_on = False
        eng.ask("¿cómo voy?")
        self.assertEqual(srv.calls["compose"], 0)


class RiskyActions(unittest.TestCase):
    RANT = ("El caso es que necesito que revises esto porque algunos audios se están pisando y por favor "
            "quiero que dejes de hablar un rato mientras pienso")

    def test_long_rambling_asks_before_muting_and_yes_executes(self):
        srv = Scripted(route=lambda c: {"tools": [{"t": "silencio"}], "say": ""})
        eng = race_engine(srv)
        r = eng.ask(self.RANT)
        self.assertIn("¿Quieres que me calle", r)
        self.assertLessEqual(eng.dialog.muted_until, eng.tr.cur_lap)        # aún no se silenció nada
        eng.ask("sí")
        self.assertGreater(eng.dialog.muted_until, eng.tr.cur_lap)

    def test_no_cancels_the_pending_confirmation(self):
        eng = race_engine(Scripted(route=lambda c: {"tools": [{"t": "silencio"}], "say": ""}))
        eng.ask(self.RANT)
        self.assertIn("lo dejo", eng.ask("no"))
        self.assertLessEqual(eng.dialog.muted_until, eng.tr.cur_lap)

    def test_short_clear_command_runs_without_asking(self):
        eng = race_engine(Scripted(route=lambda c: {"tools": [{"t": "avisos", "topic": "ers", "level": -1}], "say": ""}))
        r = eng.ask("no me hables más del ERS")
        self.assertIn("no te daré avisos del ERS", r)
        self.assertEqual(eng.tac.topics["ers"], -1)

    def test_two_tools_with_a_risky_one_asks_first(self):
        eng = race_engine(Scripted(route=lambda c: {"tools": [{"t": "posicion"}, {"t": "silencio"}], "say": ""}))
        self.assertIn("¿Quieres que me calle", eng.ask("dime la posición y cállate"))


class SessionAwareTools(unittest.TestCase):
    def test_race_only_tool_in_practice_is_explained_not_run(self):
        srv = Scripted(route=lambda c: {"tools": [{"t": "viabilidad", "pos": 3}], "say": ""})
        eng, _ = make_engine(1)
        eng.client = L.LLMClient(transport=srv)
        from brain import Brain
        eng.assistant.brain = Brain(eng, eng.client, cfg(primary=True))
        run_lap(eng, 0, 2)
        self.assertIn("Eso es de carrera", eng.ask("¿podemos terminar tercero?"))


class LlmEvalCommand(unittest.TestCase):
    def test_command_runs_the_battery_and_writes_a_summary(self):
        import argparse, tempfile, io, contextlib
        from pathlib import Path
        import voice as V
        from helpers import demo
        path, _ = demo("corta")
        srv = Scripted(route=lambda c: {"tools": [{"t": "posicion"}], "say": ""})
        folder = Path(tempfile.mkdtemp())

        def fake_engine(args, cfg_, log):
            import engineer as E
            return E.Engineer(cfg(primary=True), "normal", L.LLMClient(transport=srv), out=lambda *_: None, sync=True)
        orig, V.make_engine = V.make_engine, fake_engine
        try:
            args = argparse.Namespace(file=str(path), phrases=str(V.C.ROOT / "llm_phrases.txt"), folder=folder, no_llm=False,
                                      llm=False, mode=None)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                V.cmd_llm(args, cfg())
        finally:
            V.make_engine = orig
        out = buf.getvalue()
        self.assertIn("RESULTADO:", out)
        rows = json.loads((folder / "llm_eval.json").read_text())
        self.assertGreaterEqual(len(rows), 45)
        self.assertTrue(all("respuesta" in r for r in rows))


@unittest.skipUnless(__import__("os").environ.get("F1_LLM_URL"), "sin servidor LLM real (define F1_LLM_URL)")
class RealModel(unittest.TestCase):
    """Con Qwen real: el JSON del router debe ser usable en casi todas las frases del informe."""

    PHRASES = ["¿En qué sesión estamos?", "¿Quién va primero?", "Mándame ánimos para la clasificación", "¿Dónde pierdo tiempo?",
               "¿Qué tal el neumático medio?", "Información de Piastri", "¿Cuál es la mejor vuelta de la sesión?",
               "Empiezo vuelta lanzada", "menos avisos de rivales", "¿es mejor el medio o el duro?"]

    def test_router_returns_valid_tools(self):
        import os
        client = L.LLMClient(os.environ["F1_LLM_URL"], os.environ.get("F1_LLM_MODEL", "qwen/qwen3.5-9b"), timeout=60)
        eng, _, _ = run_until("corta", 200, client=client, primary=True)
        ok = 0
        for p in self.PHRASES:
            plan = eng.assistant.brain.route(p)
            ok += bool(plan and (plan.intents or plan.say))
        self.assertGreaterEqual(ok, 8)


if __name__ == "__main__":
    unittest.main()


class GuardsFromRealModelMistakes(unittest.TestCase):
    """Errores que Qwen3.5 9B real cometió en la batería (26 oct) y que el código ya no deja pasar."""

    def setUp(self):
        self.eng = race_engine(Scripted())
        self.br = self.eng.assistant.brain

    def plan(self, text, tools):
        return self.br.to_plan(text, {"tools": tools, "say": ""})

    def test_driver_not_named_by_the_pilot_is_dropped(self):
        # «¿Qué tal el neumático medio?» -> neumaticos con driver inventado de la lista
        p = self.plan("¿Qué tal el neumático medio?", [{"t": "neumaticos", "driver": "Alves"}])
        self.assertIsNone(p.intents[0].driver)
        p = self.plan("¿Cómo van mis gomas?", [{"t": "neumaticos", "driver": "Tanaka"}])
        self.assertIsNone(p.intents[0].driver)
        # pero si lo nombra (aunque suene distinto), se mantiene
        p = self.plan("¿Qué neumáticos tiene Alves?", [{"t": "neumaticos", "driver": "Alves"}])
        self.assertEqual(p.intents[0].driver[1], "Alves")

    def test_state_changing_tool_without_support_in_the_phrase_is_dropped_and_rules_take_over(self):
        # «No me siento cómodo con las gomas» -> avisos (¡cambiaba los avisos!)
        srv = Scripted(route=lambda c: {"tools": [{"t": "avisos", "topic": "combustible", "level": -1}], "say": ""})
        eng = race_engine(srv)
        p = eng.assistant.prepare("No me siento cómodo con las gomas")
        self.assertEqual([i.name for i in p.intents], ["gomas_mal"])
        self.assertEqual(p.source, "reglas")
        self.assertNotIn("avisos", eng.tac.topics)

    def test_deliberate_no_tool_is_respected_not_overridden_by_rules(self):
        srv = Scripted(route=lambda c: {"tools": [], "say": ""})
        eng = race_engine(srv)
        p = eng.assistant.prepare("¿Quién ganó el mundial de 1994?")
        self.assertEqual(p.intents, [])

    def test_duplicate_tools_are_collapsed(self):
        p = self.plan("Información de Alves", [{"t": "piloto", "driver": "Alves"}, {"t": "piloto", "driver": "Alves"},
                                              {"t": "piloto", "driver": "Costa"}])
        self.assertEqual(len(p.intents), 1)                      # Costa no está en la frase: sin piloto ni posición no hay herramienta

    def test_explicit_more_or_less_updates_skip_the_model(self):
        srv = Scripted(route=lambda c: {"tools": [{"t": "actualizacion"}, {"t": "ers"}], "say": ""})
        eng = race_engine(srv)
        r = eng.ask("Más actualizaciones del ERS")
        self.assertEqual(srv.calls["route"], 0)
        self.assertEqual(eng.tac.topics["ers"], 1)

    def test_rewrites_that_drift_in_meaning_are_discarded(self):
        """Casos reales: «4 vueltas de uso» salió como «4 vueltas por delante»; saludo añadido; números a palabras."""
        raw = "Costa va P2. Está a 1.0 s delante de ti. Lleva Medio con 4 vueltas de uso."
        for bad in ("Costa va en P2, a 1.0 s delante de ti con neumáticos medios y 4 vueltas por delante.",
                    "Hola, Costa va P2 a 1.0 s delante de ti con Medio y 4 vueltas de uso.",
                    "¡Vamos! Costa va P2, a 1.0 s, con Medio y 4 vueltas de uso. ¡Sigue así!"):
            self.assertFalse(self.br.same_meaning(raw, bad), bad)
        for good in ("Costa va P2, está a 1.0 s delante de ti y lleva Medio con 4 vueltas de uso.",
                     "Costa va P2. Está a 1.0 s delante de ti, lleva Medio con 4 vueltas de uso."):
            self.assertTrue(self.br.same_meaning(raw, good), good)

    def test_short_replies_are_not_sent_for_rewriting(self):
        srv = Scripted(route=lambda c: {"tools": [{"t": "ers"}], "say": ""}, compose=lambda q, t: "OTRA COSA")
        eng = race_engine(srv)
        r = eng.ask("¿cómo va la batería?")
        self.assertEqual(srv.calls["compose"], 0)
        self.assertNotIn("OTRA COSA", r)


class GuardsFromRun3(unittest.TestCase):
    """Fallos reales de Qwen en la tanda 3 (26 oct): «¿Quién va primero?» -> posicion pos=1 (respondía mi puesto)."""

    def test_posicion_with_a_pos_or_driver_asks_about_someone_else(self):
        br = race_engine(Scripted()).assistant.brain
        i = br.intent_from_tool({"t": "posicion", "pos": 1}, "¿Quién va primero?")
        self.assertEqual((i.name, i.pos), ("piloto", 1))
        self.assertEqual(br.intent_from_tool({"t": "posicion"}, "¿En qué posición voy?").name, "posicion")

    def test_chat_cannot_mention_tyres_or_pits_the_pilot_did_not_say(self):
        self.assertEqual(B.clean_say("¡Gracias! A darle con el blando.", "gracias buen trabajo"), "")
        self.assertEqual(B.clean_say("Vamos, tú puedes.", "gracias buen trabajo"), "Vamos, tú puedes.")
        self.assertEqual(B.clean_say("Entendido. A darle con el blando.", "empiezo vuelta lanzada con el blando"),
                         "Entendido. A darle con el blando.")


class GuardsFromHardBattery(unittest.TestCase):
    """Fallos reales de Qwen con la batería difícil (26 oct)."""

    def plan(self, text, obj):
        return race_engine(Scripted()).assistant.brain.to_plan(text, obj)

    def test_nested_args_are_flattened(self):
        p = self.plan("quien va primero", {"tools": [{"t": "piloto", "args": {"pos": 1}}], "say": ""})
        self.assertEqual((p.tools, p.intents[0].pos), (["piloto"], 1))

    def test_weather_tool_needs_weather_words(self):
        p = self.plan("que tiempo ha hecho el primero", {"tools": [{"t": "clima"}], "say": ""})
        self.assertEqual(p.tools, [])
        self.assertTrue(p.dropped)
        self.assertEqual(self.plan("va a llover mañana", {"tools": [{"t": "clima"}], "say": ""}).tools, ["clima"])

    def test_championship_tool_needs_championship_words(self):
        self.assertEqual(self.plan("cuando es la proxima carrera", {"tools": [{"t": "campeonato"}], "say": ""}).tools, [])

    def test_a_dropped_tool_also_drops_its_chat_so_the_rules_can_answer(self):
        p = self.plan("debo entrar a boxes ahora", {"tools": [{"t": "avisos", "topic": "alternativas"}], "say": "Entendido. Te muestro las opciones."})
        self.assertEqual((p.say, p.dropped), ("", True))

    def test_chat_cannot_promise_victory_or_fake_actions(self):
        for s in ("¡Vamos! Confía, la victoria es posible.", "Entendido, ajustando el volumen.", "No tengo chistes guardados, pero vamos a ganar."):
            self.assertEqual(B.clean_say(s, "vamos a ganar"), "", s)

    def test_when_do_i_stop_is_a_strategy_question(self):
        eng = race_engine(Scripted())
        for q in ("cuando paro", "en que vuelta entro"):
            self.assertEqual(A.parse_rules(eng.tr, q).name, "estrategia", q)


class RulesOverChat(unittest.TestCase):
    """El modelo charló ante una pregunta de datos que las reglas reconocen (real: «puedo llegar al podio» -> «objetivo ambicioso»)."""

    def test_data_question_answered_by_rules_when_the_model_only_chats(self):
        srv = Scripted(route=lambda ctx: {"tools": [], "say": "Vamos, es un objetivo ambicioso."})
        eng = race_engine(srv)
        plan = eng.assistant.prepare("cuantas vueltas faltan")
        self.assertEqual((plan.source, plan.intents[0].name), ("reglas", "vueltas"))

    def test_real_chat_still_goes_to_the_model(self):
        srv = Scripted(route=lambda ctx: {"tools": [], "say": "Aquí estoy para lo que necesites."})
        eng = race_engine(srv)
        plan = eng.assistant.prepare("hola, ¿cómo estás hoy ingeniero?")
        self.assertEqual((plan.source, plan.say), ("llm", "Aquí estoy para lo que necesites."))

    def test_how_long_is_the_race_is_laps_left(self):
        self.assertEqual(A.parse_rules(race_engine(Scripted()).tr, "cuanto dura la carrera").name, "vueltas")

    def test_ers_tool_needs_energy_words(self):
        p = race_engine(Scripted()).assistant.brain.to_plan("estoy sin gomas", {"tools": [{"t": "ers"}, {"t": "danos"}], "say": ""})
        self.assertEqual((p.tools, p.dropped), ([], True))


class RealRun3Fixture(unittest.TestCase):
    """Tanda 3 con el modelo real (prompt calibrado): 48/49 con la herramienta esperada; no llama al servidor."""

    def test_measured_quality_after_calibration(self):
        from pathlib import Path
        rows = json.loads((Path(__file__).parent / "data" / "llm_real_run3.json").read_text(encoding="utf-8"))["filas"]
        self.assertEqual(len(rows), 49)
        self.assertGreaterEqual(sum(r["ok"] for r in rows) / 49, 0.95)
        lat = sorted(r["ruta_s"] for r in rows if r["ruta_s"])
        self.assertLess(lat[len(lat) // 2], 6.0)

    def test_hard_battery_real_results(self):
        from pathlib import Path
        d = json.loads((Path(__file__).parent / "data" / "llm_real_hard.json").read_text(encoding="utf-8"))
        self.assertEqual(len(d["filas"]), 41)
        self.assertGreaterEqual(sum(r["ok"] for r in d["filas"]) / 41, 0.90)
        said = " ".join(r["respuesta"].lower() for r in d["filas"])
        for bad in ("victoria", "ajustando", "hackeado"):          # lo que el modelo inventó antes de las guardas no aparece
            self.assertNotIn(bad, said)


class RealRunFixture(unittest.TestCase):
    """Resultados REALES de Qwen3.5 9B (26 oct, 49 frases, prompt v1) guardados como referencia: lo que se midió y lo que los
    controles posteriores habrían rechazado. No llama al servidor."""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        cls.data = json.loads((Path(__file__).parent / "data" / "llm_real_run1.json").read_text(encoding="utf-8"))
        cls.rows = cls.data["filas"]

    def test_measured_quality_of_the_model(self):
        n = len(self.rows)
        ok = sum(r["ok"] for r in self.rows)
        self.assertEqual(n, 49)
        self.assertGreaterEqual(ok / n, 0.80)                       # 42/49 con el prompt v1
        lat = sorted(r["ruta_s"] for r in self.rows if r["ruta_s"])
        self.assertLess(lat[len(lat) // 2], 4.0)                    # mediana del router ~2.8 s

    def test_the_meaning_filter_rejects_the_real_bad_rewrites_and_keeps_the_good_ones(self):
        br = race_engine(Scripted()).assistant.brain
        bad = [r for r in self.rows if r["respuesta"] != r["en_bruto"] and ("por del" in r["respuesta"] or r["respuesta"].startswith("Hola,"))]
        self.assertGreaterEqual(len(bad), 3)                         # «4 vueltas por delante», «cuatro vueltas por del…», «Hola, vas P4…»
        for r in bad:
            self.assertFalse(br.same_meaning(r["en_bruto"], r["respuesta"]), r["respuesta"])
        good = [r for r in self.rows if r["en_bruto"] and r["respuesta"] != r["en_bruto"] and
                br.same_meaning(r["en_bruto"], r["respuesta"])]
        self.assertGreaterEqual(len(good), 8)                        # no se tira todo: las reescrituras fieles siguen valiendo
        for r in good:
            from llm import numbers_ok, allowed_numbers
            self.assertTrue(numbers_ok(r["respuesta"], allowed_numbers(r["en_bruto"]), tol=0.0501), r["respuesta"])

    def test_wrong_picks_recorded_in_the_run_are_covered_by_guards(self):
        wrong = {r["frase"]: r["elegido"] for r in self.rows if not r["ok"]}
        self.assertIn("No me siento cómodo con las gomas", wrong)    # avisos: cubierto por GuardsFromRealModelMistakes
        self.assertIn("¿Qué tal el neumático medio?", wrong)         # driver inventado: cubierto


