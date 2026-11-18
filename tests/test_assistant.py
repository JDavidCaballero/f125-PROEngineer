import json, unittest

from helpers import demo, cfg
import assistant as A
import dialog as D
import engineer as E
import llm as L
import strategy as S
from f1_parse import read_capture
from test_strategy import me, rivals, P


def run_until(name, t_stop, mode="normal", client=None, log=None, primary=False):
    """Ingeniero que ha visto la carrera inventada hasta t_stop (segundos de captura)."""
    path, _ = demo(name)
    out = []
    eng = E.Engineer(cfg(primary), mode, client, out=out.append, sync=True, log_path=log)
    it = read_capture(path)
    for t, d in it:
        eng.on_packet(t, d)
        if t >= t_stop:
            break
    return eng, out, it


def fake_llm(obj):
    return L.LLMClient(transport=lambda p: {"choices": [{"message": {"content": json.dumps(obj)}, "finish_reason": "stop"}]})


class Rules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng, _, _ = run_until("corta", 200)
        cls.tr = cls.eng.tr

    def parse(self, text):
        return A.parse_rules(self.tr, text)

    def test_intent_table(self):
        cases = {
            "sí": "si", "dale": "si", "ok dale": "si", "no": "no", "no, no, no": "no", "negativo": "no",
            "no sé qué hacer": "estrategia", "¿cambiamos de estrategia?": "estrategia", "qué hacemos": "estrategia",
            "quiero parar ya": "parar_ya", "vamos a parar ahora": "parar_ya",
            "no me siento cómodo con las gomas": "gomas_mal", "no tengo agarre": "gomas_mal",
            "no me siento cómodo": "incomodo",
            "¿puedo hacer undercut?": "undercut", "me conviene parar": "undercut",
            "¿por qué estamos perdiendo tiempo?": "por_que_pierdo", "voy lento": "por_que_pierdo",
            "explícame": "explica", "por qué": "explica", "silencio": "silencio", "repite": "repite", "reanuda": "reanuda",
            "¿cuánto combustible tengo?": "combustible", "¿cómo van las gomas?": "neumaticos",
            "¿dónde puedo ahorrar combustible?": "ahorro", "¿cuánto falta?": "vueltas", "¿qué hay detrás?": "hueco_detras",
            "¿y delante?": "hueco_delante", "¿cómo voy de ritmo?": "ritmo", "¿cuánta batería tengo?": "ers",
            "olvida el objetivo": "objetivo_borrar", "¿vamos bien?": "objetivo_estado",
        }
        for text, want in cases.items():
            got = self.parse(text)
            self.assertIsNotNone(got, text)
            self.assertEqual(got.name, want, text)

    def test_cases_from_lab_report_6(self):
        """Frases reales del informe 6 que antes caían en «No he entendido» o iban a la intención equivocada."""
        cases = {
            "¿Qué tal está mi alero en delantera?": "danos", "¿Jansen me ha pasado?": "hueco",
            "¿alguien me ha pasado?": "posicion", "¿Dejo pasar a Brandt?": "dejar_pasar",
            "Hansen me saca mucho en recta": "vs_rival", "El coche de Hansen es más rápido en recta": "vs_rival",
            "¿En qué curvas voy más rápido que Hansen?": "vs_rival", "¿Dónde puedo ir más rápido que Hansen?": "vs_rival",
            "¿Podemos ganar la carrera?": "viabilidad", "¿Podemos acabar quinto?": "viabilidad",
            "Probabilidad de llovía": "clima", "No me siento bien con estas gomas": "gomas_mal",
            "Quiero un neumático duro": "compuesto", "Y si vamos con neumático duro": "compuesto",
            "¿Qué tal van los otros coches con neumático duro?": "compuesto_rivales",
            "Propone un objetivo": "sugerir_objetivo", "Si voy a boxes, ¿en qué posición quedaría?": "parar_ya",
            "¿cuánto me saca Hansen?": "hueco",
        }
        for text, want in cases.items():
            got = self.parse(text)
            self.assertIsNotNone(got, text)
            self.assertEqual(got.name, want, text)
        self.assertEqual(self.parse("neumático duro").compound, 18)
        self.assertEqual(self.parse("¿Podemos ganar la carrera?").pos, 1)
        self.assertEqual(self.parse("¿Podemos acabar quinto?").pos, 5)

    def test_cases_from_lab_report_7(self):
        cases = {
            "Quiero luchar por el podio": "objetivo_pos", "Quiero terminar, P3 si es posible": "objetivo_pos",
            "¿Algún otro piloto con un neumático diferente?": "rivales_neumaticos",
            "Hansen, ¿qué neumáticos tiene?": "rival_neumaticos", "Según mi ritmo, ¿cuál es la mejor posición?": "sugerir_objetivo",
        }
        for text, want in cases.items():
            got = self.parse(text)
            self.assertIsNotNone(got, text)
            self.assertEqual(got.name, want, text)
        self.assertEqual(self.parse("Quiero luchar por el podio").pos, 3)

    def test_long_rambling_is_not_a_command(self):
        """Informe 7: una parrafada dirigida a otra persona activó «parar ya» y «silencio»."""
        text = ("El caso es que necesito que revises esto porque algunos audios se están pisando y quiero parar "
                "de hablar un momento y silencio")
        self.assertIsNone(self.parse(text))
        self.assertIsNone(A.classify_llm(fake_llm({"intencion": "silencio"}), self.tr, text))

    def test_llm_cannot_trigger_state_changing_intents(self):
        for name in ("silencio", "si", "no", "parar_ya", "objetivo_borrar", "gomas_mal"):
            self.assertIsNone(A.classify_llm(fake_llm({"intencion": name}), self.tr, "frase rara de prueba"), name)
        self.assertEqual(A.classify_llm(fake_llm({"intencion": "clima"}), self.tr, "frase rara de prueba").name, "clima")

    def test_cases_from_lab_report_8(self):
        cases = {
            "menos avisos de combustible": ("avisos", "combustible", -1), "más actualizaciones del ERS": ("avisos", "ers", 1),
            "no me avises del DRS": ("avisos", "drs", -1), "menos actualizaciones": ("avisos", None, -1),
            "avisos de combustible normales": ("avisos", "combustible", 0),
            "¿Cómo voy en el campeonato de pilotos?": ("campeonato", None, 0),
            "¿Cuántos puntos tengo en el campeonato?": ("campeonato", None, 0),
            "¿Cómo vamos en el campeonato de constructores?": ("campeonato", None, 0),
            "Neumático del de adelante": ("rival_neumaticos", "delante", 0),
        }
        for text, (name, topic, level) in cases.items():
            got = self.parse(text)
            self.assertIsNotNone(got, text)
            self.assertEqual((got.name, got.topic, got.level), (name, topic, level), text)

    def test_championship_answer_is_honest_not_the_race_position(self):
        eng, _, _ = run_until("corta", 200)
        r = eng.ask("¿Cómo voy en el campeonato de pilotos?")
        self.assertIn("no me envía la clasificación del campeonato", r)
        self.assertIn("puntos de esta carrera", r)

    def test_topic_levels_by_voice_and_follow_up(self):
        eng, _, _ = run_until("corta", 200)
        self.assertIn("¿De qué avisos", eng.ask("menos actualizaciones"))
        self.assertIn("no te daré avisos del ERS", eng.ask("del ERS"))
        self.assertEqual(eng.tac.topics["ers"], -1)
        eng.ask("más avisos de combustible")
        self.assertEqual(eng.tac.topics["combustible"], 1)

    def test_feasibility_question_does_not_set_a_goal(self):
        eng, _, _ = run_until("corta", 200)
        r = eng.ask("¿podemos acabar quinto?")
        self.assertIn("probabilidad", r)
        self.assertIsNone(eng.goals.goal)
        self.assertNotIn("No he entendido", eng.ask("¿Dejo pasar a Brandt?"))
        self.assertNotIn("No he entendido", eng.ask("Hansen me saca mucho en recta"))
        self.assertNotIn("No he entendido", eng.ask("¿qué tal van los otros coches con neumático duro?"))

    def test_unclear_phrases_are_not_guessed(self):
        for text in ("", "blablabla", "no puedo más", "mmm", "la casa azul"):
            self.assertIsNone(self.parse(text), text)

    def test_driver_names_tolerate_transcription_errors(self):
        for text in ("¿cómo voy respecto a Hansen?", "como voy contra Hansén", "como voy contra Hanson"):
            i = self.parse(text)
            self.assertEqual((i.name, i.driver[1]), ("hueco", "Hansen"), text)
        i = self.parse("necesito pasar a Ortega")
        self.assertEqual((i.name, i.driver[1]), ("objetivo_pasar", "Ortega"))

    def test_goal_positions(self):
        for text, n in (("quiero acabar quinto", 5), ("terminar en P3", 3), ("quiero quedar décimo", 10),
                        ("mi objetivo es P8", 8)):
            i = self.parse(text)
            self.assertEqual((i.name, i.pos), ("objetivo_pos", n), text)

    def test_ambiguous_driver_asks_instead_of_choosing(self):
        r = self.eng.assistant.handle("¿cómo voy contra Hansen y Ortega?")
        self.assertTrue(r.startswith("¿A cuál te refieres"), r)
        self.assertIn("Hansen", r)
        self.assertIn("Ortega", r)

    def test_unknown_gets_repeat_request(self):
        self.assertEqual(self.eng.assistant.handle("blablabla"), A.NO_UNDERSTAND)
        self.assertEqual(self.eng.assistant.handle("mmm"), A.NO_UNDERSTAND)

    def test_pass_goal_without_driver_asks_who(self):
        r = self.eng.assistant.handle("necesito pasar a Zzzzz")
        self.assertIn(r, (A.NO_UNDERSTAND, "¿De qué piloto?"))


class Queries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng, _, _ = run_until("corta", 300)

    def ask(self, q):
        return self.eng.assistant.handle(q)

    def test_gap_answer_matches_tracker(self):
        a, b = self.eng.tr.neighbours()
        name = self.eng.tr.name(a[0])
        r = self.ask(f"¿cómo voy contra {name}?")
        self.assertIn(f"{abs(a[1]):.1f} s", r)
        self.assertIn("delante", r)

    def test_simple_queries_use_real_data(self):
        self.assertRegex(self.ask("¿en qué posición voy?"), r"Vas P\d+ en la vuelta \d+ de 15")
        self.assertIn("kg", self.ask("¿cuánto combustible tengo?"))
        self.assertIn("Neumático", self.ask("¿cómo van las gomas?"))
        self.assertIn("Quedan", self.ask("¿cuántas vueltas quedan?"))
        self.assertIn("ERS", self.ask("¿cuánta batería?"))
        self.assertIn("lluvia", self.ask("¿va a llover?"))

    def test_why_losing_time_gives_numbers_or_says_no(self):
        r = self.ask("¿por qué estamos perdiendo tiempo?")
        self.assertTrue(r.startswith(("Pierdes", "No estás perdiendo", "No tengo")), r)

    def test_gap_trend_is_not_reported_in_the_first_laps(self):
        eng, _, _ = run_until("corta", 100)                   # vuelta 2: el hueco anterior aún viene de la parrilla
        self.assertLess(eng.tr.cur_lap, 3)
        r = eng.assistant.handle("¿qué hueco hay con el de delante?")
        self.assertNotIn("recortado", r)
        self.assertNotIn("perdido", r)

    def test_pace(self):
        self.assertIn("Tus últimas vueltas", self.ask("¿cómo voy de ritmo?"))


class ProposalFlow(unittest.TestCase):
    def test_accept_explain_and_reminders(self):
        eng, out, it = run_until("corta", 388)           # justo cuando sale el Safety Car
        self.assertIsNotNone(eng.dialog.pending, "el SC debe generar una propuesta")
        self.assertIn("obligatorio", eng.ask("explícame"))
        self.assertEqual(eng.dialog.pending.state, "pendiente")        # explicar no la cierra
        self.assertIn("plan confirmado", eng.ask("sí"))
        self.assertIsNotNone(eng.dialog.accepted)
        self.assertIsNone(eng.dialog.pending)
        for t, d in it:                                                # el plan se vigila: la ventana pasa sin parar
            eng.on_packet(t, d)
            if any("Pasó la ventana" in l for l in out):
                break
        self.assertTrue(any("Pasó la ventana del plan aceptado" in l for l in out))
        self.assertIsNone(eng.dialog.accepted)

    def test_decline_and_silence_of_same_plan(self):
        eng, out, it = run_until("corta", 388)
        stop = eng.dialog.pending.option.stops[0]
        self.assertIn("seguimos con el plan actual", eng.ask("no"))
        self.assertEqual(eng.dialog.history[-1]["state"], "rechazada")
        self.assertEqual(eng.ask("sí"), "No hay ninguna propuesta pendiente.")
        gate = eng.dialog.gate
        self.assertTrue(any(D.same_stop(stop, s, 2) for s, _, _ in gate.declined))

    def test_yes_with_nothing_pending(self):
        eng, _, _ = run_until("corta", 100)
        self.assertEqual(eng.ask("sí"), "No hay ninguna propuesta pendiente.")
        self.assertEqual(eng.ask("no"), "No hay ninguna propuesta pendiente.")

    def test_pit_executed_closes_plan(self):
        d = D.Dialog(cfg(), "normal")
        rep = S.evaluate(S.Situation(30, 12, 19.0, sc=1), me(wear=45, age=12, rate=3.5, compound=16, used={16, 17}, life_game=7),
                         rivals(8, 17, 25, 12, 2.2, used={16, 17}), P)
        d.propose(rep.best, rep.best.phrase, 12, 100.0)
        d.accept(12)
        self.assertEqual(d.on_pit_out(), "Plan ejecutado. Buen trabajo.")
        self.assertIsNone(d.accepted)
        self.assertEqual(d.history[-1]["state"], "ejecutada")


class PilotInitiated(unittest.TestCase):
    def test_strategy_question_runs_simulation_and_proposes(self):
        eng, _, _ = run_until("corta", 330)
        r = eng.assistant.handle("¿cambiamos de estrategia?")
        self.assertTrue(r.startswith(("Recomiendo", "Mantén el plan")), r)
        if r.startswith("Recomiendo"):
            self.assertIsNotNone(eng.dialog.pending)
            self.assertEqual(eng.dialog.pending.source, "piloto")

    def test_dont_know_what_to_do(self):
        eng, _, _ = run_until("corta", 330)
        self.assertTrue(eng.assistant.handle("no sé qué hacer").startswith(("Recomiendo", "Mantén el plan")))

    def test_i_want_to_pit_now_reports_cost(self):
        eng, _, _ = run_until("media", 600)
        r = eng.assistant.handle("quiero parar ya")
        self.assertIn("Si paras ahora", r)
        self.assertIn("¿Entro a boxes", r)
        self.assertIsNotNone(eng.dialog.pending)
        self.assertEqual(eng.dialog.pending.option.stops[0][0], eng.tr.cur_lap)
        self.assertIn("plan confirmado", eng.ask("sí"))

    def test_undercut_question(self):
        eng, _, _ = run_until("corta", 330)
        r = eng.assistant.handle("¿puedo hacer undercut?")
        self.assertTrue(r.startswith("Parar ahora") or "Ahora no hay" in r, r)

    def test_uncomfortable_tyres_changes_the_calculation_and_resets_after_a_stop(self):
        eng, out, it = run_until("media", 420)
        before = eng.last_best_epos
        r = eng.assistant.handle("no me siento cómodo con las gomas")
        self.assertIn("Anotado", r)
        self.assertTrue(eng.tr.felt_grip)
        self.assertEqual(eng.tr.limit_delta, 10.0)
        self.assertIn("gomas rinden peor", r)
        rep, sit, me_, riv, info = eng.evaluate_now()
        self.assertEqual(me_.felt_loss_s, eng.tr.params.felt_grip_loss_s)
        self.assertEqual(info["params"].wear_limit, eng.tr.params.wear_limit - 10)
        self.assertTrue(any("Incluyo" in e for o in rep.options for e in o.evidence if o.stops))
        for t, d in it:                              # pasa la parada del jugador (vuelta 11): se borra la sensación
            eng.on_packet(t, d)
            if any("Neumáticos nuevos puestos" in l for l in out):
                break
        self.assertFalse(eng.tr.felt_grip)
        self.assertEqual(eng.tr.limit_delta, 0.0)

    def test_uncomfortable_asks_what_with_and_does_not_assume(self):
        eng, _, _ = run_until("media", 420)
        r = eng.assistant.handle("no me siento cómodo")
        self.assertIn("las gomas, el coche o el ritmo", r)
        self.assertFalse(eng.tr.felt_grip)
        self.assertIn("Anotado", eng.assistant.handle("las gomas"))
        self.assertTrue(eng.tr.felt_grip)

    def test_silence_mutes_non_critical_and_resume_restores(self):
        eng, out, it = run_until("sprint", 100)
        self.assertIn("Silencio", eng.ask("silencio"))
        n = len(out)
        for t, d in it:
            eng.on_packet(t, d)
            if t > 250:
                break
        muted = [l for l in out[n:] if "defiende" in l or "Vuelta " in l]
        self.assertEqual(muted, [])
        self.assertIn("Vuelvo a avisarte", eng.ask("reanuda"))
        for t, d in it:
            eng.on_packet(t, d)
            if t > 400:
                break
        self.assertTrue(any("Fin de carrera" in l for l in out[n:]) or any("defiende" in l for l in out[n:]))


class Classifier(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng, _, _ = run_until("corta", 300)

    def test_llm_classifies_what_rules_miss(self):
        self.eng.client = fake_llm({"intencion": "hueco", "piloto": "Hansen", "posicion": None})
        r = self.eng.assistant.handle("oye dime algo de ese tipo que me persigue")
        self.eng.client = None
        self.assertIn("Hansen", r)

    def test_llm_cannot_invent_drivers_or_intents(self):
        for obj in ({"intencion": "hueco", "piloto": "Verstappen"}, {"intencion": "hackear_coche"}, {"intencion": "ninguna"}):
            self.eng.client = fake_llm(obj)
            r = self.eng.assistant.handle("frase rara que no entiendo para nada xyz")
            self.eng.client = None
            if obj["intencion"] == "hueco":
                self.assertNotIn("Verstappen", r)         # no está en la parrilla: no se inventa
            else:
                self.assertEqual(r, A.NO_UNDERSTAND)

    def test_llm_down_means_not_understood(self):
        self.eng.client = L.LLMClient(url="http://127.0.0.1:9", timeout=1)
        r = self.eng.assistant.handle("frase rara que no entiendo para nada xyz")
        self.eng.client = None
        self.assertEqual(r, A.NO_UNDERSTAND)


class GateUnit(unittest.TestCase):
    def report(self, **k):
        d = dict(wear=45, age=12, rate=3.5, compound=16, used={16, 17}, life_game=7)
        d.update(k)
        return S.evaluate(S.Situation(30, 12, 19.0), me(**d), rivals(8, 17, 25, 12, 2.2, used={16, 17}), P)

    def test_stay_best_is_silent(self):
        rep = S.evaluate(S.Situation(5, 2, 3.6), me(wear=5, age=1, rate=2.4, compound=16, used={16}), rivals(8, 16, 5, 1, 2.4), P)
        ok, why = D.Gate(cfg(), "normal").decide(2, rep, rep.best, None, False, False)
        self.assertFalse(ok)
        self.assertIn("seguir fuera", why)

    def test_persistence_then_cooldown_then_decline(self):
        g = D.Gate(cfg(), "normal")
        rep = self.report()
        self.assertEqual(rep.best.kind, "PIT")
        ok, why = g.decide(12, rep, rep.best, None, False, False)
        self.assertFalse(ok)
        self.assertIn("1/2", why)
        ok, why = g.decide(13, rep, rep.best, None, False, False)
        self.assertTrue(ok, why)
        g.proposed(13)
        self.assertFalse(g.decide(14, rep, rep.best, None, False, False)[0])           # cooldown
        g.decline(rep.best.stops[0], 14, 99.0)
        self.assertFalse(g.decide(16, rep, rep.best, None, False, False)[0])           # rechazada hace poco
        self.assertTrue(g.decide(16, rep, rep.best, None, True, False)[0])             # lo urgente salta el filtro
        self.assertTrue(g.decide(18, rep, rep.best, None, False, False)[0])            # pasado el tiempo, vuelve a poder salir

    def test_accepted_plan_is_not_proposed_again(self):
        g = D.Gate(cfg(), "normal")
        rep = self.report()
        d = D.Dialog(cfg(), "normal")
        d.propose(rep.best, rep.best.phrase, 12, 0.0)
        d.accept(12)
        g.observe(rep.best)
        ok, why = g.decide(14, rep, rep.best, d.accepted, False, False)
        self.assertFalse(ok)
        self.assertIn("ya aceptado", why)

    def test_risky_mode_has_lower_threshold(self):
        self.assertLess(D.Gate(cfg(), "arriesgado").min_gain, D.Gate(cfg(), "normal").min_gain)
        self.assertLess(D.Gate(cfg(), "normal").min_gain, D.Gate(cfg(), "conservador").min_gain)


class GoalUnit(unittest.TestCase):
    def opt(self, p, name="R1", pos=5):
        o = S.Option("A", "STAY", "x", "x", (), False)
        o.cdf = [0.0] * 22
        for k in range(pos - 1, 22):
            o.cdf[k] = p
        o.p_beat = {name: p}
        return o

    def setup(self, gap=-2.0):
        m = me(pos=6)
        r = rivals(4)
        r[0].name, r[0].idx, r[0].gap, r[0].pace = "R1", 1, gap, 92.0
        sit = S.Situation(30, 10, 20.0)
        return m, r, sit

    def test_pass_goal_status_hysteresis_and_fallback(self):
        g = D.Goals(cfg())
        m, r, sit = self.setup()
        g.set_pass(1, "R1", 10)
        self.assertEqual(g.update(self.opt(0.8), sit, m, r, 10), [])            # sigue en camino: callado
        msgs = g.update(self.opt(0.3), sit, m, r, 11)
        self.assertEqual(msgs, [])                                              # un solo dato malo no cambia el estado
        msgs = g.update(self.opt(0.3), sit, m, r, 12)
        self.assertIn("en riesgo", msgs[0][0])
        self.assertIn("necesitas vueltas de", msgs[0][0])                       # ritmo requerido
        g.update(self.opt(0.05), sit, m, r, 13)
        msgs = g.update(self.opt(0.05), sit, m, r, 14)
        self.assertTrue(any("inalcanzable" in t for t, _ in msgs))
        self.assertTrue(any("Paso a asegurar P6" in t for t, _ in msgs))
        self.assertEqual((g.goal.kind, g.goal.target), ("hold", 6))

    def test_goal_completed_when_rival_is_behind(self):
        g = D.Goals(cfg())
        m, r, sit = self.setup(gap=+0.8)
        g.set_pass(1, "R1", 10)
        msgs = g.update(self.opt(0.9), sit, m, r, 12)
        self.assertIn("Objetivo cumplido", msgs[0][0])
        self.assertIn("bien encaminados", msgs[0][0])
        self.assertEqual(g.goal.kind, "hold")

    def test_required_pace_math(self):
        g = D.Goals(cfg())
        m, r, sit = self.setup(gap=-4.0)
        m.pace = 93.5
        g.set_pass(1, "R1", 10)
        txt = g.required(m, sit, r)
        per_lap = (4.0 + 0.5) / 20.0                                            # (hueco + margen) / vueltas que quedan
        self.assertIn(f"{per_lap:.2f} s mejor", txt)
        self.assertIn(D.fmt_lap(92.0 - per_lap), txt)

    def test_position_goal_and_text(self):
        g = D.Goals(cfg())
        m, r, sit = self.setup()
        g.set_pos(5, 10)
        msgs = g.update(self.opt(0.1, pos=5), sit, m, r, 10)
        self.assertIsInstance(msgs, list)
        self.assertIn("terminar en P5", g.status_text(self.opt(0.1), sit, m, r))
        self.assertIn("No tenemos un objetivo", D.Goals(cfg()).status_text(None, None, None, []))

    def test_goal_flow_through_assistant(self):
        eng, out, it = run_until("corta", 330)
        a, _ = eng.tr.neighbours()
        name = eng.tr.name(a[0])
        r = eng.assistant.handle(f"necesito pasar a {name}")
        self.assertIn("Objetivo anotado: pasar a " + name, r)
        self.assertIsNotNone(eng.goals.goal)
        self.assertIn("Objetivo", eng.assistant.handle("¿vamos bien?"))
        self.assertEqual(eng.assistant.handle("olvida el objetivo"), "Objetivo borrado.")
        self.assertIsNone(eng.goals.goal)
        self.assertIn("Objetivo anotado: terminar en P3", eng.assistant.handle("quiero acabar tercero"))


if __name__ == "__main__":
    unittest.main()


class FromRealReport(unittest.TestCase):
    """Fallos vistos en el informe de la sesión real de voz (Whisper medium, 45/48)."""

    @classmethod
    def setUpClass(cls):
        cls.eng, _, _ = run_until("corta", 300)
        cls.tr = cls.eng.tr

    def parse(self, text):
        return A.parse_rules(self.tr, text)

    def test_whisper_wrote_cuento_instead_of_cuanto(self):
        i = self.parse("¿A cuento está Vargas?")
        self.assertEqual((i.name, i.driver[1]), ("hueco", "Vargas"))

    def test_whisper_wrote_porque_in_one_word(self):
        self.assertEqual(self.parse("Porque estoy perdiendo tiempo.").name, "por_que_pierdo")
        self.assertEqual(self.parse("¿Por qué estoy perdiendo tiempo?").name, "por_que_pierdo")
        self.assertEqual(self.parse("Porque").name, "explica")

    def test_common_words_are_not_taken_for_driver_names(self):
        # 'queda' no es Ueda, 'quiero'/'quinto' no son Quintero
        names = set(self.tr.names.values())
        self.assertTrue({"Ueda", "Quintero"} <= names)
        for text in ("¿Cuántas vueltas quedan?", "¿Cuánta batería me queda?", "Quiero parar ya", "Quiero acabar quinto",
                     "Quiero terminar en la posición 3"):
            i = self.parse(text)
            self.assertIsNone(i.driver, text)

    def test_how_much_fuel_is_left_is_not_a_gap_question(self):
        for text in ("¿Cuánto combustible me queda?", "¿Cuánto combustible queda?", "¿cuánta gasolina queda?"):
            self.assertEqual(self.parse(text).name, "combustible", text)

    def test_pasar_is_not_confused_with_parar(self):
        i = self.parse("Necesito pasar a Hansen")
        self.assertEqual((i.name, i.driver[1]), ("objetivo_pasar", "Hansen"))
        self.assertEqual(self.parse("Quiero parar ya").name, "parar_ya")

    def test_undercut_spelling_variants(self):
        for text in ("¿Puedo hacer under cut?", "¿puedo hacer andercat?", "puedo hacer undercut"):
            self.assertEqual(self.parse(text).name, "undercut", text)
        # el overcut ya no es «parar ahora»: se evalúa retrasar la parada (timing.py)
        self.assertEqual(self.parse("¿Y un overcut?").name, "overcut")

    def test_transcribed_names_with_small_errors_still_match_but_not_wildly(self):
        self.assertEqual(self.tr.find_driver("como voy contra Hanson")[0][1], "Hansen")
        self.assertEqual(self.tr.find_driver("como voy contra Vargaz")[0][1], "Vargas")
        self.assertEqual(self.tr.find_driver("como voy contra la casa azul"), [])
